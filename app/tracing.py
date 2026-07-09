"""Real Langfuse tracing. Replays the captured graph trace into Langfuse as a
nested observation tree (retrieve/auto/flag as spans, proposer/critic as
generations with token usage). Returns the public trace URL, or None if Langfuse
isn't configured — the app works either way.
"""

import os

_GEN_NODES = {"proposer", "critic"}
_lf = None


def configured() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _client():
    global _lf
    if _lf is None:
        from langfuse import Langfuse
        _lf = Langfuse()
    return _lf


def trace_run(case: dict, state: dict) -> str | None:
    if not configured():
        return None
    try:
        lf = _client()
        with lf.start_as_current_observation(
            as_type="span", name="prior-auth-review",
            input={"case_id": case.get("id"), "procedure": case.get("requested_procedure"),
                   "plan": case.get("plan"), "attempt": state.get("attempt", 1)},
        ) as root:
            for step in state.get("trace", []):
                as_type = "generation" if step["node"] in _GEN_NODES else "span"
                with lf.start_as_current_observation(
                    as_type=as_type, name=step["node"], input={"summary": step["summary"]},
                    **({"model": os.getenv("CP_MODEL", "gpt-4o")} if as_type == "generation" else {}),
                ) as ob:
                    ob.update(output=step.get("detail"))
                    if as_type == "generation" and step.get("tokens"):
                        ob.update(usage_details={"total": step["tokens"]})
            root.update(output={
                "decision": state.get("decision"), "route": state.get("route"),
                "confidence": state.get("confidence"),
                "cost_usd": state.get("usage", {}).get("cost"),
            })
            url = lf.get_trace_url()
        lf.flush()
        return url
    except Exception:
        return None  # never let tracing break a decision
