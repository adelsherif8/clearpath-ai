"""ClearPath AI — API + reviewer dashboard.

Access model (enforced server-side; the UI only mirrors it):

  anonymous   run the bundled sample cases (rate-limited, nothing stored), read samples/metrics
  submitter   + submit their own cases (stored), see only their own cases
  reviewer    + see cases assigned to them, approve / deny / send back those cases
  admin       + see all cases, assign reviewers, change roles, read the audit log

Approve and deny are reviewer-only. The agent itself can never finalize a denial.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import audit, auth, db, llm, tracing
from app.auth import Principal, current_principal, require
from app.config import settings
from app.graph import _escalation_reasons, run_case
from app.mcp_server import http_app, mcp
from app.schemas import (CaseIn, LoginRequest, ReviewRequest, RoleChangeRequest, SubmitRequest)
from app.security import (BodySizeLimitMiddleware, McpGuardMiddleware, SecurityHeadersMiddleware,
                          client_ip, new_limiter, rate_limited_response)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
if not templates.env.autoescape:   # A05: never render user or model text unescaped
    raise RuntimeError("Jinja autoescaping must stay on")

SAMPLES = json.load(open(os.path.join(_HERE, "data", "cases.json")))
SAMPLES_BY_ID = {c["id"]: c for c in SAMPLES}
try:
    METRICS = json.load(open(os.path.join(_HERE, "data", "metrics.json")))
except FileNotFoundError:
    METRICS = None


def create_app() -> FastAPI:
    s = settings()
    mcp_app = http_app()
    session_manager = mcp.session_manager      # one per app build

    @asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():       # MCP streamable-HTTP session manager
            yield
        db.close()

    prod = s.is_production
    app = FastAPI(title="ClearPath AI", lifespan=lifespan,
                  docs_url=None if prod else "/docs", redoc_url=None,
                  openapi_url=None if prod else "/openapi.json")
    limiter = new_limiter()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limited_response)

    # Middleware (outermost last): headers on everything, then host check, size limit, CORS.
    app.add_middleware(CORSMiddleware, allow_origins=[], allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["content-type", "authorization"])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=s.max_body_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=s.allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
    app.mount("/mcp", McpGuardMiddleware(mcp_app))   # MCP endpoint: <base>/mcp/
    _register_errors(app)
    _register_routes(app, limiter)
    return app


# ── errors (A10): generic to the client, detailed only in our logs ──────────
def _register_errors(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        msg = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else "Request failed."
        return JSONResponse({"error": msg}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", [])[1:]) or "body" for e in exc.errors()})
        return JSONResponse({"error": "Invalid request.", "fields": fields[:10]}, status_code=422)

    @app.exception_handler(db.DatabaseUnavailable)
    async def db_down(request: Request, exc: db.DatabaseUnavailable):
        audit.record("dependency_unavailable", None, ip=client_ip(request), outcome="error",
                     meta={"reason": "database", "path": request.url.path}, persist=False)
        return JSONResponse({"error": "The case database is unavailable. Nothing was saved or decided; "
                                      "please try again shortly."}, status_code=503)

    @app.exception_handler(db.Forbidden)
    async def db_forbidden(request: Request, exc: db.Forbidden):
        p = getattr(request.state, "principal", None)
        audit.record("permission_denied", p, ip=client_ip(request), outcome="denied",
                     meta={"path": request.url.path, "reason": "database policy"})
        return JSONResponse({"error": "Not allowed."}, status_code=403)

    @app.exception_handler(db.NotFound)
    async def db_not_found(request: Request, exc: db.NotFound):
        return JSONResponse({"error": "Case not found."}, status_code=404)

    @app.exception_handler(llm.BudgetExceeded)
    async def budget(request: Request, exc: llm.BudgetExceeded):
        audit.record("llm_budget_exhausted", getattr(request.state, "principal", None),
                     ip=client_ip(request), outcome="denied", meta={"cap": settings().daily_llm_call_cap})
        return JSONResponse({"error": "The daily AI review limit for this demo has been reached. "
                                      "Nothing was decided. Please try again tomorrow."}, status_code=429)

    @app.exception_handler(llm.LLMUnavailable)
    async def llm_down(request: Request, exc: llm.LLMUnavailable):
        audit.record("dependency_unavailable", None, ip=client_ip(request), outcome="error",
                     meta={"reason": "llm", "path": request.url.path})
        return JSONResponse({"error": "The AI reviewer is unavailable right now. Nothing was decided "
                                      "and nothing was approved."}, status_code=503)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        error_id = audit.new_error_id()
        import logging
        logging.getLogger("clearpath").error("unhandled error %s on %s: %s", error_id, request.url.path,
                                             type(exc).__name__, exc_info=exc)
        return JSONResponse({"error": "Something went wrong. Nothing was decided.", "id": error_id},
                            status_code=500)


# ── helpers ─────────────────────────────────────────────────────────────────
def _result(case: dict, state: dict, trace_url: str | None, case_id: str) -> dict:
    reasons = _escalation_reasons(state) if state["route"] == "needs_human" else []
    public_case = {k: case.get(k) for k in ("requested_procedure", "plan", "condition", "age", "sex", "notes")}
    return {
        "id": case_id,
        "case": public_case,
        "decision": state["decision"],
        "confidence": state["confidence"],
        "route": state["route"],
        "attempt": state.get("attempt", 1),
        "proposal": {k: v for k, v in state["proposal"].items() if not k.startswith("_")},
        "critique": {k: v for k, v in state["critique"].items() if not k.startswith("_")},
        "trace": state["trace"],
        "usage": state.get("usage"),
        "escalation_reasons": reasons,
        "trace_url": trace_url,
        "mode": llm.mode(),
    }


def _run(case: dict, case_id: str, notes: str = "", attempt: int = 1) -> dict:
    state = run_case(case, reviewer_notes=notes, attempt=attempt)
    trace_url = tracing.trace_run({"id": case_id, **case}, state)
    return _result(case, state, trace_url, case_id)


def _stored_case_view(row: dict, principal: Principal) -> dict:
    blob = row.get("agent_result") or {}
    return {
        "id": row["id"],
        "status": row["status"],
        "case": {k: row[k] for k in ("requested_procedure", "plan", "condition", "age", "sex", "notes")},
        "decision": row["agent_decision"],
        "confidence": row["agent_confidence"],
        "route": row["agent_route"],
        "attempt": row["attempt"],
        "proposal": blob.get("proposal") or {},
        "critique": blob.get("critique") or {},
        "trace": blob.get("trace") or [],
        "usage": blob.get("usage"),
        "escalation_reasons": blob.get("escalation_reasons") or [],
        "trace_url": None,
        "mode": blob.get("mode"),
        "assigned_to_me": row.get("assigned_reviewer_id") == principal.user_id,
        "mine": row.get("submitter_id") == principal.user_id,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


class DemoRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(max_length=16, pattern=r"^PA-\d{4}$")
    notes: str = Field(default="", max_length=1000)
    attempt: int = Field(default=1, ge=1, le=10)


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


def _same_origin_guard(request: Request) -> None:
    """CSRF defence in depth on top of SameSite=Strict: a state-changing request that
    carries an Origin header must come from one of our own hosts."""
    origin = request.headers.get("origin")
    if origin:
        host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        allowed = settings().allowed_hosts
        if "*" not in allowed and host not in allowed:
            audit.record("csrf_blocked", None, ip=client_ip(request), outcome="denied",
                         meta={"path": request.url.path})
            raise HTTPException(403, "Cross-site request blocked.")


# ── routes ──────────────────────────────────────────────────────────────────
def _register_routes(app: FastAPI, limiter) -> None:
    s = settings()
    llm_limit = limiter.limit(s.rate_limit_llm)
    auth_limit = limiter.limit(s.rate_limit_auth)
    default_limit = limiter.limit(s.rate_limit_default)

    @app.get("/", response_class=HTMLResponse)
    @default_limit
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {
            "mode": llm.mode(),
            "langfuse": tracing.configured(),
            "auth_enabled": settings().auth_configured,
            "metrics": METRICS,
        })

    @app.get("/health")
    def health():
        db_state = "unconfigured" if not db.available() else ("ok" if db.ping() else "down")
        return {"ok": db_state != "down", "mode": llm.mode(), "langfuse": tracing.configured(),
                "auth": settings().auth_configured, "db": db_state}

    @app.get("/metrics")
    @default_limit
    def metrics(request: Request):
        if METRICS is None:
            return JSONResponse({"error": "metrics not computed"}, status_code=404)
        return METRICS

    @app.get("/samples")
    @default_limit
    def samples(request: Request):
        return SAMPLES

    # ── auth ────────────────────────────────────────────────────────────────
    @app.post("/auth/login", dependencies=[Depends(_same_origin_guard)])
    @auth_limit
    def login(request: Request, body: LoginRequest):
        if not settings().auth_configured:
            raise HTTPException(503, "Sign-in is not enabled on this deployment.")
        ip = client_ip(request)
        try:
            tokens = auth.password_login(body.email, body.password)
            principal = auth.principal_from_token(tokens["access_token"])
        except auth.AuthError:
            audit.record("login_failure", None, ip=ip, outcome="denied",
                         meta={"user_hash": audit.user_hash(body.email)})
            raise HTTPException(401, "Invalid email or password.")
        audit.record("login_success", principal, ip=ip, meta={"role": principal.role})
        resp = JSONResponse({"user_id": principal.user_id, "role": principal.role})
        auth.set_session_cookies(resp, tokens)
        return resp

    @app.post("/auth/refresh", dependencies=[Depends(_same_origin_guard)])
    @auth_limit
    def refresh(request: Request):
        token = request.cookies.get(auth.REFRESH_COOKIE)
        if not token or not settings().auth_configured:
            raise HTTPException(401, "Please sign in again.")
        try:
            tokens = auth.refresh(token)
            principal = auth.principal_from_token(tokens["access_token"])
        except auth.AuthError:
            audit.record("auth_failure", None, ip=client_ip(request), outcome="denied",
                         meta={"reason": "refresh"})
            resp = JSONResponse({"error": "Please sign in again."}, status_code=401)
            auth.clear_session_cookies(resp)
            return resp
        resp = JSONResponse({"user_id": principal.user_id, "role": principal.role})
        auth.set_session_cookies(resp, tokens)
        return resp

    @app.post("/auth/logout", dependencies=[Depends(_same_origin_guard)])
    def logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE)
        if token and settings().auth_configured:
            auth.logout(token)
        audit.record("logout", None, ip=client_ip(request), persist=False)
        resp = JSONResponse({"ok": True})
        auth.clear_session_cookies(resp)
        return resp

    @app.get("/auth/me")
    def me(principal: Principal = Depends(current_principal)):
        return {"authenticated": principal.authenticated, "user_id": principal.user_id,
                "role": principal.role, "auth_enabled": settings().auth_configured}

    # ── anonymous demo: sample cases only, nothing stored ───────────────────
    @app.post("/demo/run", dependencies=[Depends(_same_origin_guard)])
    @llm_limit
    def demo_run(request: Request, body: DemoRunRequest):
        sample = SAMPLES_BY_ID.get(body.sample_id)
        if sample is None:
            raise HTTPException(404, "Unknown sample case.")
        attempt = min(body.attempt, s.max_send_back_attempts + 1)
        result = _run(dict(sample), sample["id"], notes=body.notes, attempt=attempt)
        result["demo"] = True
        return result

    # ── stored cases ────────────────────────────────────────────────────────
    @app.post("/submit", dependencies=[Depends(_same_origin_guard)])
    @llm_limit
    def submit(request: Request, body: SubmitRequest,
               principal: Principal = Depends(require("submitter", "admin"))):
        case = body.case.model_dump()
        result = _run(case, "PA-NEW")
        row = db.create_case(principal, case, result)
        audit.record("case_submitted", principal, target=row["id"], ip=client_ip(request),
                     meta={"route": result["route"], "decision": result["decision"]})
        return _stored_case_view(row, principal)

    @app.get("/cases")
    @default_limit
    def cases(request: Request, principal: Principal = Depends(require("submitter", "reviewer", "admin"))):
        return [_stored_case_view(r, principal) for r in db.list_cases(principal)]

    @app.get("/cases/{case_id}")
    @default_limit
    def case_detail(request: Request, case_id: str,
                    principal: Principal = Depends(require("submitter", "reviewer", "admin"))):
        if not _valid_case_id(case_id):
            raise HTTPException(404, "Case not found.")
        return _stored_case_view(db.get_case(principal, case_id), principal)

    @app.post("/review", dependencies=[Depends(_same_origin_guard)])
    @llm_limit
    def review(request: Request, body: ReviewRequest,
               principal: Principal = Depends(require("reviewer"))):
        if not body.id or not _valid_case_id(body.id):
            raise HTTPException(422, "A stored case id is required.")
        row = db.get_case(principal, body.id)              # RLS: only cases assigned to me
        if body.action == "send_back":
            if row["status"] != "pending_review":
                raise HTTPException(409, "Only cases pending review can be sent back.")
            if row["attempt"] > s.max_send_back_attempts:
                raise HTTPException(409, "This case has been sent back the maximum number of times.")
            case = {k: row[k] for k in ("requested_procedure", "plan", "condition", "age", "sex", "notes")}
            notes = body.notes or "Reviewer requested another look."
            result = _run(case, row["id"], notes=notes, attempt=row["attempt"] + 1)
            updated = db.update_agent_result(principal, row["id"], result)
            audit.record("case_sent_back", principal, target=row["id"], ip=client_ip(request),
                         meta={"attempt": result["attempt"], "route": result["route"]})
            view = _stored_case_view(updated, principal)
            view["self_corrected"] = True
            return view
        finalized = db.finalize_case(principal, row["id"], body.action, body.notes)
        audit.record("case_finalized", principal, target=row["id"], ip=client_ip(request),
                     meta={"action": body.action})
        return {"finalized": body.action, "id": finalized["id"], "status": finalized["status"],
                "by": principal.user_id, "role": principal.role}

    # ── admin ───────────────────────────────────────────────────────────────
    @app.post("/admin/users/{user_id}/role", dependencies=[Depends(_same_origin_guard)])
    @default_limit
    def change_role(request: Request, user_id: str, body: RoleChangeRequest,
                    principal: Principal = Depends(require("admin"))):
        if not _valid_uuid(user_id):
            raise HTTPException(404, "User not found.")
        db.set_role(principal, user_id, body.role)
        audit.record("role_changed", principal, target=user_id, ip=client_ip(request),
                     meta={"target_role": body.role})
        return {"user_id": user_id, "role": body.role}

    @app.post("/admin/cases/{case_id}/assign", dependencies=[Depends(_same_origin_guard)])
    @default_limit
    def assign(request: Request, case_id: str, body: AssignRequest,
               principal: Principal = Depends(require("admin"))):
        if not _valid_case_id(case_id):
            raise HTTPException(404, "Case not found.")
        row = db.assign_case(principal, case_id, body.reviewer_id)
        audit.record("case_assigned", principal, target=case_id, ip=client_ip(request))
        return _stored_case_view(row, principal)

    @app.get("/admin/audit")
    @default_limit
    def audit_log(request: Request, principal: Principal = Depends(require("admin"))):
        rows = db.list_audit(principal)
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]


def _valid_case_id(case_id: str) -> bool:
    import re
    return bool(re.fullmatch(r"PA-[0-9A-F]{8}", case_id or ""))


def _valid_uuid(value: str) -> bool:
    import uuid
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


app = create_app()
