/**
 * CQRS write side: every mutation is an intent dispatched as a Command.
 *
 * POST /api/commands { type, payload } -> exactly one backend handler
 * (CommandBus -> application.command_handlers, one atomic UoW each).
 * The client never mutates read-models directly; after a command succeeds
 * it re-runs the relevant queries (see queries.js).
 */
const API = '';

async function dispatch(type, payload = {}) {
  const res = await fetch(`${API}/api/commands`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, payload }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `command ${type} failed`);
  return data; // { ok, type, result, events }
}

export const commands = {
  // catalog
  publishProduct: (p) => dispatch('PublishProduct', p),
  // shop
  openCart: (customerRef) => dispatch('OpenCart', { customer_ref: customerRef }),
  addToCart: (cartId, productId, quantity = 1) =>
    dispatch('AddToCart', { cart_id: cartId, product_id: productId, quantity }),
  updateCartLine: (cartId, productId, quantity) =>
    dispatch('UpdateCartLine', { cart_id: cartId, product_id: productId, quantity }),
  removeCartLine: (cartId, productId) =>
    dispatch('RemoveCartLine', { cart_id: cartId, product_id: productId }),
  applyCoupon: (cartId, couponCode) =>
    dispatch('ApplyCoupon', { cart_id: cartId, coupon_code: couponCode }),
  removeCoupon: (cartId) => dispatch('RemoveCoupon', { cart_id: cartId }),
  placeOrder: (cartId, instrumentRef, destination) =>
    dispatch('PlaceOrder', { cart_id: cartId, instrument_ref: instrumentRef, destination }),
  cancelOrder: (orderId, reason) => dispatch('CancelOrder', { order_id: orderId, reason }),
  closeCart: (cartId) => dispatch('CloseCart', { cart_id: cartId }),
  // fulfillment
  handToCarrier: (orderId, carrier, trackingNumber) =>
    dispatch('HandToCarrier', { order_id: orderId, carrier, tracking_number: trackingNumber }),
  confirmDelivery: (shipmentId, orderId) =>
    dispatch('ConfirmDelivery', { shipment_id: shipmentId, order_id: orderId }),
  // returns (doorstep pickup)
  requestReturn: (orderId, lines, reason) =>
    dispatch('RequestReturn', { order_id: orderId, lines, reason }),
  schedulePickup: (rmaId, slot) => dispatch('SchedulePickup', { rma_id: rmaId, slot }),
  confirmPickup: (pickupId, photoRef) =>
    dispatch('ConfirmPickup', { pickup_id: pickupId, evidence: { photo_ref: photoRef } }),
  settleReturn: (rmaId) => dispatch('SettleReturn', { rma_id: rmaId }),
  seed: async () => {
    const res = await fetch(`${API}/api/seed`, { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'seed failed');
    return data;
  },
};
