"""Authentication (A07) with Supabase Auth, and role-based authorization (A01).

* No password is ever hashed, stored or compared here: `/auth/login` forwards the
  credentials to Supabase Auth (GoTrue) over TLS and gets back a signed JWT.
* Every request's JWT is verified locally: signature (ES256/RS256 via the project's JWKS,
  or HS256 with the legacy JWT secret), `exp`, `iat`, `aud=authenticated`, `iss`, `sub`.
  `alg: none` and algorithm confusion are impossible because the allowed algorithm is
  chosen from configuration, never from the token.
* The session lives in an HttpOnly, SameSite=Strict cookie (Secure in production). MCP
  clients send the same JWT as `Authorization: Bearer`.
* The role is NOT taken from the token. It is read from `clearpath.user_roles` in the
  database, which only an admin can change (enforced by RLS).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import jwt
from fastapi import HTTPException, Request

from app import audit, db
from app.config import settings
from app.security import client_ip

SESSION_COOKIE = "cp_session"
REFRESH_COOKIE = "cp_refresh"
ROLES = ("submitter", "reviewer", "admin")


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    role: str                      # anonymous | submitter | reviewer | admin
    email_hash: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None


ANONYMOUS = Principal(None, "anonymous")


class AuthError(Exception):
    pass


# ── token verification ──────────────────────────────────────────────────────
_jwks_client = None


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(settings().jwks_url, cache_keys=True, lifespan=600, timeout=5)
    return _jwks_client


def verify_token(token: str) -> dict:
    s = settings()
    if not s.auth_configured:
        raise AuthError("authentication is not configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError("malformed token") from exc
    alg = header.get("alg")
    options = {"require": ["exp", "iat", "sub", "aud", "iss"]}
    try:
        if alg == "HS256" and s.supabase_jwt_secret:
            key, algorithms = s.supabase_jwt_secret, ["HS256"]
        elif alg in ("ES256", "RS256"):
            key, algorithms = _jwks().get_signing_key_from_jwt(token).key, [alg]
        else:
            raise AuthError("unsupported token algorithm")
        claims = jwt.decode(token, key, algorithms=algorithms, audience="authenticated",
                            issuer=s.issuer, options=options, leeway=10)
    except jwt.PyJWTError as exc:
        raise AuthError(type(exc).__name__) from exc
    if claims.get("role") != "authenticated":
        raise AuthError("token is not a signed-in user session")
    return claims


def principal_from_token(token: str) -> Principal:
    claims = verify_token(token)
    p = Principal(claims["sub"], "anonymous", audit.user_hash(claims.get("email")))
    role = db.ensure_user(p)        # raises DatabaseUnavailable -> 503 (fail closed)
    return Principal(p.user_id, role, p.email_hash)


def _token_from_request(request: Request) -> str | None:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def current_principal(request: Request) -> Principal:
    """FastAPI dependency. No token: anonymous. Bad token: 401 (and audited)."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = _token_from_request(request)
    if not token:
        principal = ANONYMOUS
    else:
        try:
            principal = principal_from_token(token)
        except AuthError as exc:
            audit.record("auth_failure", None, ip=client_ip(request), outcome="denied",
                         meta={"reason": str(exc)[:60], "path": request.url.path})
            raise HTTPException(401, "Your session is invalid or has expired. Please sign in again.")
    request.state.principal = principal
    return principal


def require(*roles: str):
    """Dependency factory: allow only the given roles. Deny by default."""
    allowed = set(roles)

    def _dep(request: Request) -> Principal:
        p = current_principal(request)
        if not p.authenticated:
            audit.record("permission_denied", p, ip=client_ip(request), outcome="denied",
                         meta={"path": request.url.path, "reason": "not signed in"})
            raise HTTPException(401, "Sign in to do this.")
        if p.role not in allowed:
            audit.record("permission_denied", p, ip=client_ip(request), outcome="denied",
                         meta={"path": request.url.path, "role": p.role, "reason": "role"})
            raise HTTPException(403, "Your role is not allowed to do this.")
        return p

    return _dep


# ── Supabase Auth (GoTrue) calls ────────────────────────────────────────────
_transport: httpx.BaseTransport | None = None   # tests inject httpx.MockTransport here


def _gotrue() -> httpx.Client:
    s = settings()
    return httpx.Client(base_url=s.issuer, timeout=10, transport=_transport,
                        headers={"apikey": s.supabase_anon_key, "content-type": "application/json"})


def password_login(email: str, password: str) -> dict:
    with _gotrue() as c:
        r = c.post("/token", params={"grant_type": "password"}, json={"email": email, "password": password})
    if r.status_code != 200:
        raise AuthError("invalid credentials")
    return r.json()


def refresh(refresh_token: str) -> dict:
    with _gotrue() as c:
        r = c.post("/token", params={"grant_type": "refresh_token"}, json={"refresh_token": refresh_token})
    if r.status_code != 200:
        raise AuthError("refresh failed")
    return r.json()


def logout(access_token: str) -> None:
    try:
        with _gotrue() as c:
            c.post("/logout", headers={"authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        pass  # cookies are cleared regardless


def set_session_cookies(response, tokens: dict) -> None:
    secure = settings().cookie_secure
    response.set_cookie(SESSION_COOKIE, tokens["access_token"], max_age=int(tokens.get("expires_in", 3600)),
                        httponly=True, secure=secure, samesite="strict", path="/")
    if tokens.get("refresh_token"):
        response.set_cookie(REFRESH_COOKIE, tokens["refresh_token"], max_age=7 * 24 * 3600,
                            httponly=True, secure=secure, samesite="strict", path="/auth")


def clear_session_cookies(response) -> None:
    secure = settings().cookie_secure
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="strict")
    response.delete_cookie(REFRESH_COOKIE, path="/auth", httponly=True, secure=secure, samesite="strict")
