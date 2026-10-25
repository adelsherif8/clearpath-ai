"""Prompt-injection defences (OWASP LLM01) that do not depend on the model behaving.

1. `fence()` wraps untrusted text in a named tag and neutralises any attempt to close
   or reopen that tag from inside the text, so the instruction/data boundary in the
   prompt cannot be forged.
2. `injection_signals()` is a deterministic tripwire. It does not try to be a complete
   filter (that is impossible); it only decides whether a case must go to a human. A
   hit never denies anything and never blocks submission: it removes the auto-approve
   path, which is the only thing an injection could gain.
"""

from __future__ import annotations

import re
import unicodedata

_PATTERNS: list[tuple[str, str]] = [
    (r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instruction|instructions|rules|criteria|prompt|policy|guidelines|above|previous)\b",
     "asks the model to ignore its instructions"),
    (r"\b(system|developer|assistant)\s*(prompt|message|override|mode)?\s*[:>\]]", "contains a fake system/developer message"),
    (r"<\s*/?\s*(system|instructions?|case_data|reviewer_notes|im_start|im_end)\b", "contains prompt-structure tags"),
    (r"\byou\s+(are|must)\s+now\b|\bact\s+as\b|\bpretend\s+(to\s+be|you)\b|\bjailbreak\b|\bdan\s+mode\b",
     "tries to change the model's role"),
    (r"\b(set|return|output|respond\s+with|make)\b[^.\n]{0,40}\b(confidence|decision)\b[^.\n]{0,20}(1(\.0+)?|100\s*%|approve)",
     "dictates the decision or confidence"),
    (r"\b(auto[- ]?approve|approve\s+(this|it|immediately|without))\b", "demands approval"),
    (r"\b(do\s+not|don'?t|never)\s+(escalate|flag|send\s+to\s+(a\s+)?human)\b", "asks to skip human review"),
    (r"\b(reveal|print|repeat|show)\b[^.\n]{0,30}\b(system\s+prompt|instructions|api\s*key|secret)\b",
     "asks for the prompt or secrets"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in _PATTERNS]


# Zero-width and bidi control characters (U+200B..U+200F, U+202A..U+202E, U+2060..U+2069, U+FEFF).
_INVISIBLE = {cp: None for cp in [*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x206A), 0xFEFF]}


def _normalise(text: str) -> str:
    # Fold full-width / look-alike characters and drop zero-width characters used to dodge filters.
    text = unicodedata.normalize("NFKC", text or "")
    return text.translate(_INVISIBLE)


def injection_signals(*texts: str) -> list[str]:
    hits: list[str] = []
    for t in texts:
        n = _normalise(t)
        for rx, why in _COMPILED:
            if rx.search(n) and why not in hits:
                hits.append(why)
    return hits


def fence(tag: str, text: str) -> str:
    """Wrap untrusted text as data. Any `<tag` / `</tag` inside is defanged."""
    clean = _normalise(text)
    clean = re.sub(rf"<\s*(/?)\s*{tag}", r"[\1" + tag, clean, flags=re.IGNORECASE)
    return f"<{tag}>\n{clean}\n</{tag}>"
