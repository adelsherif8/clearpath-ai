"""Authorization matrix (A01): every endpoint and MCP tool × every role → allow or deny.

Each row is checked against the running app with a real database; a 404 for a
case the caller is not allowed to see is the expected *deny* (RLS makes an invisible
row indistinguishable from a nonexistent one, so ids can't be probed)."""

import pytest

from tests.conftest import Users, bearer, mcp_call, stored_case

ROLES = {
    "anonymous": None,
    "submitter": Users.submitter_a,        # owns the case under test
    "other_submitter": Users.submitter_b,
    "reviewer": Users.reviewer_1,          # assigned to the case under test
    "other_reviewer": Users.reviewer_2,
    "admin": Users.admin,
}

NEW_CASE = {"case": {"requested_procedure": "MRI Lumbar Spine", "plan": "Commercial",
                     "condition": "Low back pain, 8 weeks", "age": 52, "sex": "F",
                     "notes": "8 weeks of pain. Completed 6 weeks of PT and NSAIDs with no improvement."}}

#            name               method path                                    body                                   expected per role
#                                                                                                                     anon sub  osub rev  orev adm
MATRIX = [
    ("index",              "GET",  "/",                                    None,                                     (200, 200, 200, 200, 200, 200)),
    ("health",             "GET",  "/health",                              None,                                     (200, 200, 200, 200, 200, 200)),
    ("samples",            "GET",  "/samples",                             None,                                     (200, 200, 200, 200, 200, 200)),
    ("metrics",            "GET",  "/metrics",                             None,                                     (200, 200, 200, 200, 200, 200)),
    ("me",                 "GET",  "/auth/me",                             None,                                     (200, 200, 200, 200, 200, 200)),
    ("demo_run",           "POST", "/demo/run",                            {"sample_id": "PA-1001"},                 (200, 200, 200, 200, 200, 200)),
    ("submit",             "POST", "/submit",                              NEW_CASE,                                 (401, 200, 200, 403, 403, 200)),
    ("list_cases",         "GET",  "/cases",                               None,                                     (401, 200, 200, 200, 200, 200)),
    ("get_case",           "GET",  "/cases/{case}",                        None,                                     (401, 200, 404, 200, 404, 200)),
    ("approve",            "POST", "/review",                              {"action": "approve", "id": "{case}"},    (401, 403, 403, 200, 404, 403)),
    ("deny",               "POST", "/review",                              {"action": "deny", "id": "{case}"},       (401, 403, 403, 200, 404, 403)),
    ("send_back",          "POST", "/review",                              {"action": "send_back", "id": "{case}", "notes": "re-check"},
                                                                                                                     (401, 403, 403, 200, 404, 403)),
    ("change_role",        "POST", f"/admin/users/{Users.submitter_b}/role", {"role": "reviewer"},                   (401, 403, 403, 403, 403, 200)),
    ("assign",             "POST", "/admin/cases/{case}/assign",           {"reviewer_id": Users.reviewer_2},        (401, 403, 403, 403, 403, 200)),
    ("audit_log",          "GET",  "/admin/audit",                         None,                                     (401, 403, 403, 403, 403, 200)),
]
ROLE_ORDER = ["anonymous", "submitter", "other_submitter", "reviewer", "other_reviewer", "admin"]


def _sub(value, case_id):
    if isinstance(value, str):
        return value.replace("{case}", case_id)
    if isinstance(value, dict):
        return {k: _sub(v, case_id) for k, v in value.items()}
    return value


@pytest.mark.parametrize("role", ROLE_ORDER)
@pytest.mark.parametrize("name,method,path,body,expected", MATRIX, ids=[m[0] for m in MATRIX])
def test_http_matrix(client, admin_db, role, name, method, path, body, expected):
    case_id = stored_case(admin_db)
    uid = ROLES[role]
    headers = bearer(uid) if uid else {}
    url = _sub(path, case_id)
    r = client.request(method, url, json=_sub(body, case_id), headers=headers)
    want = expected[ROLE_ORDER.index(role)]
    assert r.status_code == want, f"{name} as {role}: got {r.status_code} {r.text[:200]}"


def test_finalized_decision_is_recorded_with_the_real_reviewer(client, admin_db):
    case_id = stored_case(admin_db)
    r = client.post("/review", json={"action": "deny", "id": case_id, "notes": "insufficient therapy"},
                    headers=bearer(Users.reviewer_1))
    assert r.status_code == 200
    row = admin_db.execute("select reviewer_id::text, action from clearpath.decisions where case_id=%s",
                           (case_id,)).fetchone()
    assert row == (Users.reviewer_1, "deny")
    status = admin_db.execute("select status from clearpath.cases where id=%s", (case_id,)).fetchone()[0]
    assert status == "denied"


def test_case_cannot_be_finalized_twice(client, admin_db):
    case_id = stored_case(admin_db)
    h = bearer(Users.reviewer_1)
    assert client.post("/review", json={"action": "approve", "id": case_id}, headers=h).status_code == 200
    assert client.post("/review", json={"action": "deny", "id": case_id}, headers=h).status_code == 403


def test_auto_approved_case_cannot_be_finalized_by_reviewer(client, admin_db):
    case_id = stored_case(admin_db, status="auto_approved")
    r = client.post("/review", json={"action": "deny", "id": case_id}, headers=bearer(Users.reviewer_1))
    assert r.status_code == 403


def test_list_cases_is_scoped_by_the_database(client, admin_db):
    mine = stored_case(admin_db, submitter=Users.submitter_a, reviewer=Users.reviewer_1)
    theirs = stored_case(admin_db, submitter=Users.submitter_b, reviewer=Users.reviewer_2)
    ids = lambda uid: {c["id"] for c in client.get("/cases", headers=bearer(uid)).json()}
    assert ids(Users.submitter_a) == {mine}
    assert ids(Users.submitter_b) == {theirs}
    assert ids(Users.reviewer_1) == {mine}
    assert ids(Users.reviewer_2) == {theirs}
    assert ids(Users.admin) == {mine, theirs}


def test_anonymous_can_still_run_every_sample(client):
    for sid in [c["id"] for c in client.get("/samples").json()]:
        r = client.post("/demo/run", json={"sample_id": sid})
        assert r.status_code == 200 and r.json()["demo"] is True


# ── MCP tools × roles ────────────────────────────────────────────────────────
MCP_MATRIX = [
    #  tool                 arguments                                                         anon  sub   rev   adm
    ("list_sample_cases", {},                                                                 (True, True, True, True)),
    ("search_criteria",   {"procedure": "MRI Lumbar Spine", "condition": "back pain"},        (True, True, True, True)),
    ("get_eval_metrics",  {},                                                                 (True, True, True, True)),
    ("review_case",       {"case_id": "PA-1001"},                                             (True, True, True, True)),
    ("review_case",       {"procedure": "MRI Lumbar Spine", "condition": "back pain",
                           "notes": "Six weeks of PT and NSAIDs failed."},                    (False, True, True, True)),
]
MCP_ROLES = ["anonymous", "submitter", "reviewer", "admin"]


@pytest.mark.parametrize("role", MCP_ROLES)
@pytest.mark.parametrize("tool,args,expected", MCP_MATRIX,
                         ids=["list", "search", "metrics", "review_sample", "review_custom"])
def test_mcp_matrix(client, role, tool, args, expected):
    uid = ROLES[role]
    result = mcp_call(client, tool, args, headers=bearer(uid) if uid else None)
    allowed = expected[MCP_ROLES.index(role)]
    assert result.get("isError", False) is (not allowed), result


def test_mcp_has_no_tool_that_records_a_decision(client):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"accept": "application/json, text/event-stream"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"list_sample_cases", "search_criteria", "review_case", "get_eval_metrics"}


def test_mcp_rejects_invalid_token(client):
    result = mcp_call(client, "review_case",
                      {"procedure": "MRI Lumbar Spine", "condition": "back pain", "notes": "PT and NSAIDs failed."},
                      headers={"Authorization": "Bearer not-a-jwt"})
    assert result["isError"] is True
    assert "token" in result["content"][0]["text"]
