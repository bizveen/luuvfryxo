const CACHE = 'luuv-pos-v2';

// The offline shell: the three screens the floor holds. Nothing else belongs
// here — the icons and the manifest are settings-driven now and come from the
// runtime cache below like any other asset.
//
// It used to also list '/icon.svg' and '/manifest.json', and both 404. addAll()
// is atomic, so one missing URL rejected the whole call — and the .catch()
// around it swallowed that — which meant the till precached *nothing*. A POS
// with no offline shell looks exactly like a POS with one until the wifi drops
// mid-service, which is the only moment it matters.
const STATIC = [
  '/pos',
  '/waiter',
  '/kitchen',
];

const API_CACHE = [
  '/api/method/luuvcrm.api.get_menu_data',
  '/api/method/luuvcrm.api.get_tables',
  '/api/method/luuvcrm.api.get_tables_with_status',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    // One url at a time, not addAll: a screen that fails to precache must not
    // take the other two down with it.
    caches.open(CACHE).then((c) =>
      Promise.all(STATIC.map((u) => c.add(u).catch(() => {})))
    )
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

// Takes both halves on purpose: the method is on the Request, the path is on
// the parsed URL. This read `url.pathname` off the Request, which has no such
// property — so it threw TypeError on the first fetch and the whole listener
// died, taking the offline read cache with it. Silent, because a throw in a
// fetch listener before respondWith() just lets the request go to the network:
// online it looks perfect, offline the menu and the table map are simply gone.
function isApiGet(request, url) {
  return request.method === 'GET' && API_CACHE.some((p) => url.pathname.startsWith(p));
}

// There is deliberately no isMutation() any more. See the note on POSTs below.

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // POSTs are not touched. They used to be: a failed one was answered with a
  // fabricated 201 and the words "Queued for sync", and nothing anywhere was a
  // queue — the cache it wrote to was never read, no page ever registered the
  // 'sync-orders' tag, and nothing listened for the TRIGGER_SYNC message the
  // handler sent. So a payment that never reached the server was reported to
  // the counter as taken. An honest failure the page can show beats a success
  // that did not happen; a real offline queue is a feature to decide on, not
  // something to imply.

  if (isApiGet(e.request, url)) {
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

