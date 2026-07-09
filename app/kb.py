"""Clinical criteria knowledge base with embedding retrieval (RAG grounding).

Each criterion is embedded once at load. For a given prior-auth case we embed
the case text and retrieve the most relevant criteria, so the proposer reasons
against grounded policy text instead of the model's memory.
"""

import os
import json
from app import llm

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CriteriaKB:
    def __init__(self, path: str | None = None):
        path = path or os.path.join(_HERE, "data", "criteria.json")
        with open(path) as f:
            self.criteria = json.load(f)
        self._texts = [self._doc_text(c) for c in self.criteria]
        self._emb = llm.embed(self._texts)

    def _doc_text(self, c: dict) -> str:
        return (f"{c['procedure']} for {c['indication']}. "
                f"Plans: {', '.join(c['plans'])}. Criteria: {c['criteria']}")

    def retrieve(self, case: dict, k: int = 3) -> list[dict]:
        query = (f"{case['requested_procedure']} for {case['condition']}. "
                 f"Plan: {case['plan']}. {case['notes']}")
        qvec = llm.embed([query])[0]
        scored = []
        for c, emb in zip(self.criteria, self._emb):
            scored.append((llm.cosine(qvec, emb), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, c in scored[:k]:
            item = dict(c)
            item["_score"] = round(float(score), 3)
            out.append(item)
        return out

    @staticmethod
    def as_context(criteria: list[dict]) -> str:
        blocks = []
        for c in criteria:
            rf = ("; RED FLAGS (approve/escalate): " + ", ".join(c["red_flags"])
                  if c.get("red_flags") else "")
            blocks.append(
                f"[{c['id']}] {c['procedure']} — {c['indication']}\n"
                f"Applies to plans: {', '.join(c['plans'])}\n"
                f"Criteria: {c['criteria']}{rf}\n"
                f"Source: {c['source']}"
            )
        return "\n\n".join(blocks)
