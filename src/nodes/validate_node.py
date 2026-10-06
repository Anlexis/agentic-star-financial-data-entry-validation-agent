"""AgentCore Platform v1.0"""

# Applies the configured validation rules to the parsed record fields.
#
# Rules are read from config/rules.yaml (module-level cache) and may be
# narrowed per request by the caller policy the input boundary accepted:
#   - required_fields are the union of the configured list and the caller's
#   - amount_min / amount_max replace the configured amount band when supplied
#
# Rule types applied in order:
#   1. required_fields — FAIL when a required field is absent
#   2. format_rules    — regex pattern match per named field list
#   3. range_rules     — numeric range check (amount and friends)
#   4. regex_rules     — advisory regex match (WARN by default)
#
# Outputs:
#   validation_results: per-field result dicts (field/value/status/rule/message)
#   overall_status:     "PASS" | "WARN" | "FAIL" (worst severity across fields)
#   remediation_suggestions: narrative text for the WARN / FAIL entries
#
# Numeric handling: every number that reaches a comparison — from the record
# and from the rule file alike — goes through _finite_number(). NaN and
# +/-Infinity parse happily via float() and then compare False against every
# bound, which would report an unbounded amount as "in valid range"; they are
# rejected instead of compared.
#
# Trust was established at the input boundary, so this node runs at the
# pipeline's own trust level.

import logging
import math
import pathlib
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, OUTPUT_BLOCKED

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level: load and cache config/rules.yaml at import time
# ---------------------------------------------------------------------------
_RULES_FILE = pathlib.Path(__file__).parent.parent.parent / "config" / "rules.yaml"

# Field names whose values are replaced with the mask sentinel in the results.
SENSITIVE_FIELD_PATTERNS = re.compile(
    r"(?:iban|account|card|pan|bic|swift|routing|sort_code|credential|password|secret|token)",
    re.IGNORECASE,
)

MASK_SENTINEL = "[MASKED]"


def _load_rules() -> Dict[str, Any]:
    """Load rules.yaml. Returns an empty dict when the file is absent."""
    try:
        import yaml

        with open(_RULES_FILE, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
            return dict(loaded) if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        logger.warning("ValidateNode: config/rules.yaml not found — using built-in defaults only")
        return {}
    except Exception as exc:
        logger.error("ValidateNode: failed to load rules.yaml: %s", exc)
        return {}


# Module-level rules cache (loaded once at import; re-read only in tests)
_RULES: Dict[str, Any] = _load_rules()


# ---------------------------------------------------------------------------
# Module-level output content check, called from execute() before returning.
# ---------------------------------------------------------------------------
_CRED_RE = re.compile(
    r"\b(?:password|api_key|secret|token|credential)\s*[:=]\s*\S{8,}",
    re.IGNORECASE,
)


def _output_content_violation(result: Dict[str, Any]) -> Optional[str]:
    """Return a violation name when the node's own output carries a credential.

    A module-level function rather than a gate override: the framework gates
    are final, and the domain check belongs to execute()'s own result.
    """
    suggestions: str = result.get("remediation_suggestions") or ""
    if _CRED_RE.search(suggestions):
        return "credential_in_remediation_suggestions"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finite_number(value: Any) -> Optional[float]:
    """Parse a number for comparison; return None when it is unusable.

    Rejects bool (an int subclass), non-numeric text, and the non-finite
    values NaN / +Infinity / -Infinity — each of which parses through float()
    but compares False against every bound, turning a range check into a
    silent pass.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip().replace(",", ""))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def _normalise_field_name(name: str) -> str:
    """Normalise a field name: lowercase, spaces/hyphens to underscores."""
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _mask_sensitive(field_name: str, value: Any) -> str:
    """Return the mask sentinel for fields whose name denotes an account."""
    if SENSITIVE_FIELD_PATTERNS.search(field_name):
        return MASK_SENTINEL
    return str(value) if value is not None else ""


def _check_required(
    parsed_fields: Dict[str, Any],
    required: List[str],
) -> List[Dict[str, Any]]:
    """Check that the required fields are present."""
    results = []
    norm_fields = {_normalise_field_name(k): k for k in parsed_fields}
    for req in required:
        norm_req = _normalise_field_name(req)
        if norm_req in norm_fields:
            results.append(
                {
                    "field": req,
                    "value": _mask_sensitive(req, parsed_fields[norm_fields[norm_req]]),
                    "status": "PASS",
                    "rule": "required_field",
                    "message": f"Required field '{req}' is present",
                }
            )
        else:
            results.append(
                {
                    "field": req,
                    "value": "",
                    "status": "FAIL",
                    "rule": "required_field",
                    "message": f"Required field '{req}' is missing",
                }
            )
    return results


def _check_format(
    parsed_fields: Dict[str, Any],
    format_rules: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Apply regex format rules to the fields each rule names."""
    results = []
    norm_fields = {_normalise_field_name(k): (k, v) for k, v in parsed_fields.items()}

    for rule_name, rule in format_rules.items():
        if not isinstance(rule, dict):
            logger.error("ValidateNode: rule '%s' is not a mapping — skipped", rule_name)
            continue
        pattern = rule.get("pattern", "")
        rule_fields = [_normalise_field_name(f) for f in rule.get("fields", [])]
        description = rule.get("description", rule_name)
        severity = rule.get("severity", "FAIL")

        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            logger.error("ValidateNode: invalid regex in rule '%s'", rule_name)
            continue

        for norm_fname in rule_fields:
            if norm_fname not in norm_fields:
                continue
            orig_name, raw_value = norm_fields[norm_fname]
            str_value = str(raw_value).strip() if raw_value is not None else ""
            masked_value = _mask_sensitive(orig_name, raw_value)

            if not str_value:
                # An empty value is the required_fields rule's business.
                continue

            if compiled.match(str_value):
                results.append(
                    {
                        "field": orig_name,
                        "value": masked_value,
                        "status": "PASS",
                        "rule": rule_name,
                        "message": f"{description}: format valid",
                    }
                )
            else:
                results.append(
                    {
                        "field": orig_name,
                        "value": masked_value,
                        "status": severity,
                        "rule": rule_name,
                        "message": f"{description}: format invalid (value does not match expected pattern)",
                    }
                )

    return results


def _check_range(
    parsed_fields: Dict[str, Any],
    range_rules: Dict[str, Any],
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Apply numeric range checks to the fields each rule names.

    When the caller supplied a band it replaces the configured one for every
    range rule. A value that cannot be compared — non-numeric, or non-finite —
    is reported as a failure of that rule rather than skipped, so an
    uncomparable amount can never be reported as in range.
    """
    results = []
    norm_fields = {_normalise_field_name(k): (k, v) for k, v in parsed_fields.items()}
    caller_band = amount_min is not None and amount_max is not None

    for rule_name, rule in range_rules.items():
        if not isinstance(rule, dict):
            logger.error("ValidateNode: rule '%s' is not a mapping — skipped", rule_name)
            continue
        rule_fields = [_normalise_field_name(f) for f in rule.get("fields", [])]
        description = rule.get("description", rule_name)
        severity = rule.get("severity", "WARN")

        if caller_band:
            min_val: Optional[float] = amount_min
            max_val: Optional[float] = amount_max
        else:
            # Rule-file bounds are numbers too: an unusable bound is dropped
            # rather than compared, so it cannot silently disable the check.
            min_val = _finite_number(rule.get("min")) if rule.get("min") is not None else None
            max_val = _finite_number(rule.get("max")) if rule.get("max") is not None else None
            if rule.get("min") is not None and min_val is None:
                logger.error("ValidateNode: rule '%s' has an unusable 'min' bound", rule_name)
            if rule.get("max") is not None and max_val is None:
                logger.error("ValidateNode: rule '%s' has an unusable 'max' bound", rule_name)

        for norm_fname in rule_fields:
            if norm_fname not in norm_fields:
                continue
            orig_name, raw_value = norm_fields[norm_fname]
            masked_value = _mask_sensitive(orig_name, raw_value)

            numeric = _finite_number(raw_value)
            if numeric is None:
                if str(raw_value).strip() == "":
                    continue
                results.append(
                    {
                        "field": orig_name,
                        "value": masked_value,
                        "status": severity,
                        "rule": rule_name,
                        "message": f"{description}: value is not a finite number and cannot be range-checked",
                    }
                )
                continue

            in_range = True
            msg_parts = []
            if min_val is not None and numeric < min_val:
                in_range = False
                msg_parts.append(f"below minimum ({min_val})")
            if max_val is not None and numeric > max_val:
                in_range = False
                msg_parts.append(f"above maximum ({max_val})")

            if in_range:
                results.append(
                    {
                        "field": orig_name,
                        "value": masked_value,
                        "status": "PASS",
                        "rule": rule_name,
                        "message": f"{description}: value in valid range",
                    }
                )
            else:
                results.append(
                    {
                        "field": orig_name,
                        "value": masked_value,
                        "status": severity,
                        "rule": rule_name,
                        "message": f"{description}: " + "; ".join(msg_parts),
                    }
                )

    return results


def _compute_overall_status(results: List[Dict[str, Any]]) -> str:
    """Compute PASS / WARN / FAIL from the per-field results. FAIL > WARN > PASS."""
    statuses = {r["status"] for r in results}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _build_remediation(results: List[Dict[str, Any]]) -> str:
    """Build the narrative remediation text from the WARN / FAIL results."""
    issues = [r for r in results if r["status"] in ("WARN", "FAIL")]
    if not issues:
        return "All validation rules passed. No remediation required."

    lines = ["The following fields require attention:"]
    for item in issues:
        severity_label = item["status"]
        field = item["field"]
        message = item["message"]
        lines.append(f"  [{severity_label}] {field}: {message}")
    lines.append("")
    lines.append("Please review and correct the flagged fields before resubmitting.")
    return "\n".join(lines)


def _effective_required_fields(
    configured: List[str],
    policy_required: List[str],
) -> List[str]:
    """Union of the configured required fields and the caller's, order-stable."""
    seen: Dict[str, None] = {}
    for name in list(configured) + list(policy_required):
        key = _normalise_field_name(str(name))
        if key not in seen:
            seen[key] = None
    return list(seen.keys())


class ValidateNode(FunctionNode):
    """Apply the configured validation rules to the parsed record fields.

    Reads the rule set from config/rules.yaml and narrows it with the caller
    policy the input boundary accepted. Produces per-field results, an overall
    status (PASS / WARN / FAIL) and narrative remediation text.

    Input state keys:
        parsed_fields:     {field_name: value, ...} from ParseNode
        validation_policy: accepted caller policy from PreProcessNode

    Output state keys (partial dict — ONLY changed keys):
        validation_results:      per-field result dicts
        overall_status:          "PASS" | "WARN" | "FAIL"
        remediation_suggestions: narrative text for the WARN / FAIL entries
        status:                  AgentStatus.SUCCESS.value (the node ran, even on FAIL)
        error_log:               (on internal error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        emit_progress("Checking the request...")
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        """Apply the validation rules. Returns ONLY changed state keys."""
        parsed_fields: Optional[Dict[str, Any]] = state.get("parsed_fields")

        if not parsed_fields:
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["ValidateNode: parsed_fields is empty or missing — nothing to validate"],
            }

        raw_policy = state.get("validation_policy")
        policy: Dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
        policy_required = policy.get("required_fields") or []
        amount_min = policy.get("amount_min")
        amount_max = policy.get("amount_max")

        rules = _RULES
        all_results: List[Dict[str, Any]] = []

        # 1. Required fields — configured list plus anything the caller asked for
        required = _effective_required_fields(rules.get("required_fields", []) or [], policy_required)
        if required:
            all_results.extend(_check_required(parsed_fields, required))

        # 2. Format rules
        format_rules = rules.get("format_rules", {})
        if format_rules:
            all_results.extend(_check_format(parsed_fields, format_rules))

        # 3. Range rules
        range_rules = rules.get("range_rules", {})
        if range_rules:
            all_results.extend(_check_range(parsed_fields, range_rules, amount_min, amount_max))

        # 4. Advisory regex rules
        regex_rules = rules.get("regex_rules", {})
        if regex_rules:
            all_results.extend(_check_format(parsed_fields, regex_rules))

        overall = _compute_overall_status(all_results)
        remediation = _build_remediation(all_results)

        result_partial = {
            "validation_results": all_results,
            "overall_status": overall,
            "remediation_suggestions": remediation,
            "status": AgentStatus.SUCCESS.value,
        }

        violation = _output_content_violation(result_partial)
        if violation:
            logger.error("ValidateNode: output blocked — %s", violation)
            emit_progress(OUTPUT_BLOCKED)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ValidateNode: output blocked — {violation}"],
            }

        emit_trace_event(
            "validate_complete",
            {
                "field_count": len(parsed_fields),
                "result_count": len(all_results),
                "overall_status": overall,
                "caller_band_applied": amount_min is not None and amount_max is not None,
            },
            state,
        )

        logger.info(
            "ValidateNode: %d fields validated — overall status: %s",
            len(parsed_fields),
            overall,
        )

        return result_partial
