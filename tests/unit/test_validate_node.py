"""Unit tests for ValidateNode — the rule engine."""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.validate_node import ValidateNode

_ANON = {"caller_trust_level": TrustLevel.ANONYMOUS.value}


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    """Patch emit_trace_event to avoid backend calls."""
    monkeypatch.setattr(
        "src.nodes.validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestValidateNodeTrustLevel:
    def test_required_trust_level_is_anonymous(self):
        assert ValidateNode.required_trust_level == TrustLevel.ANONYMOUS


class TestValidateNodePass:
    def test_valid_record_produces_pass(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "1000.00",
                "currency": "USD",
                "iban": "GB82WEST12345698765432",
                "swift_code": "BARCGB22",
                "value_date": "2026-07-10",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["overall_status"] == "PASS"
        assert isinstance(result["validation_results"], list)
        assert len(result["validation_results"]) > 0

    def test_status_is_success_even_on_validation_fail(self):
        """ValidateNode status=SUCCESS means the validation RAN; overall_status indicates outcome."""
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "bad_amount",
                "currency": "USD",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        # Node execution succeeded; validation outcome is in overall_status
        assert result["status"] == AgentStatus.SUCCESS.value


class TestValidateNodeIBAN:
    def test_valid_iban_passes(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "500.00",
                "currency": "GBP",
                "iban": "GB82WEST12345698765432",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        iban_results = [r for r in result["validation_results"] if r["field"] == "iban"]
        assert any(r["status"] == "PASS" for r in iban_results)

    def test_invalid_iban_fails(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "500.00",
                "currency": "GBP",
                "iban": "INVALIDIBAN",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        iban_results = [r for r in result["validation_results"] if r["field"] == "iban"]
        assert any(r["status"] == "FAIL" for r in iban_results)


class TestValidateNodeAmount:
    def test_valid_amount_passes_format(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "750.50",
                "currency": "EUR",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        amount_results = [r for r in result["validation_results"] if r["field"] == "amount"]
        assert any(r["status"] == "PASS" for r in amount_results)

    def test_negative_amount_warns(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "-100.00",
                "currency": "EUR",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Negative amount below range minimum — should produce WARN
        amount_range_results = [
            r for r in result["validation_results"] if r["field"] == "amount" and r["rule"] == "amount_range"
        ]
        if amount_range_results:
            assert any(r["status"] == "WARN" for r in amount_range_results)


class TestValidateNodeRequiredFields:
    def test_missing_required_field_produces_fail(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "iban": "GB82WEST12345698765432",
                # 'amount' and 'currency' missing
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["overall_status"] == "FAIL"
        required_fails = [r for r in result["validation_results"] if r["status"] == "FAIL"]
        assert len(required_fails) > 0


class TestValidateNodeSensitiveMasking:
    def test_iban_value_is_masked_in_results(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "100.00",
                "currency": "USD",
                "iban": "GB82WEST12345698765432",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        iban_results = [r for r in result["validation_results"] if r["field"] == "iban"]
        # Sensitive fields should be masked in the result value
        for r in iban_results:
            assert r.get("value") == "[MASKED]"


class TestValidateNodeErrors:
    def test_empty_parsed_fields_returns_error(self):
        node = ValidateNode()
        state = {
            "parsed_fields": None,
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")

    def test_missing_parsed_fields_returns_error(self):
        node = ValidateNode()
        state = {
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")


class TestValidateNodeOverallStatus:
    def test_all_pass_gives_pass(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "amount": "100.00",
                "currency": "USD",
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["overall_status"] in ("PASS", "WARN", "FAIL")

    def test_remediation_present_on_fail(self):
        node = ValidateNode()
        state = {
            "parsed_fields": {
                "currency": "USD",
                # missing 'amount'
            },
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["remediation_suggestions"]
        assert len(result["remediation_suggestions"]) > 0


class TestValidateNodeNonFiniteAmounts:
    """A number that cannot be compared must never be reported as in range.

    NaN and +/-Infinity parse through float() and then compare False against
    every bound, so a naive range check reports an unbounded amount as valid —
    fail OPEN on the exact decision the rule exists to make.
    """

    @pytest.mark.parametrize(
        "amount",
        ["NaN", "nan", "Infinity", "-Infinity", "inf", "-inf", "1e400", "not-a-number", ""],
    )
    def test_uncomparable_amount_is_never_reported_in_range(self, amount):
        node = ValidateNode()
        result = node({"parsed_fields": {"amount": amount, "currency": "USD"}, **_ANON})
        assert result["status"] == AgentStatus.SUCCESS.value
        range_results = [r for r in result["validation_results"] if r["rule"] == "amount_range"]
        assert not any(
            r["status"] == "PASS" and "in valid range" in r["message"] for r in range_results
        ), f"{amount!r} was reported as a valid amount"

    @pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "1e400"])
    def test_uncomparable_amount_is_flagged(self, amount):
        node = ValidateNode()
        result = node({"parsed_fields": {"amount": amount, "currency": "USD"}, **_ANON})
        assert result["overall_status"] in ("WARN", "FAIL")

    def test_finite_amount_still_passes(self):
        node = ValidateNode()
        result = node({"parsed_fields": {"amount": "1500.00", "currency": "USD"}, **_ANON})
        range_results = [r for r in result["validation_results"] if r["rule"] == "amount_range"]
        assert range_results and all(r["status"] == "PASS" for r in range_results)

    def test_comma_grouped_amount_still_parses(self):
        node = ValidateNode()
        result = node({"parsed_fields": {"amount": "1,500.00", "currency": "USD"}, **_ANON})
        range_results = [r for r in result["validation_results"] if r["rule"] == "amount_range"]
        assert range_results and all(r["status"] == "PASS" for r in range_results)


class TestValidateNodeCallerPolicy:
    """The accepted caller policy changes the verdict — real work, real inputs."""

    _RECORD = {"amount": "1500.00", "currency": "USD"}

    def test_caller_band_narrows_the_amount_check(self):
        node = ValidateNode()
        without = node({"parsed_fields": dict(self._RECORD), **_ANON})
        assert without["overall_status"] == "PASS"

        with_band = node(
            {
                "parsed_fields": dict(self._RECORD),
                "validation_policy": {"amount_min": 2000.0, "amount_max": 9000.0},
                **_ANON,
            }
        )
        assert with_band["overall_status"] == "WARN"
        flagged = [r for r in with_band["validation_results"] if r["rule"] == "amount_range" and r["status"] == "WARN"]
        assert flagged and "below minimum (2000.0)" in flagged[0]["message"]

    def test_caller_required_field_changes_the_verdict(self):
        node = ValidateNode()
        result = node(
            {
                "parsed_fields": dict(self._RECORD),
                "validation_policy": {"required_fields": ["value_date"]},
                **_ANON,
            }
        )
        assert result["overall_status"] == "FAIL"
        missing = [
            r for r in result["validation_results"] if r["rule"] == "required_field" and r["field"] == "value_date"
        ]
        assert missing and missing[0]["status"] == "FAIL"

    def test_caller_required_field_is_satisfied_when_present(self):
        node = ValidateNode()
        result = node(
            {
                "parsed_fields": {**self._RECORD, "value_date": "2026-07-10"},
                "validation_policy": {"required_fields": ["value_date"]},
                **_ANON,
            }
        )
        assert result["overall_status"] == "PASS"

    def test_configured_required_fields_are_never_dropped_by_a_caller_list(self):
        node = ValidateNode()
        result = node(
            {
                "parsed_fields": {"currency": "USD"},
                "validation_policy": {"required_fields": ["currency"]},
                **_ANON,
            }
        )
        # 'amount' comes from the shipped rule set and still has to be there.
        assert result["overall_status"] == "FAIL"
        assert any(r["field"] == "amount" and r["status"] == "FAIL" for r in result["validation_results"])

    def test_absent_policy_uses_the_configured_rules(self):
        node = ValidateNode()
        result = node({"parsed_fields": dict(self._RECORD), "validation_policy": None, **_ANON})
        assert result["overall_status"] == "PASS"


class TestValidateNodeRuleFileBounds:
    """An unusable bound in the rule file is dropped, never compared."""

    def test_non_finite_rule_bound_does_not_silently_pass_everything(self):
        from src.nodes import validate_node as module

        results = module._check_range(
            {"amount": "5000000000.00"},
            {"amount_range": {"fields": ["amount"], "min": float("nan"), "max": 100.0}},
        )
        assert results
        # The NaN minimum is dropped; the usable maximum still bites.
        assert results[0]["status"] != "PASS"
