/* 片流 PWA Service Worker：应用外壳离线可用，列表 API 保留最近一次成功响应。
 * 策略：
 * - 页面导航 network-first，成功后更新缓存外壳，离线回退缓存首页；
 * - /api/titles|stats|releases network-first，离线回退最近成功响应并标注来源时间；
 * - /static/ 资源 stale-while-revalidate，缓存写入纳入 waitUntil；
 * - 只维护 stream- 前缀的缓存；写入个人数据后由页面消息失效数据缓存。
 *
 * 发布新版本时同时更新 SW_VERSION 与 SHELL_ASSETS 中的 ?v= 版本号，
 * 保持 HTML/CSS/JS 同版预缓存。
 */
const SW_VERSION = 'e6f41c6';
const SHELL_CACHE = `stream-shell-${SW_VERSION}`;
const DATA_CACHE = `stream-data-${SW_VERSION}`;
const CACHE_PREFIX = 'stream-';
const DATA_CACHE_MAX = 40;
const SHELL_ASSETS = [
    '/',
    '/static/manifest.json',
    '/static/icon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/css/style.css?v=e6f41c6',
    '/static/js/app.js?v=e6f41c6',
];

self.addEventListener('install', event => {
    event.waitUntil((async () => {
        const cache = await caches.open(SHELL_CACHE);
        // 单个资源失败不阻塞安装（例如版本号尚未发布的过渡期）
        await Promise.all(SHELL_ASSETS.map(async url => {
            try {
                await cache.add(new Request(url, { cache: 'reload' }));
            } catch (_) { /* 忽略单个资源失败 */ }
        }));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(
            keys
                .filter(key => key.startsWith(CACHE_PREFIX)
                    && key !== SHELL_CACHE && key !== DATA_CACHE)
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

/* 离线回退响应附加来源标记：页面据此提示“缓存数据”与获取时间 */
function withCacheNotice(response) {
    const headers = new Headers(response.headers);
    headers.set('X-Stream-Cache', 'hit');
    const dateValue = response.headers.get('date');
    if (dateValue) headers.set('X-Stream-Cached-At', dateValue);
    return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers,
    });
}

async function networkFirstData(request) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(DATA_CACHE);
            await cache.put(request, response.clone());
            await trimCache(DATA_CACHE, DATA_CACHE_MAX);
        }
        return response;
    } catch (error) {
        const cached = await caches.match(request);
        if (cached) return withCacheNotice(cached);
        throw error;
    }
}

async function networkFirstNavigation(request) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(SHELL_CACHE);
            await cache.put('/', response.clone());
        }
        return response;
    } catch (error) {
        const cached = await caches.match('/');
        if (cached) return cached;
        throw error;
    }
}

self.addEventListener('message', event => {
    if (event.data?.type === 'invalidate-data') {
        event.waitUntil((async () => {
            const cache = await caches.open(DATA_CACHE);
            const keys = await cache.keys();
            await Promise.all(keys
                .filter(request => new URL(request.url).pathname.startsWith('/api/'))
                .map(request => cache.delete(request)));
        })());
    }
});

self.addEventListener('fetch', event => {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    // 页面导航：network-first，成功后更新离线外壳首页
    if (request.mode === 'navigate') {
        event.respondWith(networkFirstNavigation(request));
        return;
    }

    // 列表/统计/更新：network-first + 最近一次成功响应兜底；回退时标注缓存来源
    if (url.pathname.startsWith('/api/titles')
        || url.pathname === '/api/stats'
        || url.pathname === '/api/releases') {
        event.respondWith(networkFirstData(request));
        return;
    }

    // 静态资源：stale-while-revalidate（缓存写入纳入 waitUntil）
    if (url.pathname.startsWith('/static/')) {
        event.respondWith((async () => {
            const cache = await caches.open(SHELL_CACHE);
            const cached = await cache.match(request);
            const network = fetch(request).then(response => {
                if (response.ok) {
                    event.waitUntil(cache.put(request, response.clone()));
                }
                return response;
            }).catch(() => cached);
            return cached || network;
        })());
    }
});
