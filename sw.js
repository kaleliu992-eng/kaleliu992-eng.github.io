const CACHE_NAME = 'readflow-541c6d9110b1f910';
const OFFLINE_PATHS = ["/","/manifest.webmanifest","/icon-192.png","/icon-512.png","/apple-touch-icon.png","/readflow-sample.epub","/dictionary/finance-v1.json","/_next/static/9BmEkqakIcX2FiVDiCE0K/_buildManifest.js","/_next/static/9BmEkqakIcX2FiVDiCE0K/_clientMiddlewareManifest.js","/_next/static/9BmEkqakIcX2FiVDiCE0K/_ssgManifest.js","/_next/static/chunks/08o_k-4cvafb7.js","/_next/static/chunks/0cz1d0mv5g_q7.js","/_next/static/chunks/0j1we3cwg31qe.js","/_next/static/chunks/0l3v-kc891aaf.css","/_next/static/chunks/0sepz0ow60kn-.js","/_next/static/chunks/0tvqmc2uq7jck.js","/_next/static/chunks/1-_4_l553m6o5.js","/_next/static/chunks/11iyqs7hauntt.js","/_next/static/chunks/1nznowgsgtpt9.js","/_next/static/chunks/1rihuxxdfe9g-.js","/_next/static/chunks/2qgpm5u0_ww49.js","/_next/static/chunks/3qfy96eoyh-ek.js","/_next/static/chunks/turbopack-2u1d16houkiyo.js"];
const offlinePaths = new Set(OFFLINE_PATHS);
const DICTIONARY_CACHE = 'readflow-dictionary-v1';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => {
    const files = OFFLINE_PATHS.map((path) => new Request(path, { cache: 'reload' }));
    return cache.addAll(files);
  }));
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith('readflow-') && name !== CACHE_NAME && name !== DICTIONARY_CACHE)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (request.mode === 'navigate' && url.pathname === '/') {
    // Serve an application shell and chunk set from the same build, even during a deploy.
    event.respondWith(caches.open(CACHE_NAME).then(async (cache) => (
      (await cache.match('/')) ?? fetch(request)
    )));
    return;
  }

  if (!url.search && offlinePaths.has(url.pathname)) {
    event.respondWith(caches.open(CACHE_NAME).then(async (cache) => (
      (await cache.match(request)) ?? fetch(request)
    )));
    return;
  }

  if (!url.search && /^\/dictionary\/wordnet-v1\/[a-z0-9_-]+\.json$/i.test(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(DICTIONARY_CACHE);
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) {
        try {
          await cache.put(request, response.clone());
        } catch {
          // Reading can continue even if the browser cannot retain another dictionary shard.
        }
      }
      return response;
    })());
  }
});
