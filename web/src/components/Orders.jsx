import { useState } from 'react';
import { commands } from '../cqrs/commands.js';

/**
 * Orders tab: order read-models plus fulfillment writes.
 * HandToCarrier commits stock + captures payment; ConfirmDelivery opens
 * the 30-day return window; CancelOrder voids pre-shipment only.
 */
export default function Orders({ orders, shipments, payments, onRefresh, notify }) {
  const [carrier, setCarrier] = useState('UPS');
  const [tracking, setTracking] = useState('TRK-1');
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

  const shipmentFor = (orderId) => shipments.find((s) => s.order_id === orderId);

  return (
    <div className="grid2">
      <section className="card">
        <h2>Orders <span className="tag">query: GET /api/orders</span></h2>
        <div className="row">
          <input value={carrier} onChange={(e) => setCarrier(e.target.value)} placeholder="carrier" />
          <input value={tracking} onChange={(e) => setTracking(e.target.value)} placeholder="tracking #" />
        </div>
        <ul className="list">
          {orders.map((o) => {
            const pay = payments[o.order_id];
            return (
              <li key={o.order_id} className="item col">
                <div><b>{o.order_id}</b> · <span className={`pill ${o.status}`}>{o.status}</span>
                  <span className="muted"> · {o.money_total.display}{o.coupon_code ? ` · coupon ${o.coupon_code}` : ''}</span></div>
                <div className="muted small">{o.lines.map((l) => `${l.product_id}×${l.quantity}`).join(', ')}
                  {pay && <span> · payment {pay.status} · refunded {pay.refunded.display}</span>}</div>
                <div className="row">
                  {o.status === 'Paid' && (
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.handToCarrier(o.order_id, carrier, `${tracking}-${o.order_id.slice(-4)}`);
                      notify(`shipped ${o.order_id} — stock committed, payment captured`);
                    })}>HandToCarrier (commit + capture)</button>
                  )}
                  {(o.status === 'Placed' || o.status === 'Paid') && (
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.cancelOrder(o.order_id, 'customer request');
                      notify(`order ${o.order_id} cancelled (void + release)`);
                    })}>CancelOrder</button>
                  )}
                  {o.status === 'Shipped' && shipmentFor(o.order_id) && (
                    <button disabled={busy} onClick={() => run(async () => {
                      await commands.confirmDelivery(shipmentFor(o.order_id).shipment_id, o.order_id);
                      notify(`order ${o.order_id} delivered — return window open`);
                    })}>ConfirmDelivery</button>
                  )}
                </div>
              </li>
            );
          })}
          {orders.length === 0 && <li className="muted">No orders yet — place one from the Shop tab.</li>}
        </ul>
      </section>
      <section className="card">
        <h2>Shipments <span className="tag">query: GET /api/shipments</span></h2>
        <ul className="list">
          {shipments.map((s) => (
            <li key={s.shipment_id} className="item col">
              <div><b>{s.shipment_id}</b> · <span className={`pill ${s.status}`}>{s.status}</span>
                <span className="muted"> · {s.carrier || '—'} · {s.tracking_number || 'no tracking yet'}</span></div>
              <div className="muted small">order {s.order_id} · {s.lines.map((l) => `${l.product_id}×${l.quantity}`).join(', ')}</div>
            </li>
          ))}
          {shipments.length === 0 && <li className="muted">No shipments yet.</li>}
        </ul>
      </section>
    </div>
  );
}
