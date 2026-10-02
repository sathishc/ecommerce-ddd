/**
 * Storefront hero: editorial headline + live store stats + promo card.
 * Reads come from the dashboard/stock read-models — pure queries.
 */
export default function Hero({ dashboard, stocks, onShopNow }) {
  const delivered = dashboard?.orders_by_status?.Delivered || 0;
  return (
    <section className="relative overflow-hidden rounded-3xl bg-ink text-white">
      <div
        className="pointer-events-none absolute inset-0 opacity-40"
        style={{
          background:
            'radial-gradient(600px 300px at 15% 10%, rgba(22,107,77,.55), transparent 60%), radial-gradient(500px 260px at 90% 90%, rgba(194,65,12,.4), transparent 60%)',
        }}
      />
      <div className="relative grid gap-8 p-8 sm:p-12 lg:grid-cols-[1.4fr_1fr]">
        <div className="animate-fade-up">
          <p className="mb-3 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-xs font-medium uppercase tracking-[0.16em] text-amber-200">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            Authorize now · pay on dispatch
          </p>
          <h1 className="font-display text-4xl font-semibold leading-[1.05] sm:text-5xl">
            Everyday tech goods, delivered to your door.
          </h1>
          <p className="mt-4 max-w-md text-[15px] leading-relaxed text-stone-300">
            One warehouse, one trusted courier, zero hassle. Pay when your parcel
            ships — and if it's not right, we'll collect it from your doorstep for free.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <button
              onClick={onShopNow}
              className="rounded-xl bg-white px-5 py-2.5 text-sm font-semibold text-ink transition hover:bg-amber-100"
            >
              Shop the collection
            </button>
            <div className="flex items-center gap-4 rounded-xl border border-white/15 bg-white/5 px-4 py-2 text-sm">
              <span><b className="font-display text-lg">{stocks.length}</b> <span className="text-stone-300">products</span></span>
              <span className="h-6 w-px bg-white/15" />
              <span><b className="font-display text-lg">{delivered}</b> <span className="text-stone-300">delivered</span></span>
              <span className="h-6 w-px bg-white/15" />
              <span><b className="font-display text-lg">{dashboard?.revenue?.display || '—'}</b> <span className="text-stone-300">served</span></span>
            </div>
          </div>
        </div>
        <div className="flex flex-col justify-center gap-3">
          {[
            ['Truck', 'Ships when you order', 'Stock reserved at checkout, payment captured only at carrier handoff.'],
            ['Refresh', 'Free doorstep returns', 'Our courier collects from your home — no labels, no post office.'],
            ['Shield', 'Pro-rata refunds', 'Goods + tax + a fair share of shipping, back on your card.'],
          ].map(([t, h, p]) => (
            <div key={t} className="rounded-2xl border border-white/10 bg-white/[0.06] p-4 backdrop-blur">
              <p className="text-sm font-semibold text-amber-100">{h}</p>
              <p className="mt-0.5 text-[13px] leading-relaxed text-stone-300">{p}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
