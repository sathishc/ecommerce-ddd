import { useCallback, useEffect, useState } from 'react';
import { queries } from './cqrs/queries.js';
import { commands } from './cqrs/commands.js';
import Shop from './components/Shop.jsx';
import Orders from './components/Orders.jsx';
import Returns from './components/Returns.jsx';
import EventsFeed from './components/EventsFeed.jsx';

const TABS = ['Shop', 'Orders', 'Returns', 'Events'];

export default function App() {
  const [tab, setTab] = useState('Shop');
  const [cartId, setCartId] = useState(null);
  const [data, setData] = useState({ products: [], stocks: [], coupons: [], carts: [], orders: [], shipments: [], returns: [], pickups: [], dashboard: null, events: [], eventTotal: 0 });
  const [toast, setToast] = useState(null);

  const notify = useCallback((msg, isErr = false) => {
    setToast({ msg, isErr });
    setTimeout(() => setToast(null), 4500);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [dashboard, products, stocks, coupons, carts, orders, shipments, returns, pickups, ev] = await Promise.all([
        queries.dashboard().catch(() => null),
        queries.products().catch(() => []),
        queries.stocks().catch(() => []),
        queries.coupons().catch(() => []),
        queries.carts().catch(() => []),
        queries.orders().catch(() => []),
        queries.shipments().catch(() => []),
        queries.returns().catch(() => []),
        queries.pickups().catch(() => []),
        queries.events(80).catch(() => ({ events: [], total: 0 })),
      ]);
      // hydrate per-order payments (read-model join done client-side)
      const payments = {};
      await Promise.all(orders.map(async (o) => {
        try { payments[o.order_id] = await queries.orderPayment(o.order_id); } catch { /* unpaid */ }
      }));
      setData({ dashboard, products, stocks, coupons, carts, orders, shipments, returns, pickups, payments, events: ev.events || [], eventTotal: ev.total || 0 });
    } catch (e) {
      notify(`API unreachable — is the backend running? (python3 -m api.server). ${e.message}`, true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (tab !== 'Events') return;
    const t = setInterval(async () => {
      try {
        const ev = await queries.events(80);
        setData((d) => ({ ...d, events: ev.events || [], eventTotal: ev.total || 0 }));
      } catch { /* offline */ }
    }, 3000);
    return () => clearInterval(t);
  }, [tab]);

  const cart = cartId ? data.carts.find((c) => c.cart_id === cartId) || null : null;

  return (
    <div className="app">
      <header>
        <div>
          <h1>Ecommerce CQRS demo</h1>
          <p className="muted">writes = <code>POST /api/commands</code> (CommandBus → one atomic UoW) · reads = <code>GET /api/…</code> (repository read-models)</p>
        </div>
        <div className="row">
          {data.dashboard && (
            <span className="muted small">
              orders {data.dashboard.counts.orders} · revenue {data.dashboard.revenue.display} · {Object.entries(data.dashboard.orders_by_status).map(([k, v]) => `${k}:${v}`).join(' ')}
            </span>
          )}
          <button onClick={async () => { try { await commands.seed(); await refresh(); notify('demo data seeded'); } catch (e) { notify(e.message, true); } }}>Seed demo data</button>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? 'active' : ''} onClick={() => { setTab(t); refresh(); }}>
            {t}{t === 'Orders' && data.orders.length ? ` (${data.orders.length})` : ''}{t === 'Returns' && data.returns.length ? ` (${data.returns.length})` : ''}
          </button>
        ))}
        <span className="muted small stock">stock: {data.stocks.map((s) => `${s.product_name} ${s.available}`).join(' · ')}</span>
      </nav>

      {toast && <div className={`toast ${toast.isErr ? 'err' : ''}`}>{toast.msg}</div>}

      <main>
        {tab === 'Shop' && <Shop products={data.products} coupons={data.coupons} cart={cart} onRefresh={refresh} setCartId={setCartId} notify={notify} />}
        {tab === 'Orders' && <Orders orders={data.orders} shipments={data.shipments} payments={data.payments || {}} onRefresh={refresh} notify={notify} />}
        {tab === 'Returns' && <Returns orders={data.orders} returns={data.returns} pickups={data.pickups} onRefresh={refresh} notify={notify} />}
        {tab === 'Events' && <EventsFeed events={data.events} total={data.eventTotal} />}
      </main>

      <footer className="muted small">
        Happy path: Shop → PlaceOrder (authorize) → HandToCarrier (commit + capture) → ConfirmDelivery ·
        Return path: RequestReturn → SchedulePickup → ConfirmPickup (scan + photo) → SettleReturn (pro-rata).
      </footer>
    </div>
  );
}
