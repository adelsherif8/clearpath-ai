"""Runtime configuration. Everything comes from the environment; nothing secret lives here.

On Cloud Run the secrets (OPENAI_API_KEY, LANGFUSE_*, DATABASE_URL) are mounted from
Google Secret Manager as environment variables; locally they come from a git-ignored .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    env: str
    on_cloud_run: bool
    trust_proxy: str                      # "" (none) or "cloudrun"
    allowed_hosts: list[str]
    supabase_url: str
    supabase_anon_key: str
    supabase_jwt_secret: str              # legacy HS256 secret (optional)
    database_url: str
    daily_llm_call_cap: int
    max_body_bytes: int
    rate_limit_llm: str                   # slowapi syntax, e.g. "10/minute"
    rate_limit_auth: str
    rate_limit_default: str
    max_send_back_attempts: int
    cookie_secure: bool
    extra_connect_src: list[str] = field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def auth_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def settings() -> Settings:
    on_cloud_run = bool(os.getenv("K_SERVICE"))
    env = os.getenv("CP_ENV") or ("production" if on_cloud_run else "development")
    hosts = _csv(os.getenv("ALLOWED_HOSTS")) or _csv(os.getenv("MCP_ALLOWED_HOSTS"))
    if env != "production":
        hosts += ["localhost", "127.0.0.1", "testserver"]
    return Settings(
        env=env,
        on_cloud_run=on_cloud_run,
        trust_proxy=os.getenv("TRUST_PROXY", "cloudrun" if on_cloud_run else ""),
        allowed_hosts=hosts or ["*"],
        supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", ""),
        supabase_jwt_secret=os.getenv("SUPABASE_JWT_SECRET", ""),
        database_url=os.getenv("DATABASE_URL", ""),
        daily_llm_call_cap=int(os.getenv("CP_DAILY_LLM_CALL_CAP", "200")),
        max_body_bytes=int(os.getenv("CP_MAX_BODY_BYTES", str(64 * 1024))),
        rate_limit_llm=os.getenv("CP_RATE_LIMIT_LLM", "10/minute"),
        rate_limit_auth=os.getenv("CP_RATE_LIMIT_AUTH", "5/minute"),
        rate_limit_default=os.getenv("CP_RATE_LIMIT_DEFAULT", "120/minute"),
        max_send_back_attempts=int(os.getenv("CP_MAX_SEND_BACK_ATTEMPTS", "3")),
        cookie_secure=env == "production",
        extra_connect_src=_csv(os.getenv("CP_CSP_CONNECT_SRC")),
    )


def reset() -> None:
    """Forget cached settings (tests change the environment between app instances)."""
    settings.cache_clear()
