"""Input validation (A05) and output encoding (LLM05)."""

import os
import re

import pytest

from tests.conftest import ROOT, Users, bearer

GOOD = {"requested_procedure": "MRI Lumbar Spine", "plan": "Commercial", "condition": "Low back pain",
        "age": 52, "sex": "F", "notes": "Eight weeks of pain; six weeks of PT and NSAIDs failed."}


def submit(client, case, uid=Users.submitter_a):
    return client.post("/submit", json={"case": case}, headers=bearer(uid))


@pytest.mark.parametrize("field,value", [
    ("plan", "Nope"),
    ("sex", "zz"),
    ("age", "abc"),
    ("age", 400),
    ("notes", "x" * 4001),
    ("notes", "hi"),
    ("requested_procedure", "y" * 121),
    ("condition", ""),
])
def test_bad_case_fields_are_rejected(client, field, value):
    r = submit(client, {**GOOD, field: value})
    assert r.status_code == 422
    assert field in r.json()["fields"][0]


def test_unknown_top_level_fields_are_rejected(client):
    r = client.post("/submit", json={"case": GOOD, "route": "auto_approved"}, headers=bearer(Users.submitter_a))
    assert r.status_code == 422


def test_client_cannot_steer_the_offline_planner_with_expected(client):
    """Pre-fix, `"expected": "approve"` in the JSON body made the mock planner approve
    (F-25). The field is now dropped by the schema."""
    weak = {**GOOD, "notes": "Two weeks of pain. No physical therapy yet.", "expected": "approve"}
    r = submit(client, weak)
    assert r.status_code == 200
    assert r.json()["route"] == "needs_human"


def test_malformed_json_is_a_clean_4xx(client):
    r = client.post("/submit", content=b"not json", headers={**bearer(Users.submitter_a),
                                                          "content-type": "application/json"})
    assert r.status_code == 422
    assert "Traceback" not in r.text


def test_non_object_case_is_a_clean_4xx(client):
    r = client.post("/submit", json={"case": "string"}, headers=bearer(Users.submitter_a))
    assert r.status_code == 422


def test_body_size_limit(client):
    big = {"case": {**GOOD, "notes": "A" * 200_000}}
    r = client.post("/submit", json=big, headers=bearer(Users.submitter_a))
    assert r.status_code == 413


def test_body_size_limit_without_content_length(client):
    def gen():
        for _ in range(100):
            yield b"A" * 1024
    r = client.post("/demo/run", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_demo_only_accepts_sample_ids(client):
    assert client.post("/demo/run", json={"sample_id": "PA-9999"}).status_code == 404
    assert client.post("/demo/run", json={"sample_id": "../../etc"}).status_code == 422
    r = client.post("/demo/run", json={"sample_id": "PA-1001", "case": GOOD})
    assert r.status_code == 422


def test_send_back_attempts_are_capped(client, admin_db):
    from tests.conftest import stored_case
    case_id = stored_case(admin_db)
    h = bearer(Users.reviewer_1)
    codes = [client.post("/review", json={"action": "send_back", "id": case_id, "notes": "again"},
                         headers=h).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [409, 409]


def test_bad_case_id_shapes_are_404_not_500(client):
    h = bearer(Users.admin)
    assert client.get("/cases/'; drop table x;--", headers=h).status_code == 404
    assert client.post("/admin/users/not-a-uuid/role", json={"role": "admin"}, headers=h).status_code == 404


# ── output encoding ─────────────────────────────────────────────────────────
def test_jinja_autoescape_is_on():
    from app.main import templates
    assert templates.env.autoescape is not False
    out = templates.env.from_string("{{ x }}").render(x="<script>alert(1)</script>")
    assert "<script>" not in out


def test_page_has_no_inline_script_or_handlers(client):
    html = client.get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> found"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler found"
    assert "style=" not in html


def test_dashboard_js_escapes_every_interpolation():
    js = open(os.path.join(ROOT, "static", "app.js")).read()
    # Every ${...} or string concatenation of server data goes through esc(); there are no template
    # literals with raw interpolation and no inline handlers built into markup.
    assert "${" not in js
    assert not re.search(r"on(click|load|error|mouseover)\s*=", js)
    assert "function esc(" in js


def test_xss_payload_in_case_is_returned_as_data_not_markup(client):
    payload = "<img src=x onerror=alert(1)>"
    r = submit(client, {**GOOD, "notes": f"Six weeks of PT failed. {payload}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "script-src 'self'" in r.headers["content-security-policy"]
