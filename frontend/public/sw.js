const CACHE = 'citko-shell-v2';
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await cache.addAll(['/', '/icon.svg', '/manifest.webmanifest']);
    const manifest = await fetch('/asset-manifest.json');
    if (manifest.ok) await cache.addAll((await manifest.json()).assets);
  })());
  self.skipWaiting();
});
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))); self.clients.claim(); });
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.url.includes('/api/') || request.headers.has('range')) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/reader/') || url.pathname.includes('/file')) return;
  if (request.mode === 'navigate') event.respondWith(fetch(request).catch(() => caches.match('/')));
  else if (['script','style','font','image'].includes(request.destination)) event.respondWith(caches.match(request).then(hit => hit || fetch(request).then(response => { if (response.ok) caches.open(CACHE).then(cache => cache.put(request, response.clone())); return response; })));
});
