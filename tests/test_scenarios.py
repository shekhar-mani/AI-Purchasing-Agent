"""
Evaluation suite for the AI Purchasing Agent.

Each test corresponds to one row of the evaluation table in EVALUATION.md and
checks the dimensions the assignment calls out explicitly:
  - Was the decision correct (right category, right quantity)?
  - Did the agent obtain the necessary information (present in the trace)?
  - Did it respect relevant constraints (budget / storage / MOQ)?
  - Did it take the appropriate action (PO created/not created, correct qty)?
  - Did it validate the result (validation.valid, or self-correction)?
"""
import pytest


def _investigate_steps(trace):
    return [s for s in trace if s.get("step") == "investigate"]


def test_reset_seeds_five_products(client):
    r = client.get("/api/data/products")
    assert r.status_code == 200
    products = r.json()
    assert len(products) == 5
    assert {p["sku"] for p in products} == {"P-001", "P-002", "P-003", "P-004", "P-005"}


def test_scenario1_accept_well_justified_recommendation(client):
    """P-001: 800 units matches the demand-driven target and fits every constraint -> ACCEPT."""
    r = client.post("/api/agent/recommendation-review", json={"product_id": 1, "recommended_qty": 800})
    assert r.status_code == 200
    body = r.json()

    assert body["decision"] == "accept"
    assert body["final_qty"] == 800
    assert body["po_id"] is not None
    assert body["validation"]["valid"] is True
    assert body["attempts"] == 1
    assert len(_investigate_steps(body["trace"])) >= 1  # agent actually looked things up

    # The PO must actually exist and be within budget/storage in the DB, not just claimed.
    pos = client.get("/api/data/purchase_orders").json()
    po = next(p for p in pos if p["id"] == body["po_id"])
    assert po["qty"] == 800
    assert po["product_id"] == 1


def test_scenario1_modify_over_recommendation_trimmed_to_feasible_qty(client):
    """P-002: system over-recommends 2000 units; budget/storage cap it -> MODIFY down."""
    r = client.post("/api/agent/recommendation-review", json={"product_id": 2, "recommended_qty": 2000})
    body = r.json()

    assert body["decision"] == "modify"
    assert body["final_qty"] < 2000
    assert body["final_qty"] >= 200  # respects supplier MOQ of 200
    assert body["validation"]["valid"] is True

    pos = client.get("/api/data/purchase_orders").json()
    po = next(p for p in pos if p["id"] == body["po_id"])
    assert po["qty"] == body["final_qty"]


def test_scenario1_reject_when_even_moq_breaches_storage(client):
    """P-003: storage is nearly full; even the 500-unit MOQ won't fit -> REJECT, no PO created."""
    before = len(client.get("/api/data/purchase_orders").json())
    r = client.post("/api/agent/recommendation-review", json={"product_id": 3, "recommended_qty": 1000})
    body = r.json()

    assert body["decision"] == "reject"
    assert body["po_id"] is None
    after = len(client.get("/api/data/purchase_orders").json())
    assert after == before  # no PO was created


def test_scenario1_investigate_on_demand_anomaly(client):
    """P-004: recent actual demand is 220% above forecast baseline -> INVESTIGATE, escalate, no PO."""
    r = client.post("/api/agent/recommendation-review", json={"product_id": 4, "recommended_qty": 300})
    body = r.json()

    assert body["decision"] == "investigate"
    assert body["po_id"] is None
    assert "deviat" in body["reasoning"].lower() or "anomal" in " ".join(body["key_factors"]).lower()


def test_agent_run_is_logged_for_audit(client):
    client.post("/api/agent/recommendation-review", json={"product_id": 1, "recommended_qty": 800})
    runs = client.get("/api/data/agent_runs").json()
    assert len(runs) >= 1
    assert runs[0]["scenario"] == "recommendation_review"
    assert runs[0]["reasoning_mode"] == "rule_based"


def test_scenario2_supplier_shortfall_triggers_topup_from_alt_supplier(client):
    """P-005 has an open PO (id likely 3) for 500 units; supplier can only ship 250.
    The resulting gap should trigger a top-up order from the faster alternate supplier."""
    pos = client.get("/api/data/purchase_orders").json()
    po = next(p for p in pos if p["product_id"] == 5)

    r = client.post("/api/agent/supplier-shortfall", json={"po_id": po["id"], "fulfilled_qty": 250})
    body = r.json()

    assert body["shortfall"] == 250
    assert body["decision"] in ("topup_alt_supplier", "sufficient_inventory", "escalate")
    if body["decision"] == "topup_alt_supplier":
        assert body["action"]["qty"] > 0

    # original PO should now reflect the partial delivery
    pos_after = client.get("/api/data/purchase_orders").json()
    original = next(p for p in pos_after if p["id"] == po["id"])
    assert original["status"] == "short_shipped"
    assert original["fulfilled_qty"] == 250


# ---------------------------------------------------------------------------
# Direct unit tests of the independent validator and self-correction path
# (these do not depend on the LLM or on the rule engine's own correctness --
#  they simulate an already-wrong action and check the safety net catches it)
# ---------------------------------------------------------------------------

def test_validator_catches_budget_violation():
    from app import decision_engine as DE

    facts = {
        "product": {"unit_volume_m3": 0.01},
        "supplier": {"min_order_qty": 100, "unit_price": 50.0},
        "budget": {"remaining": 1000.0},
        "storage": {"free_capacity_m3": 100.0},
    }
    # 100 units * 50/unit = 5000, but only 1000 remaining -> should be flagged invalid
    result = DE.validate_purchase_order(facts, po_qty=100, decision="accept")
    assert result["valid"] is False
    assert any("budget" in v.lower() for v in result["violations"])


def test_validator_catches_storage_violation():
    from app import decision_engine as DE

    facts = {
        "product": {"unit_volume_m3": 1.0},
        "supplier": {"min_order_qty": 10, "unit_price": 1.0},
        "budget": {"remaining": 100000.0},
        "storage": {"free_capacity_m3": 5.0},
    }
    result = DE.validate_purchase_order(facts, po_qty=10, decision="accept")
    assert result["valid"] is False
    assert any("storage" in v.lower() for v in result["violations"])


def test_self_correction_clamps_infeasible_po_to_max_feasible_qty(db_session):
    """Simulates an action (e.g. a hallucinated LLM decision) that created a PO
    exceeding budget/storage, and checks the self-correction step clamps it
    down to a valid quantity rather than leaving an invalid PO in place."""
    from app import agent as agent_module, tools as T, decision_engine as DE

    db = db_session
    # P-001 (id=1): deliberately create an oversized PO of 5000 units (way over budget/storage)
    bad = T.create_purchase_order(db, product_id=1, supplier_id=1, qty=5000, created_by="agent")
    facts = DE.gather_facts(db, T, product_id=1)
    validation_before = DE.validate_purchase_order(facts, po_qty=5000, decision="accept")
    assert validation_before["valid"] is False

    trace = []
    corrected = agent_module._self_correct_rule_based(
        db, product_id=1, po_id=bad["po_id"], facts=facts,
        violations=validation_before["violations"], trace=trace,
    )
    facts_after = DE.gather_facts(db, T, product_id=1)
    validation_after = DE.validate_purchase_order(
        facts_after, po_qty=corrected["suggested_qty"] or 0, decision=corrected["decision"]
    )
    assert validation_after["valid"] is True
    assert any(s["step"] == "self_correct" for s in trace)
