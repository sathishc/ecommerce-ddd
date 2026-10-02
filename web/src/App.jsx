import { useCallback, useEffect, useMemo, useState } from 'react';
import { queries } from './cqrs/queries.js';
import { commands } from './cqrs/commands.js';
import Header from './components/Header.jsx';
import Hero from './components/Hero.jsx';
import Catalog from './components/Catalog.jsx';
import CartDrawer from './components/CartDrawer.jsx';
import Orders from './components/Orders.jsx';
import Returns from './components/Returns.jsx';
import ActivityFeed from './components/ActivityFeed.jsx';

const CUSTOMER = 'guest-1';
const POLL_MS = 3000;

export default function App() {
  const [view, setView] = useState('shop');
  const [cartId, setCartId] = useState(null);
  const [cartOpen, setCartOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [data, setData] = useState({
    products: [], stocks: [], coupons: [], carts: [], orders: [],
    shipments: [], returns: [], pickups: [], payments: {},
    dashboard: null, events: [], eventTotal: 0,
  });
  const [toast, setToast] = useState(null);

  const notify = useCallback((msg, isErr = false) => {
    setToast({ msg, isErr });
    setTimeout(() => setToast(null), 4500);
  }, []);

  const refresh = useCallback(async (quiet = false) => {
    try {
      const [dashboard, products, stocks, coupons, carts, orders, shipments, returns, pickups, ev] =
        await Promise.all([
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
      const payments = {};
      await Promise.all(
        orders.map(async (o) => {
          try {
            payments[o.order_id] = await queries.orderPayment(o.order_id);
          } catch {
            /* unpaid leg */
          }
        })
      );
      setData({ dashboard, products, stocks, coupons, carts, orders, shipments, returns, pickups, payments, events: ev.events || [], eventTotal: ev.total || 0 });
    } catch (e) {
      if (!quiet) notify(`Store backend unreachable — run: python3 -m api.server. ${e.message}`, true);
    }
  }, [notify]);

  useEffect(() => {
    refresh();
  }, [refresh]);
  useEffect(() => {
    const t = setInterval(() => refresh(true), POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const cart = useMemo(
    () => data.carts.find((c) => c.cart_id === cartId && c.status === 'Open') || null,
    [data.carts, cartId]
  );
  const cartCount = useMemo(
    () => (cart ? cart.lines.reduce((n, l) => n + l.quantity, 0) : 0),
    [cart]
  );
  const activeCoupon = useMemo(
    () => data.coupons.find((c) => c.code === 'SAVE20')?.code || data.coupons[0]?.code,
    [data.coupons]
  );

  /** Storefront add-to-cart: lazily open a cart, then dispatch AddToCart. */
  const addToCart = async (productId) => {
    setAdding(true);
    try {
      let id = cart?.cart_id;
      if (!id) {
        const r = await commands.openCart(CUSTOMER);
        id = r.result.cart.cart_id;
        setCartId(id);
      }
      await commands.addToCart(id, productId, 1);
      await refresh();
      setCartOpen(true);
    } catch (e) {
      notify(e.message, true);
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="min-h-screen">
      <Header
        view={view}
        setView={setView}
        cartCount={cartCount}
        onCartOpen={() => setCartOpen(true)}
        orderCount={data.orders.length}
        returnCount={data.returns.length}
        couponCode={activeCoupon}
      />

      <main className="mx-auto max-w-6xl px-4 pb-16 sm:px-6">
        {view === 'shop' && (
          <>
            <div className="mt-6">
              <Hero
                dashboard={data.dashboard}
                stocks={data.stocks}
                onShopNow={() => document.getElementById('catalog')?.scrollIntoView({ behavior: 'smooth' })}
              />
            </div>
            <Catalog products={data.products} stocks={data.stocks} onAdd={addToCart} busy={adding} />
            <div className="mt-8 grid gap-5 md:grid-cols-3">
              {[
                ['01 · Reserve', 'Your items are reserved the moment you check out — all-or-nothing, never oversold.'],
                ['02 · Authorize', 'Your card is authorized at checkout and captured only when the courier takes your parcel.'],
                ['03 · Collect', 'Returns are picked up at your door, free. Refunds cover goods, tax and a fair shipping share.'],
              ].map(([h, p]) => (
                <div key={h} className="rounded-2xl border border-stone-200 bg-white p-5">
                  <p className="font-display text-lg font-semibold">{h}</p>
                  <p className="mt-1 text-sm leading-relaxed text-stone-500">{p}</p>
                </div>
              ))}
            </div>
          </>
        )}

        {view === 'orders' && (
          <div className="mt-6">
            <div className="mb-5">
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-moss">Track & manage</p>
              <h2 className="font-display text-3xl font-semibold">My orders</h2>
            </div>
            <Orders
              orders={data.orders}
              shipments={data.shipments}
              payments={data.payments}
              products={data.products}
              onRefresh={refresh}
              notify={notify}
              onReturn={() => setView('returns')}
            />
          </div>
        )}

        {view === 'returns' && (
          <div className="mt-6">
            <div className="mb-5">
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-moss">Doorstep collection</p>
              <h2 className="font-display text-3xl font-semibold">Returns & refunds</h2>
              <p className="mt-1 max-w-xl text-sm text-stone-500">
                Request a return on any delivered order. Our courier collects it from your
                door — you just need to be home and hand it over.
              </p>
            </div>
            <Returns
              returns={data.returns}
              pickups={data.pickups}
              products={data.products}
              onRefresh={refresh}
              notify={notify}
            />
          </div>
        )}

        <div className="mt-10">
          <ActivityFeed events={data.events} total={data.eventTotal} />
        </div>
      </main>

      <footer className="border-t border-stone-200 bg-white/60">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-5 text-[13px] text-stone-500 sm:px-6">
          <p>
            <b className="font-display text-ink">Northloop Supply Co.</b> · demo storefront on a CQRS API —
            writes via <span className="font-mono">POST /api/commands</span>, reads via <span className="font-mono">GET /api/…</span>
          </p>
          <p>
            {data.dashboard
              ? `${data.dashboard.counts.orders} orders · ${data.dashboard.revenue.display} served`
              : 'connecting to store backend…'}
          </p>
          <button
            onClick={async () => {
              try {
                const r = await commands.seed();
                await refresh();
                notify(`Demo data seeded — ${r.seed.totals.products} products, ${r.seed.totals.coupons} coupons`);
              } catch (e) {
                notify(e.message, true);
              }
            }}
            className="rounded-full border border-stone-300 bg-white px-3 py-1 text-xs font-medium text-stone-500 transition hover:border-moss hover:text-moss-deep"
            title="Restore the demo catalog, stock and coupons (idempotent)"
          >
            Reseed demo data
          </button>
        </div>
      </footer>

      <CartDrawer
        open={cartOpen}
        onClose={() => setCartOpen(false)}
        cart={cart}
        products={data.products}
        coupons={data.coupons}
        onRefresh={refresh}
        notify={notify}
        onPlaced={() => {
          setCartId(null);
          setCartOpen(false);
          setView('orders');
        }}
      />

      {toast && (
        <div className={`animate-fade-up fixed bottom-6 left-1/2 z-[60] max-w-md -translate-x-1/2 rounded-2xl px-5 py-3 text-sm font-medium shadow-xl ${
          toast.isErr ? 'bg-red-900 text-white' : 'bg-ink text-white'
        }`}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}
