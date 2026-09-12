/* 片流 PWA Service Worker：应用外壳离线可用，列表 API 保留最近一次成功响应。
 * 策略：
 * - 页面导航 network-first，成功后更新缓存外壳，离线回退缓存首页；
 * - /api/titles|stats|releases network-first，离线回退最近成功响应并标注来源时间；
 * - /static/ 资源 stale-while-revalidate，缓存写入纳入 waitUntil；
 * - 只维护 stream- 前缀的缓存；写入个人数据后由页面消息失效数据缓存。
 *
 * 提交语义（R4-01）：
 * - 核心外壳（入口 HTML 与同版本 CSS/JS）必须全部写入成功，install 才算完成；
 *   任一失败即清理本次未完成的 SHELL_CACHE 并让 waitUntil 拒绝，旧 worker 与旧缓存保留。
 * - 仅在核心外壳完整后调用 skipWaiting，避免不完整的新版本替换可用的离线版本。
 * - 图标、manifest 等可选资源 best-effort，失败只记录，不影响安装。
 * - activate 先确认当前外壳完整，才清理其它 stream-shell-/stream-data- 旧版本缓存；
 *   外壳不完整则保留旧缓存，断网时仍可回退到上一可用版本。
 *
 * 发布新版本时同时更新 SW_VERSION 与外壳资源清单中的 ?v= 版本号，
 * 保持 HTML/CSS/JS 同版预缓存。
 */
const SW_VERSION = '1ff462c';
const SHELL_CACHE = `stream-shell-${SW_VERSION}`;
const DATA_CACHE = `stream-data-${SW_VERSION}`;
const CACHE_PREFIX = 'stream-';
const DATA_CACHE_MAX = 40;
/* 核心外壳：任一失败都不提交新版本（提交 = skipWaiting） */
const CORE_SHELL_ASSETS = [
    '/',
    '/static/css/style.css?v=1ff462c',
    '/static/js/app.js?v=1ff462c',
];
/* 可选外壳：manifest 与图标 best-effort，失败只记录 */
const OPTIONAL_SHELL_ASSETS = [
    '/static/manifest.json',
    '/static/icon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
];

/* 当前外壳是否完整：核心资源都能从 SHELL_CACHE 取出才算可提交 */
async function shellIsComplete() {
    const names = await caches.keys();
    if (!names.includes(SHELL_CACHE)) return false;
    const cache = await caches.open(SHELL_CACHE);
    const results = await Promise.all(CORE_SHELL_ASSETS.map(async url => {
        try {
            return Boolean(await cache.match(new Request(url)));
        } catch (_) {
            return false;
        }
    }));
    return results.every(Boolean);
}

self.addEventListener('install', event => {
    event.waitUntil((async () => {
        const cache = await caches.open(SHELL_CACHE);
        // 核心资源全部成功才提交；任一失败都清理半成品，让旧的可用版本继续服务
        const failed = [];
        await Promise.all(CORE_SHELL_ASSETS.map(async url => {
            try {
                await cache.add(new Request(url, { cache: 'reload' }));
            } catch (error) {
                failed.push({ url, error });
            }
        }));
        if (failed.length) {
            try {
                await caches.delete(SHELL_CACHE);
            } catch (cleanupError) {
                console.warn('[sw] 清理未完成外壳缓存失败：', cleanupError);
            }
            throw new Error(`核心外壳资源缓存失败，安装未提交：${failed.map(item => item.url).join('、')}`);
        }
        // 可选资源 best-effort，失败只记录，不影响安装提交
        await Promise.all(OPTIONAL_SHELL_ASSETS.map(async url => {
            try {
                await cache.add(new Request(url, { cache: 'reload' }));
            } catch (error) {
                console.warn('[sw] 可选外壳资源缓存失败，忽略：', url, error);
            }
        }));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        // 只有当前外壳完整（HTML/CSS/JS 均可离线取出）才允许清理旧版本
        if (await shellIsComplete()) {
            const keys = await caches.keys();
            await Promise.all(
                keys
                    .filter(key => key.startsWith(CACHE_PREFIX)
                        && key !== SHELL_CACHE && key !== DATA_CACHE)
                    .map(key => caches.delete(key))
            );
        } else {
            console.warn('[sw] 当前外壳不完整，保留旧版本缓存以供回退');
        }
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
