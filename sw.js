// UltraAccounting Service Worker
// v2: network-first (update turant dikhe), offline hone par cache se chalega
const CACHE = 'ua-v2';
const ASSETS = ['/', '/manifest.json', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // API calls — always network
  if (url.pathname.startsWith('/api/')) return;
  // Super admin panel — hamesha fresh, cache nahi
  if (url.pathname.startsWith('/super')) return;
  // Network first, offline me cache
  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.status === 200) {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return resp;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
