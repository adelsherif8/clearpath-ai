"""ClearPath as an MCP server (Model Context Protocol, streamable HTTP at /mcp).

Exposes the same agent the dashboard uses as tools any MCP client can call:
  list_sample_cases  — synthetic prior-auth requests to try
  search_criteria    — RAG over the clinical-criteria knowledge base
  review_case        — runs the LangGraph pipeline (retrieve → proposer → critic → route)
  get_eval_metrics   — the offline evaluation results (accuracy, auto-resolution, cost)

Safety: decision SUPPORT only. review_case never issues an autonomous denial; denials and
low-confidence cases come back routed to a human, exactly as in the web app.
"""
import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.graph import run_case, _escalation_reasons, kb
from app import llm

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = json.load(open(os.path.join(_HERE, "data", "cases.json")))
_BY_ID = {c["id"]: c for c in SAMPLES}

mcp = MCPServer(
    name="clearpath",
    title="ClearPath AI — prior-authorization review",
    instructions=("Decision support for prior-authorization requests. Use search_criteria to see which "
                  "clinical criteria apply, and review_case to run the full agent. Denials and low-confidence "
                  "cases are always routed to a human reviewer; never present them as final."),
    version="1.0.0",
)


def _case(case_id: str | None, procedure: str | None, condition: str | None, notes: str | None,
          age: int | None, sex: str | None, plan: str | None) -> dict:
    if case_id:
        if case_id not in _BY_ID:
            raise ValueError(f"unknown case_id {case_id}; call list_sample_cases")
        return dict(_BY_ID[case_id])
    if not (procedure and condition and notes):
        raise ValueError("give either case_id, or procedure + condition + notes")
    return {"id": "MCP-CASE", "age": age or 0, "sex": sex or "U", "plan": plan or "Commercial",
            "requested_procedure": procedure, "condition": condition, "notes": notes}


@mcp.tool(description="List the synthetic prior-authorization cases bundled with ClearPath (no PHI).")
def list_sample_cases() -> list[dict[str, Any]]:
    return [{"case_id": c["id"], "procedure": c["requested_procedure"], "condition": c["condition"],
             "plan": c["plan"]} for c in SAMPLES]


@mcp.tool(description="Retrieve the clinical criteria most relevant to a request (RAG over the criteria knowledge base).")
def search_criteria(procedure: str, condition: str, notes: str = "", plan: str = "Commercial",
                    k: int = 3) -> list[dict[str, Any]]:
    case = {"requested_procedure": procedure, "condition": condition, "plan": plan, "notes": notes}
    return [{key: v for key, v in c.items() if key != "embedding"} for c in kb().retrieve(case, k=max(1, min(k, 5)))]


@mcp.tool(description=("Run the ClearPath agent on a case: retrieve criteria, propose a decision, critique it, and route. "
                       "Pass case_id for a sample case, or procedure + condition + notes for your own (synthetic) case. "
                       "Returns decision, confidence, route (auto_approved or needs_human), rationale and escalation reasons."))
def review_case(case_id: str | None = None, procedure: str | None = None, condition: str | None = None,
                notes: str | None = None, age: int | None = None, sex: str | None = None,
                plan: str | None = None) -> dict[str, Any]:
    case = _case(case_id, procedure, condition, notes, age, sex, plan)
    state = run_case(case)
    return {
        "case_id": case.get("id"),
        "decision": state["decision"],
        "confidence": state["confidence"],
        "route": state["route"],
        "needs_human_review": state["route"] == "needs_human",
        "escalation_reasons": _escalation_reasons(state) if state["route"] == "needs_human" else [],
        "rationale": (state.get("proposal") or {}).get("rationale"),
        "critique": state.get("critique"),
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
