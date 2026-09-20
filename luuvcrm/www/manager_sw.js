// Luuv Fryxo Manager — service worker (PWA shell)
const CACHE = 'lf-manager-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });

self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // Network-first for the app shell; fall back to the cached page when offline.
  // Only the app page itself: any other navigation (the printed statement, a
  // downloaded PDF) is not the shell, and caching it under /manager would
  // replace the app with that document the next time the phone is offline.
  if (req.mode === 'navigate') {
    if (new URL(req.url).pathname !== '/manager') return;
    e.respondWith(
      fetch(req)
        .then((res) => { const c = res.clone(); caches.open(CACHE).then((ca) => ca.put('/manager', c)); return res; })
        .catch(() => caches.match('/manager'))
    );
    return;
  }
  // Cache-first for fonts/icons.
  if (/fonts\.(googleapis|gstatic)\.com|\/assets\/luuvcrm\/manager\//.test(req.url)) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const c = res.clone(); caches.open(CACHE).then((ca) => ca.put(req, c)); return res;
      }).catch(() => hit))
    );
  }
});
