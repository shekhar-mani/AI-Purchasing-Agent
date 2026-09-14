# Evaluation

## Approach

Rather than a generic eval harness, the mock data (`app/seed.py`) was
deliberately constructed so that **each seeded product exercises a different
branch** of the decision policy. This makes it possible to assert on the
*correct category of decision* for each one, not just "the agent said
something plausible." `tests/test_scenarios.py` automates these as
integration tests through the real HTTP API (i.e. exercising routers, agent,
tools, and DB together, not the policy function in isolation).

For each test we check the dimensions the assignment calls out:

| # | Product | Recommended qty | Binding factor | Expected decision | Was decision correct? | Necessary info obtained? | Constraints respected? | Appropriate action taken? | Result validated? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | P-001 Wireless Earbuds | 800 | None — well justified | **Accept** | ✅ qty unchanged, PO created | ✅ inventory, forecast, open PO, supplier, budget, storage all read | ✅ within MOQ/budget/storage | ✅ PO for 800 created | ✅ validator passes on first attempt |
| 2 | P-002 Yoga Mat | 2000 (over-recommended) | Storage (tighter than budget) | **Modify** | ✅ trimmed to feasible qty (≥ MOQ 200, ≤ cap) | ✅ | ✅ | ✅ PO created at corrected qty | ✅ |
| 3 | P-003 Instant Noodles | 1000 | Storage can't even fit the MOQ | **Reject** | ✅ no PO created | ✅ | ✅ (correctly refuses instead of forcing an order) | ✅ no action taken | ✅ (validator confirms no PO exists) |
| 4 | P-004 Phone Case | 300 | Demand anomaly (+220% vs forecast, "rising") | **Investigate** | ✅ escalated instead of guessing | ✅ | N/A (no order sized) | ✅ escalate_to_human called | ✅ |
| 5 | P-005 Energy Drink (Scenario 2) | existing PO for 500, only 250 fulfilled | Shortfall creates a coverage gap | **Top-up from alternate supplier** | ✅ gap correctly computed, top-up ordered from the faster alt-supplier | ✅ | ✅ affordability/storage re-checked before the top-up PO is created | ✅ | ✅ |

## What happens when the initial action doesn't work

Two dedicated tests exercise the safety net directly, independent of whether
the *initial* decision happened to be correct:

- `test_validator_catches_budget_violation` / `test_validator_catches_storage_violation` —
  unit tests proving the validator actually flags a PO that breaches budget or
  storage, rather than always returning "valid."
- `test_self_correction_clamps_infeasible_po_to_max_feasible_qty` — simulates
  an already-wrong action (a PO for 5000 units, deliberately blowing through
  every constraint, standing in for e.g. a hallucinated LLM decision) and
  checks that the self-correction step brings it back to a valid state
  (clamped to the max feasible quantity, or cancelled + escalated if nothing
  feasible exists) rather than leaving an invalid PO sitting in the system.

In the full agent loop (`run_purchase_recommendation_review`), this same
mechanism is wired to `AGENT_MAX_RETRIES` (default 2): if a correction attempt
*itself* still doesn't validate, the situation is escalated to a human buyer
rather than looping indefinitely — every run either returns a validated
action or an explicit escalation, never a silently-invalid one.

## Why rule-based mode is the default for automated evaluation

`tests/conftest.py` forces rule-based mode (unsets `ANTHROPIC_API_KEY`) so the
test suite is deterministic, reproducible, and runnable in CI without API
cost or network dependency. LLM mode should be spot-checked manually (run the
dashboard with a real key and try each product) — its tool-selection order
and phrasing will vary run to run, but the deliverable it produces (a PO,
checked by the same validator) is held to the identical bar.

## Running it

```bash
cd backend && pip install -r requirements.txt && cd ..
pytest tests/ -v
```
