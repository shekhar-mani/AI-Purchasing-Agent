import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import agent as agent_module
from ..models import AgentRun
from ..schemas import RecommendationReviewRequest, SupplierShortfallRequest, AgentResponse

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/recommendation-review", response_model=AgentResponse)
def recommendation_review(req: RecommendationReviewRequest, db: Session = Depends(get_db)):
    try:
        result = agent_module.run_purchase_recommendation_review(db, req.product_id, req.recommended_qty)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))

    run = AgentRun(
        scenario="recommendation_review", product_id=req.product_id,
        input_payload=json.dumps({"recommended_qty": req.recommended_qty}),
        trace_json=json.dumps(result["trace"], default=str),
        decision=result["decision"], final_qty=result.get("final_qty"),
        validation_result=json.dumps(result["validation"]),
        attempts=result["attempts"], reasoning_mode=result["reasoning_mode"],
    )
    db.add(run)
    db.commit()
    return result


@router.post("/supplier-shortfall")
def supplier_shortfall(req: SupplierShortfallRequest, db: Session = Depends(get_db)):
    try:
        result = agent_module.run_supplier_shortfall(db, req.po_id, req.fulfilled_qty)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))

    run = AgentRun(
        scenario="supplier_shortfall", product_id=None,
        input_payload=json.dumps(req.dict()),
        trace_json=json.dumps(result["trace"], default=str),
        decision=result["decision"], final_qty=None,
        validation_result=json.dumps({}), attempts=1, reasoning_mode="rule_based",
    )
    db.add(run)
    db.commit()
    return result
