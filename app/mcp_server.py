"""ClearPath as an MCP server (Model Context Protocol, streamable HTTP at /mcp).

Exposes the same agent the dashboard uses as tools any MCP client can call:
  list_sample_cases  — synthetic prior-auth requests to try                     (public)
  search_criteria    — RAG over the clinical-criteria knowledge base            (public, rate-limited)
  review_case        — runs the LangGraph pipeline (retrieve → proposer → critic → route)
                       sample case_id: public, rate-limited
                       your own case text: requires `Authorization: Bearer <Supabase access token>`
  get_eval_metrics   — the offline evaluation results (accuracy, auto-resolution, cost) (public)

Safety: decision SUPPORT only. There is deliberately no tool that finalizes, approves or
denies a case (OWASP LLM06, excessive agency): an MCP client can ask for an opinion, never
record a decision. Denials and low-confidence cases come back routed to a human.
"""
import json
import os
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app import audit, llm
from app.graph import _escalation_reasons, kb, run_case
from app.schemas import CaseIn

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = json.load(open(os.path.join(_HERE, "data", "cases.json")))
_BY_ID = {c["id"]: c for c in SAMPLES}

mcp = MCPServer(
    name="clearpath",
    title="ClearPath AI — prior-authorization review",
    instructions=("Decision support for prior-authorization requests. Use search_criteria to see which "
                  "clinical criteria apply, and review_case to run the full agent. Denials and low-confidence "
                  "cases are always routed to a human reviewer; never present them as final."),
    version="1.1.0",
)


def _ip(ctx: Context | None) -> str:
    headers = (ctx.headers if ctx is not None else None) or {}
    return headers.get("x-cp-client-ip", "unknown")


def _principal(ctx: Context | None):
    """Bearer-token principal, or None when no token was sent. A bad token is an error."""
    from app import auth, db
    headers = (ctx.headers if ctx is not None else None) or {}
    authz = headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        return None
    try:
        return auth.principal_from_token(authz[7:].strip())
    except auth.AuthError:
        audit.record("auth_failure", None, ip=_ip(ctx), outcome="denied", meta={"tool": "mcp"})
        raise ToolError("invalid or expired access token")
    except db.DatabaseUnavailable:
        raise ToolError("authorization service unavailable; nothing was run")


def _limit(ctx: Context | None, bucket: str) -> None:
    from app.config import settings
    from app.security import hit
    ip = _ip(ctx)
    if not hit(bucket, ip, settings().rate_limit_llm):
        audit.record("rate_limited", None, ip=ip, outcome="denied", meta={"tool": bucket})
        raise ToolError("rate limit exceeded; try again in a minute")


def _case(ctx, case_id, procedure, condition, notes, age, sex, plan) -> dict:
    if case_id:
        if case_id not in _BY_ID:
            raise ToolError(f"unknown case_id {case_id}; call list_sample_cases")
        return dict(_BY_ID[case_id])
    if not (procedure and condition and notes):
        raise ToolError("give either case_id, or procedure + condition + notes")
    principal = _principal(ctx)
    if principal is None or principal.role not in ("submitter", "reviewer", "admin"):
        audit.record("permission_denied", principal, ip=_ip(ctx), outcome="denied",
                     meta={"tool": "review_case", "reason": "custom case needs sign-in"})
        raise ToolError("reviewing your own case text requires an access token "
                              "(Authorization: Bearer ...); sample case_ids work without one")
    try:
        c = CaseIn(requested_procedure=procedure, condition=condition, notes=notes,
                   age=age, sex=sex or "U", plan=plan or "Commercial")
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors()})
        raise ToolError(f"invalid case fields: {', '.join(fields)}")
    return {"id": "MCP-CASE", **c.model_dump()}


def _run(case: dict) -> dict:
    try:
        return run_case(case)
    except llm.BudgetExceeded:
        raise ToolError("daily AI review limit reached; nothing was decided")
    except llm.LLMUnavailable:
        raise ToolError("AI reviewer unavailable; nothing was decided or approved")


@mcp.tool(description="List the synthetic prior-authorization cases bundled with ClearPath (no PHI).")
def list_sample_cases() -> list[dict[str, Any]]:
    return [{"case_id": c["id"], "procedure": c["requested_procedure"], "condition": c["condition"],
             "plan": c["plan"]} for c in SAMPLES]


@mcp.tool(description="Retrieve the clinical criteria most relevant to a request (RAG over the criteria knowledge base).")
def search_criteria(procedure: str, condition: str, notes: str = "", plan: str = "Commercial",
                    k: int = 3, ctx: Context = None) -> list[dict[str, Any]]:
    _limit(ctx, "mcp_search")
    case = {"requested_procedure": procedure[:120], "condition": condition[:200], "plan": plan[:40],
            "notes": notes[:4000]}
    try:
        hits = kb().retrieve(case, k=max(1, min(k, 5)))
    except (llm.BudgetExceeded, llm.LLMUnavailable):
        raise ToolError("criteria search unavailable right now")
    return [{key: v for key, v in c.items() if key != "embedding"} for c in hits]


@mcp.tool(description=("Run the ClearPath agent on a case: retrieve criteria, propose a decision, critique it, and route. "
                       "Pass case_id for a sample case, or procedure + condition + notes for your own (synthetic) case "
                       "(requires an access token). Returns decision, confidence, route (auto_approved or needs_human), "
                       "rationale and escalation reasons. This tool never records or finalizes a decision."))
def review_case(case_id: str | None = None, procedure: str | None = None, condition: str | None = None,
                notes: str | None = None, age: int | None = None, sex: str | None = None,
                plan: str | None = None, ctx: Context = None) -> dict[str, Any]:
    _limit(ctx, "mcp_review")
    case = _case(ctx, case_id, procedure, condition, notes, age, sex, plan)
    state = _run(case)
    return {
        "case_id": case.get("id"),
        "decision": state["decision"],
        "confidence": state["confidence"],
        "route": state["route"],
        "needs_human_review": state["route"] == "needs_human",
        "escalation_reasons": _escalation_reasons(state) if state["route"] == "needs_human" else [],
        "rationale": (state.get("proposal") or {}).get("rationale"),
        "critique": {k: v for k, v in (state.get("critique") or {}).items() if not k.startswith("_")},
        "llm_mode": llm.mode(),
    }


@mcp.tool(description="ClearPath's offline evaluation results: accuracy, auto-resolution rate, escalation rate, cost per decision.")
def get_eval_metrics() -> dict[str, Any]:
    try:
        return json.load(open(os.path.join(_HERE, "data", "metrics.json")))
    except FileNotFoundError:
        return {"error": "metrics not generated yet"}


def http_app():
    """Starlette app for the streamable-HTTP transport, mounted by app.main at /mcp."""
    hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                          allowed_hosts=hosts + ["localhost:*", "127.0.0.1:*"])
                if hosts else TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                   transport_security=security)
