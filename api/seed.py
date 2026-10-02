"""Demo seed: catalog products + stock + coupons (idempotent)."""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.commands import PublishProduct
from domain.coupon import Coupon, FIXED_AMOUNT, FREE_SHIPPING, PERCENTAGE
from domain.stock import Stock
from domain.value_objects import Money


SEED_PRODUCTS = [
    {"sku": "WIDGET-1", "name": "Gadget Pro", "price_minor": 10000,
     "description": "Flagship gadget with extended battery.", "weight_grams": 400,
     "stock": 25},
    {"sku": "WIDGET-2", "name": "Gadget Mini", "price_minor": 5000,
     "description": "Compact everyday gadget.", "weight_grams": 200, "stock": 40},
    {"sku": "CABLE-1", "name": "Braided Cable 2m", "price_minor": 1499,
     "description": "Fast-charge braided USB-C cable.", "weight_grams": 80, "stock": 100},
    {"sku": "STAND-1", "name": "Desk Stand", "price_minor": 2999,
     "description": "Aluminium desk stand.", "weight_grams": 600, "stock": 15},
]


def seed(c, currency: str = "USD") -> dict:
    """Seed products/stock/coupons; returns counts. Safe to run twice."""
    existing_skus = {p.sku for p in c.products.list()}
    created_products = 0
    for item in SEED_PRODUCTS:
        if item["sku"] in existing_skus:
            continue
        product = c.command_handlers.publish_product(PublishProduct(
            sku=item["sku"], name=item["name"],
            price=Money(item["price_minor"], currency),
            description=item["description"], weight_grams=item["weight_grams"]))
        # publish_product creates a zeroed stock row; top it up to demo levels
        try:
            stk = c.stocks.get(product.product_id)
            stk._available = item["stock"]
        except KeyError:
            c.stocks.add(Stock(product.product_id, item["stock"]))
        created_products += 1
    now = datetime.now()
    coupon_defs = [
        ("SAVE20", PERCENTAGE, 20, 100),
        ("FLAT5", FIXED_AMOUNT, Money(500, currency), 50),
        ("FREESHIP", FREE_SHIPPING, 0, None),
    ]
    created_coupons = 0
    for code, ctype, value, limit in coupon_defs:
        try:
            c.coupons.find_by_code(code)
        except KeyError:
            c.coupons.add(Coupon(code, ctype, value, now - timedelta(days=1),
                                 now + timedelta(days=30),
                                 usage_limit=limit))
            created_coupons += 1
    return {"products": created_products, "coupons": created_coupons,
            "totals": {"products": len(c.products.list()),
                       "coupons": len(c.coupons.list())}}
