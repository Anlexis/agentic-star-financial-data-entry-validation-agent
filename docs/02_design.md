# Template Design Specification — FIN-C2-006

## Position in the Architecture

- **Agent Class**: FinancialDataEntryValidationAgent
- **Category**: Cat 2 — domain-specific validation pipeline
- **Industry**: FIN

| Role | Value |
|------|-------|
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Composition | Flat — all domain nodes registered directly in the backbone, no `GraphNode` wrapper |
| Determinism | Fully deterministic — no model call, no network dependency |

- **Three-Layer Separation**:
  - State: flat TypedDict (`src/schemas/state.py` extends `AgentState`)
  - Node: each node extends `FunctionNode` and implements `execute(state) -> dict`
  - Graph: flat composition — `register_nodes()` fills all five domain slots directly

---

## Architecture Overview

### Composition Model (FLAT — not the nested Cat-2 shape)

This template uses a **flat five-node pipeline** rather than the nested Cat-2
`GraphNode`/inner-graph pattern. All domain nodes sit at the same level in the
`AgentBaseGraph` backbone:

```
START → initialize → pre_process → main(parse) → [SUCCESS] validate
                                                → [ERROR]   finalize
        validate → generate_report → post_process → finalize → END
```

The `main` backbone slot holds `ParseNode` (the first domain step after
`pre_process`). `ValidateNode` and `GenerateReportNode` are extra nodes in
`_nodes`, wired by a custom `add_edges()`.

Because the graph is flat, `input_context` is already present in the state
every node sees — there is no inner graph for it to be forwarded into, and so
no bridging is required.

### Node Configuration

| Slot | Class | Responsibility | Trust Level |
|------|-------|---------------|-------------|
| initialize | InitializeNode (framework) | Sets schema_version, session_id, trust_level | framework |
| pre_process | PreProcessNode | Input boundary — record screening + caller-policy validation | VERIFIED_EXTERNAL |
| main | ParseNode | Parse input_record → parsed_fields (JSON/CSV/KV) | ANONYMOUS |
| validate | ValidateNode | Apply the rule set → validation_results | ANONYMOUS |
| generate_report | GenerateReportNode | Compile the report, redacting every rendered value | ANONYMOUS |
| post_process | PostProcessNode | Output boundary — scan the finished report, block on a leak | ANONYMOUS |
| finalize | FinalizeNode (framework) | Builds response_metadata, total_time_ms | framework |


**Completion is not the same as answering.** A run that ends with
`AgentStatus.SUCCESS` reports that the request was handled safely to a defined
end, not that the request was carried out. A value the caller can correct (an
out-of-contract field, an empty or over-long request) ends this way so the
caller receives the reason and can send a corrected request on the same
conversation; terminating instead would end the calling surface's turn and
surface only an exception type, leaving the reason reachable solely from the
audit trail. The reason travels as `error_code` in State, every later domain
node passes through without doing work once it is set — the reason settled
first is the one the caller receives, never a second vaguer one from a later
node — the structured output fields are withheld, and `PostProcessNode` renders the
reason as a static caller-facing sentence.

Two classes keep terminating, and must not be folded into the above: content
the agent refuses outright (an instruction-override payload — re-sending a
reworded variant is not a correction), and a breach of a contract the caller
cannot influence.

### Data Flow

```
user_input (raw financial record) + input_context (caller policy)
  ↓ PreProcessNode [trust gate, injection scan, caller-policy bounds]
input_record + validation_policy
  ↓ ParseNode [JSON / CSV / KV auto-detection]
parsed_fields (structured {field: value} dict)
  ↓ ValidateNode [required / format / range / regex, narrowed by the policy]
validation_results + overall_status + remediation_suggestions
  ↓ GenerateReportNode [render, redacting every value by field name and by form]
formatted_output (the validation report)
  ↓ PostProcessNode [scan the finished report; block on a leak]
status: SUCCESS → FinalizeNode → invoke() returns {"output": formatted_output, ...}
```

---

## Caller Contract

### Record (`user_input`)

A single financial record as a string: a JSON object, key-value lines
(`field: value` or `field=value`, newline- or semicolon-separated), or CSV with
a header row. Maximum 10 000 characters.

### Caller policy (`input_context`)

Optional. Every field is validated at the input boundary against explicit
bounds before any later node may read it. An invalid value **fails closed** —
the request is rejected naming the field, and the rejected value is never
echoed into the error or the response. Absent fields fall back to the shipped
rule set. No field accepts free text, so caller policy cannot carry renderable
prose into the report.

| Field | Accepted | Default |
|-------|----------|---------|
| `channel` | identifier `[a-z0-9_]{1,32}` | `"unknown"` |
| `required_fields` | list of ≤ 20 identifiers, merged with the configured list | `[]` |
| `amount_min` | finite number in `[0, 1e12]` | the configured minimum |
| `amount_max` | finite number in `[0, 1e12]` | the configured maximum |

`amount_min`/`amount_max` are accepted only as a coherent pair
(`amount_min ≤ amount_max`); a one-sided band would widen the check it is meant
to narrow, so it is refused.

The HTTP adapter caps the serialized `input_context` at 256 KB and answers
`413` above it, so an oversized payload never reaches the graph.

**Numeric handling.** Every caller-controlled number goes through
`_finite_in_range()`, which rejects `bool` (an `int` subclass), non-numeric
types, and — critically — `NaN` and `±Infinity`. Those parse cleanly through
`float()` and then compare `False` against every bound, so a naive check would
silently accept an unbounded value on exactly the decision the rule exists to
make. Python's `json` module also parses bare `NaN`/`Infinity` literals out of
a request body, so this is reachable over HTTP, not just in Python callers.

---

## State Schema (`src/schemas/state.py`)

Extends `AgentState` (flat TypedDict). Domain-specific fields:

| Field | Type | Set by | Description |
|-------|------|--------|-------------|
| `input_record` | `Optional[str]` | PreProcessNode | Validated raw financial record |
| `validation_policy` | `Optional[Dict[str, Any]]` | PreProcessNode | Accepted caller policy, defaults applied |
| `parsed_fields` | `Optional[Dict[str, Any]]` | ParseNode | Structured field dict |
| `validation_results` | `Optional[List[Dict[str, Any]]]` | ValidateNode | Per-field: field / value / status / rule / message |
| `overall_status` | `Optional[str]` | ValidateNode | `"PASS"` \| `"WARN"` \| `"FAIL"` |
| `remediation_suggestions` | `Optional[str]` | ValidateNode | Narrative for the WARN/FAIL entries |
| `formatted_output` | `Optional[Any]` | GenerateReportNode | The final report (inherited from AgentState) |

Inherited from `AgentState`: `user_input`, `validated_input`, `input_context`,
`status`, `error_log`, `node_history`, `session_id`, `trace_id`,
`correlation_id`, `caller_trust_level`.

---

## Validation Rules (`config/rules.yaml`)

`ValidateNode` reads the rule set at import time. Rule types:

| Rule Type | Description | Severity |
|-----------|-------------|---------|
| `required_fields` | Field must be present in parsed_fields | FAIL |
| `format_rules.iban` | IBAN format: `^[A-Z]{2}[0-9]{2}[A-Z0-9]{4}[0-9]{7}[A-Z0-9]{0,16}$` | FAIL |
| `format_rules.swift_bic` | SWIFT/BIC: `^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$` | FAIL |
| `format_rules.date_iso` | ISO date `YYYY-MM-DD` | FAIL |
| `format_rules.currency_code` | ISO 4217 `^[A-Z]{3}$` | FAIL |
| `format_rules.amount_format` | Numeric up to 2 decimals | FAIL |
| `range_rules.amount_range` | 0.01 ≤ amount ≤ 100,000,000 | WARN |
| `regex_rules.reference_format` | Payment reference 1–35 chars | WARN |

A bound in the rule file is a number like any other: an unusable `min`/`max`
is dropped rather than compared, so a malformed rule file cannot silently
disable the check it declares. A field value that cannot be compared at all is
reported at the rule's severity, never as "in valid range".

---

## Output Boundary

The documented invariant is:

> **Account identifiers and credential-bearing values never reach the caller.
> They appear in the report as `[MASKED]`.**

Two independent layers enforce it.

### Layer 1 — redact (`GenerateReportNode`)

Every value rendered into the report passes through `redact_value()` first. A
value is masked when **either** condition holds:

1. its **field name** denotes an account or credential — `iban`, `account`,
   `card`, `pan`, `bic`, `swift`, `routing`, `sort_code`, `credential`,
   `password`, `secret`, `token`;
2. its **value has the form** of an account identifier.

Form recognition runs on a copy of the value with its grouping separators
(space, tab, non-breaking space, hyphen) removed, because
`GB82 WEST 1234 5698 7654 32` is the same identifier as
`GB82WEST12345698765432` and must be treated alike. The decimal point is *not*
a grouping separator — removing it would turn every amount into a long digit
run. Recognised forms:

| Form | Example |
|------|---------|
| IBAN — 2 letters, 2 check digits, 11–30 alphanumerics | `GB82WEST12345698765432` |
| Grouped account/card number (≥ 8 digits once separators are removed) | `4111 1111 1111 1111`, `123-45-6789`, `090-1234-5678` |
| Undelimited digit run of 12+ digits | `4111111111111111` |
| Email address | `payer@example.com` |

Amounts (including comma-grouped and signed) and ISO dates are never
identifiers: the configured amount rules top out at nine digits, which is what
makes the 12-digit threshold safe. Below that threshold an undelimited digit
run is genuinely indistinguishable from an amount — `12345678` is also
12,345,678 — so a short, unseparated account number is covered by naming its
field (`account_number`, `payer_iban`, …) rather than by guessing at the value.

The email form is checked here rather than assumed from the framework input
gate, which masks addresses in the record before `execute()` runs. That gate is
defence in depth; the report is this template's own guarantee, so it does not
depend on it. The unit tests call `redact_value()` directly, with no framework
wrapper in front, for exactly that reason.

Redaction replaces the **whole** value; it never rewrites part of one. Values
are not trimmed to the column width either. Both matter because a layer that
rewrote characters in place could destroy the very shape the next layer looks
for.

### Layer 2 — scan (`PostProcessNode`)

The finished report is scanned for anything that survived: API keys, JWTs,
bearer tokens, credential assignments, IBANs, and grouped or undelimited
account numbers. A hit **blocks** the report — the caller receives a notice and
an error status instead of the leaking text.

The scan is read-only: it decides, it never rewrites, so it cannot destroy the
pattern it exists to catch. Both the IBAN pattern and the grouped-account
pattern pin one separator for the whole identifier (a regex backreference);
allowing a separator between every character instead would let the pattern run
across ordinary spaced text — `TX-2026-001 BIC BARCGB22` reads as a single
22-character identifier under the looser grammar, and an ISO date reads as a
grouped account number.

### Monetary rounding grid — not applicable

Some templates in this family round every monetary figure in an external report
to a fixed grid. This one renders no aggregates: the report shows the record's
own field values, each against the rule that judged it. There is no derived
figure to round, so no grid is enforced. The invariant this template does state
— the redaction rule above — is enforced for every representation, and both
directions are pinned in tests.

---

## Security Design

### Trust
- `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL` — the
  outer entry gate. Only VERIFIED_EXTERNAL or INTERNAL callers may invoke.
- Every later node runs at `TrustLevel.ANONYMOUS`; trust was established at the
  boundary.
- The HTTP adapter promotes a caller presenting a valid `INVOKE_AUTH_TOKEN`
  bearer credential to VERIFIED_EXTERNAL. Unauthenticated callers stay
  ANONYMOUS and are refused.

### Input
- `FunctionNode._security_gate_input()` (framework, `@final`) masks
  email/phone/card PII in `user_input`/`validated_input` and rejects
  high-confidence prompt injection before `execute()` runs.
- **The template does not rely on that gate alone.** `PreProcessNode.execute()`
  runs its own injection scan, so the record is refused even where the
  framework gate is absent or configured off. The unit tests call `execute()`
  directly, with no framework wrapper in front, to prove it.
- The scan is anchored on both sides of every word-shaped pattern, and the
  bare SQL verb pairs count only in statement context. Both matter in this
  domain: without them, `Transact as a settlement agent` and
  `Insert Into Trust Holdings Ltd` are refused as attacks — a validator that
  fails closed on a legitimate beneficiary name blocks a real payment.
- Length guard: 10 000 characters. Adapter guard: 256 KB of `input_context`.
- A financial record legitimately contains account identifiers, so the input
  boundary does not reject them; confidentiality is enforced on the way out.

### Output
- The two layers described under **Output Boundary** above.
- `FunctionNode._security_gate_output()` (framework, `@final`) additionally
  scans every `execute()` return value for credential patterns.
- **No `_extra_security_gate_*` overrides** — the domain checks live inside
  `execute()` and in module-level helpers.

### Audit
- `PreProcessNode`: `pre_process_validated` | `pre_process_rejected`
- `ParseNode`: `parse_complete`
- `ValidateNode`: `validate_complete`
- `GenerateReportNode`: `generate_report_complete` (carries `masked_value_count`)
- `PostProcessNode`: `post_process_complete` | `post_process_blocked`
- All calls use the positional form, never keyword arguments.

### Credentials
- No hardcoded keys, passwords, or secrets in node code.
- `config/agent.yaml` declares no secrets and no extras — the pipeline
  constructs no client and makes no external call.
- `config/rules.yaml` holds validation patterns only.

---

## Configuration Files

| File | Purpose |
|------|---------|
| `config/agent.yaml` | Static manifest, flat — every key at root level; `class:` is one dotted path |
| `config/config.yaml` | Runtime parameters (`max_retry`, `timeout_s`), passed to the graph constructor as `config=` |
| `config/rules.yaml` | The validation rule set |

The standalone server loads `config/config.yaml` itself and passes it to the
constructor. Without that, a graph built with no config falls back to framework
defaults and the declared runtime values never reach it.

---

## Graph Implementation (`src/graph/graph.py`)

```python
class FinancialDataEntryValidationAgent(AgentBaseGraph):
    def register_nodes(self):
        super().register_nodes()  # initialize + finalize
        self._nodes["pre_process"]     = PreProcessNode()
        self._nodes["main"]            = ParseNode()
        self._nodes["validate"]        = ValidateNode()
        self._nodes["generate_report"] = GenerateReportNode()
        self._nodes["post_process"]    = PostProcessNode()

    def add_edges(self):
        # flat wiring: pre → parse(main) → validate → generate_report → post → finalize
        ...
```

`add_edges()` is overridden to insert `validate` and `generate_report` between
`main` (parse) and `post_process`. `route()` is inherited from
`AgentBaseGraph`; it satisfies the abstract contract but is not used by this
wiring, which routes from `main` through `_route_after_parse`.

---

## Test Strategy

See `docs/03_test_spec.md` for the full test specification.

### Unit Tests (`tests/unit/`)
- `test_pre_process_node.py` — trust gate, record screening, injection refusal
  proven at `execute()` level in both directions, and the caller-policy
  contract (non-finite matrix per numeric field, identifier locking, band
  coherence, no echo of a rejected value)
- `test_parse_node.py` — JSON / KV / CSV parsing, malformed input rejection
- `test_validate_node.py` — PASS/WARN/FAIL per rule, non-finite amounts,
  caller-policy effects, unusable rule-file bounds
- `test_generate_report_node.py` — report structure, value rendering, and the
  redaction invariant in both directions
- `test_post_process_node.py` — the output scan in both directions, on reports
  the redaction layer never saw
- `test_framework_compliance_tc06_tc07.py` — the framework gates are final

### Proof-of-Boundary Tests (`tests/proof_of_boundary/`)
- `test_import_isolation.py` — PB-4: no platform-internal imports in `src/`
- `test_pb_invoke_order.py` — PB-6: full backbone `invoke()` → node_history order
- `test_pb_invoke_endpoint.py` — PB-8: end-to-end through the real ASGI
  `/invoke` with bearer auth
- `test_state_safety.py` — PB-2/PB-5: State field safety
- `test_pb7_hitl_interrupt_propagation.py` — PB-7: skip stub (not enabled here)

## Entry Points

The agent is reachable through three entry points, all of which build the graph
from the same `config/config.yaml`:

| Entry point | Construction | Notes |
|---|---|---|
| Platform registry | `Graph(config=...)` by the registry | Reads `config/config.yaml` itself |
| Standalone HTTP (`src/api/server.py`) | Loads `config/config.yaml`, passes `Graph(config=...)` | Caller-auth boundary; see Security Design |
| Marketplace (`cli.py`) | `run_agent_marketplace(...)` is handed the graph class and the resolved config | The runner constructs the graph itself, so `cli.py` resolves `config/config.yaml` with `load_agent_config()` and passes it in; `extend_config` is the seam for deployment-specific overrides |

`cli.py` sits at the repository root because the deployment image starts it as
`CMD ["python", "cli.py"]`. It adds no business logic: graph construction,
lifecycle, secret provisioning and the invocation loop belong to
`run_agent_marketplace()`.

## Caller-Facing Events

Nodes report progress and rejection reasons to the caller as non-terminal
events, so a caller watching a run sees the pipeline advance instead of a
silent wait, and learns what to change when a request is refused.

- **Progress** — each node reports its phase at the top of `execute()`.
- **Rejection reason** — a node that returns `status: error` sends the reason
  first. It has to happen there: once the run carries an error status the
  framework skips `execute()` on every later node, so no downstream node could
  send it. Wording separates what the caller can fix (missing question,
  oversized request, malformed value) from what they cannot (retrieval or
  output failures), so a caller is not invited into a pointless retry.

Both are best-effort: the emitter is resolved lazily and failures are
swallowed, because reporting must never change the outcome of a run. Messages
are static phase and reason labels — no request value, record value or
internal identifier is ever included, since these events leave the process and
are not covered by the S-3 output gate. Terminal delivery (success/failure)
belongs to the platform runner alone.
