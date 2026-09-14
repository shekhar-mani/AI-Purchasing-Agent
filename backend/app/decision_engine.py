"""
Deterministic buyer policy.

This module encodes the actual business rules a buyer would apply. It serves
two purposes:

1. RULE-BASED REASONING MODE: used directly as the agent's "brain" when no
   LLM API key is configured, so the whole system is runnable/demoable
   without any external dependency or cost.

2. INDEPENDENT VALIDATION: even when the LLM makes the decision, we
   re-derive the same hard numbers (needed qty, affordability cap, storage
   cap, MOQ) here and check the LLM's chosen action against them. This is
   the feedback loop the assignment asks for -- the agent's action is
   checked against ground truth, not just trusted because an LLM said so.

Policy parameters (documented so they're easy to justify/tune in review):
  - REVIEW_PERIOD_DAYS: how many days pass, on average, between purchasing
    reviews for a given product (order-up-to horizon beyond lead time).
  - SAFETY_DAYS: extra buffer stock, expressed in days of demand.
  - ANOMALY_DEVIATION_PCT: if recent actual demand deviates from the
    forecast baseline by more than this, the evidence is considered
    unreliable and the agent should investigate rather than decide.
  - ACCEPT_TOLERANCE_PCT: how close the recommended qty must be to the
    policy's target qty (and within all hard caps) to be accepted as-is.
"""

REVIEW_PERIOD_DAYS = 14
SAFETY_DAYS = 7
ANOMALY_DEVIATION_PCT = 50.0
ACCEPT_TOLERANCE_PCT = 10.0


def gather_facts(db, tools_module, product_id: int) -> dict:
    """Calls every read tool once, in a fixed order, to build a complete
    picture of the situation. Used by the rule-based agent and by the
    validator. The LLM agent instead chooses tool calls dynamically, but
    ends up needing the same facts."""
    product = tools_module.get_product(db, product_id)
    inv = tools_module.get_inventory(db, product_id)
    forecast = tools_module.get_demand_forecast(db, product_id)
    open_pos = tools_module.get_open_purchase_orders(db, product_id)
    supplier = tools_module.get_supplier_terms(db, product["supplier_id"])
    budget = tools_module.get_budget_status(db)
    storage = tools_module.get_storage_status(db, inv["fulfillment_node"])

    open_po_qty = sum(po["qty"] - po.get("fulfilled_qty", 0) for po in open_pos
                       if po["status"] in ("open", "pending_review"))

    return {
        "product": product,
        "inventory": inv,
        "forecast": forecast,
        "open_purchase_orders": open_pos,
        "open_po_qty": open_po_qty,
        "supplier": supplier,
        "budget": budget,
        "storage": storage,
    }


def evaluate_recommendation(facts: dict, recommended_qty: int) -> dict:
    """Scenario 1 policy: accept / modify / reject / investigate."""
    forecast = facts["forecast"]
    deviation_pct = forecast["deviation_pct"]
    volatility = forecast["volatility"]
    key_factors = []

    # 1. Reliability of demand signal
    if abs(deviation_pct) > ANOMALY_DEVIATION_PCT and volatility != "normal":
        key_factors.append(
            f"Recent actual demand ({forecast['recent_daily_actual']}/day) deviates "
            f"{deviation_pct:+.1f}% from the forecast baseline ({forecast['daily_avg_demand']}/day), "
            f"flagged as '{volatility}' volatility."
        )
        return {
            "decision": "investigate",
            "suggested_qty": None,
            "key_factors": key_factors,
            "reasoning": (
                "Demand evidence is inconsistent/unreliable, so the recommended quantity "
                "cannot be sized with confidence. Recommend confirming whether the recent "
                "spike/drop is a genuine, durable trend (e.g. promotion, seasonality, viral "
                "demand) or a data anomaly before committing budget to a purchase."
            ),
        }

    effective_daily_demand = (forecast["daily_avg_demand"] + forecast["recent_daily_actual"]) / 2
    coverage_days = facts["supplier"]["lead_time_days"] + REVIEW_PERIOD_DAYS + SAFETY_DAYS
    on_hand = facts["inventory"]["on_hand_qty"]
    open_po_qty = facts["open_po_qty"]

    target_stock_level = effective_daily_demand * coverage_days
    needed_qty = max(0, round(target_stock_level - on_hand - open_po_qty))

    key_factors.append(
        f"Effective demand ~{effective_daily_demand:.1f}/day over a {coverage_days}-day coverage "
        f"window (lead time {facts['supplier']['lead_time_days']}d + {REVIEW_PERIOD_DAYS}d review "
        f"+ {SAFETY_DAYS}d safety stock) implies a target stock level of ~{target_stock_level:.0f} units."
    )
    key_factors.append(
        f"Current position: {on_hand} on hand + {open_po_qty} already on open PO(s) "
        f"-> net need ~{needed_qty} units."
    )

    if needed_qty <= 0:
        key_factors.append("Existing inventory and open purchase orders already cover projected demand.")
        return {
            "decision": "reject",
            "suggested_qty": 0,
            "key_factors": key_factors,
            "reasoning": (
                "No purchase is currently needed: on-hand stock plus quantity already on order "
                "covers the demand-driven target stock level for the coverage window."
            ),
        }

    moq = facts["supplier"]["min_order_qty"]
    unit_price = facts["supplier"]["unit_price"]
    unit_volume = facts["product"]["unit_volume_m3"]
    budget_remaining = facts["budget"]["remaining"]
    storage_free = facts["storage"]["free_capacity_m3"]

    max_affordable_qty = int(budget_remaining // unit_price) if unit_price > 0 else float("inf")
    max_storage_qty = int(storage_free // unit_volume) if unit_volume > 0 else float("inf")
    hard_cap = min(max_affordable_qty, max_storage_qty)

    key_factors.append(
        f"Budget allows up to {max_affordable_qty} units (₹{budget_remaining:.0f} remaining @ "
        f"₹{unit_price}/unit); storage allows up to {max_storage_qty} units "
        f"({storage_free:.2f} m3 free @ {unit_volume} m3/unit) -> binding cap {hard_cap} units."
    )
    key_factors.append(f"Supplier minimum order quantity is {moq} units.")

    if hard_cap < moq:
        return {
            "decision": "reject",
            "suggested_qty": 0,
            "key_factors": key_factors,
            "reasoning": (
                f"Even the supplier's minimum order quantity ({moq} units) cannot be met within "
                f"current budget/storage constraints (max feasible {hard_cap} units). Purchasing "
                f"now would breach a hard constraint; recommend deferring, freeing storage/budget, "
                f"or sourcing a smaller-MOQ alternate supplier before ordering."
            ),
        }

    target_qty = max(needed_qty, moq)
    final_qty = min(target_qty, hard_cap)

    within_tolerance = (
        recommended_qty >= moq
        and recommended_qty <= hard_cap
        and abs(recommended_qty - target_qty) / max(target_qty, 1) * 100 <= ACCEPT_TOLERANCE_PCT
    )

    if within_tolerance:
        return {
            "decision": "accept",
            "suggested_qty": recommended_qty,
            "key_factors": key_factors,
            "reasoning": (
                f"The recommended {recommended_qty} units is within {ACCEPT_TOLERANCE_PCT:.0f}% of "
                f"the policy target (~{target_qty} units), respects the MOQ ({moq}), and fits "
                f"within both budget and storage constraints (cap {hard_cap})."
            ),
        }

    return {
        "decision": "modify",
        "suggested_qty": final_qty,
        "key_factors": key_factors,
        "reasoning": (
            f"The recommended {recommended_qty} units does not match policy: target need is "
            f"~{target_qty} units (respecting MOQ {moq}), and the maximum feasible quantity given "
            f"budget/storage is {hard_cap}. Adjusting to {final_qty} units."
        ),
    }


def validate_purchase_order(facts: dict, po_qty: int, decision: str) -> dict:
    """Independent, deterministic check of the *result* of the agent's action.
    This does NOT re-run the LLM -- it re-derives hard constraints and checks
    the actual PO quantity against them, exactly like a compliance check a
    human auditor would run."""
    violations = []

    if decision in ("accept", "modify"):
        moq = facts["supplier"]["min_order_qty"]
        unit_price = facts["supplier"]["unit_price"]
        unit_volume = facts["product"]["unit_volume_m3"]
        budget_remaining = facts["budget"]["remaining"]
        storage_free = facts["storage"]["free_capacity_m3"]

        if po_qty < moq:
            violations.append(f"PO qty {po_qty} is below supplier MOQ {moq}.")
        if po_qty * unit_price > budget_remaining + 1e-6:
            violations.append(
                f"PO cost (₹{po_qty * unit_price:.0f}) exceeds remaining budget (₹{budget_remaining:.0f})."
            )
        if po_qty * unit_volume > storage_free + 1e-6:
            violations.append(
                f"PO volume ({po_qty * unit_volume:.2f} m3) exceeds free storage ({storage_free:.2f} m3)."
            )
        if po_qty <= 0:
            violations.append("PO quantity must be positive for an accept/modify decision.")

    elif decision == "reject":
        if po_qty not in (0, None):
            violations.append(f"Decision was 'reject' but a PO with qty {po_qty} was created.")

    return {"valid": len(violations) == 0, "violations": violations}
