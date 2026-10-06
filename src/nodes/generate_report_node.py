"""AgentCore Platform v1.0"""

# Compiles the per-field validation results into the caller-facing report.
#
# This node is the first of the two independent layers that stand between the
# record and the caller:
#
#   layer 1 (here)              redact — every rendered field value is replaced
#                               with the mask sentinel when its FIELD NAME
#                               denotes an account or credential, or when its
#                               VALUE has the form of an account identifier or
#                               an email address
#   layer 2 (PostProcessNode)   scan — the finished report is pattern-scanned
#                               and blocked if anything sensitive survived
#
# The layers are deliberately ordered redact-then-scan and the scan never
# rewrites: a read-only second layer cannot destroy the very pattern it exists
# to catch, so no ordering hazard exists between them. Each emits its own
# audit event.
#
# Output: formatted_output (declared in AgentState) — the final report.
# AgentBaseGraph.get_output() reads state["formatted_output"] to build the
# caller response, so writing that key is the final-output contract.
#
# Trust was established at the input boundary, so this node runs at the
# pipeline's own trust level.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import PROCESSING_FAILED

from src.nodes.validate_node import MASK_SENTINEL, SENSITIVE_FIELD_PATTERNS

logger = logging.getLogger(__name__)

# Status symbols for the report
_STATUS_SYMBOL = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}

# Minimum width of the value column. A longer value overflows the column
# rather than being trimmed: trimming would rewrite the text before the
# output scan reads it, and a rewrite can destroy the very pattern the
# scan exists to catch.
_VALUE_WIDTH = 26

# ---------------------------------------------------------------------------
# Account-identifier recognition (layer 1)
#
# Recognition is by FORM, on a copy of the value with its grouping separators
# removed — an identifier written "GB82 WEST 1234 5698 7654 32" is the same
# identifier as "GB82WEST12345698765432" and must be treated alike. The
# decimal point is NOT a grouping separator: removing it would turn every
# amount into a long digit run.
# ---------------------------------------------------------------------------

_GROUPING_SEP_RE = re.compile("[ \\t\\u00a0\\-]")

# Forms that are an amount or a date, never an identifier.
_AMOUNT_FORM_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_ISO_DATE_FORM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# An undelimited digit run this long is an account or card number, not an
# amount: the amount rules in config/rules.yaml top out at nine digits.
_BARE_ACCOUNT_DIGITS = 12
_BARE_ACCOUNT_RE = re.compile(rf"^\d{{{_BARE_ACCOUNT_DIGITS},}}$")

# Forms matched against the separator-stripped value.
_IBAN_FORM_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_GROUPED_ACCOUNT_RE = re.compile(r"^\d{8,}$")

# An address is personal data, and it is the one sensitive form that carries no
# digits for the rules above to recognise. The framework input gate masks
# addresses in the submitted record before execute() runs, but the report is
# this template's own guarantee, so the check is made here too rather than
# assumed. Bounded on both sides of the @ so a long value cannot make the
# match expensive.
_EMAIL_FORM_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}$")


def looks_like_account_identifier(value: Any) -> bool:
    """True when the value has the form of an account or card identifier.

    Recognises an IBAN, a grouped account/card number, and an undelimited
    digit run too long to be an amount. Plain amounts (including
    comma-grouped and signed ones) and ISO dates are never identifiers.
    """
    text = str(value).strip()
    if not text:
        return False
    if _BARE_ACCOUNT_RE.match(text):
        return True
    if _ISO_DATE_FORM_RE.match(text) or _AMOUNT_FORM_RE.match(text):
        return False
    compact = _GROUPING_SEP_RE.sub("", text)
    if not compact:
        return False
    return bool(_IBAN_FORM_RE.match(compact.upper()) or _GROUPED_ACCOUNT_RE.match(compact))


def looks_like_personal_contact(value: Any) -> bool:
    """True when the value is an email address."""
    return bool(_EMAIL_FORM_RE.match(str(value).strip()))


def redact_value(field_name: str, value: Any) -> str:
    """Return the value as it may appear in the report, masked when sensitive."""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if text == MASK_SENTINEL:
        return MASK_SENTINEL
    if SENSITIVE_FIELD_PATTERNS.search(field_name or ""):
        return MASK_SENTINEL
    if looks_like_account_identifier(text) or looks_like_personal_contact(text):
        return MASK_SENTINEL
    return text


def _format_field_table(results: List[Dict[str, Any]]) -> tuple[str, int]:
    """Build the text table of per-field results; also report how many masked."""
    if not results:
        return "  (no field-level results)", 0

    header = f"  {'Field':<30} {'Value':<{_VALUE_WIDTH}} {'Status':<8} {'Rule':<25} Message"
    separator = "  " + "-" * 110
    rows = [header, separator]
    masked_count = 0
    for r in results:
        raw_field = str(r.get("field", ""))
        field = raw_field[:30]
        rendered = redact_value(raw_field, r.get("value", ""))
        if rendered == MASK_SENTINEL:
            masked_count += 1
        status = _STATUS_SYMBOL.get(r.get("status", "FAIL"), "[????]")
        rule = (r.get("rule") or "")[:25]
        message = r.get("message", "")
        rows.append(f"  {field:<30} {rendered:<{_VALUE_WIDTH}} " f"{status:<8} {rule:<25} {message}")
    return "\n".join(rows), masked_count


class GenerateReportNode(FunctionNode):
    """Compile the validation results into the caller-facing report.

    Produces formatted_output. Every rendered field value passes through
    redact_value() first, so an account identifier reaches the report as the
    mask sentinel whether or not its field name announced it.

    Input state keys:
        validation_results:      per-field result dicts (from ValidateNode)
        overall_status:          "PASS" | "WARN" | "FAIL" (from ValidateNode)
        remediation_suggestions: narrative text (from ValidateNode)
        validation_policy:       accepted caller policy (from PreProcessNode)

    Output state keys (partial dict — ONLY changed keys):
        formatted_output: the validation report string
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:        (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        emit_progress("Composing the answer...")
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        """Compile the validation report. Returns ONLY changed state keys."""
        validation_results: Optional[List[Dict[str, Any]]] = state.get("validation_results")
        overall_status: str = state.get("overall_status") or "FAIL"
        remediation: str = state.get("remediation_suggestions") or ""
        raw_policy = state.get("validation_policy")
        policy: Dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}

        if validation_results is None:
            emit_progress(PROCESSING_FAILED)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateReportNode: validation_results is missing — ValidateNode may have failed"],
            }

        overall_symbol = _STATUS_SYMBOL.get(overall_status, "[FAIL]")
        table, masked_count = _format_field_table(validation_results)

        # The applied policy is echoed so the caller can see which rule set
        # produced the verdict. Every value here was locked to an inert
        # identifier or a finite number at the input boundary.
        amount_min = policy.get("amount_min")
        amount_max = policy.get("amount_max")
        if amount_min is not None and amount_max is not None:
            band = f"caller band {amount_min} – {amount_max}"
        else:
            band = "configured amount rules"

        sections = [
            "=" * 70,
            "FINANCIAL DATA ENTRY VALIDATION REPORT",
            "=" * 70,
            "",
            f"Overall Status: {overall_symbol} {overall_status}",
            f"Submitted via:  {policy.get('channel') or 'unknown'}",
            f"Amount policy:  {band}",
            "",
            "Field-Level Validation Results:",
            table,
            "",
        ]

        if remediation:
            sections += [
                "Remediation Guidance:",
                "  " + "\n  ".join(remediation.splitlines()),
                "",
            ]

        sections += [
            "-" * 70,
            f"Note: account identifiers and credential-bearing values are shown as "
            f"{MASK_SENTINEL}; {masked_count} value(s) were masked in this report.",
            "=" * 70,
        ]

        formatted_output = "\n".join(sections)

        emit_trace_event(
            "generate_report_complete",
            {
                "overall_status": overall_status,
                "result_count": len(validation_results),
                "masked_value_count": masked_count,
                "report_length": len(formatted_output),
            },
            state,
        )

        logger.info(
            "GenerateReportNode: report compiled — overall=%s, fields=%d, masked=%d, length=%d",
            overall_status,
            len(validation_results),
            masked_count,
            len(formatted_output),
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
