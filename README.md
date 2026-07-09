# ClearPath AI — Agentic Prior Authorization & Clinical Decision Review

A **LangGraph** agent that reviews prior-authorization requests against clinical
criteria, auto-resolves clear cases, escalates every denial and low-confidence
case to a human, self-corrects when a reviewer sends a case back, and produces a
full, auditable rationale for every decision.

**Live demo:** _(deployed on Vercel)_

> Decision **support**, not automation of denials. The agent never issues an
> autonomous denial — every denial and every low-confidence case is routed to a
> human reviewer. Demo uses synthetic, non-PHI data.

## The pipeline (LangGraph)

```
retrieve → proposer → critic → route
   │          │          │        ├─ auto_finalize    (clear, high-confidence approvals)
   │          │          │        └─ flag_for_review  (denials + low confidence → human)
   │          │          └─ scores confidence, flags gaps & red flags
   │          └─ drafts approve/deny + rationale, grounded in retrieved criteria
   └─ vector search over the clinical-criteria knowledge base (RAG)

human review → Approve / Deny / Send Back
                                   └─ self-correction: re-runs the graph with
                                      the reviewer's notes injected as context
```

### Proposer / Critic / Judge
- **Proposer** drafts a decision grounded ONLY in the retrieved criteria and cites them.
- **Critic** independently reviews it, scores confidence, and flags logical gaps.
- **Router** (judge) auto-resolves confident approvals; routes denials and anything
  below the confidence threshold to a human.

### Human-in-the-loop + self-correction
Escalated cases land in a review queue. A reviewer can Approve, Deny, or **Send Back** —
which re-enters the graph with their notes injected, and the agent revises its decision.

### Observability
Every node is traced in **Langfuse** (inputs, outputs, token cost, latency) and shown
in an in-app audit trail. A metrics panel reports auto-resolution rate, escalation
rate, cost per decision, and average decision time over the mock case set.

## Measured on 20 synthetic cases (GPT-4o)
- **55%** auto-resolved · **45%** escalated to human
- **100%** of denials routed to a human (zero autonomous denials)
- **~$0.005** per decision · **~3.6s** average decision time

## Modes
- **GPT-4o live** — set `OPENAI_API_KEY` (+ `LANGFUSE_*` for tracing).
- **Offline demo** — no key needed; a deterministic planner keeps the demo and
  metrics working. The UI shows which mode is live.

## Run locally
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload   # http://localhost:8000
python -m app.evals             # recompute metrics.json
```

## Stack
Python · LangGraph · GPT-4o · Langfuse · FastAPI · RAG (embedding retrieval over clinical criteria)

Built by **Adel Atya** — adelatya.vercel.app
