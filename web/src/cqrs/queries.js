/**
 * CQRS read side: pure queries against backend read-models.
 *
 * GET /api/<resource> served by api/queries.py straight from repositories.
 * Queries never mutate state. React components call these after every
 * command (or on a poll) to refresh their view of the world.
 */
const API = '';

async function get(path) {
  const res = await fetch(`${API}${path}`);
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `query ${path} failed`);
  return data;
}

export const queries = {
  dashboard: () => get('/api/dashboard').then((d) => d.dashboard),
  products: () => get('/api/products').then((d) => d.products),
  stocks: () => get('/api/stocks').then((d) => d.stocks),
  coupons: () => get('/api/coupons').then((d) => d.coupons),
  carts: () => get('/api/carts').then((d) => d.carts),
  cart: (id) => get(`/api/carts/${id}`).then((d) => d.cart),
  orders: () => get('/api/orders').then((d) => d.orders),
  order: (id) => get(`/api/orders/${id}`).then((d) => d.order),
  orderPayment: (id) => get(`/api/orders/${id}/payment`).then((d) => d.payment),
  shipments: () => get('/api/shipments').then((d) => d.shipments),
  returns: () => get('/api/returns').then((d) => d.returns),
  pickups: () => get('/api/pickups').then((d) => d.pickups),
  events: (limit = 50) => get(`/api/events?limit=${limit}`),
};

export const DEFAULT_ADDRESS = {
  line1: '1 Main St',
  city: 'Springfield',
  postal_code: '12345',
  country: 'US',
};
