# Financial Data Entry Validation Agent

AI agent for validating financial data entries, built with Agentic Star.

> **Category**: Cat 2 (domain-specific validation pipeline)
> **Industry**: Finance
> **Template ID**: FIN-C2-006

## Overview

Checks financial data entries — payment instructions, trade tickets, account records — against a
declarative rule set before they are booked or sent downstream. A record arrives as JSON, CSV or
key-value text; the agent parses it, applies presence, format, range and pattern rules from
`config/rules.yaml`, and returns a per-field report marking every field PASS, WARN or FAIL together
with remediation text for whatever needs correcting. Rules ship for IBAN, SWIFT/BIC, ISO 4217
currency codes, ISO 8601 dates, amount format and amount range, and are edited in YAML rather than
in code.

Field values that name an account or a credential (IBAN, card, SWIFT, routing, token, …) are
replaced with `[MASKED]` before they reach the report, and the report is scanned again at the
output boundary — a raw account identifier or credential pattern that reaches it blocks the report
instead of shipping it. The whole pipeline is deterministic: no model call, no network dependency,
same input in, same report out.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design specification and test specification.

## Customising

1. Edit `config/rules.yaml` — the required fields, format patterns, ranges and severities are the
   template's whole policy surface, and none of it lives in code.
2. Adjust `config/config.yaml` for your own runtime parameters.
3. Review the node implementations under `src/nodes/` for domain-specific logic — in particular the
   sensitive-field patterns masked in the report and the output-boundary scan that enforces it.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
