import { useState } from 'react';
import { commands } from '../cqrs/commands.js';
import { EmptyState, Field, Money, StatusPill, Timeline, btnGhost, btnPrimary, inputCls } from './ui.jsx';

/**
 * Returns: RMA cards with pickup timeline + refund breakdown.
 * The customer only taps "request" — courier/ops steps live under a
 * "courier demo controls" disclosure so the doorstep-pickup flow
 * (schedule → scan+photo → pro-rata settle) stays demonstrable.
 */

const STEPS = ['Approved', 'Pickup', 'Collected', 'Refunded'];
function stepIndex(status) {
  switch (status) {
    case 'Requested':
    case 'Approved': return 0;
    case 'PickupScheduled': return 1;
    case 'PickedUp':
    case 'GoodsReceived': return 2;
    case 'Refunded': return 3;
    default: return 0;
  }
}
function tomorrowLocal() {
  const d = new Date(Date.now() + 24 * 3600 * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function Returns({ returns, pickups, products, onRefresh, notify }) {
  const [busy, setBusy] = useState(false);
  const [slots, setSlots] = useState({});

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
  const pickupFor = (rmaId) => pickups.find((p) => p.return_id === rmaId);
  const slotFor = (rmaId) => slots[rmaId] || tomorrowLocal();

  if (returns.length === 0) {
    return (
      <EmptyState
        title="No returns"
        hint="Changed your mind? Request a return from any delivered order — our courier collects it from your doorstep, free."
      />
    );
  }

  return (
    <div className="space-y-4">
      {returns.map((r) => {
        const p = pickupFor(r.rma_id);
        const rejected = r.status === 'Rejected';
        return (
          <article key={r.rma_id} className="animate-fade-up overflow-hidden rounded-2xl border border-stone-200 bg-white">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-100 px-5 py-3">
              <div className="flex items-center gap-2.5">
                <span className="font-mono text-sm font-bold">{r.rma_id}</span>
                <StatusPill status={r.status} />
                {r.is_complete && <span className="text-xs font-medium text-stone-400">full return</span>}
              </div>
              <span className="text-[13px] text-stone-500">order <span className="font-mono">{r.order_id}</span></span>
            </div>

            <div className="grid gap-5 px-5 py-4 md:grid-cols-[1.2fr_1fr]">
              <div>
                {!rejected && <Timeline steps={STEPS} current={stepIndex(r.status)} terminal={r.status === 'Refunded' ? null : null} />}
                <ul className="mt-3 space-y-1.5">
                  {r.lines.map((l) => (
                    <li key={l.product_id} className="flex justify-between text-sm">
                      <span>{nameOf(l.product_id)} <span className="text-stone-400">× {l.quantity}</span></span>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-[13px] text-stone-500">Reason: <i>“{r.reason}”</i></p>
                {p && (
                  <div className="mt-3 rounded-xl bg-stone-50 px-3 py-2 text-[13px] text-stone-600">
                    Courier pickup <b className="font-mono text-ink">{p.pickup_id}</b> · <StatusPill status={p.status} />
                    <span className="mt-1 block text-xs text-stone-400">
                      slot {p.scheduled_slot ? new Date(p.scheduled_slot).toLocaleString() : '—'}
                      {p.evidence ? ` · photo ${p.evidence.photo_ref}` : ' · photo required at handover'}
                    </span>
                  </div>
                )}
                {r.settlement && (
                  <dl className="mt-3 space-y-1 rounded-xl bg-emerald-50/70 px-3 py-2 text-sm">
                    <div className="flex justify-between text-stone-600"><dt>Goods refund</dt><dd><Money value={r.settlement.goods} /></dd></div>
                    <div className="flex justify-between text-stone-600"><dt>Tax refund</dt><dd><Money value={r.settlement.tax} /></dd></div>
                    <div className="flex justify-between text-stone-600"><dt>Shipping refund</dt><dd><Money value={r.settlement.shipping} /></dd></div>
                    <div className="flex justify-between border-t border-emerald-100 pt-1 font-bold text-moss-deep">
                      <dt>Total refunded</dt><dd><Money value={r.settlement.total} /></dd>
                    </div>
                  </dl>
                )}
              </div>

              <div className="flex flex-col gap-2">
                {!p && !rejected && (
                  <div className="rounded-2xl border border-stone-200 p-3">
                    <Field label="Pickup window">
                      <input type="datetime-local" value={slotFor(r.rma_id)}
                        onChange={(e) => setSlots((m) => ({ ...m, [r.rma_id]: e.target.value }))}
                        className={inputCls} />
                    </Field>
                    <button disabled={busy} className={`${btnPrimary} mt-2 w-full`} onClick={() => run(async () => {
                      await commands.schedulePickup(r.rma_id, new Date(slotFor(r.rma_id)).toISOString());
                      notify(`Courier scheduled for ${r.rma_id} — free doorstep collection`);
                    })}>
                      Schedule free pickup
                    </button>
                  </div>
                )}

                <details className="group rounded-2xl border border-dashed border-stone-300 bg-stone-50 px-3 py-2">
                  <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-stone-400 group-hover:text-stone-600">
                    Courier demo controls
                  </summary>
                  <div className="space-y-2 py-2">
                    {p && p.status === 'EnRoute' && (
                      <button disabled={busy} className={`${btnGhost} w-full text-xs`} onClick={() => run(async () => {
                        await commands.confirmPickup(p.pickup_id, `photo-${p.pickup_id}`);
                        notify(`Pickup ${p.pickup_id} confirmed — courier scan + photo received`);
                      })}>
                        Confirm pickup (door scan + photo)
                      </button>
                    )}
                    {['PickedUp', 'GoodsReceived'].includes(r.status) && (
                      <button disabled={busy} className={`${btnGhost} w-full text-xs`} onClick={() => run(async () => {
                        const res = await commands.settleReturn(r.rma_id);
                        notify(`Refunded ${res.result.breakdown.total.display} — goods + tax + shipping share`);
                      })}>
                        Settle return (pro-rata refund)
                      </button>
                    )}
                    {!((p && p.status === 'EnRoute') || ['PickedUp', 'GoodsReceived'].includes(r.status)) && (
                      <p className="text-xs text-stone-400">Nothing for the courier to do in status {r.status}.</p>
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
