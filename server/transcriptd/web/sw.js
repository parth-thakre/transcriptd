// Offline shell for the phone app. Network first so a server update shows up at once;
// the cache only answers when the server is unreachable. API calls are never cached.
const CACHE = 'transcriptd-m-v1';
const SHELL = ['./', 'index.html', 'app.css', 'app.js', 'annotate.js', 'pcm-worklet.js', 'manifest.webmanifest',
  'icon-192.png', 'icon-512.png', 'icon-maskable-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || !url.pathname.startsWith('/m/')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then(c => c.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request, { ignoreSearch: true })));
});
