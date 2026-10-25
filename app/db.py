"""Postgres access as the least-privilege `clearpath_app` role.

Every call runs in its own transaction that first sets `app.user_id` (transaction-local)
from the verified JWT. Row-level security in the database does the filtering; this module
never adds "WHERE user_id = ..." as its only line of defence. All SQL is parameterized.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.config import settings


class DatabaseUnavailable(Exception):
    """DB not configured or not reachable. Callers fail closed (HTTP 503)."""


class Forbidden(Exception):
    """The database refused the operation (RLS policy, missing privilege or guard trigger)."""


class NotFound(Exception):
    pass


_pool = None


def available() -> bool:
    return settings().db_configured


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            settings().database_url, min_size=0, max_size=4, timeout=5, open=True,
            # prepare_threshold=None: compatible with the Supabase transaction pooler.
            kwargs={"prepare_threshold": None, "connect_timeout": 5,
                    "options": "-c statement_timeout=5000"},
        )
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def session(principal=None) -> Iterator[psycopg.Connection]:
    if not available():
        raise DatabaseUnavailable("database not configured")
    uid = getattr(principal, "user_id", None) or ""
    try:
        with _get_pool().connection() as conn:
            conn.row_factory = dict_row
            with conn.transaction():
                conn.execute("select set_config('app.user_id', %s, true)", (uid,))
                yield conn
    except psycopg.errors.InsufficientPrivilege as exc:
        raise Forbidden(str(exc).splitlines()[0]) from exc
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise DatabaseUnavailable(type(exc).__name__) from exc
    except Exception as exc:
        from psycopg_pool import PoolTimeout
        if isinstance(exc, PoolTimeout):
            raise DatabaseUnavailable("pool timeout") from exc
        raise


def ping() -> bool:
    try:
        with session() as conn:
            conn.execute("select 1")
        return True
    except DatabaseUnavailable:
        return False


# ── users and roles ─────────────────────────────────────────────────────────
def ensure_user(principal) -> str:
    """Return the caller's role, registering them as a submitter on first sight."""
    with session(principal) as conn:
        conn.execute("insert into clearpath.user_roles (user_id) values (%s) on conflict (user_id) do nothing",
                     (principal.user_id,))
        row = conn.execute("select clearpath.app_role() as role").fetchone()
    return row["role"]


def set_role(principal, target_user_id: str, role: str) -> None:
    with session(principal) as conn:
        cur = conn.execute("update clearpath.user_roles set role = %s, updated_at = now() where user_id = %s",
                           (role, target_user_id))
        if cur.rowcount == 0:
            raise NotFound(target_user_id)


# ── cases ───────────────────────────────────────────────────────────────────
# Composed with psycopg.sql (identifiers/literals are never string-formatted into queries).
_CASE_COLS = sql.SQL(
    "id, submitter_id::text, assigned_reviewer_id::text, status, requested_procedure, plan, "
    "condition, age, sex, notes, agent_decision, agent_confidence, agent_route, agent_result, "
    "attempt, created_at, updated_at")


def _q(template: str) -> sql.Composed:
    return sql.SQL(template).format(cols=_CASE_COLS)


def new_case_id() -> str:
    return "PA-" + secrets.token_hex(4).upper()


def create_case(principal, case: dict, result: dict) -> dict:
    status = "auto_approved" if result["route"] == "auto_approved" else "pending_review"
    case_id = new_case_id()
    with session(principal) as conn:
        row = conn.execute(
            _q("""insert into clearpath.cases
                  (id, submitter_id, status, requested_procedure, plan, condition, age, sex, notes,
                   agent_decision, agent_confidence, agent_route, agent_result, attempt)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning {cols}"""),
            (case_id, principal.user_id, status, case["requested_procedure"], case["plan"],
             case["condition"], case.get("age"), case["sex"], case["notes"],
             result["decision"], result["confidence"], result["route"],
             json.dumps(_agent_blob(result)), result.get("attempt", 1)),
        ).fetchone()
    return row


def list_cases(principal, limit: int = 50) -> list[dict]:
    with session(principal) as conn:
        return conn.execute(
            _q("select {cols} from clearpath.cases order by created_at desc limit %s"), (limit,)
        ).fetchall()


def get_case(principal, case_id: str) -> dict:
    with session(principal) as conn:
        row = conn.execute(_q("select {cols} from clearpath.cases where id = %s"), (case_id,)).fetchone()
    if row is None:
        raise NotFound(case_id)   # invisible under RLS looks exactly like nonexistent
    return row


def update_agent_result(principal, case_id: str, result: dict) -> dict:
    with session(principal) as conn:
        row = conn.execute(
            _q("""update clearpath.cases
                   set agent_decision = %s, agent_confidence = %s, agent_route = %s,
                       agent_result = %s, attempt = %s
                 where id = %s
             returning {cols}"""),
            (result["decision"], result["confidence"], result["route"],
             json.dumps(_agent_blob(result)), result.get("attempt", 1), case_id),
        ).fetchone()
    if row is None:
        raise NotFound(case_id)
    return row


def finalize_case(principal, case_id: str, action: str, notes: str) -> dict:
    status = {"approve": "approved", "deny": "denied"}[action]
    with session(principal) as conn:
        row = conn.execute(
            _q("update clearpath.cases set status = %s where id = %s returning {cols}"),
            (status, case_id),
        ).fetchone()
        if row is None:
            raise NotFound(case_id)
        conn.execute("insert into clearpath.decisions (case_id, reviewer_id, action, notes) values (%s, %s, %s, %s)",
                     (case_id, principal.user_id, action, notes))
    return row


def assign_case(principal, case_id: str, reviewer_id: str) -> dict:
    with session(principal) as conn:
        row = conn.execute(
            _q("update clearpath.cases set assigned_reviewer_id = %s where id = %s returning {cols}"),
            (reviewer_id, case_id),
        ).fetchone()
    if row is None:
        raise NotFound(case_id)
    return row


def _agent_blob(result: dict) -> dict:
    keep = ("proposal", "critique", "trace", "usage", "escalation_reasons", "mode")
    return {k: result.get(k) for k in keep}


# ── audit log (append-only) ─────────────────────────────────────────────────
def insert_audit(principal, entry: dict) -> None:
    with session(principal) as conn:
        conn.execute(
            """insert into clearpath.audit_log (ts, event, actor_id, actor_role, ip, target, outcome, meta)
               values (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (entry["ts"], entry["event"], entry["actor_id"], entry["actor_role"], entry["ip"],
             entry["target"], entry["outcome"], json.dumps(entry["meta"])),
        )


def list_audit(principal, limit: int = 100) -> list[dict]:
    with session(principal) as conn:
        return conn.execute(
            """select id, ts, event, actor_id::text, actor_role, ip, target, outcome, meta
                 from clearpath.audit_log order by ts desc, id desc limit %s""", (limit,)
        ).fetchall()


# ── LLM spend cap ───────────────────────────────────────────────────────────
def consume_llm_calls(n: int, cap: int) -> bool:
    """Atomically add `n` to today's counter unless that would exceed `cap`."""
    with session() as conn:
        row = conn.execute(
            """insert into clearpath.llm_usage as u (day, calls) values (current_date, %s)
               on conflict (day) do update set calls = u.calls + excluded.calls
                 where u.calls + excluded.calls <= %s
               returning calls""",
            (n, cap),
        ).fetchone()
    return row is not None and row["calls"] <= cap
