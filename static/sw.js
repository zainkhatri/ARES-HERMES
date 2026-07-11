// ARES Service Worker — offline-capable with layered caching
const THUMB_CACHE = 'ares-thumbs-v14';  // v14: thumbs no longer SW-cached (native HTTP cache); v13 purged on activate
const PAGE_CACHE = 'ares-pages-v12';  // v12: /breakdown is now the Finance net-worth view

// App shell — precached on install so the app works offline immediately
const APP_SHELL = [
    '/',
    '/photos',
    '/journals',
    '/breakdown',
    '/static/favicon.svg',
    '/static/icon-180.png',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/manifest.json',
    '/static/style.css',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(PAGE_CACHE).then(cache =>
            cache.addAll(APP_SHELL).catch(() => {})
        ).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', e => e.waitUntil(
    caches.keys().then(names =>
        Promise.all(names.filter(n => n !== THUMB_CACHE && n !== PAGE_CACHE).map(n => caches.delete(n)))
    ).then(() => self.clients.claim())
));

self.addEventListener('fetch', event => {
    const { pathname } = new URL(event.request.url);

    // ── Video URLs: do NOT touch. Chrome issues many short Range requests
    //    during playback; piping each through the SW adds significant
    //    overhead and stalls first-frame by several seconds. Let the
    //    browser hit the network directly — Caddy + Flask handle Range. ──
    if (pathname === '/api/photos/video' || pathname.startsWith('/api/photos/video?')
        || pathname.startsWith('/static/video_cache')) {
        return;  // no respondWith → browser fetches natively
    }

    // ── Range requests in general: skip SW. Range + SW is fragile. ──
    if (event.request.headers.get('range')) {
        return;
    }

    // ── Thumbnails & previews: do NOT touch (same lesson as video above).
    //    Caddy serves them with immutable 7-day Cache-Control, so the
    //    browser's native HTTP cache is already optimal. Routing tens of
    //    thousands of tiles through CacheStorage added a SW hop + disk
    //    lookup per tile and a disk write per first view — the grid
    //    visibly lagged. Native fetch = instant. ──
    if (pathname.startsWith('/static/thumbs')) {
        return;  // no respondWith → browser fetches natively
    }

    // ── Journal page renders: cache-first ──
    if (pathname.includes('/api/journals/') && pathname.includes('/page/')) {
        event.respondWith(
            caches.open(THUMB_CACHE).then(cache =>
                cache.match(event.request).then(hit => {
                    if (hit) return hit;
                    return fetch(event.request, { credentials: 'same-origin' }).then(res => {
                        if (res.ok) cache.put(event.request, res.clone());
                        return res;
                    }).catch(() => new Response('', { status: 503 }));
                })
            )
        );
        return;
    }

    // ── HTML pages: network-first, cache fallback (offline browsing) ──
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).then(res => {
                if (res.ok) {
                    const clone = res.clone();
                    caches.open(PAGE_CACHE).then(c => c.put(event.request, clone));
                }
                return res;
            }).catch(() => caches.open(PAGE_CACHE).then(c => c.match(event.request)))
        );
        return;
    }

    // ── /api/ JSON: passthrough.
    //    The previous stale-while-revalidate had two failure modes that wedged
    //    the page: (1) `cached || networkFetch || offline` always short-circuits
    //    on networkFetch (a truthy Promise), so the offline fallback is dead
    //    code; (2) when the network fetch rejected, the promise resolved to
    //    `null` and respondWith got a non-Response, which made the page-side
    //    fetch hang indefinitely instead of failing fast. JSON that changes
    //    every sync gains nothing from caching, so go straight to network.
    if (pathname.startsWith('/api/')) {
        event.respondWith(fetch(event.request, { credentials: 'same-origin' }));
        return;
    }

    // ── Static assets (CSS, JS, fonts): cache-first ──
    if (pathname.startsWith('/static/') || event.request.url.includes('fonts.googleapis') || event.request.url.includes('fonts.gstatic') || event.request.url.includes('cdn.tailwindcss')) {
        event.respondWith(
            caches.open(PAGE_CACHE).then(cache =>
                cache.match(event.request).then(hit => {
                    if (hit) return hit;
                    return fetch(event.request).then(res => {
                        if (res.ok) cache.put(event.request, res.clone());
                        return res;
                    }).catch(() => new Response('', { status: 503 }));
                })
            )
        );
        return;
    }
});

// ── Thumb bundle pre-cache (bulk download on first visit) ──
// Pulls both the 475px base tier (~1.7 GB) and the 800px hq retina tier
// (~5 GB). After install, grid scrolling is local-disk-instant — works
// remotely over slow Tailscale on iPhone + Mac alike.
self.addEventListener('message', event => {
    if (event.data?.type === 'PRECACHE_BUNDLE') {
        const expected = event.data.expectedCount || 0;
        (async () => {
            // Base tier first (smaller, gives immediate visual win).
            await precacheBundle('/api/photos/thumb-bundle', expected);
            // hq tier second — only after base is done, so we don't double
            // the bandwidth contention while the user is actively browsing.
            await precacheBundle('/api/photos/thumb-bundle?tier=hq', expected);
        })();
    }
});

async function precacheBundle(url, expectedCount) {
    const cache = await caches.open(THUMB_CACHE);
    /* Per-bundle skip: if this tier's URLs already mostly exist in cache,
       no-op. Tier inferred from the URL pathname so base / hq tracked
       separately. */
    const tierKey = url.includes('tier=hq') ? '/static/thumbs_hq/' : '/static/thumbs/';
    if (expectedCount > 0) {
        const existing = await cache.keys();
        const hits = existing.filter(r => new URL(r.url).pathname.startsWith(tierKey)).length;
        if (hits >= Math.floor(expectedCount * 0.95)) return;
    }

    let resp;
    try {
        resp = await fetch(url, { credentials: 'same-origin' });
        if (!resp.ok || !resp.body) return;
    } catch (e) { return; }

    const reader = resp.body.getReader();
    let residual = new Uint8Array(0);
    let puts = [];

    const concat = (a, b) => { const c = new Uint8Array(a.length + b.length); c.set(a); c.set(b, a.length); return c; };
    const u16 = (b, i) => (b[i] << 8) | b[i + 1];
    const u32 = (b, i) => ((b[i] << 24) | (b[i+1] << 16) | (b[i+2] << 8) | b[i+3]) >>> 0;

    while (true) {
        let done, value;
        try { ({ done, value } = await reader.read()); } catch (e) { break; }
        if (done) break;
        const chunk = residual.length ? concat(residual, value) : value;
        let pos = 0;
        while (pos + 6 <= chunk.length) {
            const urlLen = u16(chunk, pos);
            const dataOffset = pos + 2 + urlLen;
            if (dataOffset + 4 > chunk.length) break;
            const dataLen = u32(chunk, dataOffset);
            const entryEnd = dataOffset + 4 + dataLen;
            if (entryEnd > chunk.length) break;
            const entryUrl = new TextDecoder().decode(chunk.slice(pos + 2, pos + 2 + urlLen));
            const imgData = chunk.slice(dataOffset + 4, entryEnd);
            pos = entryEnd;
            puts.push(cache.put(
                new URL(entryUrl, self.location.origin).href,
                new Response(imgData, { status: 200, headers: { 'Content-Type': 'image/jpeg', 'Cache-Control': 'public, max-age=604800, immutable' } })
            ));
            if (puts.length >= 100) { await Promise.all(puts); puts = []; }
        }
        residual = chunk.slice(pos);
    }
    if (puts.length) await Promise.all(puts);
}
