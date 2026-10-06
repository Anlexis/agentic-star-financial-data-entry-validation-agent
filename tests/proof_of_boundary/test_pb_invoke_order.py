# PB-6: Backbone Invoke-Order Verification
#
# Verifies that a full Graph().invoke() with a SUCCESS-yielding payload runs all
# domain nodes in the correct backbone order and that node_history reflects it.
#
# Backbone order (flat 5-node pipeline):
#   InitializeNode → PreProcessNode → ParseNode → ValidateNode
#   → GenerateReportNode → PostProcessNode → FinalizeNode
#
# The invoke() call uses caller_trust_level=VERIFIED_EXTERNAL — the same trust
# path a real external caller takes. An INTERNAL caller would sail past the
# PreProcessNode trust gate and hide a trust-level defect until deployment.
#
# Calling each node directly instead would pass without exercising the backbone,
# and so would catch neither trust-level nor graph-wiring faults; this test runs
# the real compiled graph.
from typing import ClassVar
from framework.nodes.base_node import BaseNode

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import FinancialDataEntryValidationAgent

# A SUCCESS-yielding payload: valid JSON financial record
_VALID_PAYLOAD = '{"amount": "1500.00", "currency": "USD", "value_date": "2026-07-10"}'

# The main-slot node class name (ParseNode sits in the 'main' backbone slot)
_MAIN_SLOT_NODE = "ParseNode"

# Expected full node_history for a SUCCESS run (flat pipeline)
_EXPECTED_ORDER = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,  # ParseNode = 'main' slot
    "ValidateNode",
    "GenerateReportNode",
    "PostProcessNode",
    "FinalizeNode",
]


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def patch_emit(monkeypatch):
    """Patch emit_trace_event in all node modules to avoid backend calls."""
    for mod in [
        "src.nodes.pre_process_node",
        "src.nodes.parse_node",
        "src.nodes.validate_node",
        "src.nodes.generate_report_node",
        "src.nodes.post_process_node",
    ]:
        monkeypatch.setattr(f"{mod}.emit_trace_event", lambda *a, **k: None)


class TestPB6BackboneInvokeOrder:
    """PB-6: full Graph().invoke() must run all backbone nodes in the correct order."""

    def test_node_history_matches_expected_order(self):
        """Invoke with a SUCCESS payload; assert node_history = expected flat pipeline order."""
        agent = FinancialDataEntryValidationAgent()
        agent.compile()

        ctx = InvocationContext(
            session_id="pb6-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,  # real external caller path
        )

        result = agent.invoke(user_input=_VALID_PAYLOAD, ctx=ctx)

        # Must return status SUCCESS
        assert result["status"] == AgentStatus.SUCCESS.value, (
            f"Expected AgentStatus.SUCCESS.value but got {result['status']!r}. "
            f"error_log: {result.get('error_log', [])}"
        )

        # node_history must contain all expected nodes in order
        node_history = result.get("node_history", [])
        assert node_history == _EXPECTED_ORDER, (
            f"Backbone invoke-order violation.\n" f"Expected: {_EXPECTED_ORDER}\n" f"Actual:   {node_history}"
        )

    def test_invoke_produces_formatted_output(self):
        """Successful invoke must return a non-empty output string."""
        agent = FinancialDataEntryValidationAgent()
        agent.compile()

        ctx = InvocationContext(
            session_id="pb6-output-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )

        result = agent.invoke(user_input=_VALID_PAYLOAD, ctx=ctx)

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("output"), "invoke() must return a non-empty 'output' key from state['formatted_output']"

    def test_error_path_routes_to_finalize(self):
        """An empty / rejected input must route to finalize (not hang or exception)."""
        agent = FinancialDataEntryValidationAgent()
        agent.compile()

        ctx = InvocationContext(
            session_id="pb6-error-path-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )

        result = agent.invoke(user_input="", ctx=ctx)

        # Empty input declined by PreProcessNode → the run still reaches
        # finalize, and the caller receives the reason as the response body.
        # The internal reason code is not part of the invoke() contract.
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("output"), result
        assert "FinalizeNode" in result.get("node_history", [])

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(_PrivilegedTrustGateFixture.required_trust_level).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls
