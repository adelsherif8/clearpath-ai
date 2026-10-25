"""Security logging (A09): who/what/when for logins, denials and every decision —
and never any patient details."""

import json

from tests.conftest import Users, bearer, stored_case

MARKER = "ZEBRA-7731-PATIENT-DETAIL"


def _lines(out: str) -> list[dict]:
    return [json.loads(l) for l in out.splitlines() if l.startswith("{") and '"audit":true' in l]


def test_decisions_are_audited_with_actor_and_time(client, admin_db):
    case_id = stored_case(admin_db)
    client.post("/review", json={"action": "approve", "id": case_id}, headers=bearer(Users.reviewer_1))
    row = admin_db.execute("""select event, actor_id::text, actor_role, target, outcome, ts
                                from clearpath.audit_log where event = 'case_finalized'""").fetchone()
    assert row[:5] == ("case_finalized", Users.reviewer_1, "reviewer", case_id, "ok")
    assert row[5] is not None


def test_permission_denials_are_audited(client, admin_db, capsys):
    case_id = stored_case(admin_db)
    client.post("/review", json={"action": "approve", "id": case_id}, headers=bearer(Users.submitter_a))
    client.post("/review", json={"action": "approve", "id": case_id})
    events = [e["event"] for e in _lines(capsys.readouterr().out)]
    assert events.count("permission_denied") == 2
    rows = admin_db.execute("select actor_role from clearpath.audit_log where event='permission_denied'").fetchall()
    assert sorted(r[0] for r in rows) == ["anonymous", "submitter"]


def test_bad_token_is_audited_as_auth_failure(client, capsys):
    client.get("/cases", headers={"Authorization": "Bearer junk"})
    events = _lines(capsys.readouterr().out)
    assert events[-1]["event"] == "auth_failure" and events[-1]["severity"] == "WARNING"


def test_audit_log_has_no_patient_text(client, admin_db, capsys):
    case = {"requested_procedure": "MRI Lumbar Spine", "plan": "Commercial", "condition": f"back pain {MARKER}",
            "sex": "F", "age": 44, "notes": f"Six weeks of PT and NSAIDs failed. {MARKER}"}
    r = client.post("/submit", json={"case": case}, headers=bearer(Users.submitter_a))
    assert r.status_code == 200
    case_id = r.json()["id"]
    client.post("/review", json={"action": "send_back", "id": case_id, "notes": f"check {MARKER}"},
                headers=bearer(Users.reviewer_1))
    out = capsys.readouterr().out
    assert MARKER not in out
    dumped = json.dumps([list(r) for r in admin_db.execute("select * from clearpath.audit_log").fetchall()], default=str)
    assert MARKER not in dumped
    assert "case_submitted" in dumped


def test_audit_meta_is_allowlisted():
    from app import audit
    e = audit.record("x", None, meta={"notes": "secret", "case_id": "PA-1", "condition": "c"}, persist=False)
    assert e["meta"] == {"case_id": "PA-1"}


def test_log_lines_are_single_line_json_even_with_newlines_in_input(client, capsys):
    client.post("/auth/login", json={"email": "a\n{\"event\":\"forged\"}@x.co", "password": "whatever1"})
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("{"):
            json.loads(line)       # every line parses; injected newlines can't forge a record
            assert '"event":"forged"' not in line
