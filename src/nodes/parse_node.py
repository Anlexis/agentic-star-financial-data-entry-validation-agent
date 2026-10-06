"""AgentCore Platform v1.0"""

# ParseNode — parse the financial record into a structured field dict.
#
# Sits in the backbone `main` slot of FinancialDataEntryValidationAgent,
# which is a flat Cat-2 graph. The backbone routes from this node (via
# _route_after_parse) to ValidateNode on SUCCESS, or finalize on ERROR.
#
# Supported input formats (auto-detected):
#   JSON object  — {"field": "value", ...}
#   CSV line     — "field1,field2,..." with header inferred or passed as
#                  first line separated by '\n'
#   Key-value    — "field: value\nfield2: value2\n..." or "field=value;..."
#
# Audit: emit_trace_event("parse_complete", payload, state) on the success path.
# No content scan is needed here — the record was screened at the input boundary.
#
# Trust was established at the input boundary, so this node runs at the
# pipeline's own trust level.

import csv
import io
import json
import logging
import re
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, NOT_JSON

logger = logging.getLogger(__name__)

# Regex for key-value format: "key: value" or "key = value" or "key=value"
_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*)[\s]*[=:]+\s*(.*)$")


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to parse the text as a JSON object."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {str(k): v for k, v in obj.items()}
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _try_parse_csv(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to parse text as a CSV line (with optional header row).

    If the text has two lines separated by '\\n', the first is the header,
    the second is the data row. If it has one line, field names are
    auto-generated as col_1, col_2, …
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        if len(lines) >= 2:
            # First line = header, second line = values
            dict_reader = csv.DictReader(io.StringIO("\n".join(lines[:2])))
            for mapping in dict_reader:
                return {k.strip(): v.strip() for k, v in mapping.items() if k}
        else:
            # Single CSV line — auto-generate column names
            row_reader = csv.reader(io.StringIO(lines[0]))
            for row in row_reader:
                if len(row) < 2:
                    return None
                return {f"col_{i + 1}": v.strip() for i, v in enumerate(row)}
    except csv.Error:
        pass
    return None


def _try_parse_kv(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to parse text as key-value pairs.

    Supports 'key: value' (one per line), 'key=value' (one per line or
    semicolon-separated), and 'key = value' variants.
    """
    # Handle semicolon-separated single-line KV (field=value;field2=value2)
    if "\n" not in text and ";" in text:
        candidates = text.split(";")
    else:
        candidates = text.splitlines()

    result: Dict[str, Any] = {}
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        m = _KV_RE.match(candidate)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            value = m.group(2).strip()
            result[key] = value

    return result if result else None


class ParseNode(FunctionNode):
    """Parse the financial data record into a structured field dict.

    Backbone `main` slot of FinancialDataEntryValidationAgent.
    Auto-detects JSON, CSV (with header), and key-value formats.

    Input state keys:
        input_record: validated financial data string (from PreProcessNode)

    Output state keys (partial dict — ONLY changed keys):
        parsed_fields:  {field_name: value, ...} dict
        status:         AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:      (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        emit_progress("Reading the request...")
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        """Parse the financial data record. Returns ONLY changed state keys."""
        input_record: str = state.get("input_record") or state.get("validated_input") or state.get("user_input", "")

        if not input_record or not input_record.strip():
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["ParseNode: input_record is empty — nothing to parse"],
            }

        text = input_record.strip()

        # Attempt each format in priority order: JSON → KV → CSV
        parsed: Optional[Dict[str, Any]] = None

        # 1. JSON
        parsed = _try_parse_json(text)
        fmt = "json"

        # 2. Key-value (before CSV to avoid misidentifying "key: val" rows as CSV)
        if parsed is None:
            parsed = _try_parse_kv(text)
            fmt = "key_value"

        # 3. CSV (last resort)
        if parsed is None:
            parsed = _try_parse_csv(text)
            fmt = "csv"

        if not parsed:
            emit_progress(NOT_JSON)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "ParseNode: unable to parse input_record — "
                    "expected JSON object, key-value pairs, or CSV with header row"
                ],
            }

        emit_trace_event(
            "parse_complete",
            {"format": fmt, "field_count": len(parsed)},
            state,
        )

        logger.info("ParseNode: parsed %d fields (format=%s)", len(parsed), fmt)

        return {
            "parsed_fields": parsed,
            "status": AgentStatus.SUCCESS.value,
        }
