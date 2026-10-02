/**
 * Shared storefront primitives: status pills, timelines, money, empty states.
 * Pure presentation — no API calls here.
 */

const PILL_STYLES = {
  Open: 'bg-sky-100 text-sky-800',
  CheckedOut: 'bg-stone-200 text-stone-700',
  Placed: 'bg-sky-100 text-sky-800',
  Paid: 'bg-violet-100 text-violet-800',
  Shipped: 'bg-amber-100 text-amber-900',
  HandedToCarrier: 'bg-amber-100 text-amber-900',
  InTransit: 'bg-amber-100 text-amber-900',
  EnRoute: 'bg-amber-100 text-amber-900',
  Scheduled: 'bg-sky-100 text-sky-800',
  PickupScheduled: 'bg-amber-100 text-amber-900',
  Delivered: 'bg-emerald-100 text-emerald-900',
  PickedUp: 'bg-emerald-100 text-emerald-900',
  GoodsReceived: 'bg-teal-100 text-teal-900',
  Captured: 'bg-emerald-100 text-emerald-900',
  Approved: 'bg-violet-100 text-violet-800',
  Refunded: 'bg-emerald-100 text-emerald-900',
  Cancelled: 'bg-stone-200 text-stone-600',
  Voided: 'bg-stone-200 text-stone-600',
  Rejected: 'bg-red-100 text-red-800',
  Expired: 'bg-stone-200 text-stone-600',
  Requested: 'bg-sky-100 text-sky-800',
  Active: 'bg-emerald-100 text-emerald-900',
};

export function StatusPill({ status }) {
  const cls = PILL_STYLES[status] || 'bg-stone-200 text-stone-700';
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>
      {status}
    </span>
  );
}

export function Money({ value, className = '' }) {
  if (!value) return <span className={className}>—</span>;
  return <span className={className}>{value.display}</span>;
}

/** Horizontal step timeline. steps: string[], current: index reached, terminal: optional end label. */
export function Timeline({ steps, current, terminal = null }) {
  return (
    <ol className="flex items-center gap-0">
      {steps.map((s, i) => {
        const done = i <= current;
        const isLast = i === steps.length - 1;
        return (
          <li key={s} className={`flex items-center ${isLast ? '' : 'flex-1'}`}>
            <div className="flex flex-col items-start gap-1">
              <span
                className={`h-2.5 w-2.5 rounded-full ring-4 ${
                  done ? 'bg-emerald-700 ring-emerald-100' : 'bg-stone-300 ring-stone-100'
                }`}
              />
              <span className={`text-[11px] font-medium ${done ? 'text-stone-800' : 'text-stone-400'}`}>
                {s}
              </span>
            </div>
            {!isLast && (
              <div className={`mx-2 mb-5 h-0.5 flex-1 rounded ${i < current ? 'bg-emerald-700' : 'bg-stone-200'}`} />
            )}
          </li>
        );
      })}
      {terminal && (
        <li className="ml-3">
          <StatusPill status={terminal} />
        </li>
      )}
    </ol>
  );
}

export function EmptyState({ title, hint, action = null }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-stone-300 bg-white/60 px-6 py-12 text-center">
      <p className="font-display text-xl">{title}</p>
      <p className="max-w-sm text-sm text-stone-500">{hint}</p>
      {action}
    </div>
  );
}

export function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-stone-500">{label}</span>
      {children}
    </label>
  );
}

export const inputCls =
  'w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm outline-none transition focus:border-emerald-700 focus:ring-2 focus:ring-emerald-100';

export const btnPrimary =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-ink px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-stone-800 disabled:cursor-not-allowed disabled:opacity-40';

export const btnAccent =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-moss px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-moss-deep disabled:cursor-not-allowed disabled:opacity-40';

export const btnGhost =
  'inline-flex items-center justify-center gap-2 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-medium text-stone-700 transition hover:border-stone-400 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-40';
