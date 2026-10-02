/**
 * Storefront header: announcement bar, brand, nav, cart button.
 * Sticky with backdrop blur — the persistent frame of the shop.
 */
export default function Header({ view, setView, cartCount, onCartOpen, orderCount, returnCount, couponCode }) {
  const link = (id, label, count = 0) => (
    <button
      key={id}
      onClick={() => setView(id)}
      className={`relative rounded-full px-4 py-2 text-sm font-medium transition ${
        view === id ? 'bg-ink text-white' : 'text-stone-600 hover:bg-stone-200/70 hover:text-ink'
      }`}
    >
      {label}
      {count > 0 && (
        <span className="ml-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-clay px-1 text-[11px] font-bold text-white">
          {count}
        </span>
      )}
    </button>
  );

  return (
    <div className="sticky top-0 z-30">
      <div className="bg-ink text-center text-[13px] font-medium tracking-wide text-amber-100/90">
        <p className="px-4 py-2">
          Autumn offer — take 20% off everything with code{' '}
          <span className="rounded-md bg-white/10 px-2 py-0.5 font-mono font-bold text-white">
            {couponCode || 'SAVE20'}
          </span>{' '}
          · free doorstep returns, always
        </p>
      </div>
      <header className="border-b border-stone-200/80 bg-cream/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <button onClick={() => setView('shop')} className="flex items-center gap-2.5 text-left">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-moss font-display text-lg font-bold text-white">
              N
            </span>
            <span>
              <span className="block font-display text-lg font-semibold leading-none">Northloop</span>
              <span className="block text-[11px] font-medium uppercase tracking-[0.18em] text-stone-500">
                Supply Co.
              </span>
            </span>
          </button>
          <nav className="hidden items-center gap-1 rounded-full border border-stone-200 bg-white/70 p-1 sm:flex">
            {link('shop', 'Shop')}
            {link('orders', 'My orders', orderCount)}
            {link('returns', 'Returns', returnCount)}
          </nav>
          <div className="flex items-center gap-2">
            <nav className="flex items-center gap-1 sm:hidden">
              {link('shop', 'Shop')}
              {link('orders', `Orders${orderCount ? ` (${orderCount})` : ''}`)}
            </nav>
            <button
              onClick={onCartOpen}
              className="relative inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-sm font-semibold text-white transition hover:bg-stone-800"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="9" cy="21" r="1" />
                <circle cx="20" cy="21" r="1" />
                <path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6" />
              </svg>
              Cart
              {cartCount > 0 && (
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-400 px-1 text-[11px] font-bold text-ink">
                  {cartCount}
                </span>
              )}
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1 overflow-x-auto px-4 pb-2 sm:hidden">
          {link('returns', `Returns${returnCount ? ` (${returnCount})` : ''}`)}
        </div>
      </header>
    </div>
  );
}
