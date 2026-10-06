"""Unit tests for PostProcessNode — the output boundary scan.

The scan is the second, independent output layer: it reads the finished report
and blocks it if anything sensitive survived redaction. These tests feed it
reports the first layer never saw, so the layer is proven on its own rather
than through the pipeline that normally precedes it.
"""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.post_process_node import PostProcessNode, scan_output

_ANON = {"caller_trust_level": TrustLevel.ANONYMOUS.value}

_CLEAN_REPORT = """======================================================================
FINANCIAL DATA ENTRY VALIDATION REPORT
======================================================================

Overall Status: [PASS] PASS
Submitted via:  swift_mt103
Amount policy:  configured amount rules

Field-Level Validation Results:
  Field                          Value                      Status   Rule                      Message
  --------------------------------------------------------------------------------------------
  amount                         1,234,567.89               [PASS]   amount_range              value in valid range
  currency                       USD                        [PASS]   currency_code             format valid
  value_date                     2026-07-10                 [PASS]   date_iso                  format valid
  settlement_date                2026-07-11                 [PASS]   date_iso                  format valid
  reference                      TX-2026-001                [PASS]   reference_format          format valid
  swift_code                     [MASKED]                   [PASS]   swift_bic                 format valid

----------------------------------------------------------------------
Note: account identifiers and credential-bearing values are shown as [MASKED]; 1 value(s) were masked.
======================================================================"""


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    """Patch emit_trace_event to avoid backend calls."""
    monkeypatch.setattr(
        "src.nodes.post_process_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestPostProcessNodeTrustLevel:
    def test_required_trust_level_is_anonymous(self):
        assert PostProcessNode.required_trust_level == TrustLevel.ANONYMOUS


class TestCleanReportPasses:
    def test_a_clean_report_is_released_unchanged(self):
        result = PostProcessNode()({"formatted_output": _CLEAN_REPORT, **_ANON})
        assert result["status"] == AgentStatus.SUCCESS.value
        # The gate does not rewrite the report; it only decides.
        assert "formatted_output" not in result

    @pytest.mark.parametrize(
        "line",
        [
            "amount 1500.00 2026-07-10 TX-2026-001",
            "Transaction amount must be between 0.01 and 100,000,000: value in valid range",
            "value_date 2026-07-10 settlement_date 2026-07-11",
            "currency USD amount 1,234,567.89",
            "reference TX-2026-001 BIC BARCGB22",
            "ISO 4217 currency code (e.g. USD, EUR, JPY)",
            "session 2026-07-10 12:30:45 duration 120 ms",
            "Payment reference (1-35 chars, alphanumeric + limited punctuation): format valid",
            "=" * 70,
            "-" * 110,
        ],
    )
    def test_ordinary_report_lines_are_not_flagged(self, line):
        assert scan_output(line) is None


class TestLeakFormsAreBlocked:
    @pytest.mark.parametrize(
        ("label", "leak"),
        [
            ("iban", "beneficiary GB82WEST12345698765432"),
            ("iban spaced", "beneficiary GB82 WEST 1234 5698 7654 32"),
            ("iban hyphenated", "beneficiary GB82-WEST-1234-5698-7654-32"),
            ("iban other country", "beneficiary DE89370400440532013000"),
            ("card grouped", "card 4111 1111 1111 1111"),
            ("card hyphenated", "card 4111-1111-1111-1111"),
            ("account bare", "acct 987654321098765"),
            ("account grouped", "acct 1234 5678 9012"),
            ("api key", "trace sk-abcdefghijklmnopqrst12345"),
            ("jwt", "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1"),
            ("bearer", "header Bearer abcdefghijklmnopqrstuvwxyz123"),
            ("credential assignment", "note password: hunter2hunter2"),
        ],
    )
    def test_each_leak_form_is_detected(self, label, leak):
        assert scan_output(leak) is not None, label

    @pytest.mark.parametrize(
        "leak",
        [
            "beneficiary GB82WEST12345698765432",
            "card 4111 1111 1111 1111",
            "trace sk-abcdefghijklmnopqrst12345",
        ],
    )
    def test_a_leaking_report_fails_closed(self, leak):
        result = PostProcessNode()({"formatted_output": f"{_CLEAN_REPORT}\n{leak}\n", **_ANON})
        assert result["status"] == AgentStatus.ERROR.value
        # The report is replaced with a notice, so the leak never ships.
        assert "GB82" not in result["formatted_output"]
        assert "4111" not in result["formatted_output"]
        assert "sk-" not in result["formatted_output"]
        assert result["error_log"]

    def test_blocking_names_the_pattern_not_the_value(self):
        result = PostProcessNode()({"formatted_output": "beneficiary GB82WEST12345698765432", **_ANON})
        assert "GB82WEST12345698765432" not in repr(result)
        assert any("account_iban" in msg for msg in result["error_log"])


class TestScanDoesNotRewriteStructure:
    """The scan decides; it never edits.

    A gate that rewrote the report in place could renumber a section or split
    an identifier, destroying the very pattern the next reader depends on. This
    one only blocks, and its patterns stay inside a single line.
    """

    def test_a_three_letter_code_before_a_numbered_heading_is_untouched(self):
        text = "Currency: JPY\n\n3. Cash Position\n  amount 1500.00\n"
        assert scan_output(text) is None
        result = PostProcessNode()({"formatted_output": text, **_ANON})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "formatted_output" not in result

    def test_patterns_do_not_join_material_across_lines(self):
        # A grouped account number confined to one line is caught.
        assert scan_output("reference 1234\n5678 9012 3456\n") is not None
        # What must never happen is a match manufactured by joining two lines
        # that are individually harmless.
        assert scan_output("reference 1234\namount 10.00\n") is None
        assert scan_output("value_date 2026\n07 10 2026\n") is None

    def test_a_clean_report_is_returned_byte_identical_by_the_pipeline(self):
        result = PostProcessNode()({"formatted_output": _CLEAN_REPORT, **_ANON})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("formatted_output") is None


class TestEmptyReport:
    @pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
    def test_empty_report_is_an_error(self, empty):
        result = PostProcessNode()({"formatted_output": empty, **_ANON})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_missing_report_key_is_an_error(self):
        result = PostProcessNode()(dict(_ANON))
        assert result["status"] == AgentStatus.ERROR.value
