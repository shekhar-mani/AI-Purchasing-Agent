"""
Agent orchestrator for Scenario 1 (Purchase Recommendation Review), with a
bonus flow for Scenario 2 (supplier partial fulfilment).

Two reasoning modes, same tools, same validator:

  - "llm"        : Claude drives an agentic tool-use loop -- it decides which
                    read tools to call to investigate, weighs the factors
                    itself, calls an action tool (create/modify/cancel PO or
                    escalate), and calls `submit_decision` to report its
                    reasoning. Used automatically when ANTHROPIC_API_KEY is set.

  - "rule_based" : Deterministic policy engine (decision_engine.py) makes the
                    same category of decision. Used automatically when no API
                    key is configured, so the system is fully runnable/
                    demoable offline. Also used as the safety net if the LLM
                    call fails for any reason.

In BOTH modes, the resulting action (the PO that was created/modified, or the
absence of one) is checked by an independent deterministic validator
(`decision_engine.validate_purchase_order`). If validation fails, the agent
is given the specific violation and asked to correct itself, up to
AGENT_MAX_RETRIES times, after which it escalates to a human buyer. This is
the feedback loop the assignment calls out.
"""
import os
import json
from sqlalchemy.orm import Session

from . import tools as T
from . import decision_engine as DE
from . import models

MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _reasoning_mode() -> str:
    return "llm" if os.getenv("ANTHROPIC_API_KEY") else "rule_based"


# ---------------------------------------------------------------------------
# Rule-based reasoning (also used as ground truth / fallback)
# ---------------------------------------------------------------------------

def _run_rule_based(db: Session, product_id: int, recommended_qty: int, trace: list) -> dict:
    facts = DE.gather_facts(db, T, product_id)
    trace.append({"step": "investigate", "tool": "gather_facts", "result": facts})

    result = DE.evaluate_recommendation(facts, recommended_qty)
    trace.append({"step": "decide", "decision": result["decision"],
                  "suggested_qty": result["suggested_qty"],
                  "reasoning": result["reasoning"], "key_factors": result["key_factors"]})

    po_id = None
    if result["decision"] in ("accept", "modify"):
        action = T.create_purchase_order(
            db, product_id, facts["product"]["supplier_id"], result["suggested_qty"],
            created_by="agent",
            note=f"{result['decision'].upper()} of buyer recommendation ({recommended_qty} -> "
                 f"{result['suggested_qty']}) by rule-based agent.",
        )
        po_id = action["po_id"]
        trace.append({"step": "act", "tool": "create_purchase_order", "result": action})
    elif result["decision"] == "investigate":
        esc = T.escalate_to_human(db, product_id, result["reasoning"])
        trace.append({"step": "act", "tool": "escalate_to_human", "result": esc})
    else:  # reject
        trace.append({"step": "act", "tool": "none", "result": {"info": "No PO created (reject)."}})

    return {"decision": result["decision"], "suggested_qty": result["suggested_qty"],
            "reasoning": result["reasoning"], "key_factors": result["key_factors"],
            "po_id": po_id, "facts": facts}


def _self_correct_rule_based(db: Session, product_id: int, po_id: int, facts: dict,
                              violations: list, trace: list) -> dict:
    """If the created PO violates a hard constraint, clamp it to the nearest
    feasible quantity and re-validate; if still infeasible, cancel + escalate."""
    moq = facts["supplier"]["min_order_qty"]
    unit_price = facts["supplier"]["unit_price"]
    unit_volume = facts["product"]["unit_volume_m3"]
    max_affordable = int(facts["budget"]["remaining"] // unit_price)
    max_storage = int(facts["storage"]["free_capacity_m3"] // unit_volume)
    hard_cap = min(max_affordable, max_storage)

    if hard_cap < moq:
        cancel = T.cancel_purchase_order(db, po_id, note="Cancelled: infeasible under current constraints.")
        esc = T.escalate_to_human(db, product_id, f"PO {po_id} infeasible: {violations}")
        trace.append({"step": "self_correct", "tool": "cancel_purchase_order", "result": cancel})
        trace.append({"step": "self_correct", "tool": "escalate_to_human", "result": esc})
        return {"decision": "escalate", "suggested_qty": None, "po_id": None,
                "reasoning": f"No feasible quantity exists under current constraints: {violations}",
                "key_factors": []}

    # Clamp down to the largest quantity that fits both budget and storage.
    new_qty = hard_cap
    mod = T.modify_purchase_order(db, po_id, new_qty, note=f"Auto-corrected after validation failure: {violations}")
    trace.append({"step": "self_correct", "tool": "modify_purchase_order", "result": mod})
    return {"decision": "modify", "suggested_qty": new_qty, "po_id": po_id,
            "reasoning": f"Original quantity violated constraints ({violations}); corrected to the "
                         f"maximum feasible quantity ({new_qty}) given budget/storage.",
            "key_factors": []}


# ---------------------------------------------------------------------------
# LLM-driven reasoning (Anthropic tool-use agentic loop)
# ---------------------------------------------------------------------------

SUBMIT_DECISION_TOOL = {
    "name": "submit_decision",
    "description": "Report your final decision for this purchasing situation. Call this exactly once, "
                    "after you have investigated using the read tools and (if applicable) taken action "
                    "with create_purchase_order / modify_purchase_order / cancel_purchase_order / escalate_to_human.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["accept", "modify", "reject", "investigate"]},
            "suggested_qty": {"type": ["integer", "null"]},
            "reasoning": {"type": "string"},
            "key_factors": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["decision", "reasoning", "key_factors"],
    },
}

SYSTEM_PROMPT = """You are an AI purchasing agent assisting a buyer at a quick-commerce retailer.

You are given a purchasing system's recommendation (e.g. "buy 800 units of product X"). \
This recommendation is NOT necessarily correct -- treat it as a hypothesis to check, not an instruction to follow.

Your job:
1. Investigate using the read tools: get_product, get_inventory, get_demand_forecast, \
get_open_purchase_orders, get_supplier_terms, get_budget_status, get_storage_status. \
Gather ALL of these before deciding -- do not skip any, since each represents a real constraint \
a human buyer would check.
2. Decide whether the recommendation should be: accepted, modified (to a different quantity), \
rejected (no purchase needed / infeasible), or investigated further (evidence is unreliable/insufficient).
3. If the demand evidence is inconsistent (e.g. recent actual demand differs drastically from the \
forecast baseline and volatility is flagged), do not guess -- choose "investigate" instead of sizing an order.
4. If you decide to accept or modify, you MUST take the corresponding action by calling \
create_purchase_order (for accept, or for modify if no PO exists yet) or modify_purchase_order \
(if adjusting an existing PO). Respect the supplier's minimum order quantity, and make sure the \
order fits within both the remaining budget and free storage capacity -- these are hard constraints, \
not suggestions.
5. If you decide "investigate", call escalate_to_human with a clear reason instead of creating a PO.
6. If you decide "reject", do not create any PO.
7. Finally, call submit_decision exactly once with your decision, suggested quantity (or null), \
reasoning, and a list of the key factors that drove your decision.

Be precise with numbers -- always base them on tool results, never assume or estimate a fact you can query.
"""


def _execute_tool(db: Session, name: str, tool_input: dict):
    fn = T.TOOL_REGISTRY.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    return fn(db, **tool_input)


def _run_llm(db: Session, product_id: int, recommended_qty: int, trace: list,
             feedback: str = None, prior_po_id: int = None) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    tool_schemas = T.ANTHROPIC_TOOL_SCHEMAS + [SUBMIT_DECISION_TOOL]

    user_msg = (
        f"Purchasing recommendation: buy {recommended_qty} units of product_id={product_id}. "
        f"Evaluate this recommendation."
    )
    if feedback:
        user_msg += (
            f"\n\nNOTE: your previous attempt failed independent validation with these violations: "
            f"{feedback}. The PO you created/modified was id={prior_po_id}. Do NOT create a new PO -- "
            f"call modify_purchase_order on PO {prior_po_id} to correct the quantity (or "
            f"cancel_purchase_order and escalate_to_human if no feasible quantity exists), then "
            f"re-submit your decision."
        )

    messages = [{"role": "user", "content": user_msg}]
    submitted = None
    po_id = None
    last_action_decision = None

    for _ in range(12):  # hard cap on tool-use turns to prevent runaway loops
        response = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
            tools=tool_schemas, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_decision":
                submitted = block.input
                trace.append({"step": "decide", **block.input})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                      "content": "Decision recorded."})
                continue

            result = _execute_tool(db, block.name, block.input)
            step_type = "investigate" if block.name.startswith("get_") else "act"
            trace.append({"step": step_type, "tool": block.name, "input": block.input, "result": result})

            if block.name in ("create_purchase_order", "modify_purchase_order") and "po_id" in result:
                po_id = result["po_id"]
                last_action_decision = "accept_or_modify"
            elif block.name == "escalate_to_human":
                last_action_decision = "investigate"

            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                  "content": json.dumps(result, default=str)})

        messages.append({"role": "user", "content": tool_results})
        if submitted is not None:
            break

    if submitted is None:
        # Model never called submit_decision within the turn budget -- fail safe.
        raise RuntimeError("LLM agent did not submit a final decision within the allotted turns.")

    return {"decision": submitted["decision"], "suggested_qty": submitted.get("suggested_qty"),
            "reasoning": submitted["reasoning"], "key_factors": submitted.get("key_factors", []),
            "po_id": po_id}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_purchase_recommendation_review(db: Session, product_id: int, recommended_qty: int) -> dict:
    """
    Runs the agent, then independently validates whatever it did. On failure,
    the SAME PO is corrected (rule-based) or the LLM is told exactly which PO
    to fix (never creating a duplicate), up to MAX_RETRIES times, after which
    the situation is escalated to a human buyer.
    """
    trace = []
    mode = _reasoning_mode()
    attempts = 0
    feedback = None
    prior_po_id = None

    def finalize(outcome, validation):
        return {
            "decision": outcome["decision"], "final_qty": outcome.get("suggested_qty"),
            "po_id": outcome.get("po_id"), "reasoning": outcome.get("reasoning"),
            "key_factors": outcome.get("key_factors", []), "validation": validation,
            "attempts": attempts, "reasoning_mode": mode, "trace": trace,
        }

    while attempts < MAX_RETRIES + 1:
        attempts += 1
        trace.append({"step": "attempt_start", "attempt": attempts, "mode": mode})

        if attempts == 1:
            if mode == "llm":
                try:
                    outcome = _run_llm(db, product_id, recommended_qty, trace)
                except Exception as e:  # noqa: BLE001 - any LLM/infra failure falls back safely
                    trace.append({"step": "llm_error_fallback", "error": str(e)})
                    mode = "rule_based"
                    outcome = _run_rule_based(db, product_id, recommended_qty, trace)
            else:
                outcome = _run_rule_based(db, product_id, recommended_qty, trace)
        else:
            # This is a correction attempt: fix the SAME PO, never create a new one.
            if mode == "llm":
                try:
                    outcome = _run_llm(db, product_id, recommended_qty, trace, feedback, prior_po_id)
                except Exception as e:  # noqa: BLE001
                    trace.append({"step": "llm_error_fallback", "error": str(e)})
                    mode = "rule_based"
                    facts = DE.gather_facts(db, T, product_id)
                    outcome = _self_correct_rule_based(db, product_id, prior_po_id, facts, feedback, trace)
            else:
                facts = DE.gather_facts(db, T, product_id)
                outcome = _self_correct_rule_based(db, product_id, prior_po_id, facts, feedback, trace)
                outcome.setdefault("reasoning", "Auto-corrected after validation failure.")
                outcome.setdefault("key_factors", [])

        facts = outcome.get("facts") or DE.gather_facts(db, T, product_id)
        validation = DE.validate_purchase_order(facts, outcome.get("suggested_qty") or 0, outcome["decision"])
        trace.append({"step": "validate", "result": validation})

        if validation["valid"] or outcome["decision"] == "escalate":
            return finalize(outcome, validation)

        prior_po_id = outcome.get("po_id")
        feedback = validation["violations"]
        # loop again for a correction attempt

    # Exhausted retries without a valid outcome -> escalate to human as the safe default.
    esc = T.escalate_to_human(db, product_id, f"Agent could not produce a valid action after "
                                               f"{attempts} attempts. Last violations: {feedback}")
    trace.append({"step": "final_escalation", "result": esc})
    return {
        "decision": "escalate", "final_qty": None, "po_id": prior_po_id,
        "reasoning": "Escalated to human buyer after repeated validation failures.",
        "key_factors": [], "validation": {"valid": False, "violations": feedback},
        "attempts": attempts, "reasoning_mode": mode, "trace": trace,
    }


# ---------------------------------------------------------------------------
# Scenario 2 bonus: supplier can only partially fulfil an existing PO
# ---------------------------------------------------------------------------

def run_supplier_shortfall(db: Session, po_id: int, fulfilled_qty: int) -> dict:
    """Deterministic policy (kept rule-based for this bonus flow, for speed):
    when a supplier under-delivers, decide whether the shortfall can be
    absorbed by existing inventory, needs a top-up order (same or alternate
    supplier), or must be escalated."""
    trace = []
    po = db.query(models.PurchaseOrder).filter_by(id=po_id).first()
    if not po:
        return {"error": "po not found"}

    shortfall = po.qty - fulfilled_qty
    po.fulfilled_qty = fulfilled_qty
    po.status = "short_shipped"
    db.commit()
    trace.append({"step": "investigate", "tool": "record_short_shipment",
                  "result": {"po_id": po_id, "ordered": po.qty, "fulfilled": fulfilled_qty,
                             "shortfall": shortfall}})

    facts = DE.gather_facts(db, T, po.product_id)
    trace.append({"step": "investigate", "tool": "gather_facts", "result": facts})

    on_hand_after = facts["inventory"]["on_hand_qty"] + fulfilled_qty
    effective_daily_demand = (facts["forecast"]["daily_avg_demand"] + facts["forecast"]["recent_daily_actual"]) / 2
    days_until_next_review = DE.REVIEW_PERIOD_DAYS
    projected_need = effective_daily_demand * days_until_next_review
    gap = projected_need - on_hand_after

    trace.append({"step": "decide", "detail": {
        "on_hand_after_partial_delivery": on_hand_after,
        "projected_need_next_review_cycle": round(projected_need, 1),
        "gap": round(gap, 1),
    }})

    if gap <= 0:
        trace.append({"step": "act", "tool": "none",
                      "result": {"info": "Existing inventory + partial delivery covers demand until "
                                          "next review; no top-up order needed."}})
        return {"decision": "sufficient_inventory", "shortfall": shortfall, "gap": round(gap, 1),
                "action": None, "trace": trace}

    alt_supplier_id = facts["product"]["alt_supplier_id"]
    if alt_supplier_id:
        alt_terms = T.get_supplier_terms(db, alt_supplier_id)
        topup_qty = max(int(gap), alt_terms["min_order_qty"])
        budget_remaining = facts["budget"]["remaining"]
        cost = topup_qty * alt_terms["unit_price"]
        storage_free = facts["storage"]["free_capacity_m3"]
        volume_needed = topup_qty * facts["product"]["unit_volume_m3"]

        if cost <= budget_remaining and volume_needed <= storage_free:
            action = T.create_purchase_order(
                db, po.product_id, alt_supplier_id, topup_qty, created_by="agent",
                note=f"Top-up order after supplier short-shipped PO {po_id} by {shortfall} units "
                     f"(faster alt-supplier lead time {alt_terms['lead_time_days']}d).",
            )
            trace.append({"step": "act", "tool": "create_purchase_order (alt supplier)", "result": action})
            return {"decision": "topup_alt_supplier", "shortfall": shortfall, "gap": round(gap, 1),
                    "action": action, "trace": trace}

    esc = T.escalate_to_human(
        db, po.product_id,
        f"Supplier short-shipped PO {po_id} by {shortfall} units and projected gap is {gap:.0f} units, "
        f"but no affordable/feasible alternate sourcing option was found automatically."
    )
    trace.append({"step": "act", "tool": "escalate_to_human", "result": esc})
    return {"decision": "escalate", "shortfall": shortfall, "gap": round(gap, 1),
            "action": None, "trace": trace}
