# ClearPath AI — Agentic Prior Authorization & Clinical Decision Review

A **LangGraph** agent that reviews prior-authorization requests against clinical
criteria, auto-resolves clear cases, escalates every denial and low-confidence
case to a human, self-corrects when a reviewer sends a case back, and produces a
full, auditable rationale for every decision.

**Live:** https://clearpath-798991357908.us-central1.run.app (Google Cloud Run) · **MCP server:** `https://clearpath-798991357908.us-central1.run.app/mcp/`

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
pip install --require-hashes --no-deps -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # fill in what you have; everything is optional for the offline demo
uvicorn app.main:app --reload   # http://localhost:8000
python -m app.evals             # recompute metrics.json
python -m app.evals --injection # prompt-injection eval set

# tests need a Postgres you can create databases on (Homebrew, Docker, ...)
createdb clearpath_test
TEST_ADMIN_DATABASE_URL=postgresql://localhost/clearpath_test pytest
```

## Stack
Python · LangGraph · GPT-4o · Langfuse · FastAPI · RAG (embedding retrieval over clinical criteria)

Built by **Adel Atya** — adelatya.vercel.app


## Deployment: Google Cloud Run

One container serves the reviewer dashboard, the JSON API and the MCP server (`Dockerfile`:
digest-pinned base image, hash-verified dependencies, non-root user). Deploy with `infra/deploy.sh`,
which runs as a dedicated least-privilege service account and mounts every secret from Google
Secret Manager. First-time setup is in [`infra/README.md`](infra/README.md).

Runs inside the Cloud Run always-free tier: scales to zero when idle, one instance max, CPU billed only
during requests, an Artifact Registry cleanup policy that keeps only the latest image, and a 1 USD budget
alert on the project. Without `OPENAI_API_KEY` it runs the deterministic offline mode (no LLM cost).
With a key, a daily LLM call cap (default 200) fails closed.

## MCP server

ClearPath is also a Model Context Protocol server (streamable HTTP, stateless, JSON responses) at `/mcp/`,
built with the official MCP Python SDK (v2). Any MCP client can call the same agent the dashboard uses:

| Tool | What it does |
|---|---|
| `list_sample_cases` | the synthetic prior-auth requests bundled with the demo (no PHI) |
| `search_criteria` | RAG over the clinical-criteria knowledge base |
| `review_case` | runs the LangGraph pipeline (retrieve → proposer → critic → route); denials and low-confidence cases come back `needs_human`. Sample `case_id`s work anonymously; your own case text needs `Authorization: Bearer <Supabase access token>` |
| `get_eval_metrics` | offline evaluation: accuracy, auto-resolution and escalation rates, cost per decision |

Claude Desktop / any client config:
```json
{"mcpServers": {"clearpath": {"url": "https://clearpath-798991357908.us-central1.run.app/mcp/"}}}
```
DNS-rebinding protection is on: set `MCP_ALLOWED_HOSTS` to the service's hostnames. All MCP tools are
rate-limited per IP, and there is deliberately no tool that records or finalizes a decision.

## Security

A full security review against the **OWASP Top 10:2025** and the **OWASP Top 10 for LLM Applications 2025**,
with a threat model, 25 findings with evidence, the fix for each and the test that proves it:
**[docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md)**.

- **Authentication:** Supabase Auth. JWTs are verified on every request (signature, expiry, audience, issuer). Sessions live in HttpOnly, SameSite=Strict, Secure cookies. This app never handles password hashes.
- **Authorization:** roles `submitter`, `reviewer` and `admin` are enforced server-side on every endpoint and MCP tool. Only the assigned reviewer can approve or deny a case. Visitors can still run the sample cases.
- **Database-level permissions:** Postgres row-level security is forced on every table. The app connects as a least-privilege role with no DELETE privilege, and the audit log is append-only even for the table owner. See [`db/migrations/`](db/migrations/).
- **Secrets management:** Google Secret Manager, a dedicated runtime service account, keyless GitHub-to-GCP auth for backups, and gitleaks over the full history on every push.
- **Prompt injection:** untrusted case text is fenced and the instruction hierarchy is explicit. A deterministic tripwire forces human review, model output is schema-validated, and the agent can never auto-deny or finalize. An injection eval set covers 8 attacks.
- **Abuse and cost:** per-IP rate limits, a daily LLM call cap that fails closed, and request size and length limits.
- **Logging and backups:** structured audit logging with no patient text, an alert on repeated auth failures, and a daily `pg_dump` to a private bucket that is restored and verified on every run.
- **CI on every push:** pytest (250 tests, including a full authorization matrix and database-enforced RLS tests), Bandit, Semgrep, pip-audit, CycloneDX SBOM, gitleaks, Trivy and an OWASP ZAP baseline scan. All actions are pinned to commit SHAs.

---

Built by **Adel Atya**, AI engineer: AI voice agents, agentic systems and automation for businesses. [adelatya.com](https://adelatya.com)
