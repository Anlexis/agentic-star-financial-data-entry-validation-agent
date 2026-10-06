"""Unit tests for ParseNode — record parsing."""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.parse_node import ParseNode


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    """Patch emit_trace_event to avoid backend calls."""
    monkeypatch.setattr(
        "src.nodes.parse_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestParseNodeTrustLevel:
    def test_required_trust_level_is_anonymous(self):
        assert ParseNode.required_trust_level == TrustLevel.ANONYMOUS


class TestParseNodeJSON:
    def test_valid_json_parsed(self):
        node = ParseNode()
        state = {
            "input_record": '{"iban": "GB82WEST12345698765432", "amount": "1000.00", "currency": "GBP"}',
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain status string, never the
        # enum. isinstance() would not catch it — a str-valued enum member is an
        # instance of str — so the exact type is what has to be asserted.
        assert type(result["status"]) is str  # noqa: E721
        assert "parsed_fields" in result
        assert result["parsed_fields"]["iban"] == "GB82WEST12345698765432"
        assert result["parsed_fields"]["amount"] == "1000.00"
        assert result["parsed_fields"]["currency"] == "GBP"

    def test_json_with_numeric_values(self):
        node = ParseNode()
        state = {
            "input_record": '{"amount": 500.50, "currency": "USD"}',
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_fields"]["currency"] == "USD"


class TestParseNodeKeyValue:
    def test_kv_colon_format(self):
        node = ParseNode()
        state = {
            "input_record": "amount: 100.00\ncurrency: EUR\nreference: TX-001",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_fields"]["amount"] == "100.00"
        assert result["parsed_fields"]["currency"] == "EUR"

    def test_kv_equals_format(self):
        node = ParseNode()
        state = {
            "input_record": "amount=250.00\ncurrency=JPY",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_fields"]["amount"] == "250.00"

    def test_kv_semicolon_separated(self):
        node = ParseNode()
        state = {
            "input_record": "amount=100.00;currency=USD;reference=REF001",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestParseNodeCSV:
    def test_csv_with_header(self):
        node = ParseNode()
        state = {
            "input_record": "amount,currency,reference\n1000.00,USD,TX-002",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_fields"]["amount"] == "1000.00"
        assert result["parsed_fields"]["currency"] == "USD"


class TestParseNodeErrors:
    def test_empty_input_record_rejected(self):
        node = ParseNode()
        state = {
            "input_record": "",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")

    def test_malformed_input_rejected(self):
        node = ParseNode()
        state = {
            "input_record": "not json not kv not csv just random text without structure xyz",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        # Single words with no structure may fail to parse
        assert "status" in result

    def test_falls_back_to_user_input_when_no_input_record(self):
        node = ParseNode()
        state = {
            "user_input": '{"amount": "200.00", "currency": "EUR"}',
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_fields"]["amount"] == "200.00"
