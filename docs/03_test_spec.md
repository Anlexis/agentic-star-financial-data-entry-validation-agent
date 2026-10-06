# Test Specification — FIN-C2-006

## Overview

Financial Data Entry Validation Agent. Flat Cat-2 pipeline:
PreProcessNode → ParseNode (main) → ValidateNode → GenerateReportNode →
PostProcessNode.

The suite is organised around the two surfaces that carry the template's
guarantees — the caller inputs and the output boundary — and every rule there
is pinned in **both directions**: the hostile form is refused, and the
legitimate look-alike is not.

| Test module | Cases |
|-------------|-------|
| `tests/unit/test_pre_process_node.py` | 76 |
| `tests/unit/test_parse_node.py` | 10 |
| `tests/unit/test_validate_node.py` | 34 |
| `tests/unit/test_generate_report_node.py` | 53 |
| `tests/unit/test_post_process_node.py` | 35 |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | 2 |
| `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | 32 |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | 3 |
| `tests/proof_of_boundary/test_import_isolation.py` | 1 |
| `tests/proof_of_boundary/test_state_safety.py` | 1 |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | 1 (skipped) |

---

## Unit Test Suite (`tests/unit/`)

### PreProcessNode (`test_pre_process_node.py`)

**Record screening**

| TC | Scenario | Expected |
|----|----------|---------|
| PP-01 | Valid JSON record | status=SUCCESS, input_record set |
| PP-02 | Valid key-value record | status=SUCCESS |
| PP-03 | Leading/trailing whitespace | input_record is stripped |
| PP-04 | Empty string | status=ERROR, error_log populated |
| PP-05 | Whitespace-only string | status=ERROR |
| PP-06 | Non-string input | status=ERROR |
| PP-07 | Input longer than 10 000 chars | status=ERROR, message names the length |
| PP-08 | No caller policy supplied | validation_policy carries the documented defaults |

**Trust gate** — enforced by `BaseNode.__call__`, not by `execute()`

| TC | Scenario | Expected |
|----|----------|---------|
| PP-10 | ANONYMOUS caller | status=ERROR before execute() runs; no execute-only keys present |
| PP-11 | VERIFIED_EXTERNAL caller | passes the gate, node runs |

**Injection refusal owned by the node** — `execute()` called directly, with no
framework wrapper in front, so the guarantee is proven to be the template's own

| TC | Scenario | Expected |
|----|----------|---------|
| PP-20 | 8 hostile forms (prompt override, role reassignment, SQL in statement context, script, `javascript:`) | status=ERROR, nothing carried forward |
| PP-21 | 6 ordinary records containing the same words (`Transact as a settlement agent`, `Insert Into Trust Holdings Ltd`, `SELECT-2026-001`, `DROP-SHIP-0041`, …) | status=SUCCESS, record unchanged |
| PP-22 | Injection through the framework wrapper | status=ERROR, nothing published |

**Caller-policy numerics** — parametrized matrix, per numeric field

| TC | Scenario | Expected |
|----|----------|---------|
| PP-30 | `amount_min` / `amount_max` × {`"NaN"`, `"Infinity"`, `"-Infinity"`, `float("nan")`, `float("inf")`, `float("-inf")`, `True`, `False`, `"12"`, `None`, `[]`, `{}`, `1e13`, `-1.0`} | status=ERROR naming the field; no validation_policy emitted |
| PP-31 | Valid band | reaches validation_policy unchanged |
| PP-32 | One-sided band, inverted band | status=ERROR |

**Caller-policy strings** — locked to inert identifiers `[a-z0-9_]{1,32}`

| TC | Scenario | Expected |
|----|----------|---------|
| PP-40 | 12 non-identifier forms (spaces, uppercase, over-length, wrong type, over-long list, non-list) | status=ERROR |
| PP-41 | Valid identifiers | reach validation_policy |
| PP-42 | Rejected value carries a marker string | marker absent from the entire result |
| PP-43 | Unknown context field / non-dict input_context | ignored; request proceeds |

### ParseNode (`test_parse_node.py`)

| TC | Scenario | Expected |
|----|----------|---------|
| PA-01 | Valid JSON object | parsed_fields matches the JSON keys/values |
| PA-02 | JSON with numeric values | status=SUCCESS |
| PA-03 | Key-value colon format | parsed_fields["amount"] = "100" |
| PA-04 | Key-value equals format | status=SUCCESS |
| PA-05 | Semicolon-separated key-value | status=SUCCESS |
| PA-06 | CSV with header row | parsed_fields matches the column mapping |
| PA-07 | Empty input_record | status=ERROR |
| PA-08 | Falls back to user_input when input_record is absent | parsed from user_input |

### ValidateNode (`test_validate_node.py`)

**Rule application**

| TC | Scenario | Expected |
|----|----------|---------|
| VA-01 | All valid fields | status=SUCCESS, overall_status="PASS" |
| VA-02 | Fields with violations | status=SUCCESS (the node ran), overall_status="FAIL" |
| VA-03 | Valid IBAN | iban rule → PASS |
| VA-04 | Invalid IBAN | iban rule → FAIL |
| VA-05 | Valid amount format | amount_format → PASS |
| VA-06 | Negative amount | amount_range → WARN |
| VA-07 | Missing required field | required_field → FAIL, overall_status="FAIL" |
| VA-08 | IBAN value in the results | value == "[MASKED]" |
| VA-09 | None / missing parsed_fields | status=ERROR |
| VA-10 | FAIL case | remediation_suggestions non-empty |

**Non-finite amounts** — the fail-open case this rule exists to close

| TC | Scenario | Expected |
|----|----------|---------|
| VA-20 | amount ∈ {`NaN`, `nan`, `Infinity`, `-Infinity`, `inf`, `-inf`, `1e400`, `not-a-number`, `""`} | never reported as "in valid range" |
| VA-21 | amount ∈ {`NaN`, `Infinity`, `-Infinity`, `1e400`} | overall_status is WARN or FAIL |
| VA-22 | Finite and comma-grouped amounts | amount_range → PASS |

**Caller policy**

| TC | Scenario | Expected |
|----|----------|---------|
| VA-30 | Caller band narrower than the amount | overall_status="WARN", message names the caller minimum |
| VA-31 | Caller-required field absent | overall_status="FAIL" for that field |
| VA-32 | Caller-required field present | overall_status="PASS" |
| VA-33 | Caller list supplied | the configured required fields still apply |
| VA-34 | No policy | the configured rules apply unchanged |

**Rule-file bounds**

| TC | Scenario | Expected |
|----|----------|---------|
| VA-40 | Non-finite `min` in a range rule | the bound is dropped, the usable bound still bites |

### GenerateReportNode (`test_generate_report_node.py`)

**Report structure**

| TC | Scenario | Expected |
|----|----------|---------|
| GR-01 | PASS results | status=SUCCESS, formatted_output non-empty |
| GR-02 | Report carries the overall status | "PASS" in formatted_output |
| GR-03 | FAIL results | "FAIL" in formatted_output |
| GR-04 | Report states the redaction rule | the mask sentinel and the note are present |
| GR-05 | FAIL case | remediation text is in formatted_output |
| GR-06 | Field values are rendered | the values appear in the table |
| GR-07 | Accepted caller policy | channel and band are echoed in the header |
| GR-08 | No caller policy | header names the configured rules |
| GR-09 | Missing validation_results | status=ERROR |
| GR-10 | Output key | writes `formatted_output`, never `output` |
| GR-11 | Empty result list | renders, with the "no field-level results" line |

**Redaction invariant — both directions**

| TC | Scenario | Expected |
|----|----------|---------|
| GR-20 | 9 identifier forms (IBAN unseparated / spaced / hyphenated / other country, card grouped and bare, grouped account, SSN-shaped, 15-digit run) | masked, regardless of field name |
| GR-21 | 13 structural values (amounts signed / comma-grouped / decimal, ISO date, reference, BIC, currency, short digit runs) | byte-identical |
| GR-22 | 7 sensitive field names | masked whatever they hold |
| GR-23 | Identifier reaches the report | no fragment of it appears; masked count reported |
| GR-24 | Credential-shaped value | replaced wholesale, never partially rewritten |
| GR-25 | Over-long value | not trimmed before the output scan reads it |
| GR-26 | 4 personal-data forms (email, grouped phone numbers), checked with no framework wrapper in front | masked |
| GR-27 | Look-alikes (`ACME Corp`, `not-an-email@`, `@example.com`, a reference) | byte-identical |

### PostProcessNode (`test_post_process_node.py`)

The scan is fed reports the redaction layer never produced, so the layer is
proven independently.

| TC | Scenario | Expected |
|----|----------|---------|
| PO-01 | Clean report | status=SUCCESS; the report is not rewritten |
| PO-02 | 10 ordinary report lines (amount beside a date, two dates in a row, comma-grouped amount, reference beside a BIC, rule descriptions, rule separators) | not flagged |
| PO-03 | 12 leak forms (IBAN in 4 spellings, card grouped/hyphenated, account bare/grouped, API key, JWT, bearer token, credential assignment) | each detected |
| PO-04 | A leaking report | status=ERROR; the leak is replaced by a notice |
| PO-05 | Blocking message | names the pattern, never the value |
| PO-06 | Empty / whitespace / missing report | status=ERROR |
| PO-07 | A three-letter code before a numbered heading | not flagged, and the report is not rewritten |
| PO-08 | Two individually harmless lines | no match manufactured by joining them |
| PO-09 | Clean report through the node | returned byte-identical (the gate writes no formatted_output) |

### Framework compliance (`test_framework_compliance_tc06_tc07.py`)

| TC | Scenario | Expected |
|----|----------|---------|
| TC-06 | Subclass overrides the default input gate | TypeError at class definition |
| TC-07 | Subclass overrides the default output gate | TypeError at class definition |

---

## Marketplace Entry Point — `tests/unit/test_cli_entry_point.py`

| ID | Case | Expected |
|----|------|----------|
| CLI-01 | `cli.py` imports | module loads; `run_agent_marketplace`, `load_agent_config` and `FinancialDataEntryValidationAgent` are present |
| CLI-02 | override seam ships empty | `extend_config == {}`; a stray value would silently outrank `config/config.yaml` on the Marketplace path only |
| CLI-03 | the runner receives what the image's CMD would send | executing `cli.py` as `__main__` with the runner replaced captures the call: the graph class, `agent_name`, `namespace`, and every value declared in `config/config.yaml`. Loading the module alone never runs that block, so a wrong class or a dropped config there would otherwise ship unnoticed |

`cli.py` is imported by no other module, so nothing else in the suite would
notice if its import path, graph class or config assembly broke; the image
would build and fail only when the Pod starts. Skipped where the platform
events package is absent.

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

### PB-4 Import Isolation (`test_import_isolation.py`)

Verifies that `src/` never imports the platform internals.

### PB-6 Backbone Invoke Order (`test_pb_invoke_order.py`)

Full `invoke()` with `caller_trust_level=VERIFIED_EXTERNAL` and a SUCCESS payload.

| TC | Scenario | Expected |
|----|----------|---------|
| PB6-01 | Valid payload | node_history == [Initialize, PreProcess, Parse, Validate, GenerateReport, PostProcess, Finalize] |
| PB6-02 | Valid payload | result["output"] non-empty |
| PB6-03 | Empty payload | status=ERROR, "FinalizeNode" in node_history |

### PB-8 End-to-end through `/invoke` (`test_pb_invoke_endpoint.py`)

The real ASGI application with bearer auth — HTTP adapter, trust promotion,
runtime config, compiled graph and output boundary in one path.

| TC | Scenario | Expected |
|----|----------|---------|
| PB8-01 | `/health` | 200, status ok |
| PB8-02 | Runtime config | `max_retry`/`timeout_s` from `config/config.yaml` are on the compiled graph |
| PB8-03 | Authenticated invoke with a caller policy | real report computed from this record and this policy |
| PB8-04 | Two different records | different reports |
| PB8-05 | PASS / WARN / FAIL paths | each reachable from caller data |
| PB8-06 | Caller-required field | reaches the verdict |
| PB8-07 | Unauthenticated / wrong token | status=ERROR, no output |
| PB8-08 | 10 invalid caller policies | status=ERROR, no output, value never echoed |
| PB8-09 | Bare `NaN`/`Infinity`/`-Infinity` JSON literals | never a successful report |
| PB8-10 | `input_context` over 256 KB | HTTP 413 |
| PB8-11 | Injection content | status=ERROR, nothing published |
| PB8-12 | Ordinary record containing attack words | status=SUCCESS |
| PB8-13 | 5 account-identifier forms in a rendered field | none reaches the caller, in any spelling |
| PB8-14 | Shipped report | scans clean at the output boundary |

### PB-2/PB-5 State Safety (`test_state_safety.py`)

Verifies no credential fields and no Pydantic types in `src/schemas/state.py`.

**PB-5 is auto-waived — checkpointing disabled**: `config/config.yaml` enables neither `memory_enabled` nor `hitl.enabled`, so no checkpoint surface exists. The conditional gate and the non-lossy traversal helper ship with the stub and become live assertions once checkpointing is enabled.

### PB-7 HITL Interrupt Propagation (`test_pb7_hitl_interrupt_propagation.py`)

Skip stub — this template is a flat pipeline, so there is no cross-boundary
HITL propagation to assert.

---

## End-to-End Scenarios

| Scenario | Input | Expected |
|----------|-------|---------|
| Valid payment instruction | JSON with amount / currency / IBAN / SWIFT / value_date | overall_status=PASS, IBAN masked |
| Missing currency | key-value record without currency | overall_status=FAIL |
| Invalid IBAN | JSON with `"iban": "BADIBAN"` | iban rule FAIL |
| Amount out of range | JSON with `"amount": "-500"` | amount_range WARN |
| Caller narrows the band | `input_context.amount_min/amount_max` | verdict follows the caller band |
| Non-finite amount | `"amount": "NaN"` | never reported as in range |
| Injection attempt | `ignore previous instructions` | refused at the input boundary |
| Identifier in a free field | `"reference": "GB82 WEST 1234 5698 7654 32"` | masked in the report |
