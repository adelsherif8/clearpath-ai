"""Abuse controls (A06 / LLM10): per-IP rate limits that can't be dodged by spoofing headers."""

from tests.conftest import mcp_call


def test_demo_runs_are_rate_limited(make_app):
    c = make_app(CP_RATE_LIMIT_LLM="3/minute")
    codes = [c.post("/demo/run", json={"sample_id": "PA-1001"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = c.post("/demo/run", json={"sample_id": "PA-1001"})
    assert r.headers["retry-after"] == "60"
    assert "Too many requests" in r.json()["error"]


def test_spoofed_x_forwarded_for_does_not_bypass_the_limit(make_app):
    """Behind Cloud Run the right-most XFF hop is the one Google's front end appended; a
    client can only control the left part, so rotating it must not reset the limit."""
    c = make_app(CP_RATE_LIMIT_LLM="2/minute", TRUST_PROXY="cloudrun")
    codes = [c.post("/demo/run", json={"sample_id": "PA-1001"},
                    headers={"x-forwarded-for": f"10.0.0.{i}, 203.0.113.7"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_xff_is_ignored_when_not_behind_a_trusted_proxy(make_app):
    c = make_app(CP_RATE_LIMIT_LLM="2/minute")
    codes = [c.post("/demo/run", json={"sample_id": "PA-1001"},
                    headers={"x-forwarded-for": f"198.51.100.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_login_is_rate_limited(make_app):
    c = make_app(CP_RATE_LIMIT_AUTH="5/minute")
    import app.auth as auth
    import httpx
    auth._transport = httpx.MockTransport(lambda req: httpx.Response(400, json={"error": "invalid_grant"}))
    try:
        codes = [c.post("/auth/login", json={"email": "a@b.co", "password": "wrongpassword"}).status_code
                 for _ in range(7)]
    finally:
        auth._transport = None
    assert codes == [401] * 5 + [429, 429]


def test_mcp_review_is_rate_limited(make_app):
    c = make_app(CP_RATE_LIMIT_LLM="2/minute")
    results = [mcp_call(c, "review_case", {"case_id": "PA-1001"}) for _ in range(3)]
    assert [r.get("isError", False) for r in results] == [False, False, True]
    assert "rate limit" in results[2]["content"][0]["text"]


def test_mcp_endpoint_has_a_coarse_limit_too(make_app):
    c = make_app(CP_RATE_LIMIT_DEFAULT="3/minute")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    h = {"accept": "application/json, text/event-stream"}
    codes = [c.post("/mcp/", json=body, headers=h).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
