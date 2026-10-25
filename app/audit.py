"""Structured audit logging (A09).

Every security-relevant event is written as one JSON line to stdout (Cloud Logging
parses `severity` and `jsonPayload.*`, which the auth-failure alert is built on) and,
when a database is configured, appended to the `audit_log` table, which the app role
can only INSERT into.

Rule: an audit record carries identifiers, roles, actions and outcomes — never case
text, never notes, never an email address in clear.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_ALLOWED_META = {"case_id", "action", "attempt", "role", "reason", "limit", "path", "method",
                 "status", "tool", "target_role", "error_id", "decision", "route", "mode",
                 "count", "cap", "user_hash"}

class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is *now* (Cloud Run ships stdout to Cloud Logging)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


logger = logging.getLogger("clearpath.audit")
if not logger.handlers:
    _h = _StdoutHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

_SEVERITY = {"denied": "WARNING", "error": "ERROR"}


def user_hash(value: str | None) -> str | None:
    """Stable, non-reversible handle for an identifier we must not log in clear."""
    if not value:
        return None
    return hashlib.sha256(value.lower().encode()).hexdigest()[:12]


def record(event: str, principal, *, target: str | None = None, outcome: str = "ok",
           ip: str | None = None, meta: dict[str, Any] | None = None, persist: bool = True) -> dict:
    meta = {k: v for k, v in (meta or {}).items() if k in _ALLOWED_META}
    entry = {
        "severity": _SEVERITY.get(outcome, "INFO"),
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "audit": True,
        "event": event,
        "actor_id": getattr(principal, "user_id", None),
        "actor_role": getattr(principal, "role", "anonymous") if principal is not None else "anonymous",
        "ip": ip,
        "target": target,
        "outcome": outcome,
        "meta": meta,
    }
    logger.info(json.dumps(entry, separators=(",", ":"), default=str))
    if persist:
        try:
            from app import db
            if db.available():
                db.insert_audit(principal, entry)
        except Exception as exc:  # the audit sink must never break a request
            logger.error(json.dumps({"severity": "ERROR", "audit": True, "event": "audit_persist_failed",
                                     "error": type(exc).__name__}))
    return entry


def new_error_id() -> str:
    return uuid.uuid4().hex[:12]
