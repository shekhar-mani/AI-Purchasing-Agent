from pydantic import BaseModel
from typing import Optional, Any


class RecommendationReviewRequest(BaseModel):
    product_id: int
    recommended_qty: int


class SupplierShortfallRequest(BaseModel):
    po_id: int
    fulfilled_qty: int


class AgentResponse(BaseModel):
    decision: str
    final_qty: Optional[int] = None
    po_id: Optional[int] = None
    reasoning: Optional[str] = None
    key_factors: list = []
    validation: dict = {}
    attempts: int = 1
    reasoning_mode: str = "rule_based"
    trace: list = []
