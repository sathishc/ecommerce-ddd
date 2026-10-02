/** Events tab: the committed domain-event stream (the Notification context feed). */
export default function EventsFeed({ events, total }) {
  return (
    <section className="card">
      <h2>Domain events <span className="tag">query: GET /api/events (polls every 3s)</span></h2>
      <p className="muted small">{total} committed events · newest last · published only on UoW commit (transactional outbox).</p>
      <ul className="list mono">
        {[...events].reverse().map((e, i) => (
          <li key={`${e.name}-${i}`} className="item">
            <span><b>{e.name}</b> <span className="muted">{Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}</span></span>
          </li>
        ))}
        {events.length === 0 && <li className="muted">No events yet — run a command.</li>}
      </ul>
    </section>
  );
}
