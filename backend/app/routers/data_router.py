from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, tools as T

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/products")
def list_products(db: Session = Depends(get_db)):
    products = db.query(models.Product).all()
    out = []
    for p in products:
        inv = db.query(models.Inventory).filter_by(product_id=p.id).first()
        forecast = db.query(models.DemandForecast).filter_by(product_id=p.id).first()
        supplier = db.query(models.Supplier).filter_by(id=p.supplier_id).first()
        out.append({
            "id": p.id, "sku": p.sku, "name": p.name,
            "on_hand_qty": inv.on_hand_qty if inv else None,
            "daily_avg_demand": forecast.daily_avg_demand if forecast else None,
            "recent_daily_actual": forecast.recent_daily_actual if forecast else None,
            "volatility": forecast.volatility if forecast else None,
            "supplier_name": supplier.name if supplier else None,
            "unit_price": supplier.unit_price if supplier else None,
            "moq": supplier.min_order_qty if supplier else None,
            "lead_time_days": supplier.lead_time_days if supplier else None,
        })
    return out


@router.get("/budget")
def budget(db: Session = Depends(get_db)):
    return T.get_budget_status(db)


@router.get("/storage")
def storage(node: str = "DEL-DC1", db: Session = Depends(get_db)):
    return T.get_storage_status(db, node)


@router.get("/purchase_orders")
def purchase_orders(db: Session = Depends(get_db)):
    pos = db.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id.desc()).all()
    return [
        {"id": po.id, "product_id": po.product_id, "supplier_id": po.supplier_id, "qty": po.qty,
         "fulfilled_qty": po.fulfilled_qty, "status": po.status, "unit_price": po.unit_price,
         "created_by": po.created_by, "note": po.note,
         "created_at": po.created_at.isoformat() if po.created_at else None}
        for po in pos
    ]


@router.get("/agent_runs")
def agent_runs(db: Session = Depends(get_db)):
    runs = db.query(models.AgentRun).order_by(models.AgentRun.id.desc()).all()
    return [
        {"id": r.id, "scenario": r.scenario, "product_id": r.product_id, "decision": r.decision,
         "final_qty": r.final_qty, "attempts": r.attempts, "reasoning_mode": r.reasoning_mode,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in runs
    ]


@router.post("/reset")
def reset_db():
    from .. import seed
    seed.reset_and_seed()
    return {"status": "reset"}
