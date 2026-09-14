from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, DateTime, Text
)
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    reliability_score = Column(Float, default=0.9)  # 0-1, historical on-time-in-full rate
    lead_time_days = Column(Integer, nullable=False)
    min_order_qty = Column(Integer, nullable=False)  # MOQ
    unit_price = Column(Float, nullable=False)

    products = relationship("Product", back_populates="supplier", foreign_keys="Product.supplier_id")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    unit_volume_m3 = Column(Float, default=0.01)  # storage footprint per unit
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    alt_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    supplier = relationship("Supplier", back_populates="products", foreign_keys=[supplier_id])
    inventory = relationship("Inventory", back_populates="product", uselist=False)
    forecast = relationship("DemandForecast", back_populates="product", uselist=False)


class Inventory(Base):
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), unique=True)
    on_hand_qty = Column(Integer, nullable=False)
    fulfillment_node = Column(String, default="DEL-DC1")

    product = relationship("Product", back_populates="inventory")


class DemandForecast(Base):
    __tablename__ = "demand_forecasts"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), unique=True)
    daily_avg_demand = Column(Float, nullable=False)
    forecast_horizon_days = Column(Integer, default=30)
    recent_daily_actual = Column(Float, nullable=False)  # last 7d actual avg, for anomaly detection
    volatility = Column(String, default="normal")  # normal | rising | falling

    product = relationship("Product", back_populates="forecast")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    qty = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    status = Column(String, default="open")  # open | pending_review | confirmed | cancelled | short_shipped
    fulfilled_qty = Column(Integer, default=0)
    created_by = Column(String, default="system")  # system | agent | human
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(Text, default="")


class Budget(Base):
    __tablename__ = "budget"
    id = Column(Integer, primary_key=True)
    period = Column(String, default="current_month")
    total_budget = Column(Float, nullable=False)
    spent = Column(Float, default=0.0)


class StorageCapacity(Base):
    __tablename__ = "storage_capacity"
    id = Column(Integer, primary_key=True)
    fulfillment_node = Column(String, unique=True)
    total_capacity_m3 = Column(Float, nullable=False)
    used_capacity_m3 = Column(Float, default=0.0)


class AgentRun(Base):
    """Every agent invocation is logged for auditability/evaluation."""
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True)
    scenario = Column(String)
    product_id = Column(Integer, ForeignKey("products.id"))
    input_payload = Column(Text)
    trace_json = Column(Text)  # full step-by-step trace (tool calls + reasoning)
    decision = Column(String)  # accept | modify | reject | investigate | escalate
    final_qty = Column(Integer, nullable=True)
    validation_result = Column(Text)
    attempts = Column(Integer, default=1)
    reasoning_mode = Column(String, default="rule_based")  # llm | rule_based
    created_at = Column(DateTime, default=datetime.utcnow)
