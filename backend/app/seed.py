"""
Seeds the mock database with suppliers, products, inventory, forecasts,
budget and storage capacity — deliberately constructed so that different
products exercise different branches of the agent's decision logic:

  P-001 WIRELESS-EARBUDS -> recommendation of 800 is well justified -> ACCEPT
  P-002 YOGA-MAT         -> budget is tight -> qty should be trimmed -> MODIFY
  P-003 INSTANT-NOODLES  -> storage node is nearly full -> MODIFY/REJECT
  P-004 PHONE-CASE       -> demand forecast looks stale/anomalous -> INVESTIGATE
  P-005 ENERGY-DRINK     -> used for Scenario 2 (supplier can only partially fulfil)
"""
from datetime import datetime
from .database import Base, engine, SessionLocal
from .models import (
    Supplier, Product, Inventory, DemandForecast, PurchaseOrder, Budget, StorageCapacity
)


def reset_and_seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        s1 = Supplier(name="Acme Audio Ltd", reliability_score=0.97, lead_time_days=7,
                      min_order_qty=100, unit_price=12.5)
        s2 = Supplier(name="FitGear Supply Co", reliability_score=0.90, lead_time_days=10,
                      min_order_qty=200, unit_price=8.0)
        s3 = Supplier(name="QuickSnacks Distributors", reliability_score=0.85, lead_time_days=3,
                      min_order_qty=500, unit_price=1.2)
        s4 = Supplier(name="MobileAccess Traders", reliability_score=0.80, lead_time_days=14,
                      min_order_qty=300, unit_price=3.5)
        s5 = Supplier(name="Buzz Beverages Inc", reliability_score=0.75, lead_time_days=5,
                      min_order_qty=400, unit_price=1.8)
        # Alternate/backup supplier for P-005, used in the Scenario 2 (partial fulfilment) flow
        s6 = Supplier(name="Rapid Bev Traders", reliability_score=0.88, lead_time_days=2,
                      min_order_qty=100, unit_price=2.1)
        db.add_all([s1, s2, s3, s4, s5, s6])
        db.flush()

        p1 = Product(sku="P-001", name="Wireless Earbuds X1", unit_volume_m3=0.002, supplier_id=s1.id)
        p2 = Product(sku="P-002", name="Premium Yoga Mat", unit_volume_m3=0.02, supplier_id=s2.id)
        p3 = Product(sku="P-003", name="Instant Noodles (Box of 24)", unit_volume_m3=0.03, supplier_id=s3.id)
        p4 = Product(sku="P-004", name="Phone Case - Universal", unit_volume_m3=0.001, supplier_id=s4.id)
        p5 = Product(sku="P-005", name="Energy Drink 250ml (Pack 12)", unit_volume_m3=0.015,
                     supplier_id=s5.id, alt_supplier_id=s6.id)
        db.add_all([p1, p2, p3, p4, p5])
        db.flush()

        # Inventory
        db.add_all([
            Inventory(product_id=p1.id, on_hand_qty=150, fulfillment_node="DEL-DC1"),
            Inventory(product_id=p2.id, on_hand_qty=80, fulfillment_node="DEL-DC1"),
            Inventory(product_id=p3.id, on_hand_qty=1200, fulfillment_node="DEL-DC1"),
            Inventory(product_id=p4.id, on_hand_qty=300, fulfillment_node="DEL-DC1"),
            Inventory(product_id=p5.id, on_hand_qty=100, fulfillment_node="DEL-DC1"),
        ])

        # Demand forecasts
        db.add_all([
            # ~35/day average, recent actual close to forecast -> stable, supports 800 recommendation
            DemandForecast(product_id=p1.id, daily_avg_demand=35, forecast_horizon_days=30,
                            recent_daily_actual=37, volatility="normal"),
            # modest demand, budget is the binding constraint
            DemandForecast(product_id=p2.id, daily_avg_demand=10, forecast_horizon_days=30,
                            recent_daily_actual=11, volatility="normal"),
            # storage is the binding constraint
            DemandForecast(product_id=p3.id, daily_avg_demand=60, forecast_horizon_days=30,
                            recent_daily_actual=58, volatility="normal"),
            # recent actual demand looks stale/erratic vs forecast -> should be investigated, not blindly bought
            DemandForecast(product_id=p4.id, daily_avg_demand=15, forecast_horizon_days=30,
                            recent_daily_actual=48, volatility="rising"),
            # Scenario 2 product: demand is steady
            DemandForecast(product_id=p5.id, daily_avg_demand=40, forecast_horizon_days=30,
                            recent_daily_actual=41, volatility="normal"),
        ])

        # Existing open purchase orders
        db.add_all([
            PurchaseOrder(product_id=p1.id, supplier_id=s1.id, qty=100, unit_price=12.5,
                          status="open", created_by="system", note="Scheduled replenishment"),
            PurchaseOrder(product_id=p2.id, supplier_id=s2.id, qty=50, unit_price=8.0,
                          status="open", created_by="system"),
            PurchaseOrder(product_id=p5.id, supplier_id=s5.id, qty=500, unit_price=1.8,
                          status="open", created_by="system", note="Awaiting supplier fulfilment"),
        ])

        # Budget: total available purchasing budget for the current month
        db.add(Budget(period="current_month", total_budget=20000.0, spent=8000.0))
        # -> remaining = 12000.

        # Storage: shared fulfilment node capacity
        db.add(StorageCapacity(fulfillment_node="DEL-DC1", total_capacity_m3=50.0, used_capacity_m3=41.0))
        # -> only 9 m3 free. Instant noodles at 0.03 m3/unit -> max additional ~300 units before overflow.

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    reset_and_seed()
    print("Database seeded.")
