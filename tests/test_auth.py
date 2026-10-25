"""Authentication (A07): Supabase Auth, verified JWTs, safe cookies, generic failures."""

import json
import time

import httpx
import jwt
import pytest

import app.auth as auth
from tests.conftest import ISSUER, JWT_SECRET, Users, make_token


@pytest.fixture()
def gotrue():
    """A fake Supabase Auth server. Accepts one user; anything else is invalid_grant."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/logout"):
            return httpx.Response(204)
        body = json.loads(request.content or b"{}")
        grant = request.url.params.get("grant_type")
        ok = (grant == "password" and body.get("email") == "rev@example.test" and body.get("password") == "correct horse")
        ok = ok or (grant == "refresh_token" and body.get("refresh_token") == "r-good")
        if not ok:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": make_token(Users.reviewer_1, email="rev@example.test"),
                                         "refresh_token": "r-good", "expires_in": 3600, "token_type": "bearer"})

    auth._transport = httpx.MockTransport(handler)
    yield seen
    auth._transport = None


def test_login_sets_httponly_samesite_cookie(client, gotrue):
    r = client.post("/auth/login", json={"email": "rev@example.test", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["role"] == "reviewer"
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("cp_session="))
    assert "HttpOnly" in session and "SameSite=strict" in session and "Path=/" in session
    refresh = next(c for c in cookies if c.startswith("cp_refresh="))
    assert "HttpOnly" in refresh and "Path=/auth" in refresh
    assert gotrue[0].headers["apikey"] == "test-anon-key"       # forwarded to Supabase, not checked here
    assert client.get("/auth/me").json()["role"] == "reviewer"


def test_cookies_are_secure_in_production(make_app, gotrue):
    c = make_app(CP_ENV="production", ALLOWED_HOSTS="testserver")
    r = c.post("/auth/login", json={"email": "rev@example.test", "password": "correct horse"})
    assert all("Secure" in h for h in r.headers.get_list("set-cookie"))


def test_login_failure_is_generic_and_audited(client, gotrue, admin_db, capsys):
    r = client.post("/auth/login", json={"email": "rev@example.test", "password": "wrong password"})
    assert r.status_code == 401 and r.json() == {"error": "Invalid email or password."}
    r2 = client.post("/auth/login", json={"email": "nobody@example.test", "password": "wrong password"})
    assert r2.json() == r.json()                             # no user enumeration
    rows = admin_db.execute("select event, outcome, meta from clearpath.audit_log where event='login_failure'").fetchall()
    assert len(rows) == 2 and rows[0][1] == "denied"
    assert "rev@example.test" not in capsys.readouterr().out  # email is hashed in logs


def test_logout_clears_cookies_and_revokes(client, gotrue):
    client.post("/auth/login", json={"email": "rev@example.test", "password": "correct horse"})
    r = client.post("/auth/logout")
    assert any(c.startswith('cp_session=""') or "Max-Age=0" in c for c in r.headers.get_list("set-cookie"))
    assert any(req.url.path.endswith("/logout") for req in gotrue)
    assert client.get("/auth/me").json()["authenticated"] is False


def test_refresh_rotates_session(client, gotrue):
    client.post("/auth/login", json={"email": "rev@example.test", "password": "correct horse"})
    assert client.post("/auth/refresh").status_code == 200
    client.cookies.clear()
    client.cookies.set("cp_refresh", "r-bad", domain="testserver.local", path="/auth")
    assert client.post("/auth/refresh").status_code == 401
    assert json.loads(gotrue[-1].content)["refresh_token"] == "r-bad"   # it really reached Supabase and was refused


def _now():
    return int(time.time())


@pytest.mark.parametrize("token", [
    make_token(Users.reviewer_1, exp_in=-120),                                      # expired
    make_token(Users.reviewer_1, aud="something-else"),                             # wrong audience
    make_token(Users.reviewer_1, iss="https://evil.supabase.co/auth/v1"),           # wrong issuer
    make_token(Users.reviewer_1, secret="not-the-real-secret-0123456789abcdef"),    # forged signature
    make_token(Users.reviewer_1, role="anon"),                                      # anon key, not a user session
    make_token(Users.reviewer_1, role="service_role"),                              # service key used as session
    jwt.encode({"sub": Users.admin, "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
                "iat": _now(), "exp": _now() + 600}, key=None, algorithm="none"),   # alg=none
    "not.a.jwt",
])
def test_bad_tokens_are_rejected(client, token):
    r = client.get("/cases", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_algorithm_confusion_is_rejected(client):
    """An HS256 token signed with a *public* key value must not verify against the JWKS path."""
    tok = jwt.encode({"sub": Users.admin, "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
                      "iat": _now(), "exp": _now() + 600}, "-----BEGIN PUBLIC KEY-----fake", algorithm="HS256")
    assert client.get("/cases", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_role_claims_in_the_token_are_ignored(client):
    """Supabase lets a user write their own user_metadata; the role comes from the database only."""
    tok = make_token(Users.submitter_a, extra={"user_metadata": {"role": "admin"},
                                                "app_metadata": {"role": "admin"}, "user_role": "admin"})
    assert client.get("/admin/audit", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_first_login_registers_as_submitter_only(client, admin_db):
    newbie = "00000000-0000-0000-0000-00000000abcd"
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {make_token(newbie)}"})
    assert r.json()["role"] == "submitter"
    assert admin_db.execute("select role from clearpath.user_roles where user_id=%s", (newbie,)).fetchone()[0] == "submitter"


def test_cross_site_post_is_blocked(client, gotrue):
    r = client.post("/auth/login", json={"email": "rev@example.test", "password": "correct horse"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_hs256_is_refused_when_no_shared_secret_is_configured(make_app):
    c = make_app(SUPABASE_JWT_SECRET="")
    r = c.get("/cases", headers={"Authorization": f"Bearer {make_token(Users.admin)}"})
    assert r.status_code == 401
