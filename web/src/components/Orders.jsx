import { useState } from 'react';
import { commands } from '../cqrs/commands.js';
import { EmptyState, Field, Money, StatusPill, Timeline, btnGhost, btnPrimary, inputCls } from './ui.jsx';

/**
 * My orders: customer order cards with delivery timeline, payment +
 * tracking, cancel / return actions, and a discreet "demo controls"
 * disclosure for the warehouse steps (ship / deliver) so the whole
 * backend flow stays demonstrable from the storefront.
 */

const STEPS = ['Placed', 'Paid', 'Shipped', 'Delivered'];
const STEP_IDX = { Placed: 0, Paid: 1, Shipped: 2, Delivered: 3, Refunded: 3 };

export default function Orders({ orders, shipments, payments, products, onRefresh, notify, onReturn }) {
  const [busy, setBusy] = useState(false);
  const [returnFor, setReturnFor] = useState(null);
  const [returnLines, setReturnLines] = useState('');
  const [reason, setReason] = useState('Changed my mind');
  const [shipForm, setShipForm] = useState({});

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

  const nameOf = (pid) => products.find((p) => p.product_id === pid)?.name || pid;
  const shipmentFor = (oid) => shipments.find((s) => s.order_id === oid);
  const terminal = (s) => (['Refunded', 'Cancelled', 'Expired'].includes(s) ? s : null);

  if (orders.length === 0) {
    return (
      <EmptyState
        title="No orders yet"
        hint="Your orders will appear here with live tracking — from reservation to doorstep."
      />
    );
  }

  return (
    <div className="space-y-4">
      {orders.map((o) => {
        const pay = payments[o.order_id];
        const shp = shipmentFor(o.order_id);
        const sf = shipForm[o.order_id] || { carrier: 'UPS', tracking: `TRK-${o.order_id.slice(-4).toUpperCase()}` };
        const setSf = (k, v) => setShipForm((m) => ({ ...m, [o.order_id]: { ...sf, [k]: v } }));
        return (
          <article key={o.order_id} className="animate-fade-up overflow-hidden rounded-2xl border border-stone-200 bg-white">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-100 px-5 py-3">
              <div className="flex items-center gap-2.5">
                <span className="font-mono text-sm font-bold">{o.order_id}</span>
                <StatusPill status={o.status} />
                {o.coupon_code && (
                  <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-moss">
                    {o.coupon_code}
                  </span>
                )}
              </div>
              <Money value={o.money_total} className="font-display text-xl font-semibold" />
            </div>

            <div className="grid gap-5 px-5 py-4 md:grid-cols-[1.2fr_1fr]">
              <div>
                <Timeline steps={STEPS} current={STEP_IDX[o.status] ?? 0} terminal={terminal(o.status)} />
                <ul className="mt-3 space-y-1.5">
                  {o.lines.map((l) => (
                    <li key={l.product_id} className="flex justify-between text-sm">
                      <span>{nameOf(l.product_id)} <span className="text-stone-400">× {l.quantity}</span></span>
                      <Money value={l.line_total} className="font-medium" />
                    </li>
                  ))}
                </ul>
                <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-stone-500">
                  {pay && <span>Payment <b className="text-ink">{pay.status}</b> · refunded {pay.refunded.display}</span>}
                  {shp && <span>Carrier <b className="text-ink">{shp.carrier || '—'}</b> · {shp.tracking_number || 'no tracking yet'}</span>}
                </div>
              </div>

              <div className="flex flex-col gap-2">
                {(o.status === 'Placed' || o.status === 'Paid') && (
                  <button disabled={busy} className={btnGhost} onClick={() => run(async () => {
                    await commands.cancelOrder(o.order_id, 'Changed my mind');
                    notify(`Order ${o.order_id} cancelled — authorization voided, stock released`);
                  })}>
                    Cancel order
                  </button>
                )}
                {o.status === 'Delivered' && returnFor !== o.order_id && (
                  <button className={btnPrimary} onClick={() => {
                    setReturnFor(o.order_id);
                    setReturnLines(o.lines.map((l) => `${l.product_id}:${l.quantity}`).join(', '));
                  }}>
                    Return items
                  </button>
                )}
                {returnFor === o.order_id && (
                  <div className="rounded-2xl border border-moss/30 bg-emerald-50/50 p-3">
                    <Field label="Items (product:qty, …)">
                      <input value={returnLines} onChange={(e) => setReturnLines(e.target.value)} className={inputCls} />
                    </Field>
                    <div className="mt-2">
                      <Field label="Reason">
                        <input value={reason} onChange={(e) => setReason(e.target.value)} className={inputCls} />
                      </Field>
                    </div>
                    <div className="mt-2 flex gap-2">
                      <button className={`${btnGhost} flex-1`} onClick={() => setReturnFor(null)}>Never mind</button>
                      <button disabled={busy} className={`${btnPrimary} flex-[2]`} onClick={() => run(async () => {
                        const lines = returnLines.split(',').map((s) => s.trim()).filter(Boolean)
                          .map((s) => { const [pid, q] = s.split(':'); return [pid.trim(), parseInt(q || '1', 10)]; });
                        const r = await commands.requestReturn(o.order_id, lines, reason);
                        setReturnFor(null);
                        notify(`Return ${r.result.return.rma_id} approved — our courier will collect it`);
                        onReturn();
                      })}>
                        Request return
                      </button>
                    </div>
                  </div>
                )}

                <details className="group rounded-2xl border border-dashed border-stone-300 bg-stone-50 px-3 py-2">
                  <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-stone-400 group-hover:text-stone-600">
                    Store demo controls
                  </summary>
                  <div className="space-y-2 py-2">
                    {o.status === 'Paid' && (
                      <>
                        <div className="grid grid-cols-2 gap-2">
                          <input value={sf.carrier} onChange={(e) => setSf('carrier', e.target.value)} className={inputCls} placeholder="Carrier" />
                          <input value={sf.tracking} onChange={(e) => setSf('tracking', e.target.value)} className={inputCls} placeholder="Tracking #" />
                        </div>
                        <button disabled={busy} className={`${btnGhost} w-full text-xs`} onClick={() => run(async () => {
                          await commands.handToCarrier(o.order_id, sf.carrier, sf.tracking);
                          notify(`Order ${o.order_id} handed to ${sf.carrier} — stock committed, payment captured`);
                        })}>
                          Hand to carrier (commit stock + capture)
                        </button>
                      </>
                    )}
                    {o.status === 'Shipped' && shp && (
                      <button disabled={busy} className={`${btnGhost} w-full text-xs`} onClick={() => run(async () => {
                        await commands.confirmDelivery(shp.shipment_id, o.order_id);
                        notify(`Order ${o.order_id} delivered — 30-day return window open`);
                      })}>
                        Confirm delivery (carrier scan)
                      </button>
                    )}
                    {!['Paid', 'Shipped'].includes(o.status) && (
                      <p className="text-xs text-stone-400">No warehouse actions available in status {o.status}.</p>
                    )}
                  </div>
                </details>
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}
