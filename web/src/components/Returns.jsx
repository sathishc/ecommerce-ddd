import { useState } from 'react';
import { commands } from '../cqrs/commands.js';

function tomorrowISO() {
  const d = new Date(Date.now() + 24 * 3600 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Returns tab: the doorstep-pickup flow.
 * RequestReturn (customer) -> SchedulePickup (staff) -> ConfirmPickup
 * (courier scan + photo) -> SettleReturn (pro-rata goods+tax+shipping).
 */
export default function Returns({ orders, returns, pickups, onRefresh, notify }) {
  const [orderId, setOrderId] = useState('');
  const [linesText, setLinesText] = useState('');
  const [reason, setReason] = useState('too big');
  const [slot, setSlot] = useState(tomorrowISO());
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

  const delivered = orders.filter((o) => o.status === 'Delivered');
  const pickupFor = (rmaId) => pickups.find((p) => p.return_id === rmaId);

  return (
    <div className="grid2">
      <section className="card">
        <h2>Request a return <span className="tag">command: RequestReturn</span></h2>
        <p className="muted small">Only <b>Delivered</b> orders, within 30 days, qty ≤ shipped. The courier collects at your door — no label.</p>
        <div className="col gap">
          <select value={orderId} onChange={(e) => {
            const id = e.target.value;
            setOrderId(id);
            const o = orders.find((x) => x.order_id === id);
            if (o) setLinesText(o.lines.map((l) => `${l.product_id}:${l.quantity}`).join(', '));
          }}>
            <option value="">— pick a delivered order —</option>
            {delivered.map((o) => <option key={o.order_id} value={o.order_id}>{o.order_id} · {o.money_total.display}</option>)}
          </select>
          <input value={linesText} onChange={(e) => setLinesText(e.target.value)} placeholder="product_id:qty, ..." />
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="reason" />
          <button className="primary" disabled={busy || !orderId} onClick={() => run(async () => {
            const lines = linesText.split(',').map((s) => s.trim()).filter(Boolean)
              .map((s) => { const [pid, q] = s.split(':'); return [pid.trim(), parseInt(q || '1', 10)]; });
            const r = await commands.requestReturn(orderId, lines, reason);
            notify(`RMA ${r.result.return.rma_id} approved`);
          })}>RequestReturn</button>
        </div>
        <h3>Schedule pickup <span className="tag">command: SchedulePickup</span></h3>
        <div className="row">
          <input type="datetime-local" value={slot} onChange={(e) => setSlot(e.target.value)} />
        </div>
      </section>

      <section className="card">
        <h2>RMAs + pickups <span className="tag">query: GET /api/returns /api/pickups</span></h2>
        <ul className="list">
          {returns.map((r) => {
            const p = pickupFor(r.rma_id);
            return (
              <li key={r.rma_id} className="item col">
                <div><b>{r.rma_id}</b> · <span className={`pill ${r.status}`}>{r.status}</span>
                  <span className="muted"> · order {r.order_id} · {r.lines.map((l) => `${l.product_id}×${l.quantity}`).join(', ')}</span></div>
                {r.settlement && <div className="muted small">refunded goods {r.settlement.goods.display} + tax {r.settlement.tax.display} + ship {r.settlement.shipping.display} = <b>{r.settlement.total.display}</b>{r.settlement.is_full ? ' (full)' : ' (partial)'}</div>}
                <div className="row">
                  {!p && (
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.schedulePickup(r.rma_id, new Date(slot).toISOString());
                      notify(`pickup scheduled for ${r.rma_id}`);
                    })}>SchedulePickup</button>
                  )}
                  {p && p.status === 'EnRoute' && (
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.confirmPickup(p.pickup_id, `photo-${p.pickup_id}`);
                      notify(`pickup ${p.pickup_id} confirmed (scan + photo)`);
                    })}>ConfirmPickup (scan + photo)</button>
                  )}
                  {p && <span className="muted small">pickup {p.pickup_id} · {p.status}{p.evidence ? ` · ${p.evidence.photo_ref}` : ''}</span>}
                  {(r.status === 'GoodsReceived' || r.status === 'PickedUp') && (
                    <button disabled={busy} onClick={() => run(async () => {
                      const res = await commands.settleReturn(r.rma_id);
                      notify(`settled: ${res.result.breakdown.total.display} refunded`);
                    })}>SettleReturn (pro-rata)</button>
                  )}
                </div>
              </li>
            );
          })}
          {returns.length === 0 && <li className="muted">No returns yet.</li>}
        </ul>
      </section>
    </div>
  );
}
