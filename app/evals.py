"""Metrics harness: batch-run every mock case through the pipeline and compute the
numbers the proposal quotes — auto-resolution rate, escalation rate, cost per
decision, and how many over-confident proposals the critic caught.

Run once (ideally with a real key) to produce data/metrics.json, which the live
app serves — so the metrics panel is instant and doesn't re-bill on every visit.
"""

import os
import json
from app.graph import run_case, CONFIDENCE_THRESHOLD
from app import llm

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(save: bool = True) -> dict:
    cases = json.load(open(os.path.join(_HERE, "data", "cases.json")))
    rows, auto, esc = [], 0, 0
    total_cost = 0.0
    critic_cost = 0.0
    denials_to_human = 0
    low_conf_escalations = 0
    total_latency = 0

    for c in cases:
        s = run_case(c)
        is_auto = s["route"] == "auto_approved"
        auto += 1 if is_auto else 0
        esc += 0 if is_auto else 1
        c_tok = next((st["tokens"] for st in s["trace"] if st["node"] == "critic"), 0)
        critic_cost += c_tok / 1_000_000 * 6.0   # blended $/1M for a "≈" display
        total_cost += s["usage"]["cost"]
        total_latency += sum(st.get("latency_ms", 0) for st in s["trace"])
        if not is_auto:
            if s["decision"] == "deny":
                denials_to_human += 1
            else:
                low_conf_escalations += 1
        rows.append({"id": c["id"], "procedure": c["requested_procedure"],
                     "decision": s["decision"], "confidence": s["confidence"],
                     "route": s["route"], "cost": s["usage"]["cost"]})

    n = len(cases)
    metrics = {
        "mode": llm.mode(),
        "total_cases": n,
        "auto_resolved": auto,
        "escalated": esc,
        "auto_resolution_rate": round(auto / n, 3),
        "escalation_rate": round(esc / n, 3),
        "denials_to_human": denials_to_human,
        "low_conf_escalations": low_conf_escalations,
        "avg_cost_per_decision": round(total_cost / n, 5) if n else 0,
        "critic_added_cost_per_decision": round(critic_cost / n, 5) if n else 0,
        "avg_decision_ms": round(total_latency / n) if n else 0,
        "rows": rows,
    }
    if save:
        with open(os.path.join(_HERE, "data", "metrics.json"), "w") as f:
            json.dump(metrics, f, indent=2)
    return metrics


if __name__ == "__main__":
    m = run()
    print(f"mode={m['mode']}  cases={m['total_cases']}")
    print(f"auto-resolved: {m['auto_resolved']} ({m['auto_resolution_rate']*100:.0f}%)")
    print(f"escalated:     {m['escalated']} ({m['escalation_rate']*100:.0f}%)")
    print(f"  denials to human: {m['denials_to_human']} | low-confidence: {m['low_conf_escalations']}")
    print(f"avg cost/decision: ${m['avg_cost_per_decision']:.4f}")
    print(f"avg decision time: {m['avg_decision_ms']} ms")
