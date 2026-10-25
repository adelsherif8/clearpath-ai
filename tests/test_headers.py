"""Security misconfiguration (A02) and integrity (A08) checks on what the server sends."""

import re

from tests.conftest import mcp_call


def test_security_headers_on_html(client):
    h = client.get("/").headers
    csp = h["content-security-policy"]
    for directive in ("default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
                      "base-uri 'self'", "form-action 'self'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"
    assert "camera=()" in h["permissions-policy"]
    assert h["cross-origin-embedder-policy"] == "require-corp"
    assert h["cross-origin-opener-policy"] == "same-origin"


def test_security_headers_on_api_and_mcp(client):
    assert client.get("/health").headers["x-content-type-options"] == "nosniff"
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"accept": "application/json, text/event-stream"})
    assert r.headers["content-security-policy"].startswith("default-src 'none'")


def test_hsts_in_production(make_app):
    c = make_app(CP_ENV="production", ALLOWED_HOSTS="testserver")
    assert c.get("/health").headers["strict-transport-security"].startswith("max-age=31536000")


def test_docs_disabled_in_production(make_app):
    c = make_app(CP_ENV="production", ALLOWED_HOSTS="testserver")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_docs_disabled_automatically_on_cloud_run(make_app):
    c = make_app(K_SERVICE="clearpath", CP_ENV=None, ALLOWED_HOSTS="testserver")
    assert c.get("/docs").status_code == 404


def test_bad_host_rejected_in_production(make_app):
    c = make_app(CP_ENV="production", ALLOWED_HOSTS="clearpath.example")
    assert c.get("/health", headers={"host": "evil.example"}).status_code == 400
    assert c.get("/health", headers={"host": "clearpath.example"}).status_code == 200


def test_cors_is_locked(client):
    r = client.options("/submit", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/samples", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cdn_assets_have_sri(client):
    html = client.get("/").text
    for tag in re.findall(r"<(?:link|script)[^>]+(?:href|src)=\"https://[^>]+>", html):
        assert "integrity=\"sha" in tag and "crossorigin" in tag, tag


def test_mcp_dns_rebinding_protection_kept(make_app):
    c = make_app(MCP_ALLOWED_HOSTS="clearpath.example", ALLOWED_HOSTS="clearpath.example,testserver")
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
               headers={"accept": "application/json, text/event-stream", "host": "testserver"})
    assert r.status_code in (403, 421)
    ok = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"accept": "application/json, text/event-stream", "host": "clearpath.example"})
    assert ok.status_code == 200


def test_mcp_still_works_for_anonymous_clients(client):
    res = mcp_call(client, "list_sample_cases", {})
    assert res.get("isError") is not True and "PA-1001" in res["content"][0]["text"]
