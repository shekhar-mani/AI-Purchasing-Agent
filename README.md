# AI Purchasing Agent

A full-stack agent that reviews a purchasing system's recommendation, investigates
the real operational picture (inventory, demand, open POs, supplier terms, budget,
storage), decides whether to **accept / modify / reject / investigate**, takes
action (creates or adjusts a Purchase Order, or escalates to a human), and then
**independently validates its own action** — self-correcting or escalating if
the result would violate a hard constraint.

Implements **Scenario 1 (Purchase Recommendation Review)** end-to-end, plus a
bonus **Scenario 2 (Supplier Partial Fulfilment)** flow that reuses the same
tools/infrastructure. See `APPROACH.md` for why Scenario 1 was chosen and how
the agent is designed.

## Quick start

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
cp .env.example .env                                     # edit if you want LLM mode, see below
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** — the dashboard (in `frontend/index.html`) is served
directly by the backend. The database is a local SQLite file, auto-created and
auto-seeded with mock data on first run.

## Two reasoning modes (same tools, same validation)

| | Rule-based (default) | LLM tool-use (opt-in) |
|---|---|---|
| Enabled when | `ANTHROPIC_API_KEY` is **not** set | `ANTHROPIC_API_KEY` **is** set in `.env` |
| Reasoning engine | Deterministic buyer policy (`app/decision_engine.py`) | Claude, driving an agentic tool-use loop (`app/agent.py`) |
| Why it exists | Makes the whole system runnable, testable, and demoable **for free, offline, deterministically** | Demonstrates the actual "AI agent" investigating and deciding autonomously |

Both modes call the exact same tool functions (`app/tools.py`) against the exact
same mock database, and both are checked by the exact same independent validator
(`decision_engine.validate_purchase_order`). Nothing about the validation or the
data model changes based on which mode is active — see `APPROACH.md`.

To turn on LLM mode:
```bash
# in backend/.env
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```
If the LLM call errors for any reason (bad key, rate limit, network), the agent
automatically falls back to rule-based mode for that request rather than failing outright.

## Running the tests / evaluation suite

```bash
cd backend    # (dependencies must already be installed)
cd ..
pytest tests/ -v
```

The suite runs against an isolated SQLite file and forces rule-based mode, so it's
fully deterministic and doesn't require an API key. See `EVALUATION.md` for the
evaluation approach and what each test is checking.

## Project layout

```
backend/
  app/
    main.py            FastAPI app, mounts routers + serves the frontend
    models.py           SQLAlchemy models (mock operational data)
    seed.py              Deterministic mock data, tuned to hit each decision branch
    tools.py              The agent's "hands and eyes" — read + action tools
    decision_engine.py Deterministic buyer policy + independent validator
    agent.py               Orchestrator: LLM tool-use loop, rule-based fallback,
                              retry/self-correction loop
    routers/               API endpoints
  requirements.txt
  .env.example
frontend/
  index.html            Single-page dashboard (no build step)
tests/
  test_scenarios.py     Evaluation suite (see EVALUATION.md)
ARCHITECTURE.md
APPROACH.md
EVALUATION.md
```

## API

- `POST /api/agent/recommendation-review` `{product_id, recommended_qty}` → Scenario 1
- `POST /api/agent/supplier-shortfall` `{po_id, fulfilled_qty}` → Scenario 2 (bonus)
- `GET /api/data/products|budget|storage|purchase_orders|agent_runs`
- `POST /api/data/reset` — reseed mock data
- `GET /api/health` — reports which reasoning mode is active
