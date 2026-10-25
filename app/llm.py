"""OpenAI wrapper (GPT-4o) with a deterministic MOCK fallback.

If OPENAI_API_KEY is set we call real GPT-4o (structured JSON) and real
embeddings. Otherwise we degrade to a deterministic offline mode so the demo,
the metrics run, and the graph all still work end to end. Token usage is
returned so the metrics panel can compute real cost per decision.

Cost controls (OWASP LLM10): every live call first takes a unit from a daily
call budget (`CP_DAILY_LLM_CALL_CAP`). The budget is kept in Postgres when one
is configured, otherwise in process memory (the service runs one instance).
If the budget is spent, or cannot be checked, the call is refused: fail closed.
Each call also has a hard timeout and an output-token ceiling.
"""

import hashlib
import json
import math
import os
import threading
from datetime import date

REASON_MODEL = os.getenv("CP_MODEL", "gpt-4o")
EMBED_MODEL = os.getenv("CP_EMBED_MODEL", "text-embedding-3-small")
MAX_OUTPUT_TOKENS = int(os.getenv("CP_MAX_OUTPUT_TOKENS", "700"))
LLM_TIMEOUT_S = float(os.getenv("CP_LLM_TIMEOUT_S", "25"))

# GPT-4o pricing (USD per token) — used for cost-per-decision metrics.
PRICE_IN = 2.50 / 1_000_000
PRICE_OUT = 10.00 / 1_000_000


class BudgetExceeded(Exception):
    """Today's LLM call budget is spent."""


class LLMUnavailable(Exception):
    """The model (or the budget store) could not be reached. Nothing was decided."""


def live() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def mode() -> str:
    return "openai" if live() else "mock"


# ── daily budget ─────────────────────────────────────────────────────────────
_mem_lock = threading.Lock()
_mem_usage: dict[str, int] = {}


def _cap() -> int:
    from app.config import settings
    return settings().daily_llm_call_cap


def consume_budget(n: int = 1) -> None:
    from app import db
    cap = _cap()
    if db.available():
        try:
            ok = db.consume_llm_calls(n, cap)
        except db.DatabaseUnavailable as exc:
            raise LLMUnavailable("budget store unreachable") from exc
    else:
        with _mem_lock:
            day = date.today().isoformat()
            used = _mem_usage.get(day, 0)
            ok = used + n <= cap
            if ok:
                _mem_usage.clear()
                _mem_usage[day] = used + n
    if not ok:
        raise BudgetExceeded(f"daily LLM call cap of {cap} reached")


def reset_budget() -> None:
    with _mem_lock:
        _mem_usage.clear()


# ── client ───────────────────────────────────────────────────────────────────
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(timeout=LLM_TIMEOUT_S, max_retries=1)
    return _client


def chat_json(system: str, user: str) -> tuple[dict, dict]:
    """Return (parsed_json, usage). usage = {in, out, cost}. Raises LLMUnavailable on any
    transport/provider error and BudgetExceeded when the daily cap is hit."""
    consume_budget(1)
    try:
        resp = _get_client().chat.completions.create(
            model=REASON_MODEL,
            temperature=0,
            max_tokens=MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        u = resp.usage
    except Exception as exc:
        raise LLMUnavailable(type(exc).__name__) from exc
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {}          # schema validation downstream turns this into an escalation
    usage = {
        "in": u.prompt_tokens,
        "out": u.completion_tokens,
        "cost": round(u.prompt_tokens * PRICE_IN + u.completion_tokens * PRICE_OUT, 6),
    }
    return data, usage


def embed(texts: list[str]) -> list[list[float]]:
    if live():
        consume_budget(1)
        try:
            resp = _get_client().embeddings.create(model=EMBED_MODEL, input=texts)
        except Exception as exc:
            raise LLMUnavailable(type(exc).__name__) from exc
        return [d.embedding for d in resp.data]
    return [_mock_embed(t) for t in texts]


def _mock_embed(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        # Non-cryptographic use: bucket tokens into a fixed-size vector.
        h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(
        c.lower() if c.isalnum() else " " for c in text).split() if len(t) > 1]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
