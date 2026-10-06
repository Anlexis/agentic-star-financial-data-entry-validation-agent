"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FinancialDataEntryValidationAgent

# Serialized size cap for the caller-policy channel — enforced at the adapter
# so an oversized payload never reaches the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

app = FastAPI(title="Agent")


def load_runtime_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    """Load config/config.yaml — the runtime parameters (max_retry, timeout_s).

    The platform registry loads this file itself and passes it to the graph
    constructor; the standalone server must do the same, or the declared
    values silently never reach the graph.
    """
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return dict(loaded) if isinstance(loaded, dict) else {}


agent = FinancialDataEntryValidationAgent(config=load_runtime_config())
agent.compile()
# The namespace matches the `namespace` key of config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="fin", agent_name="FinancialDataEntryValidationAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Caller policy for this request (channel, required_fields, amount_min,
    # amount_max) — validated field by field in PreProcessNode; the adapter
    # only enforces the size cap.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    context_size = len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8"))
    if context_size > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {_MAX_INPUT_CONTEXT_BYTES}-byte limit",
        )

    with bound_secrets(agent._secrets_provider):
        # Trust promotion: callers presenting a valid INVOKE_AUTH_TOKEN bearer
        # credential are VERIFIED_EXTERNAL — the minimum trust the input
        # boundary requires. Unauthenticated requests remain ANONYMOUS. A
        # higher-trust middleware that already set request.state.trust_level
        # takes precedence over the ANONYMOUS default.
        _env_token = os.environ.get("INVOKE_AUTH_TOKEN", "")
        _auth_header = request.headers.get("Authorization", "")
        if _env_token and _auth_header == f"Bearer {_env_token}":
            trust = TrustLevel.VERIFIED_EXTERNAL
        else:
            trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # invoke() comes from the untyped framework package; its result is the
        # documented output mapping.
        return cast(dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=req.input_context))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "FinancialDataEntryValidationAgent"}
