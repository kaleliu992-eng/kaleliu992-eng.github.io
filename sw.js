const APP_VERSION = "2.1.0";
const CACHE_NAME = 'readflow-49816004399407b9';
const OFFLINE_PATHS = ["/manifest.webmanifest","/icon-192.png","/icon-512.png","/apple-touch-icon.png","/readflow-sample.epub","/dictionary/finance-v1.json","/_next/static/cM175-aMGRpN6eLX9VWN1/_buildManifest.js","/_next/static/cM175-aMGRpN6eLX9VWN1/_clientMiddlewareManifest.js","/_next/static/cM175-aMGRpN6eLX9VWN1/_ssgManifest.js","/_next/static/chunks/08o_k-4cvafb7.js","/_next/static/chunks/0cz1d0mv5g_q7.js","/_next/static/chunks/0j1we3cwg31qe.js","/_next/static/chunks/0tvqmc2uq7jck.js","/_next/static/chunks/1-_4_l553m6o5.js","/_next/static/chunks/11iyqs7hauntt.js","/_next/static/chunks/1nznowgsgtpt9.js","/_next/static/chunks/1rihuxxdfe9g-.js","/_next/static/chunks/2qgpm5u0_ww49.js","/_next/static/chunks/39xw0ixg8-wuu.js","/_next/static/chunks/3qfy96eoyh-ek.js","/_next/static/chunks/3rj2s5is8hwl-.css","/_next/static/chunks/turbopack-2u1d16houkiyo.js"];
const offlinePaths = new Set(OFFLINE_PATHS);
const DICTIONARY_CACHE = 'readflow-dictionary-v1';
const SPEECH_CATALOG = '/speeches/catalog-v1.json';
const SPEECH_CATALOG_CACHE = 'readflow-speech-catalog-v1';

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    const shell = await fetch(`/?app-version=${APP_VERSION}`, { cache: 'reload' });
    const html = shell.ok ? await shell.clone().text() : '';
    if (!html.includes(`data-readflow-version="${APP_VERSION}"`)) {
      throw new Error('The ReadFlow homepage and worker versions do not match.');
    }
    const files = OFFLINE_PATHS.map((path) => new Request(path, { cache: 'reload' }));
    await cache.addAll(files);
    await cache.put('/', shell);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    // Keep the previous build for a reader that was open when the new worker took over.
    const previous = names.filter((name) => /^readflow-[a-f0-9]{16}$/.test(name) && name !== CACHE_NAME);
    await Promise.all(previous.slice(0, -1).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  if (event.data === 'readflow:version') {
    event.source?.postMessage({ type: 'readflow:version', version: APP_VERSION });
  }
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (!url.search && url.pathname === SPEECH_CATALOG) {
    event.respondWith((async () => {
      const cache = await caches.open(SPEECH_CATALOG_CACHE);
      let response;
      try {
        response = await fetch(request, { cache: 'no-cache' });
      } catch {
        // An offline reader can still access speeches it has already opened.
      }
      if (response?.ok && response.headers.get('content-type')?.includes('application/json')) {
        try { await cache.put(SPEECH_CATALOG, response.clone()); } catch { /* Storage is optional. */ }
        return response;
      }
      const previous = (await cache.match(SPEECH_CATALOG)) ?? (await caches.match(SPEECH_CATALOG));
      if (!previous) return response ?? new Response(null, { status: 503 });
      return new Response(previous.body, {
        headers: {
          'Content-Type': 'application/json',
          'X-ReadFlow-Catalog': 'cached',
        },
      });
    })());
    return;
  }

  if (request.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/index.html')) {
    // Serve an application shell and chunk set from the same build, even during a deploy.
    event.respondWith(caches.open(CACHE_NAME).then(async (cache) => (
      (await cache.match('/')) ?? fetch(request)
    )));
    return;
  }

  if (!url.search && url.pathname.startsWith('/_next/static/')) {
    event.respondWith(caches.match(request).then((cached) => cached ?? fetch(request)));
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
