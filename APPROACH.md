# Approach

## Why Scenario 1 (Purchase Recommendation Review)

- It's the cleanest example of **one decision, many constraints** — exactly what
  the assignment says it wants to see ("the agent should be able to deal with
  situations where multiple factors or constraints affect the final decision").
- The decision space is a small, checkable taxonomy (accept / modify / reject /
  investigate), which makes it possible to build a genuinely **independent
  validator** — not just "ask the LLM if it thinks its own answer is right."
  That directly serves the assignment's emphasis on validation/feedback loops.
- It produces a concrete artifact (a Purchase Order) that has to satisfy hard,
  numeric constraints (MOQ, budget, storage), so "did the agent take the
  appropriate action" is an objective, testable question rather than a
  judgment call.
- Scope-wise, it fits comfortably in the ~1-day budget while still exercising
  investigation, multi-factor reasoning, action-taking, and validation — the
  full loop the assignment asks for — without needing speculative machinery
  (e.g. simulated supplier negotiation) that Scenario 2/3 would require to
  feel complete.

Scenario 2 (supplier shortfall) is implemented as a bonus on top of the same
tools/infrastructure, since the assignment rewards (but doesn't require) breadth
once the core is solid.

## Information the agent needs (and why)

| Signal | Why it matters |
|---|---|
| Current inventory | Baseline you're purchasing against |
| Demand forecast **and** recent actual demand | The forecast alone can be stale; comparing it to recent actuals lets the agent detect anomalies instead of blindly trusting either number |
| Open purchase orders | Inventory already "on the way" must offset the new recommendation, or you double-order |
| Supplier lead time | Determines how many days of coverage a new order needs to bridge |
| Supplier MOQ | A hard floor — you cannot place an order below this, no matter how small the calculated need is |
| Purchasing budget remaining | A hard ceiling — affordability constraint |
| Storage capacity remaining | A second, independent hard ceiling — a purchase can be affordable but still physically undeliverable |

## How the agent decides

`app/decision_engine.py` encodes the policy explicitly (see the module
docstring for the tunable parameters: review period, safety stock days, the
demand-anomaly threshold, and the accept-tolerance band):

1. **Check the demand signal's reliability first.** If recent actual demand
   deviates from the forecast baseline by more than the anomaly threshold, the
   agent does not try to size an order at all — it decides `investigate` and
   escalates, because committing budget to a number built on unreliable
   evidence is worse than asking a human to confirm the trend first.
2. **Compute a target stock level** from effective demand × (lead time +
   review period + safety stock), net of on-hand inventory and open POs.
3. **Compute hard caps** from remaining budget and remaining storage.
4. If the target need can't even clear the supplier's MOQ within those caps →
   `reject` (purchasing now would necessarily breach a constraint).
5. Otherwise, compare the *recommended* quantity to the *policy* target: if
   it's close enough (within tolerance) and inside both caps → `accept`;
   otherwise → `modify` to the constrained target.

This same module is used two ways: as the actual decision-maker in
rule-based mode, and as the **independent validator** of whatever action was
taken (by either mode) — it re-derives the caps from scratch and checks the
resulting PO against them, rather than trusting the reasoning that produced it.

## Tools and how the agent uses them

`app/tools.py` exposes read tools (`get_inventory`, `get_demand_forecast`,
`get_open_purchase_orders`, `get_supplier_terms`, `get_budget_status`,
`get_storage_status`) and action tools (`create_purchase_order`,
`modify_purchase_order`, `cancel_purchase_order`, `escalate_to_human`).

In **LLM mode**, Claude drives a standard agentic tool-use loop: it chooses
which read tools to call, reasons over the results itself, calls the relevant
action tool, and finally calls a `submit_decision` tool to report its decision,
suggested quantity, reasoning, and the key factors that drove it — this is
what gets shown in the UI trace and is what a buyer would want as an audit
trail. In **rule-based mode**, the same tools are called deterministically in
a fixed order and the decision engine does the reasoning instead of an LLM.

## Feedback loop / validation (what the assignment is "particularly interested in")

After the agent acts, `decision_engine.validate_purchase_order` independently
re-checks the resulting PO (or absence of one) against MOQ / budget / storage.
If it fails:

- **Rule-based mode**: the *same* PO (never a new/duplicate one) is corrected —
  clamped down to the largest feasible quantity, or cancelled and escalated if
  no feasible quantity exists at all.
- **LLM mode**: the agent is re-invoked with the specific violation and the ID
  of the PO it must fix (not create a new one), and asked to correct itself.
- After `AGENT_MAX_RETRIES` (default 2) failed corrections, the situation is
  escalated to a human buyer rather than looping forever or leaving an invalid
  PO in place.

This means "the outcome is different from what the agent expected" is handled
explicitly, not assumed away — every agent action is checked against ground
truth before being considered final.

## What actions the agent may take, and when a human is involved

The agent may create, modify, or cancel a PO on its own for `accept`/`modify`/
`reject` decisions, because these are bounded, reversible, and fully
constraint-checked before being finalized. It does **not** unilaterally decide
in ambiguous situations: `investigate` always routes to `escalate_to_human`
rather than guessing a quantity from unreliable evidence, and any decision
that cannot be made valid after retries is escalated rather than forced
through. This mirrors how a real buying org would want autonomy scoped: act
freely within guardrails, defer when the evidence itself is in question.

## Mock data design

`app/seed.py` seeds five products, each deliberately tuned to land on a
different decision branch (see the module docstring and `EVALUATION.md`) so
that a reviewer can see all four Scenario-1 outcomes without needing to
hand-craft inputs, plus one product wired for the Scenario 2 bonus flow.
