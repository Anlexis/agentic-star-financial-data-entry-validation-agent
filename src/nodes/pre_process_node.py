"""AgentCore Platform v1.0"""

# Input boundary for the validation pipeline. Two responsibilities:
#
#   1. Record screening — type guard, empty guard, length guard and a
#      prompt-injection scan over the submitted record text.
#   2. Caller-policy validation — every field read from the caller-supplied
#      input_context channel is checked against explicit bounds before any
#      later node may consume it. Invalid values fail CLOSED (the request is
#      rejected naming the field, never echoing the value); absent fields
#      degrade to the rule set shipped in config/rules.yaml.
#
# The node owns its own injection scan rather than relying on the framework
# input gate: where that gate is absent or configured off, the record still
# has to be refused here.
#
# A financial record legitimately carries account identifiers, so the input
# boundary does NOT reject them — confidentiality is enforced where the data
# leaves the agent (GenerateReportNode masks, PostProcessNode scans).
#
# Every code path emits an audit event.
#
# Trust: this is the outer entry gate, so it requires an authenticated caller.

import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import DISALLOWED_FORM, EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt-injection / SQL / script patterns rejected in the record text.
#
# Two rules keep this scan from failing CLOSED on legitimate financial prose,
# which for a payments validator means blocking a real payment:
#
#   1. Every word-shaped alternative is \b-anchored on BOTH sides. Without the
#      leading anchor, "act as a" matches inside "Transact as a settlement
#      agent" and the record is refused as an injection attempt.
#   2. The bare SQL verb pairs — INSERT INTO, DELETE FROM, UPDATE ... SET,
#      TRUNCATE TABLE, DROP TABLE — are ordinary English before they are SQL:
#      "Insert Into Trust Holdings Ltd" is a beneficiary name. They count only
#      when a statement break (; ' " or a closing paren) precedes them, or a
#      statement terminator follows. The unambiguous forms — UNION SELECT,
#      SELECT * FROM, xp_cmdshell — need no such context.
#
# The record text is never executed as a query anywhere in this pipeline, so
# these SQL forms are defence in depth for whatever consumes the report.
# ---------------------------------------------------------------------------

# A statement break: what separates an injected statement from the field value
# it was smuggled into.
_SQL_BREAK = r"[;'\")]\s*"
_SQL_VERBS = r"(?:DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO|TRUNCATE\s+TABLE|UPDATE\s+\w+\s+SET)"

_INJECTION_RE = re.compile(
    r"(?:"
    r"\bignore\s+(?:previous|above|all|prior)\s+(?:instructions?|prompts?|context|system)\b"
    r"|\bdisregard\s+(?:your|all|previous|these)\b"
    r"|\b(?:forget|override|bypass)\s+(?:your|all|previous)\s+(?:instructions?|rules?|guidelines?)\b"
    r"|\byou\s+are\s+now\s+(?:a|an|the)\s"
    r"|\bact\s+as\s+(?:a|an|the|if\s+you)\b"
    r"|\bjailbreak\b"
    r"|\bDAN\s+mode\b"
    r"|<\s*script[^>]*>"
    r"|<\s*/\s*script\s*>"
    r"|\bjavascript\s*:"
    r"|\bvbscript\s*:"
    rf"|{_SQL_BREAK}{_SQL_VERBS}\b"
    rf"|\b{_SQL_VERBS}\b[^;\n]{{0,64}};"
    r"|\bUNION\s+(?:ALL\s+)?SELECT\b"
    r"|\bSELECT\s+\*\s+FROM\b"
    r"|\bxp_cmdshell\b"
    r"|--\s*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Maximum accepted record length (characters).
_MAX_INPUT_LENGTH = 10_000

# ---------------------------------------------------------------------------
# Caller-policy contract (input_context)
#
# The caller may narrow the validation policy for a single request. Every
# consumed field is checked here; nothing outside this contract can influence
# the pipeline. No field accepts free text — identifiers only — so caller
# policy can never carry renderable prose into the report, and there is no
# free-text channel for the record's own redaction rules to miss.
#
#   channel          identifier [a-z0-9_]{1,32}          default "unknown"
#   required_fields  list of <= 20 identifiers           default [] (rules.yaml only)
#   amount_min       finite number in [0, 1e12]          default: rules.yaml minimum
#   amount_max       finite number in [0, 1e12]          default: rules.yaml maximum
#
# amount_min/amount_max are accepted only as a consistent pair (min <= max).
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_REQUIRED_FIELDS = 20
_AMOUNT_BOUND_MIN = 0.0
_AMOUNT_BOUND_MAX = 1e12
_DEFAULT_CHANNEL = "unknown"


def _scan_injection(text: str) -> Optional[str]:
    """Return the matched injection pattern (truncated), or None if clean."""
    m = _INJECTION_RE.search(text)
    return m.group(0)[:40] if m else None


def _finite_in_range(value: Any, lo: float, hi: float) -> Tuple[bool, float]:
    """Parse a caller-controlled number defensively; fail CLOSED.

    Accepts int/float only — bool is rejected explicitly because it is an int
    subclass. NaN and +/-Infinity parse as floats but every ordered comparison
    against them evaluates False, which silently disables any bound built on
    such a comparison, so non-finite values are rejected outright; so are
    values outside [lo, hi].

    Returns (ok, parsed_value); parsed_value is 0.0 whenever ok is False.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, 0.0
    num = float(value)
    if not math.isfinite(num):
        return False, 0.0
    if num < lo or num > hi:
        return False, 0.0
    return True, num


def _reject(field: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the fail-closed rejection for an invalid caller-policy field.

    Names the field only — the rejected value is never echoed into the error
    log or the response.
    """
    emit_trace_event(
        "pre_process_rejected",
        {"reason": "invalid_caller_policy", "field": field},
        state,
    )
    logger.warning("PreProcessNode: input_context field %r failed validation", field)
    emit_progress(INPUT_REJECTED)
    return {
        "status": AgentStatus.SUCCESS.value,
        "error_code": "INVALID_REQUEST",
        "error_log": [f"PreProcessNode: input_context field '{field}' failed validation"],
    }


class PreProcessNode(FunctionNode):
    """Input gate for the financial data entry validation pipeline.

    Screens the submitted record and validates the caller-supplied policy
    before the domain pipeline (Parse -> Validate -> GenerateReport) runs:
      (1) Type guard: user_input must be a non-empty str
      (2) Length guard: max 10 000 characters
      (3) Injection scan: reject prompt injection / SQL / script patterns
      (4) Caller-policy validation: bounded, inert, fail closed
      (5) Audit event on every path (rejection and success)

    Input state keys:
        user_input:     financial record string (JSON / CSV / key-value)
        input_context:  optional caller policy (see the contract above)

    Output state keys (partial dict — ONLY changed keys):
        input_record:      validated record string (same as stripped user_input)
        validated_input:   same value (AgentState alias used by later nodes)
        validation_policy: the accepted caller policy, defaults applied
        status:            AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:         (on error) list of error message strings
    """

    # Outer trust gate — external callers must be authenticated.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        emit_progress("Checking the request...")
        """Validate the record and the caller policy. Returns ONLY changed keys."""
        # By the time execute() runs the framework input gate has masked any
        # email / phone / card PII it found in user_input to [MASKED]. The
        # checks below stand on their own and do not depend on it.
        user_input = state.get("user_input", "")
        raw_context = state.get("input_context")
        input_context: Dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}

        # (1) Type guard
        if not isinstance(user_input, str):
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "non_string_input", "input_type": type(user_input).__name__},
                state,
            )
            logger.warning("PreProcessNode: non-string user_input (type=%s)", type(user_input).__name__)
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: user_input must be a string, got {type(user_input).__name__}"],
            }

        stripped = user_input.strip()

        # (1b) Empty guard
        if not stripped:
            emit_trace_event("pre_process_rejected", {"reason": "empty_input"}, state)
            logger.warning("PreProcessNode: empty user_input rejected")
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or blank"],
            }

        # (2) Length guard
        if len(stripped) > _MAX_INPUT_LENGTH:
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "input_too_long", "length": len(stripped), "max": _MAX_INPUT_LENGTH},
                state,
            )
            logger.warning("PreProcessNode: input too long (%d chars)", len(stripped))
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [
                    f"PreProcessNode: user_input exceeds maximum length "
                    f"({len(stripped)} chars, max {_MAX_INPUT_LENGTH})"
                ],
            }

        # (3) Injection scan — owned by this node, not delegated to the framework
        injection_match = _scan_injection(stripped)
        if injection_match is not None:
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "injection_pattern", "match_prefix": injection_match},
                state,
            )
            logger.warning("PreProcessNode: injection pattern detected: %r", injection_match)
            emit_progress(DISALLOWED_FORM)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input rejected — disallowed pattern detected"],
            }

        # (4) Caller-policy validation — fail CLOSED per field
        channel = _DEFAULT_CHANNEL
        if "channel" in input_context:
            raw_channel = input_context["channel"]
            if not isinstance(raw_channel, str) or not _IDENTIFIER_RE.match(raw_channel):
                return _reject("channel", state)
            channel = raw_channel

        required_fields: List[str] = []
        if "required_fields" in input_context:
            raw_required = input_context["required_fields"]
            if not isinstance(raw_required, list) or len(raw_required) > _MAX_REQUIRED_FIELDS:
                return _reject("required_fields", state)
            for name in raw_required:
                if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
                    return _reject("required_fields", state)
            required_fields = list(raw_required)

        amount_min: Optional[float] = None
        if "amount_min" in input_context:
            ok, amount_min_value = _finite_in_range(input_context["amount_min"], _AMOUNT_BOUND_MIN, _AMOUNT_BOUND_MAX)
            if not ok:
                return _reject("amount_min", state)
            amount_min = amount_min_value

        amount_max: Optional[float] = None
        if "amount_max" in input_context:
            ok, amount_max_value = _finite_in_range(input_context["amount_max"], _AMOUNT_BOUND_MIN, _AMOUNT_BOUND_MAX)
            if not ok:
                return _reject("amount_max", state)
            amount_max = amount_max_value

        # A one-sided or inverted band would silently widen the check it is
        # meant to narrow, so the pair is accepted only when it is coherent.
        if (amount_min is None) != (amount_max is None):
            return _reject("amount_max" if amount_min is not None else "amount_min", state)
        if amount_min is not None and amount_max is not None and amount_min > amount_max:
            return _reject("amount_min", state)

        validation_policy: Dict[str, Any] = {
            "channel": channel,
            "required_fields": required_fields,
            "amount_min": amount_min,
            "amount_max": amount_max,
        }

        # (5) Audit trail — success path
        emit_trace_event(
            "pre_process_validated",
            {
                "input_length": len(stripped),
                "channel": channel,
                "required_field_count": len(required_fields),
                "amount_band_supplied": amount_min is not None,
            },
            state,
        )

        logger.info("PreProcessNode: validated financial record (%d chars)", len(stripped))

        return {
            "input_record": stripped,
            "validated_input": stripped,
            "validation_policy": validation_policy,
            "status": AgentStatus.SUCCESS.value,
        }
