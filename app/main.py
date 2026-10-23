"""ClearPath AI — API + reviewer dashboard."""

import os
import json
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.graph import run_case, _escalation_reasons
from app import llm, tracing
from app.mcp_server import mcp, http_app

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

SAMPLES = json.load(open(os.path.join(_HERE, "data", "cases.json")))
try:
    METRICS = json.load(open(os.path.join(_HERE, "data", "metrics.json")))
except FileNotFoundError:
    METRICS = None

_mcp_app = http_app()


@asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():   # MCP streamable-HTTP session manager
        yield


app = FastAPI(title="ClearPath AI", lifespan=lifespan)
app.mount("/mcp", _mcp_app)   # MCP endpoint: <base>/mcp/


def _result(case: dict, state: dict, trace_url: str | None) -> dict:
    reasons = _escalation_reasons(state) if state["route"] == "needs_human" else []
    return {
        "id": case.get("id") or f"PA-{uuid.uuid4().hex[:6].upper()}",
        "case": case,
        "decision": state["decision"],
        "confidence": state["confidence"],
        "route": state["route"],
        "attempt": state.get("attempt", 1),
        "proposal": state["proposal"],
        "critique": state["critique"],
        "trace": state["trace"],
        "usage": state.get("usage"),
        "escalation_reasons": reasons,
        "trace_url": trace_url,
        "mode": llm.mode(),
    }


def _process(case: dict, notes: str = "", attempt: int = 1) -> dict:
    state = run_case(case, reviewer_notes=notes, attempt=attempt)
    trace_url = tracing.trace_run(case, state)
    return _result(case, state, trace_url)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "mode": llm.mode(),
        "langfuse": tracing.configured(),
        "samples": SAMPLES,
        "metrics": METRICS,
    })


@app.post("/submit")
async def submit(request: Request):
    body = await request.json()
    case = body.get("case") or body
    required = ["requested_procedure", "condition", "plan", "notes"]
    if not all(case.get(k) for k in required):
        return JSONResponse({"error": "Missing required fields."}, status_code=400)
    case.setdefault("age", body.get("age", "—"))
    case.setdefault("sex", body.get("sex", "—"))
    return _process(case)


@app.post("/review")
async def review(request: Request):
    body = await request.json()
    action = body.get("action")
    case = body.get("case")
    if not case or action not in {"approve", "deny", "send_back"}:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if action == "send_back":
        notes = body.get("notes", "").strip() or "Reviewer requested another look."
        attempt = int(body.get("attempt", 1)) + 1
        result = _process(case, notes=notes, attempt=attempt)
        result["self_corrected"] = True
        return result
    # approve / deny finalize the human decision (audit record returned)
    return {
        "finalized": action,
        "id": body.get("id"),
        "by": "human reviewer",
        "note": body.get("notes", ""),
    }


@app.get("/metrics")
def metrics():
    if METRICS is None:
        return JSONResponse({"error": "metrics not computed"}, status_code=404)
    return METRICS


@app.get("/samples")
def samples():
    return SAMPLES


@app.get("/health")
def health():
    return {"ok": True, "mode": llm.mode(), "langfuse": tracing.configured()}
