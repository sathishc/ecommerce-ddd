import { useState } from 'react';
import { commands } from '../cqrs/commands.js';
import { DEFAULT_ADDRESS } from '../cqrs/queries.js';

/**
 * Shop tab: catalog query on the left, cart write-model on the right.
 * Demonstrates the write side (OpenCart/AddToCart/ApplyCoupon/PlaceOrder)
 * and the derived quote read-model (subtotal - discount + tax + shipping).
 */
export default function Shop({ products, coupons, cart, onRefresh, setCartId, notify }) {
  const [customer, setCustomer] = useState('cust-1');
  const [couponCode, setCouponCode] = useState('SAVE20');
  const [busy, setBusy] = useState(false);

  const run = async (fn) => {
    setBusy(true);
    try {
      await fn();
      await onRefresh();
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid2">
      <section className="card">
        <h2>Catalog <span className="tag">query: GET /api/products</span></h2>
        <div className="row">
          <input value={customer} onChange={(e) => setCustomer(e.target.value)} placeholder="customer ref" />
          <button disabled={busy} onClick={() => run(async () => {
            const r = await commands.openCart(customer || 'cust-1');
            setCartId(r.result.cart.cart_id);
            notify(`cart ${r.result.cart.cart_id} opened`);
          })}>New cart (OpenCart)</button>
        </div>
        <ul className="list">
          {products.map((p) => (
            <li key={p.product_id} className="item">
              <div>
                <b>{p.name}</b> <span className="muted">{p.sku} · {p.price.display}</span>
                <div className="muted small">{p.description}</div>
              </div>
              <button disabled={busy || !cart} title={!cart ? 'open a cart first' : 'AddToCart'}
                onClick={() => run(async () => { await commands.addToCart(cart.cart_id, p.product_id, 1); })}>
                Add
              </button>
            </li>
          ))}
        </ul>
        <h3>Coupons <span className="tag">query: GET /api/coupons</span></h3>
        <ul className="list">
          {coupons.map((c) => (
            <li key={c.code} className="item">
              <span><b>{c.code}</b> <span className="muted">{c.type} · {c.status} · used {c.redemptions}{c.usage_limit ? `/${c.usage_limit}` : ''}</span></span>
              <button disabled={busy || !cart} onClick={() => run(async () => {
                await commands.applyCoupon(cart.cart_id, c.code);
              })}>Apply</button>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>Cart <span className="tag">query: GET /api/carts/:id</span></h2>
        {!cart && <p className="muted">No cart yet — open one to start the happy path.</p>}
        {cart && (
          <>
            <p><b>{cart.cart_id}</b> · {cart.status} · {cart.customer_ref}
              {cart.applied_coupon_code && <span> · coupon <b>{cart.applied_coupon_code}</b></span>}</p>
            <ul className="list">
              {cart.lines.map((l) => (
                <li key={l.product_id} className="item">
                  <span>{l.product_id} × {l.quantity} <span className="muted">{l.line_total.display}</span></span>
                  <span className="row">
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.updateCartLine(cart.cart_id, l.product_id, l.quantity + 1);
                    })}>+1</button>
                    <button disabled={busy} onClick={() => run(async () => {
                      if (l.quantity <= 1) await commands.removeCartLine(cart.cart_id, l.product_id);
                      else await commands.updateCartLine(cart.cart_id, l.product_id, l.quantity - 1);
                    })}>−1</button>
                  </span>
                </li>
              ))}
            </ul>
            {cart.quote && (
              <table className="money">
                <tbody>
                  <tr><td>Subtotal</td><td>{cart.quote.subtotal.display}</td></tr>
                  <tr><td>Discount ({cart.quote.discount.rule})</td><td>−{cart.quote.discount.amount.display}</td></tr>
                  <tr><td>Tax</td><td>{cart.quote.tax.display}</td></tr>
                  <tr><td>Shipping</td><td>{cart.quote.shipping_fee.display}</td></tr>
                  <tr className="total"><td>Total</td><td>{cart.quote.total.display}</td></tr>
                </tbody>
              </table>
            )}
            <div className="row">
              <input value={couponCode} onChange={(e) => setCouponCode(e.target.value.toUpperCase())} placeholder="COUPON" />
              <button disabled={busy} onClick={() => run(async () => {
                await commands.applyCoupon(cart.cart_id, couponCode);
              })}>ApplyCoupon</button>
              <button disabled={busy} onClick={() => run(async () => {
                await commands.removeCoupon(cart.cart_id);
              })}>RemoveCoupon</button>
            </div>
            <div className="row">
              <button className="primary" disabled={busy} onClick={() => run(async () => {
                const r = await commands.placeOrder(cart.cart_id, 'card-1', DEFAULT_ADDRESS);
                setCartId(null);
                notify(`order ${r.result.order.order_id} placed · ${r.result.order.money_total.display} (authorize, not capture)`);
              })}>PlaceOrder → authorize (1 Main St)</button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
