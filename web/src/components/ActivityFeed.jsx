/**
 * Live activity: the committed domain-event stream as a store timeline.
 * Collapsible — ambience for shoppers, signal for the curious.
 */
export default function ActivityFeed({ events, total }) {
  return (
    <details className="group overflow-hidden rounded-2xl border border-stone-200 bg-white">
      <summary className="flex cursor-pointer items-center justify-between px-5 py-3">
        <span className="flex items-center gap-2 text-sm font-semibold">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-500 opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-600" />
          </span>
          Live store activity
        </span>
        <span className="text-xs text-stone-400">{total} events · updates every 3s</span>
      </summary>
      <ul className="slim-scroll max-h-64 space-y-1 overflow-y-auto border-t border-stone-100 px-5 py-3 font-mono text-xs">
        {[...events].reverse().map((e, i) => (
          <li key={`${e.name}-${i}`} className="flex gap-2 rounded-lg px-2 py-1 odd:bg-stone-50">
            <b className="shrink-0 text-moss-deep">{e.name}</b>
            <span className="truncate text-stone-400">
              {Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}
            </span>
          </li>
        ))}
        {events.length === 0 && <li className="text-stone-400">Every order, shipment and refund lands here.</li>}
      </ul>
    </details>
  );
}
