import { useState } from 'react';
import { commands } from '../cqrs/commands.js';
import { DEFAULT_ADDRESS } from '../cqrs/queries.js';
import { Field, Money, StatusPill, btnAccent, btnGhost, inputCls } from './ui.jsx';

/**
 * Cart drawer: slide-over with lines, coupon, quote, and checkout.
 * Writes: UpdateCartLine / RemoveCartLine / ApplyCoupon / PlaceOrder.
 * Everything displayed (lines, quote) is the cart read-model.
 */
export default function CartDrawer({ open, onClose, cart, products, coupons, onRefresh, notify, onPlaced }) {
  const [coupon, setCoupon] = useState('');
  const [busy, setBusy] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [address, setAddress] = useState({ ...DEFAULT_ADDRESS });
  const [done, setDone] = useState(null);

  if (!open) return null;

  const nameOf = (pid) => products.find((p) => p.product_id === pid)?.name || pid;
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

  const setAddr = (k, v) => setAddress((a) => ({ ...a, [k]: v }));
  const addrComplete = address.line1 && address.city && address.postal_code && address.country;

  return (
    <div className="fixed inset-0 z-50">
      <div className="animate-fade-in absolute inset-0 bg-ink/45 backdrop-blur-[2px]" onClick={onClose} />
      <aside className="animate-slide-in absolute right-0 top-0 flex h-full w-full max-w-md flex-col bg-cream shadow-2xl">
        <div className="flex items-center justify-between border-b border-stone-200 px-5 py-4">
          <h2 className="font-display text-xl font-semibold">
            {done ? 'Order confirmed' : placing ? 'Checkout' : 'Your cart'}
          </h2>
          <button onClick={onClose} className="rounded-full p-2 text-stone-500 transition hover:bg-stone-200/70 hover:text-ink">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>

        {done ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
            <span className="flex h-14 w-14 items-center justify-center rounded-full bg-emerald-100 text-2xl text-emerald-800">✓</span>
            <p className="font-display text-2xl">Thank you!</p>
            <p className="text-sm text-stone-500">
              Order <b className="font-mono text-ink">{done.orderId}</b> is placed for{' '}
              <b className="text-ink">{done.total}</b>. Your card is only authorized —
              we capture payment when your parcel ships.
            </p>
            <button className={btnAccent} onClick={() => { setDone(null); onPlaced(); }}>
              Track in My orders
            </button>
          </div>
        ) : !cart || cart.lines.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center">
            <p className="font-display text-2xl">Your cart is empty</p>
            <p className="text-sm text-stone-500">Beautiful goods await. Start with the collection.</p>
            <button className={btnGhost} onClick={onClose}>Continue shopping</button>
          </div>
        ) : (
          <>
            <div className="slim-scroll flex-1 space-y-3 overflow-y-auto px-5 py-4">
              {cart.lines.map((l) => (
                <div key={l.product_id} className="flex items-center gap-3 rounded-2xl border border-stone-200 bg-white p-3">
                  <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-parchment font-display text-xl font-bold text-stone-600">
                    {nameOf(l.product_id).charAt(0)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold">{nameOf(l.product_id)}</p>
                    <p className="font-mono text-[11px] text-stone-400">{l.product_id}</p>
                    <div className="mt-1.5 inline-flex items-center gap-2 rounded-full border border-stone-200 px-1 py-0.5">
                      <button disabled={busy} className="px-2 font-bold text-stone-500 hover:text-ink" onClick={() => run(async () => {
                        if (l.quantity <= 1) await commands.removeCartLine(cart.cart_id, l.product_id);
                        else await commands.updateCartLine(cart.cart_id, l.product_id, l.quantity - 1);
                      })}>−</button>
                      <span className="text-sm font-semibold">{l.quantity}</span>
                      <button disabled={busy} className="px-2 font-bold text-stone-500 hover:text-ink" onClick={() => run(async () => {
                        await commands.updateCartLine(cart.cart_id, l.product_id, l.quantity + 1);
                      })}>+</button>
                    </div>
                  </div>
                  <Money value={l.line_total} className="text-sm font-bold" />
                </div>
              ))}

              {!placing ? (
                <div className="rounded-2xl border border-dashed border-moss/40 bg-emerald-50/60 p-3">
                  {cart.applied_coupon_code ? (
                    <div className="flex items-center justify-between">
                      <p className="text-sm">
                        Coupon <b className="font-mono">{cart.applied_coupon_code}</b> applied
                      </p>
                      <button disabled={busy} className="text-xs font-semibold text-clay hover:underline" onClick={() => run(async () => {
                        await commands.removeCoupon(cart.cart_id);
                      })}>Remove</button>
                    </div>
                  ) : (
                    <>
                      <div className="flex gap-2">
                        <input value={coupon} onChange={(e) => setCoupon(e.target.value.toUpperCase())}
                          placeholder="Coupon code (try SAVE20)" className={inputCls} />
                        <button disabled={busy || !coupon} className={btnGhost} onClick={() => run(async () => {
                          await commands.applyCoupon(cart.cart_id, coupon);
                          setCoupon('');
                          notify(`Coupon ${coupon} applied`);
                        })}>Apply</button>
                      </div>
                      {coupons.length > 0 && (
                        <p className="mt-2 text-xs text-stone-500">
                          Available: {coupons.filter((c) => c.status === 'Active').map((c) => (
                            <button key={c.code} className="mr-1 font-mono font-semibold text-moss hover:underline"
                              onClick={() => setCoupon(c.code)}>{c.code}</button>
                          ))}
                        </p>
                      )}
                    </>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-2 rounded-2xl border border-stone-200 bg-white p-3">
                  <Field label="Street"><input value={address.line1} onChange={(e) => setAddr('line1', e.target.value)} className={`${inputCls} col-span-2`} /></Field>
                  <Field label="City"><input value={address.city} onChange={(e) => setAddr('city', e.target.value)} className={inputCls} /></Field>
                  <Field label="Postal code"><input value={address.postal_code} onChange={(e) => setAddr('postal_code', e.target.value)} className={inputCls} /></Field>
                  <Field label="Country"><input value={address.country} onChange={(e) => setAddr('country', e.target.value)} className={inputCls} /></Field>
                  <p className="col-span-2 flex items-center gap-2 rounded-xl bg-stone-100 px-3 py-2 text-xs text-stone-600">
                    <span className="font-bold">Visa •••• 4242</span> demo card — authorized now, captured at dispatch
                  </p>
                </div>
              )}
            </div>

            <div className="border-t border-stone-200 bg-white px-5 py-4">
              {cart.quote && (
                <dl className="space-y-1 text-sm">
                  <div className="flex justify-between text-stone-500"><dt>Subtotal</dt><dd><Money value={cart.quote.subtotal} /></dd></div>
                  <div className="flex justify-between text-moss"><dt>Discount ({cart.quote.discount.rule})</dt><dd>−<Money value={cart.quote.discount.amount} /></dd></div>
                  <div className="flex justify-between text-stone-500"><dt>Shipping</dt><dd><Money value={cart.quote.shipping_fee} /></dd></div>
                  <div className="flex justify-between border-t border-stone-100 pt-2 text-base font-bold"><dt>Total</dt><dd><Money value={cart.quote.total} /></dd></div>
                </dl>
              )}
              {!placing ? (
                <button disabled={busy} className={`${btnAccent} mt-3 w-full !py-3 text-[15px]`} onClick={() => setPlacing(true)}>
                  Checkout · <Money value={cart.quote?.total} />
                </button>
              ) : (
                <div className="mt-3 flex gap-2">
                  <button disabled={busy} className={`${btnGhost} flex-1`} onClick={() => setPlacing(false)}>Back</button>
                  <button disabled={busy || !addrComplete} title={!addrComplete ? 'Complete the address' : 'PlaceOrder'}
                    className={`${btnAccent} flex-[2] !py-3`} onClick={() => run(async () => {
                      const r = await commands.placeOrder(cart.cart_id, 'card-1', address);
                      setDone({ orderId: r.result.order.order_id, total: r.result.order.money_total.display });
                      setPlacing(false);
                    })}>
                    Place order
                  </button>
                </div>
              )}
              <p className="mt-2 flex items-center justify-center gap-2 text-[11px] text-stone-400">
                <StatusPill status="Paid" /> authorized at checkout · captured when your parcel ships
              </p>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
