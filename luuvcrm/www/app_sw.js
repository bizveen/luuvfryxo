// Luuv Fryxo Customer App — service worker (PWA shell)
const CACHE = 'lf-app-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });

self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // Network-first for the app shell; fall back to the cached page when offline.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((res) => { const c = res.clone(); caches.open(CACHE).then((ca) => ca.put('/app', c)); return res; })
        .catch(() => caches.match('/app'))
    );
    return;
  }
  // Cache-first for fonts/icons.
  if (/fonts\.(googleapis|gstatic)\.com|\/assets\/luuvcrm\/app\//.test(req.url)) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const c = res.clone(); caches.open(CACHE).then((ca) => ca.put(req, c)); return res;
      }).catch(() => hit))
    );
  }
});
