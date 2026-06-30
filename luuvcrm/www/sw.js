const CACHE = 'luuv-pos-v1';
const STATIC = [
  '/pos',
  '/waiter',
  '/kitchen',
  '/icon.svg',
  '/manifest.json',
];

const API_CACHE = [
  '/api/method/luuvcrm.api.get_menu_data',
  '/api/method/luuvcrm.api.get_tables',
  '/api/method/luuvcrm.api.get_tables_with_status',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(STATIC).catch(() => {}))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isApiGet(url) {
  return url.method === 'GET' && API_CACHE.some((p) => url.pathname.startsWith(p));
}

function isMutation(url) {
  if (url.method !== 'POST') return false;
  const path = url.pathname;
  return (
    path.includes('/api/method/luuvcrm.api.') &&
    !path.includes('get_') &&
    !path.includes('log_print')
  );
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  if (e.request.method === 'POST' && isMutation(e.request)) {
    const cloned = e.request.clone();
    e.respondWith(
      fetch(cloned).then((res) => {
        const copy = res.clone();
        caches.open('luuv-mutations').then((c) => {
          c.put(e.request.url, copy);
          caches.open(CACHE).then((cache) => {
            cache.delete('/api/method/luuvcrm.api.get_tables_with_status?_=' + Date.now()).catch(() => {});
          });
        });
        return res;
      }).catch(() => {
        return new Response(JSON.stringify({ offline: true, message: 'Queued for sync' }), {
          status: 201, headers: { 'Content-Type': 'application/json' },
        });
      })
    );
    return;
  }

  if (isApiGet(e.request)) {
    e.respondWith(
      fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request.url, copy));
        return res;
      }).catch(() => caches.match(e.request).then((r) => r || new Response(JSON.stringify({ offline: true }), {
        status: 503, headers: { 'Content-Type': 'application/json' },
      })))
    );
    return;
  }

  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then((r) => r || fetch(e.request)))
  );
});

self.addEventListener('sync', (e) => {
  if (e.tag === 'sync-orders') {
    e.waitUntil(syncOrders());
  }
});

async function syncOrders() {
  const clients = await self.clients.matchAll();
  for (const client of clients) {
    client.postMessage({ type: 'TRIGGER_SYNC' });
  }
}