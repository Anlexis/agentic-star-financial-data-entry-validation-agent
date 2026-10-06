"""AgentCore Platform v1.0"""

# Output boundary for the validation pipeline.
#
# Layer 2 of the two independent output layers: GenerateReportNode redacts
# every rendered field value, and this node scans the FINISHED report for
# anything that survived. The scan is read-only — it blocks the report, it
# never rewrites it — so it cannot destroy a pattern it was meant to catch,
# and a leak that reaches here fails CLOSED rather than shipping.
#
# What it looks for:
#   1. an empty report (nothing was generated)
#   2. credentials — API keys, JWTs, bearer tokens, credential assignments
#   3. account identifiers — IBANs and account/card numbers, recognised
#      whether or not they are written with grouping separators
#
# The identifier patterns tolerate the separators an identifier is normally
# grouped with; matching only the unseparated spelling would let the same
# identifier through simply for being written with spaces.
#
# Trust was established at the input boundary, so this node runs at the
# pipeline's own trust level.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, OUTPUT_BLOCKED, TOO_LONG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output patterns. Ordered most specific first; the first match names the
# violation.
# ---------------------------------------------------------------------------
_OUTPUT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # API key patterns: sk-..., pk-..., ak-...
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # JWT: three base64url segments
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Bearer token
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE)),
    # Credential assignment
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    # IBAN: two letters, two check digits, then 11-30 more alphanumerics —
    # written either unseparated or in the usual four-character groups. The
    # grouped form pins one separator for the whole identifier (the
    # backreference). A per-character optional separator would instead let the
    # pattern run across ordinary spaced text: "TX-2026-001 BIC BARCGB22" reads
    # as one 22-character identifier under that looser grammar.
    (
        "account_iban",
        re.compile(
            r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b" r"|\b[A-Z]{2}[0-9]{2}([ -])(?:[A-Z0-9]{4}\1){1,6}[A-Z0-9]{1,4}\b"
        ),
    ),
    # Undelimited digit run too long to be an amount (the configured amount
    # rules top out at nine digits).
    ("account_number", re.compile(r"\b[0-9]{12,}\b")),
]

# Account or card number written in separated groups (e.g. 4111 1111 1111 1111).
#
# Three conditions together, because any one of them alone misfires on ordinary
# report text:
#   - three or more digit groups,
#   - joined by the SAME single separator throughout (the backreference), so an
#     amount running into a date across a space — "1500.00 2026-07-10" — is not
#     read as one identifier, and column padding of several spaces never joins
#     two cells,
#   - carrying at least _GROUPED_ACCOUNT_MIN_DIGITS digits in total, which is
#     what still separates a real account number from an ISO date (8 digits).
_GROUPED_DIGITS_RE = re.compile(r"\b[0-9]{2,6}([ -])[0-9]{2,6}(?:\1[0-9]{2,6})+\b")
_GROUPED_ACCOUNT_MIN_DIGITS = 10


def _grouped_account_violation(text: str) -> bool:
    """True when a uniformly separated digit group carries account-length material."""
    for match in _GROUPED_DIGITS_RE.finditer(text):
        digits = sum(ch.isdigit() for ch in match.group(0))
        if digits >= _GROUPED_ACCOUNT_MIN_DIGITS:
            return True
    return False


def scan_output(text: str) -> Optional[str]:
    """Return the name of the first violation found, or None when clean."""
    for name, pattern in _OUTPUT_PATTERNS:
        if pattern.search(text):
            return name
    if _grouped_account_violation(text):
        return "account_number_grouped"
    return None


_BLOCKED_NOTICE = (
    "[OUTPUT BLOCKED — disallowed content detected in the validation report. "
    "Review the record for unmasked account identifiers or credentials and retry.]"
)


# Caller-facing wording for a run that completed without an answer. The marker
# is an internal reason code; this maps it to the sentence the caller sees.
# Static sentences only - no request value is ever substituted, so nothing the
# caller sent can be reflected back through this path.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Output gate for the financial data entry validation pipeline.

    Reads state["formatted_output"] (the report from GenerateReportNode) and
    applies the final scan:
      1. Non-empty check
      2. Credential and account-identifier scan
      3. Audit event on every path

    Input state keys:
        formatted_output: validation report string (from GenerateReportNode)

    Output state keys (partial dict — ONLY changed keys):
        formatted_output: (on violation) the blocked-output notice
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:        (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        emit_progress("Finalising the response...")

        # The run completed without an answer because the request could not be
        # accepted as written. Report the reason as the response: the caller
        # needs to know what to change, and an empty body would leave them with
        # nothing. Status stays SUCCESS - the run did what it could with the
        # request it was given, and the caller can correct it and send again on
        # the same conversation.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "formatted_output": message,
                "result": message,
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }
        """Apply the output gate. Returns ONLY changed state keys."""
        formatted_output: str = state.get("formatted_output") or ""

        # (1) Non-empty check
        if not formatted_output or not formatted_output.strip():
            emit_trace_event("post_process_blocked", {"reason": "empty_report"}, state)
            logger.warning("PostProcessNode: formatted_output is empty")
            emit_progress(OUTPUT_BLOCKED)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: formatted_output is empty — no validation report was generated"],
            }

        # (2) Credential and account-identifier scan
        violation = scan_output(formatted_output)
        if violation:
            emit_trace_event(
                "post_process_blocked",
                {"reason": "disallowed_content", "violation": violation},
                state,
            )
            logger.error("PostProcessNode: OUTPUT BLOCKED — %s detected", violation)
            emit_progress(OUTPUT_BLOCKED)
            return {
                "formatted_output": _BLOCKED_NOTICE,
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"PostProcessNode: output blocked — disallowed content "
                    f"pattern '{violation}' detected in the validation report"
                ],
            }

        logger.info("PostProcessNode: output scan clean — report length=%d", len(formatted_output))

        # (3) Audit trail
        emit_trace_event(
            "post_process_complete",
            {
                "report_length": len(formatted_output),
                "output_scan": "clean",
            },
            state,
        )

        return {
            "status": AgentStatus.SUCCESS.value,
        }
