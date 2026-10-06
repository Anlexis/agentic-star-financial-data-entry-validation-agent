"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization, and Pydantic objects corrupt silently
# on the round trip. Extend AgentState with agent-specific fields only; do NOT
# add credentials, secrets, or Pydantic models.
#
# Financial Data Entry Validation Agent — flat pipeline:
# PreProcess -> Parse -> Validate -> GenerateReport -> PostProcess.
#
# Confidentiality notes:
#   - input_record holds the raw financial record for ParseNode. Account
#     identifiers in it never reach the report unmasked: GenerateReportNode
#     redacts every rendered value and PostProcessNode scans the result.
#   - parsed_fields is intermediate structure for ValidateNode only; it is
#     ephemeral in non-checkpointed runs.
#   - No API keys, tokens, or persistent personal data in State — a checkpoint
#     database would retain them.
#   - formatted_output (inherited from AgentState) carries the redacted report.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for the Financial Data Entry Validation Agent.

    All shared fields (user_input, status, session_id, node_history,
    error_log, formatted_output, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # PreProcessNode — set once the record and the caller policy validate
    # ------------------------------------------------------------------

    # The raw financial record string (JSON / CSV / key-value).
    # Written by PreProcessNode; ParseNode reads it to extract fields.
    input_record: Optional[str]

    # The accepted caller policy, defaults applied:
    #   channel          inert identifier, echoed in the report header
    #   required_fields  additional field names the caller requires
    #   amount_min       lower amount bound, or None for the configured rules
    #   amount_max       upper amount bound, or None for the configured rules
    # Every member was bounds-checked at the input boundary.
    validation_policy: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # ParseNode — structured fields extracted from input_record
    # ------------------------------------------------------------------

    # Parsed field dict: {"field_name": "field_value", ...}
    # May carry raw financial values for ValidateNode; those values are
    # redacted before they reach the report.
    parsed_fields: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # ValidateNode — per-field results and the overall outcome
    # ------------------------------------------------------------------

    # Per-field validation results. Each entry:
    #   {
    #     "field": str,              — field name
    #     "value": str,              — value (masked for sensitive field names)
    #     "status": "PASS"|"WARN"|"FAIL",
    #     "rule": str,               — the rule that produced this result
    #     "message": str,            — human-readable description
    #   }
    validation_results: Optional[List[Dict[str, Any]]]

    # Overall outcome: "PASS" | "WARN" | "FAIL", set by ValidateNode.
    overall_status: Optional[str]

    # Narrative remediation text for the WARN / FAIL entries.
    remediation_suggestions: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]

    # Set when the run completes WITHOUT carrying out the request because the
    # caller's input could not be accepted as written - a rejection the caller
    # can correct and retry. The run still completes: nothing is processed, no
    # product is assembled, and the domain audit event for the rejection is
    # still emitted. Carrying this as a completion marker rather than a terminal
    # error is what lets the caller see the reason and send a corrected request
    # on the same conversation.
    #
    # Content the agent refuses outright, and a breach of a contract the caller
    # cannot influence, are NOT reported here - those stay terminal.
    error_code: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState (Annotated[list[str], operator.add])
