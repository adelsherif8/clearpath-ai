"""Daily LLM call cap (LLM10): fails closed with a clear message, and is kept in Postgres."""

from app import llm
from tests.conftest import Users, bearer, obedient_llm


def test_daily_cap_fails_closed_with_clear_message(make_app, fake_llm, admin_db):
    fake = fake_llm(chat_replies=obedient_llm)
    # One review = 1 query embedding + proposer + critic = 3 calls; building the KB = 1 more.
    c = make_app(CP_DAILY_LLM_CALL_CAP="4")
    assert c.post("/demo/run", json={"sample_id": "PA-1001"}).status_code == 200
    r = c.post("/demo/run", json={"sample_id": "PA-1001"})
    assert r.status_code == 429
    assert "daily AI review limit" in r.json()["error"]
    assert "Nothing was decided" in r.json()["error"]
    used = admin_db.execute("select calls from clearpath.llm_usage where day = current_date").fetchone()[0]
    assert used == 4                                  # the refused call was not counted or made
    assert len(fake.calls) == 2


def test_cap_also_covers_submissions_and_nothing_is_stored(make_app, fake_llm, admin_db):
    fake_llm(chat_replies=obedient_llm)
    c = make_app(CP_DAILY_LLM_CALL_CAP="2")
    r = c.post("/submit", json={"case": {"requested_procedure": "MRI Lumbar Spine", "plan": "Commercial",
                                         "condition": "back pain", "sex": "F",
                                         "notes": "Six weeks of PT and NSAIDs failed."}},
               headers=bearer(Users.submitter_a))
    assert r.status_code == 429
    assert admin_db.execute("select count(*) from clearpath.cases").fetchone()[0] == 0


def test_cap_is_shared_across_app_instances_via_database(make_app, fake_llm):
    fake_llm(chat_replies=obedient_llm)
    a = make_app(CP_DAILY_LLM_CALL_CAP="5")
    assert a.post("/demo/run", json={"sample_id": "PA-1001"}).status_code == 200    # 4 calls
    fake_llm(chat_replies=obedient_llm)
    b = make_app(CP_DAILY_LLM_CALL_CAP="5")
    assert b.post("/demo/run", json={"sample_id": "PA-1001"}).status_code == 429    # 4 + 1 (kb) + 1 > 5


def test_budget_store_down_means_no_llm_call(monkeypatch, fake_llm):
    from app import db
    fake = fake_llm(chat_replies=obedient_llm)

    def down(n, cap):
        raise db.DatabaseUnavailable("down")
    monkeypatch.setattr(db, "consume_llm_calls", down)
    import pytest
    with pytest.raises(llm.LLMUnavailable):
        llm.chat_json("s", "u")
    assert fake.calls == []


def test_in_memory_cap_when_no_database(monkeypatch):
    from app import config
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CP_DAILY_LLM_CALL_CAP", "2")
    config.reset()
    llm.reset_budget()
    llm.consume_budget(1)
    llm.consume_budget(1)
    import pytest
    with pytest.raises(llm.BudgetExceeded):
        llm.consume_budget(1)
    config.reset()
