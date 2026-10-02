"""Domain layer for the Ecommerce Order Platform (v1).

Package layout mirrors the bounded contexts in domain-model.md:
    value_objects  -> shared kernel (Money, Address, Discount, ...)
    product        -> Catalog
    stock          -> Inventory
    coupon / cart  -> Shop (Pricing)
    order          -> Order Management
    payment        -> Payment (generic)
    shipment/pickup-> Fulfillment
    return_rma     -> Returns
    orchestration  -> thin ACL layer that crosses contexts
"""
