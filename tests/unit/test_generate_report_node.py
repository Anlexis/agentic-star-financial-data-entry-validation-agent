"""Unit tests for GenerateReportNode — the report and its redaction layer."""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.generate_report_node import (
    GenerateReportNode,
    looks_like_account_identifier,
    redact_value,
)

_ANON = {"caller_trust_level": TrustLevel.ANONYMOUS.value}

_SAMPLE_RESULTS = [
    {"field": "amount", "value": "1000.00", "status": "PASS", "rule": "amount_format", "message": "format valid"},
    {"field": "currency", "value": "USD", "status": "PASS", "rule": "currency_code", "message": "format valid"},
    {"field": "iban", "value": "[MASKED]", "status": "PASS", "rule": "iban", "message": "IBAN format valid"},
]

_FAIL_RESULTS = [
    {"field": "iban", "value": "[MASKED]", "status": "FAIL", "rule": "iban", "message": "IBAN format invalid"},
    {"field": "amount", "value": "1000.00", "status": "PASS", "rule": "amount_format", "message": "format valid"},
]


def _state(results, **extra):
    base = {
        "validation_results": results,
        "overall_status": "PASS",
        "remediation_suggestions": "All validation rules passed. No remediation required.",
        **_ANON,
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    """Patch emit_trace_event to avoid backend calls."""
    monkeypatch.setattr(
        "src.nodes.generate_report_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestGenerateReportNodeTrustLevel:
    def test_required_trust_level_is_anonymous(self):
        assert GenerateReportNode.required_trust_level == TrustLevel.ANONYMOUS


class TestGenerateReportNodeSuccess:
    def test_report_compiled_on_pass(self):
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]

    def test_report_contains_overall_status(self):
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert "PASS" in result["formatted_output"]

    def test_report_contains_fail_marker(self):
        result = GenerateReportNode()(
            _state(_FAIL_RESULTS, overall_status="FAIL", remediation_suggestions="Field 'iban' has format issues.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "FAIL" in result["formatted_output"]

    def test_report_states_the_redaction_rule(self):
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert "[MASKED]" in result["formatted_output"]
        assert "masked" in result["formatted_output"].lower()

    def test_report_includes_remediation_when_present(self):
        result = GenerateReportNode()(
            _state(
                _FAIL_RESULTS,
                overall_status="FAIL",
                remediation_suggestions="Fix the IBAN format before resubmitting.",
            )
        )
        assert "Remediation" in result["formatted_output"]
        assert "Fix the IBAN format" in result["formatted_output"]

    def test_report_renders_the_field_values(self):
        """The report shows what each field held — that is what makes it actionable."""
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert "1000.00" in result["formatted_output"]
        assert "USD" in result["formatted_output"]

    def test_report_echoes_the_accepted_caller_policy(self):
        result = GenerateReportNode()(
            _state(
                _SAMPLE_RESULTS,
                validation_policy={
                    "channel": "swift_mt103",
                    "required_fields": [],
                    "amount_min": 10.0,
                    "amount_max": 5000.0,
                },
            )
        )
        assert "swift_mt103" in result["formatted_output"]
        assert "10.0" in result["formatted_output"] and "5000.0" in result["formatted_output"]

    def test_report_without_a_policy_names_the_configured_rules(self):
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert "configured amount rules" in result["formatted_output"]


class TestValueRedaction:
    """The stated invariant: an account identifier never reaches the report.

    Recognition is by form as well as by field name, so a value that happens
    to be an IBAN reaches the report masked even when its field name says
    nothing about accounts.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "GB82WEST12345698765432",
            "GB82 WEST 1234 5698 7654 32",
            "GB82-WEST-1234-5698-7654-32",
            "DE89370400440532013000",
            "4111 1111 1111 1111",
            "4111111111111111",
            "1234 5678 9012",
            "123-45-6789",
            "987654321098765",
        ],
    )
    def test_identifier_forms_are_masked(self, value):
        assert looks_like_account_identifier(value) is True
        assert redact_value("beneficiary", value) == "[MASKED]"

    @pytest.mark.parametrize(
        "value",
        [
            "1500.00",
            "1,234,567.89",
            "-500.25",
            "+1500.00",
            "0.01",
            "100000000.00",
            "2026-07-10",
            "TX-2026-001",
            "BARCGB22",
            "USD",
            "90",
            "2026",
            "1234567",
        ],
    )
    def test_structural_values_stay_byte_identical(self, value):
        assert looks_like_account_identifier(value) is False
        assert redact_value("reference", value) == value

    @pytest.mark.parametrize(
        "field",
        ["iban", "beneficiary_iban", "account_number", "card", "swift_code", "bic", "api_token"],
    )
    def test_sensitive_field_names_mask_whatever_they_hold(self, field):
        assert redact_value(field, "anything at all") == "[MASKED]"

    @pytest.mark.parametrize(
        "value",
        [
            "payer@example.com",
            "a.b+c%d@sub.example.co.jp",
            "090-1234-5678",
            "555-123-4567",
        ],
    )
    def test_personal_data_forms_are_masked_without_the_framework_gate(self, value):
        """The report is the template's own guarantee, not the input gate's.

        The framework masks these forms in the record before execute() runs;
        redact_value() is called here directly, so the report holds even where
        that gate is absent or configured off.
        """
        assert redact_value("reference", value) == "[MASKED]"

    @pytest.mark.parametrize(
        "value",
        ["ACME Corp", "not-an-email@", "@example.com", "TX-2026-001"],
    )
    def test_ordinary_values_are_not_mistaken_for_personal_data(self, value):
        assert redact_value("reference", value) == value

    def test_empty_value_stays_empty(self):
        assert redact_value("amount", "") == ""
        assert redact_value("amount", None) == ""

    def test_identifier_reaches_the_report_masked(self):
        results = [
            {
                "field": "beneficiary",
                "value": "GB82 WEST 1234 5698 7654 32",
                "status": "PASS",
                "rule": "reference_format",
                "message": "format valid",
            }
        ]
        result = GenerateReportNode()(_state(results))
        report = result["formatted_output"]
        assert "GB82" not in report
        assert "[MASKED]" in report
        assert "1 value(s) were masked" in report

    def test_a_credential_pattern_is_masked_not_mangled(self):
        """Redaction replaces the whole value; it never rewrites part of it.

        A layer that rewrote characters in place could destroy the very shape
        the output scan looks for, so the value is replaced wholesale.
        """
        results = [
            {
                "field": "reference",
                "value": "123-45-6789",
                "status": "WARN",
                "rule": "reference_format",
                "message": "format invalid",
            }
        ]
        report = GenerateReportNode()(_state(results))["formatted_output"]
        assert "123-45-6789" not in report
        assert "45-6789" not in report and "123-45" not in report
        assert "[MASKED]" in report

    def test_long_values_are_not_trimmed_before_the_scan(self):
        long_reference = "REF" + "A" * 60
        results = [
            {
                "field": "reference",
                "value": long_reference,
                "status": "PASS",
                "rule": "reference_format",
                "message": "format valid",
            }
        ]
        report = GenerateReportNode()(_state(results))["formatted_output"]
        assert long_reference in report


class TestGenerateReportNodeErrors:
    def test_missing_validation_results_returns_error(self):
        result = GenerateReportNode()({"overall_status": "PASS", "remediation_suggestions": "", **_ANON})
        assert result["status"] == AgentStatus.ERROR.value

    def test_writes_formatted_output_key(self):
        """AgentBaseGraph.get_output() reads state['formatted_output']."""
        result = GenerateReportNode()(_state(_SAMPLE_RESULTS))
        assert "formatted_output" in result
        assert "output" not in result or result.get("output") is None

    def test_empty_result_list_still_renders(self):
        result = GenerateReportNode()(_state([]))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "no field-level results" in result["formatted_output"]
