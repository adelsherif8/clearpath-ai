"""OpenAI wrapper (GPT-4o) with a deterministic MOCK fallback.

If OPENAI_API_KEY is set we call real GPT-4o (structured JSON) and real
embeddings. Otherwise we degrade to a deterministic offline mode so the demo,
the metrics run, and the graph all still work end to end. Token usage is
returned so the metrics panel can compute real cost per decision.
"""

import os
import json
import math
import hashlib

REASON_MODEL = os.getenv("CP_MODEL", "gpt-4o")
EMBED_MODEL = os.getenv("CP_EMBED_MODEL", "text-embedding-3-small")

# GPT-4o pricing (USD per token) — used for cost-per-decision metrics.
PRICE_IN = 2.50 / 1_000_000
PRICE_OUT = 10.00 / 1_000_000


def live() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def mode() -> str:
    return "openai" if live() else "mock"


_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    return _client


def chat_json(system: str, user: str) -> tuple[dict, dict]:
    """Return (parsed_json, usage). usage = {in, out, cost}."""
    resp = _get_client().chat.completions.create(
        model=REASON_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    u = resp.usage
    usage = {
        "in": u.prompt_tokens,
        "out": u.completion_tokens,
        "cost": round(u.prompt_tokens * PRICE_IN + u.completion_tokens * PRICE_OUT, 6),
    }
    return data, usage


def embed(texts: list[str]) -> list[list[float]]:
    if live():
        resp = _get_client().embeddings.create(model=EMBED_MODEL, input=texts)
        return [d.embedding for d in resp.data]
    return [_mock_embed(t) for t in texts]


def _mock_embed(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
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
