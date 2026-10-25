"""Shared fixtures.

Tests run against a real Postgres (local Homebrew/Docker, or the CI service container),
with the real migrations applied, and connect as the real least-privilege role. Nothing
about authorization or RLS is mocked. Only the two external services are faked: Supabase
Auth (tokens are signed locally with a test HS256 secret, exactly as Supabase's legacy
JWT secret works) and, where a test says so, the OpenAI client.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import jwt
import psycopg
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://localhost/clearpath_test")
APP_PASSWORD = os.getenv("TEST_APP_DB_PASSWORD", "localtestpw")
JWT_SECRET = "test-only-jwt-secret-not-used-anywhere-real-0123456789"
SUPABASE_URL = "https://test-project.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"


def _app_url() -> str:
    info = psycopg.conninfo.conninfo_to_dict(ADMIN_URL)
    info.update(user="clearpath_app", password=APP_PASSWORD)
    return psycopg.conninfo.make_conninfo(**info)


BASE_ENV = {
    "CP_ENV": "development",
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_ANON_KEY": "test-anon-key",
    "SUPABASE_JWT_SECRET": JWT_SECRET,
    "DATABASE_URL": _app_url(),
    "CP_RATE_LIMIT_LLM": "1000/minute",
    "CP_RATE_LIMIT_AUTH": "1000/minute",
    "CP_RATE_LIMIT_DEFAULT": "1000/minute",
    "CP_DAILY_LLM_CALL_CAP": "100000",
}
for k, v in BASE_ENV.items():
    os.environ[k] = v
for k in ("OPENAI_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "K_SERVICE", "TRUST_PROXY",
          "MCP_ALLOWED_HOSTS", "ALLOWED_HOSTS"):
    os.environ.pop(k, None)


class Users:
    submitter_a = str(uuid.UUID(int=0xA1))
    submitter_b = str(uuid.UUID(int=0xB1))
    reviewer_1 = str(uuid.UUID(int=0xC1))
    reviewer_2 = str(uuid.UUID(int=0xC2))
    admin = str(uuid.UUID(int=0xD1))
    roles = {submitter_a: "submitter", submitter_b: "submitter", reviewer_1: "reviewer",
             reviewer_2: "reviewer", admin: "admin"}


def make_token(sub: str, *, email: str | None = None, exp_in: int = 3600, aud: str = "authenticated",
               iss: str = ISSUER, role: str = "authenticated", secret: str = JWT_SECRET,
               extra: dict | None = None) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "iss": iss, "role": role, "iat": now, "exp": now + exp_in,
              "email": email or f"{sub[-4:]}@example.test", **(extra or {})}
    return jwt.encode(claims, secret, algorithm="HS256")


def bearer(sub: str) -> dict:
    return {"Authorization": f"Bearer {make_token(sub)}"}


@pytest.fixture(scope="session", autouse=True)
def database():
    """Apply the migrations once and give the app role its test password."""
    # Mimic Supabase: the Data API roles exist and get default grants on new tables.
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role} nologin")
        conn.execute("alter default privileges grant all on tables to anon, authenticated")
    os.environ["MIGRATE_DATABASE_URL"] = ADMIN_URL
    os.environ["APP_DB_PASSWORD"] = APP_PASSWORD
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import migrate
    assert migrate.main() == 0
    yield


@pytest.fixture()
def admin_db():
    """Owner/superuser connection, for arranging fixtures and for proving owner-level guards."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(autouse=True)
def clean_state(admin_db):
    """Empty every table (the audit log's own guard has to be lifted for this, which only the
    owner can do) and seed the five test users."""
    c = admin_db
    c.execute("alter table clearpath.audit_log disable trigger user")
    c.execute("alter table clearpath.decisions disable trigger user")
    c.execute("truncate clearpath.decisions, clearpath.cases, clearpath.audit_log, "
              "clearpath.user_roles, clearpath.llm_usage")
    c.execute("alter table clearpath.audit_log enable trigger user")
    c.execute("alter table clearpath.decisions enable trigger user")
    for uid, role in Users.roles.items():
        c.execute("insert into clearpath.user_roles (user_id, role) values (%s, %s)", (uid, role))
    from app import llm, security
    security.reset_limits()
    llm.reset_budget()
    yield


@pytest.fixture()
def make_app(monkeypatch):
    """Build a fresh app with extra env vars applied (settings are read at build time)."""
    from fastapi.testclient import TestClient

    clients = []

    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        from app import config, db, graph, llm, security
        config.reset()
        db.close()
        graph._kb = None
        llm._client = None
        security.reset_limits()
        import importlib
        import app.auth
        app.auth._jwks_client = None
        from app.main import create_app
        client = TestClient(create_app(), base_url="http://testserver")
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)
    from app import config, db
    config.reset()
    db.close()


@pytest.fixture()
def client(make_app):
    return make_app()


def stored_case(admin_db, *, submitter=Users.submitter_a, reviewer=Users.reviewer_1,
                status="pending_review", case_id=None) -> str:
    """Insert a case directly as the owner (arrange step), assigned to `reviewer`."""
    case_id = case_id or "PA-" + uuid.uuid4().hex[:8].upper()
    admin_db.execute(
        """insert into clearpath.cases (id, submitter_id, assigned_reviewer_id, status, requested_procedure,
               plan, condition, age, sex, notes, agent_decision, agent_confidence, agent_route)
           values (%s, %s, %s, %s, 'MRI Lumbar Spine', 'Commercial', 'Low back pain', 40, 'F',
                   'Two weeks of pain, no therapy yet.', 'deny', 0.9, 'needs_human')""",
        (case_id, submitter, reviewer, status))
    return case_id


def mcp_call(client, tool: str, arguments: dict, headers: dict | None = None) -> dict:
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}},
                    headers={"accept": "application/json, text/event-stream", **(headers or {})})
    assert r.status_code == 200, r.text
    return r.json()["result"]


class FakeOpenAI:
    """Stands in for the OpenAI client. `chat_replies` is a function (system, user) -> dict
    so a test can play an LLM that obeys an injection, returns garbage, or fails."""

    def __init__(self, chat_replies=None, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self._reply = chat_replies or (lambda s, u: {})
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.fail:
                    raise ConnectionError("provider down")
                msgs = kw["messages"]
                data = outer._reply(msgs[0]["content"], msgs[1]["content"])
                content = data if isinstance(data, str) else __import__("json").dumps(data)
                return type("R", (), {
                    "choices": [type("C", (), {"message": type("M", (), {"content": content})()})()],
                    "usage": type("U", (), {"prompt_tokens": 100, "completion_tokens": 50})(),
                })()

        class _Embeddings:
            def create(self, model, input):
                if outer.fail:
                    raise ConnectionError("provider down")
                from app.llm import _mock_embed
                return type("E", (), {"data": [type("D", (), {"embedding": _mock_embed(t)})() for t in input]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()
        self.embeddings = _Embeddings()


@pytest.fixture()
def fake_llm(monkeypatch):
    """Switch the app to live-LLM mode with a fake client. Returns a setter."""
    from app import graph, llm
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    def _install(**kw) -> FakeOpenAI:
        fake = FakeOpenAI(**kw)
        monkeypatch.setattr(llm, "_get_client", lambda: fake)
        graph._kb = None
        return fake

    yield _install
    graph._kb = None


APPROVE = {"decision": "approve", "rationale": "Criteria met.", "cited_criteria": ["MRI-LSPINE-001"],
           "met_requirements": ["6 weeks PT"], "unmet_requirements": [], "confidence": 0.97}
AGREE = {"agree": True, "confidence": 0.97, "gaps": [], "red_flag_detected": False,
         "recommend_escalate": False, "concern": ""}


def obedient_llm(system: str, user: str) -> dict:
    """An LLM that always approves with full confidence: the worst case for injection."""
    return AGREE if "auditor" in system else APPROVE
