// Luuv Fryxo Purchases — service worker (PWA shell)
const CACHE = 'lf-purchases-v1';
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then((res) => { const c = res.clone(); caches.open(CACHE).then((ca) => ca.put('/purchases', c)); return res; })
        .catch(() => caches.match('/purchases'))
    );
    return;
  }
  if (/fonts\.(googleapis|gstatic)\.com|\/manager-icon-/.test(req.url)) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => { const c = res.clone(); caches.open(CACHE).then((ca) => ca.put(req, c)); return res; }).catch(() => hit)));
  }
});
