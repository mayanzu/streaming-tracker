/* 片流 PWA Service Worker：应用外壳离线可用，列表 API 保留最近一次成功响应。
 * 策略：
 * - 页面导航 network-first，离线回退缓存首页；
 * - /api/titles|stats|releases network-first + 最近成功响应兜底；
 * - /static/ 资源 stale-while-revalidate，避免重复请求并加速二次打开。
 */
const SHELL_CACHE = 'stream-shell-v1';
const DATA_CACHE = 'stream-data-v1';
const DATA_CACHE_MAX = 40;
const SHELL_ASSETS = [
    '/',
    '/static/manifest.json',
    '/static/icon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(SHELL_CACHE)
            .then(cache => cache.addAll(SHELL_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(
            keys
                .filter(key => key !== SHELL_CACHE && key !== DATA_CACHE)
                .map(key => caches.delete(key))
        );
        await self.clients.claim();
    })());
});

async function trimCache(cacheName, maxEntries) {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    while (keys.length > maxEntries && keys.length) {
        await cache.delete(keys.shift());
    }
}

async function networkFirst(request, cacheName, fallbackPath) {
    try {
        const response = await fetch(request);
        if (response.ok && cacheName) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
            trimCache(cacheName, DATA_CACHE_MAX);
        }
        return response;
    } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        if (fallbackPath) {
            const fallback = await caches.match(fallbackPath);
            if (fallback) return fallback;
        }
        throw error;
    }
}

self.addEventListener('fetch', event => {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    // 页面导航：network-first，离线回退到缓存首页
    if (request.mode === 'navigate') {
        event.respondWith(networkFirst(request, null, '/'));
        return;
    }

    // 列表/统计/更新：network-first + 最近一次成功响应兜底，离线可继续浏览已加载内容
    if (url.pathname.startsWith('/api/titles')
        || url.pathname === '/api/stats'
        || url.pathname === '/api/releases') {
        event.respondWith(networkFirst(request, DATA_CACHE, null));
        return;
    }

    // 静态资源：stale-while-revalidate
    if (url.pathname.startsWith('/static/')) {
        event.respondWith((async () => {
            const cached = await caches.match(request);
            const network = fetch(request).then(response => {
                if (response.ok) {
                    caches.open(SHELL_CACHE).then(cache => cache.put(request, response.clone()));
                }
                return response;
            }).catch(() => cached);
            return cached || network;
        })());
    }
});
