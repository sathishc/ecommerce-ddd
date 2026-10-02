import { useMemo, useState } from 'react';
import { Money, StatusPill, btnGhost } from './ui.jsx';

/**
 * Catalog: searchable, sortable product grid.
 * "Add" dispatches AddToCart (auto-opening a cart first) — the write side.
 * Stock levels + prices are pure read-models.
 */

const ART = [
  'from-emerald-800 via-emerald-600 to-teal-400',
  'from-stone-800 via-stone-600 to-amber-500',
  'from-indigo-900 via-indigo-600 to-sky-400',
  'from-orange-900 via-clay to-amber-400',
  'from-teal-900 via-teal-600 to-lime-400',
  'from-neutral-800 via-neutral-600 to-stone-400',
];

function artFor(sku, i) {
  return ART[(sku?.length || 0 + i) % ART.length];
}

export default function Catalog({ products, stocks, onAdd, busy }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('featured');

  const stockById = useMemo(() => Object.fromEntries(stocks.map((s) => [s.product_id, s])), [stocks]);

  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = products.filter(
      (p) => !q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)
    );
    if (sort === 'price-asc') list = [...list].sort((a, b) => a.price.minor - b.price.minor);
    if (sort === 'price-desc') list = [...list].sort((a, b) => b.price.minor - a.price.minor);
    return list;
  }, [products, query, sort]);

  return (
    <section id="catalog" className="mt-10">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-moss">The collection</p>
          <h2 className="font-display text-3xl font-semibold">Shop all goods</h2>
        </div>
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search goods…"
            className="w-44 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm outline-none transition focus:border-moss focus:ring-2 focus:ring-emerald-100"
          />
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            className="rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm outline-none focus:border-moss"
          >
            <option value="featured">Featured</option>
            <option value="price-asc">Price · low to high</option>
            <option value="price-desc">Price · high to low</option>
          </select>
        </div>
      </div>

      {items.length === 0 && (
        <p className="rounded-2xl border border-dashed border-stone-300 bg-white/60 px-6 py-10 text-center text-sm text-stone-500">
          Nothing matches “{query}”. Try another search.
        </p>
      )}

      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {items.map((p, i) => {
          const stock = stockById[p.product_id];
          const low = stock && stock.available <= 5;
          const out = stock && stock.available === 0;
          return (
            <article
              key={p.product_id}
              className="group flex flex-col overflow-hidden rounded-2xl border border-stone-200/90 bg-white shadow-[0_1px_2px_rgba(28,25,23,0.05)] transition hover:-translate-y-0.5 hover:shadow-[0_12px_30px_-12px_rgba(28,25,23,0.25)]"
            >
              <div className={`relative flex h-44 items-center justify-center bg-gradient-to-br ${artFor(p.sku, i)}`}>
                <span className="font-display text-7xl font-bold text-white/90 drop-shadow-lg transition group-hover:scale-110">
                  {p.name.charAt(0)}
                </span>
                <span className="absolute left-3 top-3 rounded-full bg-black/30 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-white backdrop-blur">
                  {p.sku}
                </span>
                {low && !out && (
                  <span className="absolute bottom-3 right-3 rounded-full bg-amber-400 px-2.5 py-0.5 text-[11px] font-bold text-ink">
                    Only {stock.available} left
                  </span>
                )}
                {out && (
                  <span className="absolute bottom-3 right-3 rounded-full bg-ink px-2.5 py-0.5 text-[11px] font-bold text-white">
                    Out of stock
                  </span>
                )}
              </div>
              <div className="flex flex-1 flex-col gap-1.5 p-4">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-display text-lg font-semibold leading-tight">{p.name}</h3>
                  <Money value={p.price} className="whitespace-nowrap text-[15px] font-bold" />
                </div>
                <p className="line-clamp-2 text-[13px] leading-relaxed text-stone-500">{p.description}</p>
                <div className="mt-1 flex items-center gap-2 text-xs text-stone-500">
                  <StatusPill status={p.status} />
                  {stock && <span>{stock.available} in stock</span>}
                </div>
                <button
                  disabled={busy || out}
                  onClick={() => onAdd(p.product_id)}
                  className={`${btnGhost} mt-2 w-full font-semibold transition group-hover:border-ink group-hover:bg-ink group-hover:text-white disabled:hover:border-stone-300 disabled:hover:bg-white disabled:hover:text-stone-700`}
                >
                  {out ? 'Notify me' : 'Add to cart'}
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
