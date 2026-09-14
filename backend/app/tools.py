"""
These are the "hands and eyes" of the agent: read tools to investigate the
purchasing situation, and write tools to take action. Every tool call is
pure/deterministic against the mock DB so results are reproducible for
evaluation. The LLM (or the rule-based fallback) only decides WHICH tools to
call and HOW to weigh the results — it never touches the DB directly.
"""
from sqlalchemy.orm import Session
from . import models


def get_product(db: Session, product_id: int) -> dict:
    p = db.query(models.Product).filter_by(id=product_id).first()
    if not p:
        return {"error": f"product {product_id} not found"}
    return {"id": p.id, "sku": p.sku, "name": p.name, "unit_volume_m3": p.unit_volume_m3,
            "supplier_id": p.supplier_id, "alt_supplier_id": p.alt_supplier_id}


def get_inventory(db: Session, product_id: int) -> dict:
    inv = db.query(models.Inventory).filter_by(product_id=product_id).first()
    if not inv:
        return {"error": "no inventory record"}
    return {"on_hand_qty": inv.on_hand_qty, "fulfillment_node": inv.fulfillment_node}


def get_demand_forecast(db: Session, product_id: int) -> dict:
    f = db.query(models.DemandForecast).filter_by(product_id=product_id).first()
    if not f:
        return {"error": "no forecast record"}
    return {
        "daily_avg_demand": f.daily_avg_demand,
        "forecast_horizon_days": f.forecast_horizon_days,
        "recent_daily_actual": f.recent_daily_actual,
        "volatility": f.volatility,
        "deviation_pct": round(
            100 * (f.recent_daily_actual - f.daily_avg_demand) / max(f.daily_avg_demand, 0.001), 1
        ),
    }


def get_open_purchase_orders(db: Session, product_id: int) -> list:
    pos = db.query(models.PurchaseOrder).filter_by(product_id=product_id).filter(
        models.PurchaseOrder.status.in_(["open", "pending_review", "short_shipped"])
    ).all()
    return [
        {"id": po.id, "qty": po.qty, "fulfilled_qty": po.fulfilled_qty, "status": po.status,
         "supplier_id": po.supplier_id, "note": po.note}
        for po in pos
    ]


def get_supplier_terms(db: Session, supplier_id: int) -> dict:
    s = db.query(models.Supplier).filter_by(id=supplier_id).first()
    if not s:
        return {"error": "supplier not found"}
    return {"id": s.id, "name": s.name, "lead_time_days": s.lead_time_days,
            "min_order_qty": s.min_order_qty, "unit_price": s.unit_price,
            "reliability_score": s.reliability_score}


def get_budget_status(db: Session) -> dict:
    b = db.query(models.Budget).filter_by(period="current_month").first()
    return {"total_budget": b.total_budget, "spent": b.spent,
            "remaining": round(b.total_budget - b.spent, 2)}


def get_storage_status(db: Session, fulfillment_node: str) -> dict:
    st = db.query(models.StorageCapacity).filter_by(fulfillment_node=fulfillment_node).first()
    if not st:
        return {"error": "storage node not found"}
    return {"total_capacity_m3": st.total_capacity_m3, "used_capacity_m3": st.used_capacity_m3,
            "free_capacity_m3": round(st.total_capacity_m3 - st.used_capacity_m3, 3)}


# ---------------------------------------------------------------------------
# Action (write) tools
# ---------------------------------------------------------------------------

def create_purchase_order(db: Session, product_id: int, supplier_id: int, qty: int,
                           created_by: str = "agent", note: str = "") -> dict:
    supplier = db.query(models.Supplier).filter_by(id=supplier_id).first()
    po = models.PurchaseOrder(
        product_id=product_id, supplier_id=supplier_id, qty=qty,
        unit_price=supplier.unit_price, status="open", created_by=created_by, note=note,
    )
    db.add(po)
    # reserve budget
    budget = db.query(models.Budget).filter_by(period="current_month").first()
    budget.spent += qty * supplier.unit_price
    db.commit()
    db.refresh(po)
    return {"po_id": po.id, "qty": po.qty, "unit_price": po.unit_price, "status": po.status}


def modify_purchase_order(db: Session, po_id: int, new_qty: int, note: str = "") -> dict:
    po = db.query(models.PurchaseOrder).filter_by(id=po_id).first()
    if not po:
        return {"error": "po not found"}
    budget = db.query(models.Budget).filter_by(period="current_month").first()
    budget.spent -= po.qty * po.unit_price  # release old reservation
    po.qty = new_qty
    po.note = (po.note + " | " if po.note else "") + note
    budget.spent += po.qty * po.unit_price  # reserve new amount
    db.commit()
    db.refresh(po)
    return {"po_id": po.id, "qty": po.qty, "status": po.status}


def cancel_purchase_order(db: Session, po_id: int, note: str = "") -> dict:
    po = db.query(models.PurchaseOrder).filter_by(id=po_id).first()
    if not po:
        return {"error": "po not found"}
    budget = db.query(models.Budget).filter_by(period="current_month").first()
    budget.spent -= po.qty * po.unit_price
    po.status = "cancelled"
    po.note = (po.note + " | " if po.note else "") + note
    db.commit()
    return {"po_id": po.id, "status": po.status}


def escalate_to_human(db: Session, product_id: int, reason: str) -> dict:
    """No DB action beyond logging; represents flagging the case for buyer review."""
    return {"escalated": True, "product_id": product_id, "reason": reason}


TOOL_REGISTRY = {
    "get_product": get_product,
    "get_inventory": get_inventory,
    "get_demand_forecast": get_demand_forecast,
    "get_open_purchase_orders": get_open_purchase_orders,
    "get_supplier_terms": get_supplier_terms,
    "get_budget_status": get_budget_status,
    "get_storage_status": get_storage_status,
    "create_purchase_order": create_purchase_order,
    "modify_purchase_order": modify_purchase_order,
    "cancel_purchase_order": cancel_purchase_order,
    "escalate_to_human": escalate_to_human,
}

# JSON-schema tool definitions for the Anthropic tool-use API
ANTHROPIC_TOOL_SCHEMAS = [
    {"name": "get_product", "description": "Get basic product info including its primary and alternate supplier ids.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "get_inventory", "description": "Get current on-hand inventory quantity for a product.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "get_demand_forecast", "description": "Get demand forecast, recent actual demand, and volatility flag for a product.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "get_open_purchase_orders", "description": "List open/pending/short-shipped purchase orders for a product.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "get_supplier_terms", "description": "Get lead time, minimum order quantity, unit price and reliability for a supplier.",
     "input_schema": {"type": "object", "properties": {"supplier_id": {"type": "integer"}}, "required": ["supplier_id"]}},
    {"name": "get_budget_status", "description": "Get total, spent, and remaining purchasing budget for the current month.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_storage_status", "description": "Get total, used, and free storage capacity (m3) at a fulfillment node.",
     "input_schema": {"type": "object", "properties": {"fulfillment_node": {"type": "string"}}, "required": ["fulfillment_node"]}},
    {"name": "create_purchase_order", "description": "Create a new purchase order for a product with a given supplier and quantity.",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "integer"}, "supplier_id": {"type": "integer"},
         "qty": {"type": "integer"}, "note": {"type": "string"}},
         "required": ["product_id", "supplier_id", "qty"]}},
    {"name": "modify_purchase_order", "description": "Change the quantity on an existing purchase order.",
     "input_schema": {"type": "object", "properties": {
         "po_id": {"type": "integer"}, "new_qty": {"type": "integer"}, "note": {"type": "string"}},
         "required": ["po_id", "new_qty"]}},
    {"name": "cancel_purchase_order", "description": "Cancel an existing purchase order.",
     "input_schema": {"type": "object", "properties": {
         "po_id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["po_id"]}},
    {"name": "escalate_to_human", "description": "Flag this purchasing situation for human buyer review instead of taking automated action.",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["product_id", "reason"]}},
]
