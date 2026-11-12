// Luuv Fryxo Customer App — service worker (PWA shell)
// Register click handling before Firebase; otherwise the compat SDK may install
// its own handler first and lose the customer order route.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const notificationData = event.notification.data || {};
  const fcmData = (notificationData.FCM_MSG && notificationData.FCM_MSG.data) || {};
  const requested = notificationData.route || fcmData.route || '/cus?tab=orders';
  let target;
  try {
    target = new URL(requested, self.location.origin);
    if (target.origin !== self.location.origin) target = new URL('/cus?tab=orders', self.location.origin);
  } catch (_) {
    target = new URL('/cus?tab=orders', self.location.origin);
  }
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (existing) return existing.navigate(target.href).then(() => existing.focus());
    return self.clients.openWindow(target.href);
  }));
});

try {
  importScripts('https://www.gstatic.com/firebasejs/13.0.0/firebase-app-compat.js');
  importScripts('https://www.gstatic.com/firebasejs/13.0.0/firebase-messaging-compat.js');
  firebase.initializeApp({
    apiKey: 'AIzaSyDnyZhZ4wXIS8_dwS6LU8un1Fil6VmWEkM',
    authDomain: 'gen-lang-client-0437285569.firebaseapp.com',
    projectId: 'gen-lang-client-0437285569',
    storageBucket: 'gen-lang-client-0437285569.firebasestorage.app',
    messagingSenderId: '944300833041',
    appId: '1:944300833041:web:11121e52dfc89199cc74b0'
  });
  const messaging = firebase.messaging();
  messaging.onBackgroundMessage((payload) => {
    // Notification payloads are displayed automatically by FCM. Only data-only
    // messages need a custom notification here, avoiding duplicate alerts.
    if (payload.notification) return;
    const data = payload.data || {};
    self.registration.showNotification(data.title || 'Luuv Fryxo', {
      body: data.body || 'Your order has an update.',
      icon: '/assets/luuvcrm/app/icon-192.png',
      badge: '/assets/luuvcrm/app/icon-192.png',
      data: { route: data.route || '/cus?tab=orders' }
    });
  });
} catch (_) {
  // The offline shell still installs if Google's SDK is temporarily unreachable.
}

const CACHE = 'lf-app-v5';

self.addEventListener('install', (e) => { self.skipWaiting(); });

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith('lf-app-') && key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // Network-first for the app shell; fall back to the cached page when offline.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req, { cache: 'no-store' })
        .then((res) => { const c = res.clone(); caches.open(CACHE).then((ca) => ca.put('/cus', c)); return res; })
        .catch(() => caches.match('/cus'))
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
