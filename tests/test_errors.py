"""Mishandling of exceptional conditions (A10): generic errors, no stack traces, fail closed."""

from tests.conftest import Users, bearer, obedient_llm

CASE = {"case": {"requested_procedure": "MRI Lumbar Spine", "plan": "Commercial", "condition": "back pain",
                 "sex": "F", "notes": "Six weeks of PT and NSAIDs failed."}}


def test_unhandled_exception_is_generic(client, monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    client = TestClient(main.create_app(), raise_server_exceptions=False)

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /srv/app/graph.py line 42")
    monkeypatch.setattr(main, "run_case", boom)
    r = client.post("/demo/run", json={"sample_id": "PA-1001"})
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "Something went wrong. Nothing was decided."
    assert len(body["id"]) == 12
    assert "secret internal detail" not in r.text and "Traceback" not in r.text


def test_llm_down_fails_closed_and_stores_nothing(make_app, fake_llm, admin_db):
    fake_llm(fail=True)
    c = make_app()
    r = c.post("/submit", json=CASE, headers=bearer(Users.submitter_a))
    assert r.status_code == 503
    assert "Nothing was decided" in r.json()["error"]
    assert admin_db.execute("select count(*) from clearpath.cases").fetchone()[0] == 0


def test_llm_down_during_demo_is_503_not_an_approval(make_app, fake_llm):
    fake_llm(fail=True)
    c = make_app()
    r = c.post("/demo/run", json={"sample_id": "PA-1001"})
    assert r.status_code == 503
    assert "approve" not in r.json().get("decision", "")


def test_database_down_fails_closed(make_app):
    c = make_app(DATABASE_URL="postgresql://clearpath_app:x@127.0.0.1:1/nowhere")
    r = c.post("/submit", json=CASE, headers=bearer(Users.submitter_a))
    assert r.status_code == 503
    assert "Nothing was saved or decided" in r.json()["error"]
    r = c.post("/review", json={"action": "approve", "id": "PA-0000ABCD"}, headers=bearer(Users.reviewer_1))
    assert r.status_code == 503
    assert c.get("/health").json()["db"] == "down"


def test_demo_keeps_working_without_a_database(make_app):
    c = make_app(DATABASE_URL="", SUPABASE_URL="", SUPABASE_ANON_KEY="")
    assert c.post("/demo/run", json={"sample_id": "PA-1002"}).status_code == 200
    assert c.get("/health").json() == {"ok": True, "mode": "mock", "langfuse": False, "auth": False,
                                       "db": "unconfigured"}
    # ...and nothing privileged becomes reachable just because auth is off
    assert c.post("/review", json={"action": "approve", "id": "PA-0000ABCD"}).status_code == 401
    assert c.post("/auth/login", json={"email": "a@b.co", "password": "whatever123"}).status_code == 503


def test_unknown_route_and_method_are_clean(client):
    assert client.get("/nope").json() == {"error": "Not Found"}
    r = client.put("/submit")
    assert r.status_code == 405 and "Traceback" not in r.text
