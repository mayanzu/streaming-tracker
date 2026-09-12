const state = {
    page: 1,
    limit: 40,
    sort_by: 'release_date',
    order: 'desc',
    type: '',
    search: '',
    region: '',
    rating: 0,
    genre: '',
    maxRuntime: 0,
    watchStatus: '',
    excludeWatched: false, // 6.5：隐藏已看（仅浏览目录时生效，不影响“已看”片单）
    section: 'discover',   // 5.2 主导航：discover | library | releases
    libraryStatus: 'watchlist', // 离开“我的片单”时记住子状态
    releasedDays: 0,       // 6.2 近期新片窗口（天）：0=不限
    viewingRegion: '',     // 6.1 观看地区：用于过滤详情渠道
    surpriseScope: 'filters', // 6.6 惊喜发现范围：filters | watchlist | watching
    surpriseRecent: [],    // 最近抽过的作品 id（避免连续重复）
    surpriseLastId: null,  // 当前惊喜结果作品 id
    batchMode: false,      // 6.7 批量管理我的片单
    selectedIds: new Set(),
    loading: false,
    hasMore: true,
    requestVersion: 0,
    loadedTitleIds: [],
    currentDetailIndex: -1,
    optimisticPending: null,   // 兼容旧字段（已改用 pendingStatuses Set）
    pendingStatuses: new Set(), // F15：按作品 ID 跟踪并发片单操作
    detailRequestId: 0,      // F03：详情请求代次，旧响应不得覆盖新作品
    activeDetailId: null,    // 当前详情指向的作品 id
    detailId: null,          // F12：URL ?t= 中记录的详情作品 id
    userInitiatedFilter: false, // 用户主动变更筛选（区别于后退/前进恢复）
    restoreScroll: null,        // 待恢复的滚动位置（后退/前进导航）
    viewMode: 'grid',           // 列表展示模式：grid | list
};

// 视图模式持久化：localStorage 优先，无记录或存储不可用时回退网格
try { state.viewMode = localStorage.getItem('view_mode') === 'list' ? 'list' : 'grid'; } catch (_) { state.viewMode = 'grid'; }
// 观看地区持久化：只接受两位地区码
try { state.viewingRegion = localStorage.getItem('viewing_region_v1') || ''; } catch (_) { state.viewingRegion = ''; }
if (!/^[A-Z]{2}$/.test(state.viewingRegion)) state.viewingRegion = '';
// 6.6 惊喜发现范围与最近抽过的作品
try { state.surpriseScope = localStorage.getItem('surprise_scope_v1') || 'filters'; } catch (_) { state.surpriseScope = 'filters'; }
if (!['filters', 'watchlist', 'watching'].includes(state.surpriseScope)) state.surpriseScope = 'filters';
try {
    const surpriseRaw = JSON.parse(localStorage.getItem('surprise_recent_v1') || '[]');
    state.surpriseRecent = Array.isArray(surpriseRaw) ? surpriseRaw.map(Number).filter(Number.isInteger).slice(0, 10) : [];
} catch (_) { state.surpriseRecent = []; }

/* 播放平台只在详情中展示。六大主流保留品牌色；others 显示 TMDB 原始
 * 名称（provider_labels），中文服务做常见映射，未回填的显示“其他平台”。 */
const providerColors = {
    netflix: '#e06060', disney: '#7196dc', max: '#7483d9',
    amazon: '#55a9cf', apple: '#d8d8d2', hulu: '#67b98a',
    others: '#8b8a84',
};
const providerNames = {
    netflix: 'Netflix', disney: 'Disney+', max: 'Max',
    amazon: 'Prime Video', apple: 'Apple TV+', hulu: 'Hulu',
    others: '其他平台',
};
const OTHER_PROVIDER_ZH = {
    iQIYI: '爱奇艺', 'Tencent Video': '腾讯视频', Youku: '优酷',
    Bilibili: '哔哩哔哩', 'Mango TV': '芒果 TV',
};

function otherProviderLabel(labels) {
    const list = (labels || []).filter(Boolean);
    if (!list.length) return '其他平台';
    return list.map(name => OTHER_PROVIDER_ZH[name] || name).join(' / ');
}
const regionNames = {
    CN: '中国大陆（国产）', HK: '中国香港（港剧/港影）', TW: '中国台湾（台剧/台影）',
    JP: '日本（日剧/日影）', KR: '韩国（韩剧/韩影）', US: '美国（美剧/美影）',
    GB: '英国（英剧/英影）', CA: '加拿大', FR: '法国', DE: '德国',
    ES: '西班牙', IT: '意大利', IN: '印度', TH: '泰国', AU: '澳大利亚',
};
const regionShortNames = {
    CN: '中国大陆', HK: '中国香港', TW: '中国台湾', JP: '日本', KR: '韩国',
    US: '美国', GB: '英国', CA: '加拿大', FR: '法国', DE: '德国', ES: '西班牙',
    IT: '意大利', IN: '印度', TH: '泰国', AU: '澳大利亚',
};
const regionPriority = ['CN', 'HK', 'TW', 'JP', 'KR', 'US', 'GB', 'CA', 'FR', 'DE', 'ES', 'IT', 'IN', 'TH', 'AU'];
const regionDisplayNames = typeof Intl.DisplayNames === 'function'
    ? new Intl.DisplayNames(['zh-CN'], { type: 'region' })
    : null;
const watchStatusNames = {
    watchlist: '想看', watching: '在看', watched: '已看',
};
const ratingTierLabels = { great: '极佳', good: '优秀', fair: '良好' };
const GENRE_ZH = {
    Action: '动作', Adventure: '冒险', Animation: '动画', Comedy: '喜剧',
    Crime: '犯罪', Documentary: '纪录', Drama: '剧情', Family: '家庭',
    Fantasy: '奇幻', History: '历史', Horror: '恐怖', Music: '音乐',
    Mystery: '悬疑', Romance: '爱情', 'Science Fiction': '科幻',
    Thriller: '惊悚', War: '战争', Western: '西部',
};
// 真实库 genres_json 以中文为主，筛选直接使用中文标签
const GENRE_OPTIONS = ['剧情', '喜剧', '动作', '悬疑', '科幻', '爱情', '惊悚', '犯罪', '动画', '纪录', '家庭', '奇幻', '恐怖', '冒险', '历史', '战争', '音乐', '西部'];

function genreZh(name) {
    if (GENRE_ZH[name]) return GENRE_ZH[name];
    return name;
}

/* 6.4 搜索匹配原因与局部高亮 */
const MATCH_REASON_LABELS = {
    title: '片名匹配', original: '原名匹配', director: '导演匹配',
    cast: '演员匹配', overview: '简介提及', imdb: 'IMDb 编号',
};

function highlightSearch(text, query) {
    const value = String(text ?? '');
    const needle = (query || '').trim();
    if (!needle) return escapeHtml(value);
    const lower = value.toLowerCase();
    const target = needle.toLowerCase();
    if (!lower.includes(target)) return escapeHtml(value);
    let result = '';
    let index = 0;
    let found = lower.indexOf(target);
    while (found !== -1) {
        result += escapeHtml(value.slice(index, found));
        result += `<mark class="search-hit">${escapeHtml(value.slice(found, found + target.length))}</mark>`;
        index = found + target.length;
        found = lower.indexOf(target, index);
    }
    return result + escapeHtml(value.slice(index));
}

function matchReasonHtml(reason) {
    const label = MATCH_REASON_LABELS[reason];
    return label ? `<span class="match-reason">${label}</span>` : '';
}
const SKELETON_COUNT = 10;

let statsData = null;
let syncPollTimer = null;
let bootstrapPollTimer = null;
let previousFocus = null;
let currentDetail = null;
let displayMediaQuery = null;
let displayMediaQueryHandler = null;
let displayUpdateFrame = null;
let displayImageSignature = '';
let observedDisplayDpr = 1;

const CARD_POSTER_SIZES = '(max-width: 680px) 46vw, (max-width: 900px) 30vw, (max-width: 1180px) 23vw, (max-width: 1599px) 18vw, (max-width: 2099px) 15vw, (max-width: 2499px) 13vw, 11vw';
const DETAIL_POSTER_SIZES = '(max-width: 380px) 100px, (max-width: 680px) 112px, (max-width: 900px) 170px, 210px';
const LIST_POSTER_SIZES = '(max-width: 680px) 64px, 80px';

const posterFallback = `data:image/svg+xml,${encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="500" height="750" viewBox="0 0 500 750">
  <rect width="500" height="750" fill="#21211d"/>
  <rect x="185" y="290" width="130" height="160" rx="10" fill="none" stroke="#4a4942" stroke-width="6"/>
  <path d="M210 330h80M210 365h80M210 400h50" stroke="#4a4942" stroke-width="6" stroke-linecap="round"/>
  <text x="250" y="530" text-anchor="middle" fill="#7f7d75" font-family="Arial,sans-serif" font-size="24">暂无海报</text>
</svg>`)}`;
window.posterFallback = posterFallback;

class ApiError extends Error {
    constructor(status, body) {
        super(body?.detail || `HTTP ${status}`);
        this.status = status;
        this.body = body;
    }
}

async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 12000);
    if (options.signal) {
        if (options.signal.aborted) controller.abort();
        else options.signal.addEventListener('abort', () => controller.abort(), { once: true });
    }

    try {
        const response = await fetch(path, {
            ...options,
            signal: controller.signal,
            headers: {
                Accept: 'application/json',
                ...(options.body ? { 'Content-Type': 'application/json' } : {}),
                ...options.headers,
            },
        });
        const body = response.status === 204 ? null : await response.json().catch(() => null);
        if (!response.ok) throw new ApiError(response.status, body);
        return body;
    } catch (error) {
        if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试');
        throw error;
    } finally {
        clearTimeout(timer);
    }
}

/* ── 客户端缓存层 ──
 * detail：单条作品详情，TTL 5 分钟，LRU 上限 50 条
 * pages：分页列表结果，按筛选条件生成 cacheKey，TTL 2 分钟，LRU 上限 5 组
 * 策略：fresh 命中直接返回；stale 命中返回旧值并后台静默刷新（stale-while-revalidate）；
 *       miss 复用 in-flight promise 合并并发请求。sync 完成 / 网络恢复时整体失效。
 */
const cache = {
    detail: new Map(),   // id -> { data, ts, promise, prefetched, revalidating }
    pages: new Map(),    // cacheKey -> { pages: Map<pageNum, titles[]>, total, has_next, ts, inflight: Map, revalidating }
};
const DETAIL_TTL = 5 * 60 * 1000;
const PAGE_TTL = 2 * 60 * 1000;
const DETAIL_CACHE_MAX = 50;
const PAGE_CACHE_MAX = 5;
const PAGE_GROUP_MAX = 20; // F-cache收口：单筛选组最多保留页数，防无限滚动撑爆内存

/* LRU 淘汰：超限时删 Map 中最旧条目；命中时先删后插以刷新访问序 */
function evictOldest(map, max) {
    while (map.size > max) map.delete(map.keys().next().value);
}
function touchLru(map, key) {
    const value = map.get(key);
    if (value !== undefined) { map.delete(key); map.set(key, value); }
}

/* 分页缓存键：与 buildFilterParams 同源，search 归一化为 trim+lowercase。
 * 注意：所有影响结果集的筛选都必须入键，否则切换筛选会命中旧缓存。 */
function pageCacheKey() {
    return [
        state.type, state.region, state.rating,
        state.genre, state.maxRuntime, state.releasedDays,
        state.sort_by, state.order, state.watchStatus,
        state.excludeWatched && !state.watchStatus ? 'unwatched' : '',
        (state.search || '').trim().toLowerCase(),
    ].join('|');
}

/* 读取/写入分页缓存。F01：has_next/total 按页独立存储，不再跨页共享。
 * 返回 { data, total, has_next, fromCache, stale, refreshPromise }。
 * refreshPromise 非空时，调用方可 .then 在后台刷新完成后 patch UI（如 total 变化）。 */
async function getCachedPage(key, page, fetcher) {
    const now = Date.now();
    let entry = cache.pages.get(key);
    // LRU：命中即提升访问序
    if (entry) touchLru(cache.pages, key);

    if (entry) {
        const pageData = entry.pages.get(page);
        if (pageData) {
            const fresh = now - pageData.ts <= PAGE_TTL;
            if (fresh) {
                return { data: pageData.data, total: pageData.total, has_next: pageData.has_next, fromCache: true, stale: false, refreshPromise: null };
            }
            // stale：立即返回旧值，后台静默刷新当前页（不限 page 1）
            let refreshPromise = null;
            if (!entry.revalidating) {
                entry.revalidating = true;
                refreshPromise = (async () => {
                    try {
                        const data = await fetcher();
                        entry.pages.set(page, {
                            data: data.titles || [],
                            total: data.total,
                            has_next: Boolean(data.has_next),
                            ts: Date.now(),
                        });
                        entry.total = data.total;
                        entry.ts = Date.now();
                        evictGroupPages(entry);
                        touchLru(cache.pages, key);
                    } catch (_) { /* 保留旧值，不改 ts 以便下次仍可刷新 */ }
                    finally { entry.revalidating = false; }
                })();
            }
            return { data: pageData.data, total: pageData.total, has_next: pageData.has_next, fromCache: true, stale: true, refreshPromise };
        }
        // 该页未缓存但同 key 有其他页：合并并发 in-flight 请求
        if (entry.inflight.has(page)) {
            const shared = await entry.inflight.get(page);
            return { ...shared, fromCache: false, stale: false, refreshPromise: null };
        }
    } else {
        entry = { pages: new Map(), total: 0, has_next: false, ts: now, inflight: new Map(), revalidating: false };
        cache.pages.set(key, entry);
        evictOldest(cache.pages, PAGE_CACHE_MAX);
    }

    // miss：发请求并按页写入，in-flight 期间记录 promise 供并发合并
    const promise = (async () => {
        try {
            const data = await fetcher();
            const titles = data.titles || [];
            const record = {
                data: titles,
                total: data.total,
                has_next: Boolean(data.has_next),
                ts: Date.now(),
            };
            entry.pages.set(page, record);
            evictGroupPages(entry);
            entry.total = data.total;
            entry.has_next = Boolean(data.has_next);
            entry.ts = Date.now();
            touchLru(cache.pages, key);
            return { data: titles, total: data.total, has_next: Boolean(data.has_next) };
        } finally {
            entry.inflight.delete(page);
        }
    })();
    entry.inflight.set(page, promise);
    const result = await promise;
    return { ...result, fromCache: false, stale: false, refreshPromise: null };
}

function evictGroupPages(entry) {
    while (entry.pages.size > PAGE_GROUP_MAX) {
        entry.pages.delete(entry.pages.keys().next().value);
    }
}

/* 读取/写入详情缓存。F02：失败时清理与本次请求匹配的 promise，
 * 重试可发起新请求；旧 finally 不覆盖新请求。prefetched=true 表示预取来源 */
async function getCachedDetail(id, { prefetched = false } = {}) {
    const numId = Number(id);
    const now = Date.now();
    const entry = cache.detail.get(numId);

    if (entry) {
        touchDetailLru(numId);
        if (entry.data) {
            const fresh = now - entry.ts <= DETAIL_TTL;
            if (fresh) return entry.data;
            // stale：返回旧值并后台刷新
            if (!entry.revalidating) {
                entry.revalidating = true;
                revalidateDetail(numId).finally(() => {
                    const current = cache.detail.get(numId);
                    if (current) current.revalidating = false;
                });
            }
            return entry.data;
        }
        if (entry.promise) return entry.promise; // in-flight：复用同一请求
    }

    const promise = (async () => {
        try {
            const data = await api(`/api/titles/${numId}`);
            setDetail(numId, data, { prefetched });
            return data;
        } catch (error) {
            // 仅当仍是本次请求时清理，避免旧失败覆盖新 in-flight
            const current = cache.detail.get(numId);
            if (current && current.promise === promise) {
                if (current.data) current.promise = null;
                else cache.detail.delete(numId);
            }
            throw error;
        }
    })();

    if (entry) {
        entry.promise = promise;
    } else {
        cache.detail.set(numId, { data: null, ts: now, promise, prefetched, revalidating: false });
        evictOldest(cache.detail, DETAIL_CACHE_MAX);
    }
    // promise 异常时已在内部清理；此处附加空 catch 防未处理 rejection 告警由调用方处理
    return promise;
}

function touchDetailLru(numId) {
    const value = cache.detail.get(numId);
    if (value !== undefined) { cache.detail.delete(numId); cache.detail.set(numId, value); }
}

/* 写入详情缓存（fetch 完成 / PATCH 成功时调用） */
function setDetail(id, data, { prefetched = false } = {}) {
    const numId = Number(id);
    const entry = cache.detail.get(numId) || { data: null, ts: 0, promise: null, prefetched: false, revalidating: false };
    entry.data = data;
    entry.ts = Date.now();
    entry.promise = null;
    entry.prefetched = prefetched;
    cache.detail.set(numId, entry);
    evictOldest(cache.detail, DETAIL_CACHE_MAX);
}

/* 后台刷新详情：成功后若当前模态正展示该 id，仅同步状态变化（避免整屏重渲染闪烁） */
async function revalidateDetail(id) {
    try {
        const data = await api(`/api/titles/${id}`);
        const numId = Number(id);
        if (currentDetail?.id === numId) {
            const oldStatus = currentDetail.watch_status || '';
            setDetail(numId, data);
            currentDetail = data;
            const newStatus = data.watch_status || '';
            if (oldStatus !== newStatus) {
                document.querySelectorAll('.status-picker [data-set-status]').forEach(button => {
                    button.classList.toggle('active', button.dataset.setStatus === newStatus);
                });
                updateCardStatus(numId, newStatus);
            }
        } else {
            setDetail(numId, data);
        }
    } catch (_) { /* 后台刷新失败保留旧值 */ }
}

/* 预取详情：hover 250ms 后调用。已缓存且 fresh 或正在请求则跳过 */
function prefetchDetail(id) {
    const numId = Number(id);
    const entry = cache.detail.get(numId);
    if (entry?.data && Date.now() - entry.ts <= DETAIL_TTL) return;
    if (entry?.promise) return;
    getCachedDetail(numId, { prefetched: true }).catch(() => {});
}

/* PATCH 成功后用权威数据覆盖详情缓存 */
function patchDetailInCache(id, title) {
    const entry = cache.detail.get(Number(id));
    if (entry?.data) {
        entry.data = title;
        entry.ts = Date.now();
    }
}

/* 失效：视图内改状态导致结果集变化时按 key 清；无 key 清全部 */
function invalidatePageCache(key) {
    if (key) cache.pages.delete(key);
    else cache.pages.clear();
}

function clearAllCaches() {
    cache.detail.clear();
    cache.pages.clear();
}

function userMessage(error) {
    if (!navigator.onLine) return '网络连接已断开，请恢复网络后重试';
    if (error instanceof ApiError) {
        if (error.status === 404) return '这部作品已不存在，内容列表可能刚刚更新';
        if (error.status === 409 || error.status === 400) return error.body?.detail || '当前操作无法完成';
        if (error.status >= 500) return '服务暂时不可用，请稍后重试';
    }
    return error?.message || '操作失败，请稍后重试';
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
}

function sanitizeUrl(url) {
    if (!url) return '';
    try {
        const parsed = new URL(url, window.location.origin);
        if (['image.tmdb.org', 'api.image.tmdb.org'].includes(parsed.hostname)) return url;
        if (url.startsWith('data:image/')) return url;
    } catch (_) {
        return '';
    }
    return '';
}

function tmdbPosterUrl(url, size) {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl || safeUrl.startsWith('data:image/')) return safeUrl;
    try {
        const parsed = new URL(safeUrl, window.location.origin);
        const match = parsed.pathname.match(/^\/t\/p\/(?:w\d+|original)(\/.*)$/);
        if (!match) return safeUrl;
        return `${parsed.origin}/t/p/${size}${match[1]}${parsed.search}`;
    } catch (_) {
        return safeUrl;
    }
}

function responsivePosterAttributes(url, sizes) {
    const poster = sanitizeUrl(url) || posterFallback;
    if (poster.startsWith('data:image/')) return `src="${escapeHtml(poster)}"`;
    const candidates = [342, 500, 780]
        .map(width => `${tmdbPosterUrl(poster, `w${width}`)} ${width}w`)
        .join(', ');
    return `src="${escapeHtml(tmdbPosterUrl(poster, 'w500'))}" srcset="${escapeHtml(candidates)}" sizes="${escapeHtml(sizes)}" data-responsive-poster`;
}

function handlePosterError(image) {
    image.removeAttribute('srcset');
    image.removeAttribute('sizes');
    image.removeAttribute('data-responsive-poster');
    image.onerror = null;
    image.src = posterFallback;
}
window.handlePosterError = handlePosterError;

function handlePosterLoad(image) {
    image.classList.add('is-loaded');
    image.closest('.modal-poster')?.classList.add('is-loaded');
}
window.handlePosterLoad = handlePosterLoad;

function handleGridPosterLoad(image) {
    image.classList.add('is-loaded');
}
window.handleGridPosterLoad = handleGridPosterLoad;

const reduceMotionQuery = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false };

/* 卡片错峰入场：IntersectionObserver 按批内序号递增延迟 */
const cardEnterObserver = 'IntersectionObserver' in window
    ? new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            const card = entry.target;
            cardEnterObserver.unobserve(card);
            const index = Number(card.dataset.enterIndex || 0);
            card.style.transitionDelay = `${Math.min(index * 36, 324)}ms`;
            card.classList.add('is-visible');
            card.addEventListener('transitionend', () => { card.style.transitionDelay = ''; }, { once: true });
        });
    }, { rootMargin: '0px 0px -2% 0px', threshold: 0.01 })
    : null;

function prepareCardEntrance(card, index) {
    if (!cardEnterObserver || reduceMotionQuery.matches) return;
    card.classList.add('card-enter');
    card.dataset.enterIndex = String(index % 10);
    cardEnterObserver.observe(card);
}

function resetCardEntrance() {
    cardEnterObserver?.disconnect();
}

function animateNumber(element, target, decimals = 0) {
    const finalText = decimals
        ? Number(target).toFixed(decimals)
        : Math.round(Number(target)).toLocaleString();
    if (reduceMotionQuery.matches) {
        element.textContent = finalText;
        return;
    }
    const duration = 720;
    const start = performance.now();
    const tick = now => {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const value = Number(target) * eased;
        element.textContent = decimals ? value.toFixed(decimals) : Math.round(value).toLocaleString();
        if (progress < 1) requestAnimationFrame(tick);
        else element.textContent = finalText;
    };
    requestAnimationFrame(tick);
}

function displayLayoutBucket(width) {
    if (width >= 2500) return 'ultra';
    if (width >= 2100) return 'wide';
    if (width >= 1600) return 'large';
    if (width > 1180) return 'desktop';
    if (width > 900) return 'compact';
    if (width > 680) return 'tablet';
    return 'mobile';
}

function refreshResponsivePosters() {
    document.querySelectorAll('img[data-responsive-poster]').forEach(image => {
        const srcset = image.getAttribute('srcset');
        const sizes = image.getAttribute('sizes');
        if (sizes) image.setAttribute('sizes', sizes);
        if (srcset) image.setAttribute('srcset', srcset);
    });
}

function bindDisplayDensityListener(dpr) {
    if (!window.matchMedia) return;
    if (displayMediaQuery && displayMediaQueryHandler) {
        if (displayMediaQuery.removeEventListener) displayMediaQuery.removeEventListener('change', displayMediaQueryHandler);
        else displayMediaQuery.removeListener(displayMediaQueryHandler);
    }
    displayMediaQuery = window.matchMedia(`(resolution: ${dpr}dppx)`);
    displayMediaQueryHandler = () => scheduleDisplayAdaptation(true);
    if (displayMediaQuery.addEventListener) displayMediaQuery.addEventListener('change', displayMediaQueryHandler, { once: true });
    else displayMediaQuery.addListener(displayMediaQueryHandler);
}

function applyDisplayAdaptation(forceImageRefresh = false) {
    displayUpdateFrame = null;
    const viewport = window.visualViewport;
    const width = Math.round(viewport?.width || window.innerWidth || document.documentElement.clientWidth);
    const height = Math.round(viewport?.height || window.innerHeight || document.documentElement.clientHeight);
    const dpr = Math.max(1, Math.round((window.devicePixelRatio || 1) * 100) / 100);
    const root = document.documentElement;
    const bucket = displayLayoutBucket(width);
    const imageSignature = `${dpr}:${bucket}`;
    observedDisplayDpr = dpr;

    root.style.setProperty('--device-pixel-ratio', String(dpr));
    root.style.setProperty('--viewport-width', `${width}px`);
    root.style.setProperty('--viewport-height', `${height}px`);
    root.dataset.pixelDensity = dpr >= 2 ? 'high' : dpr >= 1.25 ? 'medium' : 'standard';
    root.dataset.viewport = bucket;

    if (forceImageRefresh || imageSignature !== displayImageSignature) {
        displayImageSignature = imageSignature;
        refreshResponsivePosters();
        bindDisplayDensityListener(dpr);
    }
}

function scheduleDisplayAdaptation(forceImageRefresh = false) {
    if (displayUpdateFrame) cancelAnimationFrame(displayUpdateFrame);
    displayUpdateFrame = requestAnimationFrame(() => applyDisplayAdaptation(forceImageRefresh));
}

function setupDisplayAdaptation() {
    applyDisplayAdaptation();
    window.addEventListener('resize', () => scheduleDisplayAdaptation(), { passive: true });
    window.visualViewport?.addEventListener('resize', () => scheduleDisplayAdaptation(), { passive: true });
    window.addEventListener('pageshow', () => scheduleDisplayAdaptation(true));
}

function ratingTier(rating) {
    if (!rating || rating <= 0) return null;
    if (rating >= 8) return 'great';
    if (rating >= 7.5) return 'good';
    return 'fair';
}

function primaryRegionLabel(countries, compact = false) {
    const code = Array.isArray(countries) ? countries[0] : countries;
    if (!code) return '';
    return displayRegionName(code, compact);
}

function displayRegionName(code, compact = false) {
    const normalized = String(code || '').toUpperCase();
    const custom = compact ? regionShortNames[normalized] : regionNames[normalized];
    if (custom) return custom;
    const localized = regionDisplayNames?.of(normalized);
    return localized && localized !== normalized ? localized : '其他地区';
}

/* 6.1 观看地区：固定常用地区；选择结果写入 localStorage，用于过滤详情渠道 */
const VIEWING_REGION_OPTIONS = ['', 'CN', 'HK', 'TW', 'JP', 'KR', 'US', 'GB', 'CA', 'AU', 'DE', 'FR', 'ES', 'IT', 'IN', 'TH', 'SG'];
function viewingRegionName(code) {
    if (!code) return '不限地区';
    return regionShortNames[code] || displayRegionName(code, true);
}
function viewingRegionSelectHtml() {
    return `<label class="viewing-region-picker"><span>观看地区</span>
        <select id="viewing-region-select" aria-label="选择观看地区">
            ${VIEWING_REGION_OPTIONS.map(code => `<option value="${code}"${code === state.viewingRegion ? ' selected' : ''}>${escapeHtml(viewingRegionName(code))}</option>`).join('')}
        </select></label>`;
}

function formatVerifiedDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('zh-CN');
}

document.addEventListener('DOMContentLoaded', async () => {
    hydrateStateFromUrl();
    setupDisplayAdaptation();
    setupEvents();
    setupAdminMenu();
    setupFilterToggle();
    setupModalSwipe();
    setupWall();
    setupInfiniteScroll();
    setupBackToTop();
    syncControlsFromState();
    renderSkeletons();
    renderRecentRow();
    // 首屏主列表立即加载，不被统计/同步状态阻塞；最近更新视图走独立数据源
    if (state.section === 'releases') loadReleases();
    else loadTitles();
    // F12：直接以 ?t=作品ID 打开时恢复详情（不重复压入历史）
    const initialDetailId = Number(new URLSearchParams(window.location.search).get('t') || 0);
    if (Number.isInteger(initialDetailId) && initialDetailId > 0) {
        showDetail(initialDetailId, { fromHistory: true });
    }
    await Promise.allSettled([loadStats(), loadSyncStatus()]);
    // 列表已先行加载，此处仅补全计数与状态，不再阻塞首屏
});

function hydrateStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const valid = (value, allowed, fallback = '') => allowed.includes(value) ? value : fallback;
    state.type = valid(params.get('type') || '', ['', 'movie', 'tv']);
    state.search = (params.get('q') || '').slice(0, 100);
    state.region = /^[A-Za-z]{2}$/.test(params.get('region') || '') ? params.get('region').toUpperCase() : '';
    state.genre = (params.get('genre') || '').slice(0, 40);
    const runtimeParam = Number(params.get('runtime') || params.get('max_runtime') || 0);
    state.maxRuntime = [0, 90, 120].includes(runtimeParam) ? runtimeParam : 0;
    state.rating = valid(params.get('rating') || '0', ['0', '7', '7.5', '8'], '0');
    state.rating = Number(state.rating);
    const view = params.get('view') || '';
    state.section = ['discover', 'library', 'releases'].includes(view) ? view : 'discover';
    // 6.7 我的片单专属排序只在 library 视图生效
    const sortAllowed = state.section === 'library'
        ? ['rating', 'release_date', 'updated_at', 'priority']
        : ['rating', 'release_date'];
    state.sort_by = valid(params.get('sort') || 'release_date', sortAllowed, 'release_date');
    state.order = params.get('order') === 'asc' ? 'asc' : 'desc';
    if (state.section === 'library') {
        state.libraryStatus = valid(params.get('status') || '', ['watchlist', 'watching', 'watched']) || 'watchlist';
    }
    state.watchStatus = state.section === 'library' ? state.libraryStatus : '';
    state.excludeWatched = params.get('unwatched') === '1';
    const fresh = Number(params.get('fresh') || 0);
    state.releasedDays = [0, 30, 90, 180].includes(fresh) ? fresh : 0;
}

/* 6.2：近期新片窗口 → API 起始日期（YYYY-MM-DD） */
function releasedAfterDate(days) {
    return new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
}

function updateUrl() {
    const params = new URLSearchParams();
    if (state.search) params.set('q', state.search);
    if (state.type) params.set('type', state.type);
    if (state.region) params.set('region', state.region);
    if (state.rating) params.set('rating', String(state.rating));
    if (state.genre) params.set('genre', state.genre);
    if (state.maxRuntime) params.set('max_runtime', String(state.maxRuntime));
    if (state.sort_by !== 'release_date') params.set('sort', state.sort_by);
    if (state.order === 'asc') params.set('order', 'asc');
    if (state.section !== 'discover') params.set('view', state.section);
    if (state.section === 'library' && state.watchStatus) params.set('status', state.watchStatus);
    if (state.releasedDays) params.set('fresh', String(state.releasedDays));
    if (state.excludeWatched) params.set('unwatched', '1');
    // F12：详情打开时保留 ?t=，避免筛选 URL 同步把分享链接抹掉
    if (state.detailId && !document.getElementById('detail-modal').classList.contains('hidden')) {
        params.set('t', String(state.detailId));
    }
    const query = params.toString();
    // 将当前滚动位置存入 history state，供后退/前进导航时恢复
    history.replaceState({ ...(history.state || {}), scrollY: window.scrollY }, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
}

function syncControlsFromState() {
    document.getElementById('search-input').value = state.search;
    document.getElementById('clear-search').classList.toggle('hidden', !state.search);
    document.getElementById('region-filter').value = state.region;
    document.getElementById('rating-filter').value = String(state.rating);
    const genreSelect = document.getElementById('genre-filter');
    if (genreSelect) {
        if (!genreSelect.dataset.filled) {
            GENRE_OPTIONS.forEach(name => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = genreZh(name);
                genreSelect.appendChild(option);
            });
            genreSelect.dataset.filled = 'true';
        }
        genreSelect.value = GENRE_OPTIONS.includes(state.genre) ? state.genre : '';
        if (state.genre && !GENRE_OPTIONS.includes(state.genre)) {
            genreSelect.appendChild(new Option(state.genre, state.genre));
            genreSelect.value = state.genre;
        }
    }
    const runtimeSelect = document.getElementById('runtime-filter');
    if (runtimeSelect) runtimeSelect.value = String(state.maxRuntime || 0);
    document.getElementById('sort-filter').value = state.sort_by;
    const orderBtn = document.getElementById('sort-order-btn');
    if (orderBtn) {
        orderBtn.dataset.order = state.order;
        orderBtn.setAttribute('aria-label', state.order === 'asc' ? '当前为升序，点击切换为降序' : '当前为降序，点击切换为升序');
        orderBtn.setAttribute('aria-pressed', state.order === 'asc' ? 'true' : 'false');
    }
    const unwatchedBtn = document.getElementById('unwatched-toggle');
    if (unwatchedBtn) {
        unwatchedBtn.classList.toggle('active', state.excludeWatched);
        unwatchedBtn.setAttribute('aria-pressed', state.excludeWatched ? 'true' : 'false');
    }
    // 5.2 主导航与 6.2 内容模式按钮状态
    document.body.dataset.section = state.section;
    document.getElementById('status-filters')?.classList.toggle('hidden', state.section !== 'library');
    document.querySelectorAll('#primary-nav [data-section]').forEach(button => {
        const active = button.dataset.section === state.section;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    const premiumBtn = document.getElementById('mode-premium');
    if (premiumBtn) {
        const active = premiumActive();
        premiumBtn.classList.toggle('active', active);
        premiumBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    const freshBtn = document.getElementById('mode-fresh');
    if (freshBtn) {
        const active = state.releasedDays > 0;
        freshBtn.classList.toggle('active', active);
        freshBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    const surpriseScopeSelect = document.getElementById('surprise-scope');
    if (surpriseScopeSelect) surpriseScopeSelect.value = state.surpriseScope;
    document.querySelectorAll('#sort-filter option[data-library-only]').forEach(option => {
        option.hidden = state.section !== 'library';
        option.disabled = state.section !== 'library';
    });
    document.querySelectorAll('#type-filters [data-type]').forEach(button => {
        const active = button.dataset.type === state.type;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.querySelectorAll('#status-filters [data-status]').forEach(button => {
        const active = button.dataset.status === state.watchStatus;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.getElementById('titles-grid')?.setAttribute('data-view', state.viewMode);
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        const active = button.dataset.view === state.viewMode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}

async function loadStats() {
    try {
        statsData = await api('/api/stats');
        const byStatus = statsData.by_status || {};
        const listTotal = Object.values(byStatus).reduce((sum, count) => sum + Number(count || 0), 0);
        document.getElementById('overview-stats').innerHTML = `
            <div><dt>收录</dt><dd data-stat="total">—</dd></div>
            <div><dt title="仅统计已有评分的作品">平均分</dt><dd data-stat="avg">—</dd></div>
            <div><dt>我的片单</dt><dd data-stat="list">—</dd></div>`;
        animateNumber(document.querySelector('[data-stat="total"]'), Number(statsData.total || 0));
        animateNumber(document.querySelector('[data-stat="avg"]'), Number(statsData.avg_rating || 0), 1);
        animateNumber(document.querySelector('[data-stat="list"]'), listTotal);
        const footerTotal = document.getElementById('footer-total');
        if (footerTotal) footerTotal.textContent = Number(statsData.total || 0).toLocaleString();
        const freshness = document.getElementById('footer-freshness');
        if (freshness) {
            const parts = [];
            if (statsData.last_synced_at) {
                const d = new Date(statsData.last_synced_at);
                parts.push(`数据更新于 ${formatRelativeDate(d)}`);
            }
            const ds = statsData.ratings_dataset || {};
            if (ds.stale) {
                parts.push(`评分库${ds.age_hours != null ? `${Math.round(ds.age_hours / 24)}天前` : '缺失'} · 可能漏掉新开分作品`);
                freshness.className = 'freshness-stale';
            } else if (ds.age_hours != null) {
                parts.push('评分库新鲜');
                freshness.className = '';
            }
            freshness.textContent = parts.length ? `（${parts.join('，')}）` : '';
        }
        document.getElementById('status-count-all').textContent = Number(statsData.total || 0).toLocaleString();
        const libraryCount = document.getElementById('status-count-library');
        if (libraryCount) libraryCount.textContent = listTotal.toLocaleString();
        ['watchlist', 'watching', 'watched'].forEach(status => {
            document.getElementById(`status-count-${status}`).textContent = Number(byStatus[status] || 0).toLocaleString();
        });

        const select = document.getElementById('region-filter');
        const current = state.region;
        select.innerHTML = '<option value="">全部地区</option>';
        const regions = [...(statsData.regions || [])].sort((a, b) => {
            const aCode = a.country_code;
            const bCode = b.country_code;
            const aPriority = regionPriority.indexOf(aCode);
            const bPriority = regionPriority.indexOf(bCode);
            if (aPriority !== -1 || bPriority !== -1) {
                if (aPriority === -1) return 1;
                if (bPriority === -1) return -1;
                return aPriority - bPriority;
            }
            return displayRegionName(aCode).localeCompare(displayRegionName(bCode), 'zh-CN');
        });
        regions.forEach(region => {
            const option = document.createElement('option');
            option.value = region.country_code;
            option.textContent = `${displayRegionName(region.country_code)} · ${Number(region.count || 0).toLocaleString()}`;
            select.appendChild(option);
        });
        if (current && !regions.some(region => region.country_code === current)) {
            select.appendChild(new Option(displayRegionName(current), current));
        }
        select.value = current;
    } catch (error) {
        showToast('概览数据暂时无法加载', 'warn');
    }
}

async function loadSyncStatus() {
    try {
        const status = await api('/api/sync/status');
        renderSyncStatus(status);
        return status;
    } catch (_) {
        return null;
    }
}

function renderSyncStatus(status) {
    const info = document.getElementById('sync-info');
    const button = document.getElementById('sync-button');
    if (status.enabled === false) {
        info.textContent = '';
        info.className = '';
        button.classList.add('hidden');
        return;
    }

    button.classList.remove('hidden');
    const sync = status.sync || {};
    const latestFinished = status.latest_finished_sync || {};
    const progress = sync.last_result || status.latest_run || {};
    button.disabled = Boolean(sync.running);
    button.classList.toggle('syncing', Boolean(sync.running));

    if (sync.running) {
        const provider = progress.current_provider ? providerNames[progress.current_provider] || progress.current_provider : '';
        const step = progress.provider_total ? `${progress.current_provider_index || 0}/${progress.provider_total}` : '';
        info.textContent = `同步中${step ? ` · ${step}` : ''}${provider ? ` · ${provider}` : ''}`;
        info.className = 'sync-pill active';
        startSyncPolling();
        return;
    }
    if (latestFinished.status === 'failed') {
        info.textContent = '上次同步失败';
        info.className = 'sync-pill danger';
        return;
    }
    if (latestFinished.finished_at) {
        const date = new Date(latestFinished.finished_at);
        info.textContent = latestFinished.status === 'partial'
            ? `部分同步 · ${formatRelativeDate(date)}`
            : `已更新 · ${formatRelativeDate(date)}`;
        info.className = latestFinished.status === 'partial' ? 'sync-pill warn' : 'sync-pill';
        return;
    }
    info.textContent = '';
    info.className = '';
}

function formatRelativeDate(date) {
    if (Number.isNaN(date.getTime())) return '未知时间';
    const days = Math.floor((Date.now() - date.getTime()) / 86400000);
    if (days <= 0) return '今天';
    if (days === 1) return '昨天';
    if (days < 7) return `${days} 天前`;
    return date.toLocaleDateString('zh-CN');
}

async function triggerSync() {
    const button = document.getElementById('sync-button');
    button.disabled = true;
    button.classList.add('syncing');
    try {
        await api('/api/sync', { method: 'POST' });
        showToast('同步已开始，可以继续浏览');
        await loadSyncStatus();
        startSyncPolling();
    } catch (error) {
        showToast(userMessage(error), 'error');
        button.disabled = false;
        button.classList.remove('syncing');
    }
}

function startSyncPolling() {
    if (syncPollTimer) return;
    syncPollTimer = setInterval(async () => {
        const status = await loadSyncStatus();
        if (status && !status.sync?.running) {
            clearInterval(syncPollTimer);
            syncPollTimer = null;
            showToast('内容同步完成，片库已刷新');
            clearAllCaches(); // 同步完成，列表与详情缓存均已过期
            await Promise.allSettled([loadStats()]);
            resetAndLoad();
        }
    }, 4000);
}

async function loadTitles() {
    if (state.section === 'releases') return; // 最近更新视图由 loadReleases 负责
    if (state.loading || !state.hasMore) return;
    const version = state.requestVersion;
    state.loading = true;
    document.documentElement.dataset.rankSort = state.sort_by === 'rating' ? 'true' : '';
    const loader = document.getElementById('scroll-loader');
    const end = document.getElementById('scroll-end');
    const grid = document.getElementById('titles-grid');
    grid.setAttribute('aria-busy', 'true');
    if (state.page > 1) loader.classList.remove('hidden');
    end.classList.add('hidden');

    const cacheKey = pageCacheKey();
    const currentPage = state.page;
    const fetcher = () => api(`/api/titles?${buildFilterParams({ page: String(currentPage), limit: String(state.limit) })}`);
    try {
        const result = await getCachedPage(cacheKey, currentPage, fetcher);
        if (version !== state.requestVersion) return;
        const titles = result.data || [];
        const isFirstPage = currentPage === 1;
        renderTitles(titles, isFirstPage, (currentPage - 1) * state.limit);
        const loaded = Math.min((currentPage - 1) * state.limit + titles.length, result.total);
        const noun = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部剧集' : '部作品';
        document.getElementById('stats-info').innerHTML = `找到 <strong>${Number(result.total).toLocaleString()}</strong> ${noun}${result.total ? ` · 已显示 ${loaded}` : ''}`;
        state.hasMore = Boolean(result.has_next);
        document.getElementById('scroll-sentinel').classList.toggle('hidden', !state.hasMore);
        if (!state.hasMore && result.total > 0) end.classList.remove('hidden');
        if (titles.length) state.page += 1;
        // F12：用请求页而非递增后的 state.page 判断首屏
        if (isFirstPage && result.total === 0 && !hasActiveFilters()) checkBootstrapSync();
        // 后退/前进导航恢复滚动位置（仅首页加载路径，用户主动筛选不触发）
        if (isFirstPage && state.restoreScroll != null && !state.userInitiatedFilter) {
            const target = state.restoreScroll;
            state.restoreScroll = null;
            requestAnimationFrame(() => window.scrollTo({ top: target, behavior: 'auto' }));
        }
        // stale-while-revalidate：后台刷新完成后若 total 变化且用户仍在该筛选，静默更新计数
        if (result.refreshPromise) {
            result.refreshPromise.then(() => {
                if (version !== state.requestVersion || pageCacheKey() !== cacheKey) return;
                const entry = cache.pages.get(cacheKey);
                if (!entry) return;
                const strongEl = document.querySelector('#stats-info strong');
                if (strongEl) strongEl.textContent = Number(entry.total).toLocaleString();
            });
        }
    } catch (error) {
        if (version !== state.requestVersion) return;
        state.hasMore = false;
        document.getElementById('scroll-sentinel').classList.add('hidden');
        if (state.page === 1) {
            renderError(userMessage(error));
            document.getElementById('stats-info').textContent = '内容加载失败';
        } else {
            end.innerHTML = `<button type="button" class="btn-retry" data-action="retry-more">加载失败，点击重试</button>`;
            end.classList.remove('hidden');
        }
    } finally {
        if (version === state.requestVersion) {
            state.loading = false;
            loader.classList.add('hidden');
            grid.setAttribute('aria-busy', 'false');
            if (!document.getElementById('detail-modal').classList.contains('hidden')) {
                // 列表变化后重算当前详情在列表中的位置，保证前后切换可用
                if (state.activeDetailId != null) {
                    state.currentDetailIndex = state.loadedTitleIds.indexOf(state.activeDetailId);
                }
                updateDetailNav();
            }
        }
    }
}

/* 5.2 / 6.8：最近更新视图 —— 近 30 天上映/首播作品，复用网格与列表卡片 */
const RELEASES_DAYS = 30;
const RELEASES_LIMIT = 60;

async function loadReleases() {
    const version = state.requestVersion;
    const grid = document.getElementById('titles-grid');
    state.hasMore = false;
    state.loading = false;
    document.getElementById('scroll-sentinel').classList.add('hidden');
    document.getElementById('scroll-end').classList.add('hidden');
    document.documentElement.dataset.rankSort = '';
    try {
        const data = await api(`/api/releases?days=${RELEASES_DAYS}&limit=${RELEASES_LIMIT}`);
        if (version !== state.requestVersion) return;
        const titles = data.titles || [];
        if (titles.length) {
            renderTitles(titles, true);
        } else {
            grid.innerHTML = `<div class="empty-state">
                <div class="empty-icon-wrap">${searchIcon()}</div>
                <div class="empty-title">最近没有新的上映或首播</div>
                <p>继续浏览发现页，或稍后回来看看</p>
                <div class="empty-actions"><button type="button" class="btn-clear-filters" data-action="goto-discover">去发现</button></div>
            </div>`;
        }
        document.getElementById('stats-info').innerHTML = `近 ${RELEASES_DAYS} 天更新 · <strong>${titles.length}</strong> 部作品`;
    } catch (error) {
        if (version !== state.requestVersion) return;
        renderError(userMessage(error));
        document.getElementById('stats-info').textContent = '更新列表加载失败';
    }
}

function renderSkeletons() {
    const grid = document.getElementById('titles-grid');
    resetCardEntrance();
    grid.setAttribute('aria-busy', 'true');
    grid.innerHTML = Array.from({ length: SKELETON_COUNT }, () => `
        <div class="skeleton-card" aria-hidden="true">
            <div class="skeleton-poster"></div>
            <div class="skeleton-info"><div class="skeleton-line medium"></div><div class="skeleton-line short"></div><div class="skeleton-line tiny"></div></div>
        </div>`).join('');
}

function renderTitles(titles, clear, rankBase = 0) {
    const grid = document.getElementById('titles-grid');
    if (clear) {
        resetCardEntrance();
        grid.innerHTML = '';
        state.loadedTitleIds = [];
    }
    if (clear && !titles.length) {
        renderEmptyState();
        return;
    }
    const showRank = state.section !== 'releases' && state.sort_by === 'rating';
    // 按当前视图模式选择卡片工厂（网格卡 / 横向列表项）
    const createCard = state.viewMode === 'list' ? createTitleListItem : createTitleCard;
    const fragment = document.createDocumentFragment();
    titles.forEach((title, index) => {
        state.loadedTitleIds.push(Number(title.id));
        const card = createCard(title, showRank ? rankBase + index + 1 : null);
        prepareCardEntrance(card, index);
        fragment.appendChild(card);
    });
    grid.appendChild(fragment);
    if (state.batchMode) refreshBatchSelectionUI();
}

function cardYear(dateStr) {
    return (dateStr || '').slice(0, 4) || '年份待定';
}

function firstGenre(genresJson) {
    const list = parseJsonList(genresJson);
    return list.length ? genreZh(list[0]) : '';
}

/* 播放平台只在详情中展示，卡片默认只回答：叫什么、是什么类型、口碑怎样。 */
function createTitleCard(title, rank = null) {
    const card = document.createElement('article');
    card.className = 'title-card';
    card.dataset.titleId = title.id;
    card.dataset.watchStatus = title.watch_status || '';
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const status = title.watch_status || '';
    const genre = firstGenre(title.genres_json);
    const typeLabel = title.type === 'movie' ? '电影' : '剧集';
    const priority = Number(title.priority) || 0;
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                ${cardPosterMarkup(title, CARD_POSTER_SIZES, typeLabel)}
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
                ${rank ? `<span class="rank-badge" data-rank="${rank}">${rank}</span>` : ''}
                <span class="poster-rating"${tier ? ` data-tier="${tier}"` : ''}><span class="r-num">${rating ? rating.toFixed(1) : '待开分'}</span>${rating ? '<small>IMDb</small>' : ''}</span>
                ${priority > 0 ? `<span class="priority-badge" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>` : ''}
            </div>
            <div class="card-info">
                <h2 class="card-title">${highlightSearch(title.title, state.search)}</h2>
                <div class="card-meta"><span>${typeLabel}</span><span>${escapeHtml(cardYear(title.release_date))}</span>${genre ? `<span>${escapeHtml(genre)}</span>` : ''}${state.search ? matchReasonHtml(title.match_reason) : ''}</div>
            </div>
        </button>
        <div class="status-menu-wrap">
            <button class="status-menu-trigger ${status ? 'has-status' : ''}" type="button" aria-label="设置 ${escapeHtml(title.title)} 的片单状态" aria-haspopup="menu" aria-expanded="false">
                ${bookmarkIcon(status)}
            </button>
            ${statusMenuHtml(title.id, status)}
        </div>`;
    const image = card.querySelector('.poster-wrap img');
    if (image?.complete && image.naturalWidth > 0) image.classList.add('is-loaded');
    return card;
}

/* 横向列表卡片：左海报 / 中标题+简介+元数据 / 右评分。
 * 播放平台只在详情中展示，不复用网格角标，评分收归右侧独立列。 */
function createTitleListItem(title, rank = null) {
    const card = document.createElement('article');
    card.className = 'title-card is-list';
    card.dataset.titleId = title.id;
    card.dataset.watchStatus = title.watch_status || '';
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const status = title.watch_status || '';
    const region = primaryRegionLabel(title.origin_countries, true);
    const typeLabel = title.type === 'movie' ? '电影' : '剧集';
    const priority = Number(title.priority) || 0;
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                ${cardPosterMarkup(title, LIST_POSTER_SIZES, typeLabel)}
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
            </div>
            <div class="list-info">
                <h2 class="card-title">${highlightSearch(title.title, state.search)}</h2>
                <p class="card-overview">${highlightSearch(title.overview || '暂无剧情简介', state.search)}</p>
                <div class="card-meta"><span>${typeLabel}</span><span>${escapeHtml(title.release_date || '日期待定')}</span>${region ? `<span>${escapeHtml(region)}</span>` : ''}${state.search ? matchReasonHtml(title.match_reason) : ''}</div>
            </div>
            <div class="list-side">
                ${priority > 0 ? `<span class="list-priority" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>` : ''}
                ${rank ? `<span class="list-rank" aria-label="评分排名第 ${rank}">#${rank}</span>` : ''}
                <span class="list-rating"${tier ? ` data-tier="${tier}"` : ''}>${rating ? rating.toFixed(1) : '待开分'}<small>IMDb</small></span>
            </div>
        </button>
        <div class="status-menu-wrap">
            <button class="status-menu-trigger ${status ? 'has-status' : ''}" type="button" aria-label="设置 ${escapeHtml(title.title)} 的片单状态" aria-haspopup="menu" aria-expanded="false">
                ${bookmarkIcon(status)}
            </button>
            ${statusMenuHtml(title.id, status)}
        </div>`;
    const image = card.querySelector('.poster-wrap img');
    if (image?.complete && image.naturalWidth > 0) image.classList.add('is-loaded');
    return card;
}

/* 无海报兜底：统一文字封面，不用播放占位符（避免看起来像加载失败） */
function posterTextCover(title, meta) {
    return `<div class="poster-text-cover"><span>${escapeHtml(title)}</span>${meta ? `<small>${escapeHtml(meta)}</small>` : ''}</div>`;
}

/* 卡片用（网格/列表）：有海报走响应式图片，无海报走文字封面 */
function cardPosterMarkup(title, sizes, typeLabel) {
    const poster = sanitizeUrl(title.poster_url);
    if (poster && !poster.startsWith('data:image/')) {
        return `<img ${responsivePosterAttributes(poster, sizes)} alt="${escapeHtml(title.title)} 海报" loading="lazy" decoding="async" onload="window.handleGridPosterLoad(this)" onerror="window.handlePosterError(this)">`;
    }
    return posterTextCover(title.title, `${typeLabel} · ${cardYear(title.release_date)}`);
}

/* 切换网格/列表视图：localStorage 持久化，重载列表以应用新布局（分页缓存命中，秒级返回） */
function setViewMode(mode) {
    if (!['grid', 'list'].includes(mode) || state.viewMode === mode) return;
    state.viewMode = mode;
    try { localStorage.setItem('view_mode', mode); } catch (_) { /* 存储不可用时仅本次生效 */ }
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        const active = button.dataset.view === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.getElementById('titles-grid')?.setAttribute('data-view', mode);
    resetAndLoad();
}

/* 6.2 内容模式：高分精选 = 评分 7.5+ 且按评分排序；近期新片 = 时间窗口 + 最新排序。
 * 按钮状态由筛选条件推导，用户手动改筛选后自然回到“自定义”，不会假装仍在某个模式。 */
function premiumActive() {
    return state.releasedDays === 0 && state.sort_by === 'rating' && state.rating >= 7.5;
}

function applyMode(mode) {
    if (mode === 'premium') {
        if (premiumActive()) {
            state.rating = 0;
            state.sort_by = 'release_date';
        } else {
            state.rating = 7.5;
            state.sort_by = 'rating';
        }
        state.order = 'desc';
        state.releasedDays = 0;
    } else if (mode === 'fresh') {
        if (state.releasedDays > 0) {
            state.releasedDays = 0;
        } else {
            state.releasedDays = 90;
            state.rating = 0;
            state.sort_by = 'release_date';
            state.order = 'desc';
        }
    }
    resetAndLoad();
}

/* 5.2 主导航：发现 / 我的片单 / 最近更新 */
function setSection(section) {
    if (!['discover', 'library', 'releases'].includes(section) || state.section === section) return;
    if (state.section === 'library') state.libraryStatus = state.watchStatus || state.libraryStatus;
    state.section = section;
    if (section === 'library') state.watchStatus = state.libraryStatus || 'watchlist';
    else state.watchStatus = '';
    // 我的片单专属排序离开该视图后回退到默认排序
    if (section !== 'library' && ['updated_at', 'priority'].includes(state.sort_by)) {
        state.sort_by = 'release_date';
        state.order = 'desc';
    }
    if (section !== 'library') setBatchMode(false);
    resetAndLoad();
}

/* 6.7 批量管理：选择、批量改状态/优先级、移出片单 */
function syncBatchUI() {
    const count = state.selectedIds.size;
    const countEl = document.getElementById('batch-count');
    if (countEl) countEl.textContent = `已选 ${count} 部`;
    document.querySelectorAll('#batch-bar [data-action^="batch-"]').forEach(button => {
        const action = button.dataset.action;
        if (action === 'batch-select-all' || action === 'batch-exit') return;
        button.disabled = count === 0;
    });
    const priority = document.getElementById('batch-priority');
    if (priority) {
        priority.disabled = count === 0;
        priority.value = '';
    }
}

function refreshBatchSelectionUI() {
    document.querySelectorAll('.title-card').forEach(card => {
        card.classList.toggle('is-selected', state.selectedIds.has(Number(card.dataset.titleId)));
    });
}

function setBatchMode(enabled) {
    state.batchMode = Boolean(enabled);
    if (!state.batchMode) state.selectedIds = new Set();
    document.body.classList.toggle('batch-mode', state.batchMode);
    document.getElementById('batch-bar')?.classList.toggle('hidden', !state.batchMode);
    const toggle = document.getElementById('batch-toggle');
    toggle?.classList.toggle('active', state.batchMode);
    toggle?.setAttribute('aria-pressed', state.batchMode ? 'true' : 'false');
    syncBatchUI();
    refreshBatchSelectionUI();
}

function toggleBatchSelection(id) {
    const numId = Number(id);
    if (state.selectedIds.has(numId)) state.selectedIds.delete(numId);
    else state.selectedIds.add(numId);
    refreshBatchSelectionUI();
    syncBatchUI();
}

async function applyBatchAction(payload, successText) {
    const ids = [...state.selectedIds];
    if (!ids.length) return;
    try {
        const result = await api('/api/titles/batch', {
            method: 'PATCH',
            body: JSON.stringify({ ids, ...payload }),
        });
        showToast(`${successText} ${result.updated} 部${result.skipped ? ` · 跳过 ${result.skipped} 部` : ''}`);
        clearAllCaches();
        state.selectedIds = new Set();
        await loadStats().catch(() => {});
        resetAndLoad();
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

function bookmarkIcon(filled = '') {
    return `<svg viewBox="0 0 24 24" fill="${filled ? 'currentColor' : 'none'}"><path d="M7 4.5h10v15l-5-3-5 3z"/></svg>`;
}

function statusMenuHtml(id, current) {
    const options = [
        ['', '不在片单'], ['watchlist', '想看'], ['watching', '在看'], ['watched', '已看'],
    ];
    return `<div class="status-menu hidden" role="menu" aria-label="选择片单状态">
        ${options.map(([value, label]) => `<button type="button" role="menuitem" data-title-id="${id}" data-set-status="${value}" class="${current === value ? 'active' : ''}">${label}</button>`).join('')}
    </div>`;
}

function renderEmptyState() {
    const filtered = hasActiveFilters();
    const statusLabel = watchStatusNames[state.watchStatus];
    const title = statusLabel ? `${statusLabel}片单还是空的` : '没有找到匹配的作品';
    const copy = statusLabel
        ? `浏览全部作品，把感兴趣的内容加入“${statusLabel}”`
        : filtered ? '试试减少筛选条件，或换一个关键词搜索' : '内容库暂时为空，请稍后等待同步完成';
    // 片单子视图为空时，“查看全部作品”已不存在，改为引导去发现页
    const inLibrary = state.section === 'library' && Boolean(statusLabel);
    document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
        <div class="empty-icon-wrap">${searchIcon()}</div>
        <div class="empty-title">${title}</div><p>${copy}</p>
        <div class="empty-actions">
        ${filtered ? `<button type="button" class="btn-clear-filters" data-action="${inLibrary ? 'goto-discover' : 'clear-filters'}">${inLibrary ? '去发现页找片' : '查看全部作品'}</button>` : ''}
        ${state.search ? '<button type="button" class="btn-retry" data-action="imdb-import">用 IMDb 链接补录</button>' : ''}
        </div>
    </div>`;
}

function renderError(message) {
    document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
        <div class="empty-icon-wrap">${alertIcon()}</div>
        <div class="empty-title">内容没有加载出来</div><p>${escapeHtml(message)}</p>
        <button type="button" class="btn-retry" data-action="retry">重新加载</button>
    </div>`;
}

function searchIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 4 4"/></svg>';
}
function alertIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.1"/></svg>';
}

async function showDetail(id, options = {}) {
    const opts = typeof options === 'object' && options !== null ? options : {};
    const modal = document.getElementById('detail-modal');
    const content = document.getElementById('detail-content');
    const numId = Number(id);
    const requestId = ++state.detailRequestId;
    // F12：详情进入浏览器历史，支持前进/后退与分享链接。
    // 普通打开 push；前后切换 replace，避免历史堆栈被连续切换塞满。
    if (!opts.fromHistory) {
        const params = new URLSearchParams(window.location.search);
        const currentT = params.get('t');
        if (currentT !== String(numId)) {
            params.set('t', String(numId));
            const url = `${window.location.pathname}?${params.toString()}`;
            if (opts.replace) {
                // 同一详情会话内切换：保留原条目标记，不再新增历史
                history.replaceState({ ...(history.state || {}), t: numId }, '', url);
            } else {
                history.pushState({ detailView: true, t: numId, scrollY: window.scrollY }, '', url);
            }
        } else if (opts.replace && history.state?.detailView) {
            history.replaceState({ ...history.state, t: numId }, '', window.location.href);
        }
    }
    state.detailId = numId;
    state.activeDetailId = numId;
    if (modal.classList.contains('hidden')) previousFocus = document.activeElement;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    state.currentDetailIndex = state.loadedTitleIds.indexOf(numId);
    updateDetailNav();
    document.querySelector('.modal-panel')?.scrollTo({ top: 0 });
    document.getElementById('close-modal').focus();
    // 缓存命中（fresh 或 stale）时跳过 loading 占位，显著降低详情打开延迟
    const hasCached = Boolean(cache.detail.get(numId)?.data);
    if (!hasCached) {
        content.innerHTML = '<div class="detail-loading"><span class="spinner" aria-label="详情加载中"></span></div>';
    }
    try {
        const detail = await getCachedDetail(numId);
        // F03：过期响应直接丢弃；关闭后也不再渲染
        if (requestId !== state.detailRequestId || state.activeDetailId !== numId) return;
        currentDetail = detail;
        if (!modal.classList.contains('hidden')) renderDetail(currentDetail, requestId);
    } catch (error) {
        if (requestId !== state.detailRequestId || state.activeDetailId !== numId) return;
        content.innerHTML = `<div class="detail-error"><div><p>${escapeHtml(userMessage(error))}</p><button type="button" class="btn-retry" data-action="retry-detail" data-title-id="${numId}">重新加载</button></div></div>`;
    }
}

function updateDetailNav() {
    const prev = document.getElementById('nav-prev');
    const next = document.getElementById('nav-next');
    const i = state.currentDetailIndex;
    const list = state.loadedTitleIds;
    const has = i >= 0 && list.length > 1;
    if (prev) prev.disabled = !has || i <= 0;
    if (next) next.disabled = !has || i >= list.length - 1;
}

function navigateDetail(direction) {
    const list = state.loadedTitleIds;
    const target = state.currentDetailIndex + direction;
    if (target < 0 || target >= list.length) return;
    // F12：同一详情的连续切换用 replace，返回键直接回到原列表
    showDetail(list[target], { replace: true });
}

function buildFilterParams(extra = {}) {
    const params = new URLSearchParams({ sort_by: state.sort_by, order: state.order, ...extra });
    if (state.type) params.set('type', state.type);
    if (state.search) params.set('search', state.search);
    if (state.region) params.set('region', state.region);
    if (state.rating > 0) params.set('min_rating', String(state.rating));
    if (state.genre) params.set('genre', state.genre);
    if (state.maxRuntime > 0) params.set('max_runtime', String(state.maxRuntime));
    if (state.releasedDays > 0) params.set('released_after', releasedAfterDate(state.releasedDays));
    if (state.excludeWatched && !state.watchStatus) params.set('exclude_watched', 'true');
    if (state.watchStatus) params.set('watch_status', state.watchStatus);
    return params;
}

/* 6.6 惊喜发现：范围可选（当前筛选/我的想看/我的在看），避开最近抽过的作品 */
function rememberSurprise(id) {
    const numId = Number(id);
    state.surpriseRecent = [numId, ...state.surpriseRecent.filter(item => Number(item) !== numId)].slice(0, 10);
    try { localStorage.setItem('surprise_recent_v1', JSON.stringify(state.surpriseRecent)); } catch (_) { /* 忽略 */ }
}

function surpriseScopeLabel(scope = state.surpriseScope) {
    return scope === 'filters' ? '当前筛选范围' : `我的${watchStatusNames[scope]}`;
}

async function surpriseFetch(scope, page) {
    if (scope === 'filters') {
        return getCachedPage(pageCacheKey(), page, () => api(`/api/titles?${buildFilterParams({ page: String(page), limit: String(state.limit) })}`));
    }
    const params = new URLSearchParams({
        watch_status: scope, sort_by: 'rating', order: 'desc',
        page: String(page), limit: String(state.limit),
    });
    const data = await api(`/api/titles?${params}`);
    return { data: data.titles || [], total: data.total, has_next: data.has_next };
}

function renderSurpriseBar(title, total, scope) {
    const bar = document.getElementById('surprise-bar');
    const text = document.getElementById('surprise-text');
    if (!bar || !text) return;
    const bits = [surpriseScopeLabel(scope), `共 ${Number(total).toLocaleString()} 部`];
    if (title.runtime) bits.push(`${title.type === 'movie' ? '' : '单集约 '}${Number(title.runtime)} 分钟`);
    if (title.providers?.length) bits.push('有观看渠道');
    text.innerHTML = `已为你挑《${escapeHtml(title.title)}》 · ${escapeHtml(bits.join(' · '))}`;
    bar.classList.remove('hidden');
}

function hideSurpriseBar() {
    document.getElementById('surprise-bar')?.classList.add('hidden');
    state.surpriseLastId = null;
}

async function surprisePick() {
    const btn = document.getElementById('surprise-btn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('loading');
    try {
        const scope = state.surpriseScope;
        const first = await surpriseFetch(scope, 1);
        const total = Number(first.total || 0);
        if (!total) {
            showToast(scope === 'filters' ? '当前筛选范围内没有可挑选的作品' : `你的${watchStatusNames[scope]}还是空的`, 'warn');
            return;
        }
        // 最多重试 6 次避开最近抽过的作品；范围很小时接受重复
        let title = null;
        for (let attempt = 0; attempt < 6; attempt++) {
            const idx = Math.floor(Math.random() * total);
            const page = Math.floor(idx / state.limit) + 1;
            const pos = idx % state.limit;
            const result = page === 1 ? first : await surpriseFetch(scope, page);
            const candidate = (result.data || [])[pos] || (result.data || [])[0];
            if (!candidate) break;
            if (!state.surpriseRecent.includes(Number(candidate.id)) || attempt === 5) {
                title = candidate;
                break;
            }
        }
        if (!title) { showToast('未找到作品，再试一次', 'warn'); return; }
        rememberSurprise(title.id);
        state.surpriseLastId = Number(title.id);
        showDetail(title.id);
        renderSurpriseBar(title, total, scope);
    } catch (error) {
        showToast(userMessage(error), 'error');
    } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
    }
}

function parseJsonList(value) {
    if (!value) return [];
    if (Array.isArray(value)) return value;
    try { const parsed = JSON.parse(value); return Array.isArray(parsed) ? parsed : []; }
    catch (_) { return []; }
}

function renderDetail(title, requestId = null) {
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const safePoster = sanitizeUrl(title.poster_url);
    const hasRealPoster = Boolean(safePoster) && !safePoster.startsWith('data:image/');
    const detailBackdrop = hasRealPoster ? tmdbPosterUrl(safePoster, 'w780') : '';
    const heroBgStyle = detailBackdrop
        ? `style="background-image:url('${escapeHtml(detailBackdrop)}')"`
        : 'style="background: linear-gradient(135deg, var(--surface-raised), var(--surface) 70%)"';
    const original = title.original_title && title.original_title !== title.title ? title.original_title : '';
    const details = title.provider_details || [];
    // 六大平台显示中文品牌名；others 展开 TMDB 原始名称（labels），
    // 老数据无 label 时诚实显示“其他平台”，下次同步回填后自动展示具体名称。
    const detailChannelName = (d) => d.provider === 'others'
        ? otherProviderLabel(d.labels)
        : (providerNames[d.provider] || d.provider);
    // 6.1 观看地区：优先展示覆盖所选地区的渠道；未核验地区的渠道单独标注。
    const viewingRegion = state.viewingRegion;
    const renderChannel = (d, pending = false) => {
        const regions = (d.regions || []).map(displayRegionName).join('、');
        const tip = pending || !regions ? '地区待确认' : `覆盖地区：${regions}`;
        return `<span class="modal-provider${pending ? ' is-pending' : ''}" title="${escapeHtml(tip)}">
            <span class="p-dot" style="background:${providerColors[d.provider] || '#7f7d75'}"></span>${escapeHtml(detailChannelName(d))}${!pending && regions ? ` · ${escapeHtml(regions)}` : ''}</span>`;
    };
    const matchedChannels = [];
    const pendingChannels = [];
    details.forEach(d => {
        const regions = d.regions || [];
        if (!viewingRegion) matchedChannels.push(d);
        else if (!regions.length) pendingChannels.push(d);
        else if (regions.includes(viewingRegion)) matchedChannels.push(d);
    });
    let providerList = '';
    if (viewingRegion) {
        providerList = matchedChannels.map(d => renderChannel(d)).join('');
        if (!matchedChannels.length) {
            providerList += `<p class="provider-note provider-note-strong">在${escapeHtml(viewingRegionName(viewingRegion))}暂无已核验的观看渠道。</p>`;
        }
        if (pendingChannels.length) {
            providerList += `<span class="provider-pending-label">以下渠道尚未核验该地区：</span>${pendingChannels.map(d => renderChannel(d, true)).join('')}`;
        }
    } else {
        providerList = details.map(d => renderChannel(d)).join('');
    }
    const providerNote = !details.length ? ''
        : viewingRegion
            ? '<p class="provider-note">已按你选择的观看地区过滤渠道；虚线渠道尚未核验该地区。</p>'
            : (details.every(d => !(d.regions && d.regions.length))
                ? '<p class="provider-note">已发现观看渠道，需确认你所在地区是否可播。</p>'
                : '');
    const providerVerified = details.map(d => d.last_seen_at).filter(Boolean).sort().pop();
    const status = title.watch_status || '';
    const imdbLink = title.imdb_id ? `<a class="modal-link" href="https://www.imdb.com/title/${encodeURIComponent(title.imdb_id)}/" target="_blank" rel="noopener noreferrer">在 IMDb 查看 ${externalIcon()}</a>` : '';
    const tmdbType = title.type === 'movie' ? 'movie' : 'tv';
    const watchLink = `<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}/watch" target="_blank" rel="noopener noreferrer">去哪看（TMDB 观看指南） ${externalIcon()}</a>`;
    const region = primaryRegionLabel(title.origin_countries, true);
    const cast = parseJsonList(title.cast_json).slice(0, 4);
    const castExtra = parseJsonList(title.cast_json).length > 4
        ? `<button type="button" class="link-more" data-action="expand-cast">展开全部 ${parseJsonList(title.cast_json).length} 位</button>` : '';
    const genres = parseJsonList(title.genres_json).map(genreZh);
    const votes = Number(title.rating_votes || 0);
    const votesText = votes ? `${votes.toLocaleString()} 票` : '样本较少';
    const metaExtra = [
        title.director ? `<span class="meta-tag">导演 ${escapeHtml(title.director)}</span>` : '',
        title.runtime ? `<span class="meta-tag">${title.type === 'movie' ? '' : '单集约 '}${Number(title.runtime)} 分钟</span>` : '',
        title.seasons ? `<span class="meta-tag">${Number(title.seasons)} 季${title.episodes ? ` · ${Number(title.episodes)} 集` : ''}</span>` : '',
    ].join('');
    const trailer = title.trailer_key ? `<a class="modal-link" href="https://www.youtube.com/watch?v=${encodeURIComponent(title.trailer_key)}" target="_blank" rel="noopener noreferrer">观看预告片 ${externalIcon()}</a>` : '';
    // 6.7 我的记录：优先级 / 个人评分 / 备注 / 观看日期（未加入片单时给出提示）
    const personalSection = status
        ? `<div class="modal-section-title">我的记录</div>
           <div class="personal-fields" data-title-id="${title.id}">
               <label class="personal-field"><span>优先级</span>
                   <select class="personal-select" data-personal="priority">
                       ${[[0, '普通'], [1, '优先'], [2, '必看']].map(([value, label]) => `<option value="${value}"${Number(title.priority || 0) === value ? ' selected' : ''}>${label}</option>`).join('')}
                   </select></label>
               <label class="personal-field"><span>我的评分</span>
                   <select class="personal-select" data-personal="personal_rating">
                       ${[['', '未评分'], [10, '10 分'], [9, '9 分'], [8, '8 分'], [7, '7 分'], [6, '6 分'], [5, '5 分'], [4, '4 分'], [3, '3 分'], [2, '2 分'], [1, '1 分']].map(([value, label]) => `<option value="${value}"${String(title.personal_rating ?? '') === String(value) ? ' selected' : ''}>${label}</option>`).join('')}
                   </select></label>
               <label class="personal-field personal-field-note"><span>备注</span>
                   <input class="personal-note-input" type="text" maxlength="200" data-personal="note" placeholder="例如：周末陪家人看" value="${escapeHtml(title.note || '')}"></label>
               ${title.watched_at ? `<p class="provider-note personal-watched">观看于 ${escapeHtml(formatVerifiedDate(title.watched_at))}</p>` : ''}
           </div>`
        : '<p class="provider-note">加入片单后，可记录优先级、个人评分与备注。</p>';
    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" ${heroBgStyle}></div>
            <div class="modal-hero-content">
                <div class="modal-hero-rating">
                    <div class="detail-score"${tier ? ` data-tier="${tier}"` : ''} role="img" aria-label="${rating ? `IMDb 评分 ${rating.toFixed(1)}，${escapeHtml(votesText)}` : '待开分，新片'}">
                        <span class="detail-score-num">${rating ? rating.toFixed(1) : '—'}</span>
                        <span class="detail-score-source">${rating ? 'IMDb' : '新片'}</span>
                    </div>
                    <div class="rating-tier"${tier ? ` data-tier="${tier}"` : ''}>${rating ? `${escapeHtml(votesText)}${tier ? ` · ${ratingTierLabels[tier]}` : ''}` : '待开分'}</div>
                </div>
                <div class="modal-hero-title"><h2 id="modal-title">${escapeHtml(title.title)}</h2>${original ? `<p>${escapeHtml(original)}</p>` : ''}</div>
            </div>
        </div>
        <div class="modal-body">
            <div class="modal-poster-shell">
                <div class="modal-poster${hasRealPoster ? '' : ' is-fallback is-loaded'}">
                    ${hasRealPoster
                        ? `<img class="modal-poster-image" ${responsivePosterAttributes(safePoster, DETAIL_POSTER_SIZES)} width="500" height="750" alt="${escapeHtml(title.title)} 海报" decoding="async" fetchpriority="high" onload="window.handlePosterLoad(this)" onerror="window.handlePosterError(this)">`
                        : posterTextCover(title.title, title.type === 'movie' ? '电影' : '剧集')}
                </div>
            </div>
            <div class="modal-summary">
                <div class="meta-tags">
                    <span class="meta-tag">${title.type === 'movie' ? '电影' : '剧集'}</span>
                    ${region ? `<span class="meta-tag">${escapeHtml(region)}</span>` : ''}
                    <span class="meta-tag">${escapeHtml(title.release_date || '日期待定')}</span>
                    ${metaExtra}
                </div>
            </div>
            <div class="modal-actions">
                <div class="status-picker" data-title-id="${title.id}">
                    ${[['', '未加入'], ['watchlist', '想看'], ['watching', '在看'], ['watched', '已看']].map(([value, label]) => `<button type="button" data-set-status="${value}" class="${status === value ? 'active' : ''}">${label}</button>`).join('')}
                </div>
                ${providerList ? `<a class="btn-channel" href="#channels" data-action="goto-channels">查看观看渠道</a>` : ''}
                <button type="button" class="btn-copy-link" data-action="copy-link" aria-label="复制这部作品的链接">复制链接</button>
            </div>
            <div class="modal-details">
                ${personalSection}
                <div class="modal-section-title">剧情简介</div>
                <div class="modal-overview">${escapeHtml(title.overview || '暂无剧情简介')}</div>
                ${cast.length ? `<div class="modal-cast">主演：${cast.map(name => escapeHtml(name)).join(' / ')} ${castExtra}</div>` : ''}
                ${genres.length ? `<div class="modal-genres">${genres.map(g => `<span class="genre-tag">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
                ${providerList ? `<div class="channels-head"><div class="modal-section-title" id="channels">观看渠道</div>${viewingRegionSelectHtml()}</div><div class="modal-providers">${providerList}</div>${providerNote}${providerVerified ? `<p class="provider-note">最近核验：${escapeHtml(formatVerifiedDate(providerVerified))}</p>` : ''}` : ''}
                <div class="modal-section-title">资料与观看指南</div>
                <div class="modal-links">${imdbLink}<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}" target="_blank" rel="noopener noreferrer">在 TMDB 查看 ${externalIcon()}</a>${watchLink}${trailer}</div>
                <div class="modal-section-title">同类推荐</div>
                <div class="related-row" id="related-row"><span class="related-loading">正在加载推荐…</span></div>
            </div>
        </div>`;
    recordRecentView(title); // 详情渲染即计入最近浏览
    loadRelated(title.id, requestId);
}

async function loadRelated(id, requestId = null) {
    const row = document.getElementById('related-row');
    if (!row) return;
    try {
        const data = await api(`/api/titles/${encodeURIComponent(id)}/related?limit=12`);
        // F03：详情已切换时不更新旧推荐区
        if (requestId !== null && (requestId !== state.detailRequestId || state.activeDetailId !== Number(id))) return;
        const items = data.titles || [];
        if (!items.length) { row.innerHTML = '<span class="related-empty">暂无同类推荐</span>'; return; }
        row.innerHTML = items.map(item => `
            <button type="button" class="related-chip" data-related-id="${item.id}" aria-label="查看 ${escapeHtml(item.title)}${item.reason ? `，${escapeHtml(item.reason)}` : ''}">
                ${sanitizeUrl(item.poster_url) ? `<img src="${escapeHtml(sanitizeUrl(item.poster_url))}" alt="" loading="lazy" decoding="async" width="90" height="135">` : ''}
                <span class="related-title">${escapeHtml(item.title)}</span>
                <span class="related-rating">${item.imdb_rating ? Number(item.imdb_rating).toFixed(1) : '待开分'}${item.reason ? ` · ${escapeHtml(item.reason)}` : ''}</span>
            </button>`).join('');
        row.querySelectorAll('[data-related-id]').forEach(btn => {
            btn.addEventListener('click', () => showDetail(btn.getAttribute('data-related-id')));
        });
    } catch (_) {
        row.innerHTML = '<span class="related-empty">推荐加载失败</span>';
    }
}

/* F12/6.9：复制当前作品的可分享链接（含 ?t=），失败时回退手动复制提示 */
async function copyCurrentLink(titleId) {
    if (!titleId) return;
    const url = new URL(window.location.href);
    url.searchParams.set('t', String(titleId));
    const text = url.toString();
    const fallbackCopy = () => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand('copy');
        textarea.remove();
        return ok;
    };
    try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
        else if (!fallbackCopy()) throw new Error('copy unavailable');
        showToast('作品链接已复制，可直接分享');
    } catch (_) {
        showToast('复制失败，请从地址栏手动复制', 'warn');
    }
}

async function exportWatchlist() {
    try {
        const data = await api('/api/watchlist/export');
        const blob = new Blob([JSON.stringify({
            schema_version: data.schema_version || 1,
            exported_at: data.exported_at || new Date().toISOString(),
            count: data.count, items: data.items,
        }, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `streaming-watchlist-${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        showToast(`已导出 ${data.count} 部片单`);
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

/* F-restore：读取备份 → 与当前片单对比预览 → 用户确认后合并导入（可重复执行） */
async function importWatchlistFile(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    let parsed;
    try {
        parsed = JSON.parse(await file.text());
    } catch {
        showToast('备份文件无法解析，请选择导出的 JSON', 'error');
        return;
    }
    const items = Array.isArray(parsed) ? parsed : parsed.items;
    if (!Array.isArray(items) || !items.length) {
        showToast('备份中没有可恢复的片单条目', 'warn');
        return;
    }
    const keyOf = (item) => `${item.type}:${item.tmdb_id}`;
    let currentKeys = new Set();
    try {
        const current = await api('/api/watchlist/export');
        currentKeys = new Set((current.items || []).map(keyOf));
    } catch { /* 预览失败也允许继续，由服务端幂等合并 */ }
    const valid = items.filter(item => item && item.tmdb_id && (item.type === 'movie' || item.type === 'tv') && item.watch_status);
    const invalid = items.length - valid.length;
    const addCount = valid.filter(item => !currentKeys.has(keyOf(item))).length;
    const existCount = valid.length - addCount;
    const ok = window.confirm(
        `从备份恢复片单？\n新增 ${addCount} 部 · 已存在 ${existCount} 部（状态不一致会被覆盖）${invalid ? `\n无效条目 ${invalid} 部将被跳过` : ''}`,
    );
    if (!ok) return;
    try {
        const result = await api('/api/watchlist/import', {
            method: 'POST',
            body: JSON.stringify({ items: valid }),
        });
        showToast(`恢复完成：新增 ${result.added} · 更新 ${result.updated} · 跳过 ${result.skipped}`);
        clearAllCaches();
        await Promise.allSettled([loadStats()]);
        resetAndLoad();
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

/* F-import：显式 IMDb 补录入口，展示 imported / pending / failed */
async function importByImdb(prefill = '') {
    const reference = window.prompt('输入 IMDb ID 或作品链接（例如 tt1234567），补录到内容库', prefill || '');
    if (!reference || !reference.trim()) return;
    showToast('正在补录，请稍候…');
    try {
        const result = await api('/api/titles/import', {
            method: 'POST',
            body: JSON.stringify({ imdb: reference.trim() }),
        });
        if (result.status === 'pending') {
            showToast(`已进入待处理：${result.title || result.tmdb_id}（${result.pending_reason || '等待开分或补全资料'}）`, 'warn');
        } else {
            showToast(`补录成功：${result.title || result.tmdb_id}`);
        }
        clearAllCaches();
        resetAndLoad();
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

function externalIcon() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M14 5h5v5M19 5l-8 8M17 13v5H6V7h5"/></svg>';
}

/* ── 最近浏览：localStorage 持久化，跨会话保留，上限 20 条 ── */
const RECENT_MAX = 20;
const RECENT_KEY = 'recent_viewed_v2';

function getRecentViewed() {
    try {
        const raw = localStorage.getItem(RECENT_KEY) || sessionStorage.getItem('recent_viewed') || '[]';
        const list = JSON.parse(raw);
        return Array.isArray(list) ? list : [];
    }
    catch (_) { return []; } // 数据损坏时视为空
}

function setRecentViewed(list) {
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); }
    catch (_) {
        try { sessionStorage.setItem('recent_viewed', JSON.stringify(list)); }
        catch (_) { /* 隐私模式等场景静默失败 */ }
    }
}

/* 记录浏览：同 id 去重后置顶，截断上限 */
function recordRecentView(title) {
    const list = getRecentViewed().filter(item => Number(item.id) !== Number(title.id));
    list.unshift({ id: Number(title.id), title: title.title, poster: sanitizeUrl(title.poster_url) || '' });
    setRecentViewed(list.slice(0, RECENT_MAX));
    renderRecentRow();
}

/* 渲染最近浏览：有数据才显示；默认折叠为单行，点击展开（5.9：不再挤占首屏正文） */
function renderRecentRow() {
    const row = document.getElementById('recent-row');
    const scroller = document.getElementById('recent-scroller');
    if (!row || !scroller) return;
    const list = getRecentViewed();
    if (!list.length) {
        row.classList.add('hidden');
        scroller.innerHTML = '';
        return;
    }
    const count = document.getElementById('recent-count');
    if (count) count.textContent = String(list.length);
    scroller.innerHTML = list.map(item => `
        <button type="button" class="recent-chip" data-recent-id="${item.id}" aria-label="查看 ${escapeHtml(item.title)}">
            <img src="${escapeHtml(item.poster || posterFallback)}" alt="" loading="lazy" decoding="async" width="48" height="72">
            <span>${escapeHtml(item.title)}</span>
        </button>`).join('');
    row.classList.remove('hidden');
}

function toggleRecentRow(force) {
    const row = document.getElementById('recent-row');
    const toggle = document.getElementById('recent-toggle');
    if (!row || !toggle) return;
    const open = typeof force === 'boolean' ? force : !row.classList.contains('is-open');
    row.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

/* ── 乐观更新：状态切换先改本地 UI，API 失败时回滚 ──
 * 快照包含旧状态、DOM 统计计数文本与缓存引用，回滚时逐项还原，
 * 保证视觉一致性与数据最终一致（PATCH 成功后用权威数据覆盖缓存）。
 */

/* 从当前详情或卡片 dataset 派生作品当前状态 */
function readCurrentStatus(id) {
    const numId = Number(id);
    if (currentDetail?.id === numId) return currentDetail.watch_status || '';
    const card = document.querySelector(`.title-card[data-title-id="${CSS.escape(String(id))}"]`);
    return card?.dataset.watchStatus || '';
}

/* 构建回滚快照：DOM 计数文本 + 详情缓存引用 */
function buildStatusSnapshot(id, newStatus) {
    const countTexts = {};
    ['all', 'watchlist', 'watching', 'watched'].forEach(key => {
        countTexts[key] = document.getElementById(`status-count-${key}`)?.textContent ?? null;
    });
    const listEl = document.querySelector('[data-stat="list"]');
    return {
        id: Number(id),
        oldStatus: readCurrentStatus(id),
        newStatus,
        countTexts,
        listText: listEl?.textContent ?? null,
        detailRef: cache.detail.get(Number(id)),
    };
}

/* 解析计数文本（含 toLocaleString 千分位）并安全增减后重渲染 */
function bumpCountEl(element, delta) {
    if (!element) return;
    const current = Number((element.textContent || '0').replace(/[^\d]/g, '')) || 0;
    element.textContent = Math.max(0, current + delta).toLocaleString();
}

/* 本地统计计数乐观增减：状态桶移动，all（总收录）恒不变 */
function applyStatusCounts(oldStatus, newStatus) {
    if (oldStatus === newStatus) return;
    if (oldStatus) bumpCountEl(document.getElementById(`status-count-${oldStatus}`), -1);
    if (newStatus) bumpCountEl(document.getElementById(`status-count-${newStatus}`), +1);
    // “我的片单”汇总 = 三个状态桶之和：进出片单时 ±1
    const listEl = document.querySelector('[data-stat="list"]');
    if (!oldStatus && newStatus) bumpCountEl(listEl, +1);
    if (oldStatus && !newStatus) bumpCountEl(listEl, -1);
}

/* 回滚时按快照原样还原统计计数文本 */
function restoreStatusCounts(snapshot) {
    Object.entries(snapshot.countTexts).forEach(([key, text]) => {
        const el = document.getElementById(`status-count-${key}`);
        if (el && text != null) el.textContent = text;
    });
    const listEl = document.querySelector('[data-stat="list"]');
    if (listEl && snapshot.listText != null) listEl.textContent = snapshot.listText;
}

/* 同步模态内状态选择器的高亮 */
function syncPickerActive(id, status) {
    document.querySelectorAll(`.status-picker[data-title-id="${id}"] [data-set-status]`).forEach(button => {
        button.classList.toggle('active', button.dataset.setStatus === status);
    });
}

/* 乐观应用新状态：卡片、详情缓存、计数、模态选择器一次同步 */
function applyStatusOptimistic(id, newStatus) {
    const numId = Number(id);
    const oldStatus = readCurrentStatus(numId); // 必须在 updateCardStatus 改 dataset 之前读取
    updateCardStatus(numId, newStatus);
    const cached = cache.detail.get(numId)?.data;
    if (cached) cached.watch_status = newStatus;
    syncPickerActive(numId, newStatus);
    applyStatusCounts(oldStatus, newStatus);
}

/* 失败回滚：反向还原所有已改动的视觉与数据 */
function rollbackStatus(snapshot) {
    updateCardStatus(snapshot.id, snapshot.oldStatus);
    if (snapshot.detailRef?.data) snapshot.detailRef.data.watch_status = snapshot.oldStatus;
    if (currentDetail?.id === snapshot.id) {
        currentDetail = { ...currentDetail, watch_status: snapshot.oldStatus };
        syncPickerActive(snapshot.id, snapshot.oldStatus);
    }
    restoreStatusCounts(snapshot);
}

async function setTitleStatus(id, watchStatus, sourceButton) {
    const numId = Number(id);
    // 幂等短路：目标状态与当前一致时不发请求
    if (readCurrentStatus(numId) === watchStatus) { closeStatusMenus(); return; }
    // F15：按作品跟踪并发，A 未完成时 B 仍可操作
    if (state.pendingStatuses.has(numId)) {
        showToast('操作进行中，请稍候', 'warn');
        return;
    }
    const snapshot = buildStatusSnapshot(numId, watchStatus);
    applyStatusOptimistic(numId, watchStatus);
    state.pendingStatuses.add(numId);
    state.optimisticPending = numId; // 兼容旧调试字段
    const scope = sourceButton?.closest('.status-picker, .status-menu');
    scope?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
        const title = await api(`/api/titles/${numId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ watch_status: watchStatus }),
        });
        state.pendingStatuses.delete(numId);
        state.optimisticPending = state.pendingStatuses.size ? [...state.pendingStatuses][0] : null;
        // 以权威数据覆盖详情缓存（含最新 watch_status 与时间戳）
        setDetail(numId, title);
        if (currentDetail?.id === numId) {
            currentDetail = title;
            syncPickerActive(numId, watchStatus);
        }
        // F04：状态写成功后失效全部分页缓存，避免其他视图/排序沿用旧成员集合
        invalidatePageCache();
        showToast(watchStatus ? `已加入“${watchStatusNames[watchStatus]}”` : '已从片单移除');
        // 后台校正统计计数（乐观值通常已一致，仅防并发漂移）
        loadStats().catch(() => {});
        // 当前视图按状态过滤且该作品已移出结果集 → 刷新列表
        if (state.watchStatus) {
            // 同一状态桶内成员变化（如空想看页新增）也需刷新
            resetAndLoad();
        } else if (state.excludeWatched && watchStatus === 'watched') {
            // 隐藏已看模式下新标记“已看”的作品应立即离开结果集
            resetAndLoad();
        }
    } catch (error) {
        state.pendingStatuses.delete(numId);
        state.optimisticPending = state.pendingStatuses.size ? [...state.pendingStatuses][0] : null;
        rollbackStatus(snapshot);
        // F15：失败后以权威统计覆盖，避免全局快照覆盖并发成功项
        loadStats().catch(() => {});
        showToast(userMessage(error), 'error');
    } finally {
        scope?.querySelectorAll('button').forEach(button => { button.disabled = false; });
        closeStatusMenus();
    }
}

/* 6.7 保存个人记录字段：成功后用权威数据覆盖详情缓存，不整屏重渲染以避免输入焦点丢失 */
async function savePersonalField(id, field, rawValue) {
    const numId = Number(id);
    let value;
    if (field === 'priority') value = Number(rawValue) || 0;
    else if (field === 'personal_rating') value = rawValue === '' ? 0 : Number(rawValue);
    else value = String(rawValue ?? '').slice(0, 200);
    try {
        const title = await api(`/api/titles/${numId}/preference`, {
            method: 'PATCH',
            body: JSON.stringify({ [field]: value }),
        });
        setDetail(numId, title);
        if (currentDetail?.id === numId) currentDetail = title;
        showToast(field === 'note' ? '备注已保存' : '已保存');
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

function updateCardStatus(id, status) {
    const card = document.querySelector(`.title-card[data-title-id="${CSS.escape(String(id))}"]`);
    if (!card) return;
    card.dataset.watchStatus = status;
    const poster = card.querySelector('.poster-wrap');
    poster.querySelector('.status-badge')?.remove();
    if (status) poster.insertAdjacentHTML('afterbegin', `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>`);
    const trigger = card.querySelector('.status-menu-trigger');
    trigger.classList.toggle('has-status', Boolean(status));
    trigger.innerHTML = bookmarkIcon(status);
    card.querySelectorAll('.status-menu [data-set-status]').forEach(button => {
        button.classList.toggle('active', button.dataset.setStatus === status);
    });
}

function finalizeCloseModal() {
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    document.body.style.overflow = '';
    currentDetail = null;
    state.currentDetailIndex = -1;
    // F03：关闭即让在途详情请求失效
    state.detailRequestId += 1;
    state.activeDetailId = null;
    state.detailId = null;
    previousFocus?.focus?.();
}

function closeModal(options = {}) {
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) return;
    // F12：由浏览器后退触发的关闭直接收起；主动关闭时优先回退历史条目。
    if (!options.fromHistory && state.detailId != null && history.state?.detailView) {
        history.back();
        return;
    }
    if (!options.fromHistory && state.detailId != null) {
        const url = new URL(window.location.href);
        url.searchParams.delete('t');
        history.replaceState({ ...(history.state || {}), detailView: false }, '', url.toString());
    }
    finalizeCloseModal();
}

/* 键盘帮助浮层开关 */
function toggleShortcuts(show) {
    const overlay = document.getElementById('shortcuts-overlay');
    if (!overlay) return;
    overlay.classList.toggle('hidden', !show);
    if (show) {
        previousFocus = document.activeElement;
        document.getElementById('close-shortcuts')?.focus();
    } else {
        previousFocus?.focus?.();
    }
}

/* 移动端模态框下滑关闭手势（底部 sheet 模式） */
let swipeState = null;
const SWIPE_THRESHOLD = 80;       // px
const SWIPE_VELOCITY = 0.4;       // px/ms

function setupModalSwipe() {
    const panel = document.querySelector('.modal-panel');
    if (!panel) return;
    panel.addEventListener('touchstart', (e) => {
        // 仅在移动端底部 sheet 模式下激活
        if (!window.matchMedia('(max-width: 680px)').matches) return;
        const t = e.touches[0];
        swipeState = { startY: t.clientY, startTime: Date.now(), dy: 0, dragging: false };
    }, { passive: true });
    panel.addEventListener('touchmove', (e) => {
        if (!swipeState || swipeState.dy < 0) return;
        const t = e.touches[0];
        const dy = t.clientY - swipeState.startY;
        if (dy > 6 && !swipeState.dragging) {
            swipeState.dragging = true;
            panel.style.transition = 'none';
        }
        if (swipeState.dragging && dy > 0) {
            swipeState.dy = dy;
            // 非线性阻尼：越快越难拉
            const dampened = dy * Math.max(0.35, 1 - dy / 500);
            panel.style.transform = `translateY(${dampened}px)`;
            e.preventDefault();
        }
    }, { passive: false });
    panel.addEventListener('touchend', () => {
        if (!swipeState) return;
        const { dy, dragging, startTime } = swipeState;
        const elapsed = Date.now() - startTime;
        const velocity = dy / Math.max(elapsed, 1);
        panel.style.transition = '';
        panel.style.transform = '';
        swipeState = null;
        if (dragging && (dy > SWIPE_THRESHOLD || velocity > SWIPE_VELOCITY)) {
            closeModal();
        }
    }, { passive: true });
}

function hasActiveFilters() {
    return Boolean(state.type || state.search || state.region || state.rating || state.genre || state.maxRuntime || state.watchStatus || state.excludeWatched || state.releasedDays);
}

function resetAndLoad() {
    state.requestVersion += 1;
    state.page = 1;
    state.loading = false;
    state.hasMore = true;
    state.userInitiatedFilter = true; // 用户主动变更，禁止恢复旧滚动位置
    updateUrl();
    syncControlsFromState();
    renderActiveFilters();
    document.getElementById('scroll-end').classList.add('hidden');
    document.getElementById('scroll-sentinel').classList.remove('hidden');
    renderSkeletons();
    state.selectedIds = new Set();
    syncBatchUI();
    // 筛选变更后回到内容区顶部；尊重减少动态偏好
    const workspace = document.querySelector('.workspace');
    if (workspace) workspace.scrollIntoView({ behavior: reduceMotionQuery.matches ? 'auto' : 'smooth', block: 'start' });
    if (state.section === 'releases') loadReleases();
    else loadTitles();
}

function clearAllFilters() {
    state.type = '';
    state.search = '';
    state.region = '';
    state.rating = 0;
    state.genre = '';
    state.maxRuntime = 0;
    state.watchStatus = state.section === 'library' ? state.libraryStatus : '';
    state.excludeWatched = false;
    state.releasedDays = 0;
    state.sort_by = 'release_date';
    state.order = 'desc';
    resetAndLoad();
}

function renderActiveFilters() {
    const container = document.getElementById('active-filters');
    const chips = [];
    if (state.search) chips.push(['关键词', state.search, '', () => { state.search = ''; resetAndLoad(); }]);
    if (state.type) chips.push(['类型', state.type === 'movie' ? '电影' : '剧集', '', () => { state.type = ''; resetAndLoad(); }]);
    if (state.region) chips.push(['地区', displayRegionName(state.region), '', () => { state.region = ''; resetAndLoad(); }]);
    if (state.rating) chips.push(['评分', `${state.rating} 分以上`, '', () => { state.rating = 0; resetAndLoad(); }]);
    if (state.genre) chips.push(['题材', genreZh(state.genre), '', () => { state.genre = ''; resetAndLoad(); }]);
    if (state.maxRuntime) chips.push(['时长', `${state.maxRuntime} 分钟内`, '', () => { state.maxRuntime = 0; resetAndLoad(); }]);
    if (state.excludeWatched && !state.watchStatus) chips.push(['片单', '隐藏已看', '', () => { state.excludeWatched = false; resetAndLoad(); }]);
    if (state.releasedDays > 0) chips.push(['上映', `近 ${state.releasedDays} 天`, '', () => { state.releasedDays = 0; resetAndLoad(); }]);
    if (!chips.length) {
        container.classList.add('hidden');
        container.innerHTML = '';
        updateFilterSummary();
        return;
    }
    container.classList.remove('hidden');
    container.innerHTML = '<span class="active-filters-label">已筛选</span>';
    chips.forEach(([key, label, color, clear]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'active-filter-chip';
        button.setAttribute('aria-label', `移除${key}筛选：${label}`);
        if (color) button.style.setProperty('--chip-color', color);
        button.innerHTML = `${color ? '<span class="chip-dot"></span>' : ''}<span><span class="chip-label-key">${key}</span>${escapeHtml(label)}</span><span class="chip-x">×</span>`;
        button.addEventListener('click', clear);
        container.appendChild(button);
    });
    if (chips.length > 1) {
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'btn-clear-all-filters';
        clear.textContent = '全部清除';
        clear.addEventListener('click', clearAllFilters);
        container.appendChild(clear);
    }
    updateFilterSummary();
}

function updateFilterSummary() {
    const el = document.getElementById('filter-summary');
    if (!el) return;
    const parts = [];
    if (state.type) parts.push(state.type === 'movie' ? '电影' : '剧集');
    if (state.region) parts.push(displayRegionName(state.region));
    if (state.rating > 0) parts.push(`≥${state.rating}`);
    if (state.genre) parts.push(genreZh(state.genre));
    if (state.maxRuntime > 0) parts.push(`≤${state.maxRuntime}分`);
    if (state.excludeWatched && !state.watchStatus) parts.push('隐藏已看');
    if (state.releasedDays > 0) parts.push(`近${state.releasedDays}天`);
    if (state.sort_by !== 'release_date') {
        const sortLabel = '评分最高';
        parts.push(state.order === 'asc' ? `${sortLabel}↑` : sortLabel);
    }
    el.textContent = parts.join(' · ');
}

function setupFilterToggle() {
    const btn = document.getElementById('filter-toggle');
    const panel = document.getElementById('filter-bar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (!btn || !panel || !backdrop) return;
    const close = (restoreFocus = false) => {
        if (!document.body.classList.contains('sidebar-open')) return;
        document.body.classList.remove('sidebar-open');
        backdrop.classList.add('hidden');
        backdrop.setAttribute('aria-hidden', 'true');
        btn.setAttribute('aria-expanded', 'false');
        document.body.style.overflow = '';
        panel.removeAttribute('role');
        panel.removeAttribute('aria-modal');
        // F14：焦点回到触发按钮，Tab 不再进入屏外抽屉
        if (restoreFocus) btn.focus();
    };
    const open = () => {
        document.body.classList.add('sidebar-open');
        backdrop.classList.remove('hidden');
        backdrop.setAttribute('aria-hidden', 'false');
        btn.setAttribute('aria-expanded', 'true');
        // F14：抽屉作为模态对话框管理焦点；移动端 drawer-mode 时生效
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-label', '筛选');
        // F09：打开时锁定背景滚动并将焦点移入抽屉
        document.body.style.overflow = 'hidden';
        panel.querySelector('button, select, input')?.focus();
    };
    btn.addEventListener('click', () => {
        if (document.body.classList.contains('sidebar-open')) close(true);
        else open();
    });
    backdrop.addEventListener('click', () => close(true));
    // F08：抽屉内点击不再自动关闭，仅关闭按钮/遮罩/Esc/桌面切换关闭。
    // 选择控件 change 后保持打开，支持连续设置平台、产地、评分。
    const desktopMq = window.matchMedia('(min-width: 901px)');
    desktopMq.addEventListener('change', (e) => { if (e.matches) close(); });
    window.__closeSidebar = () => close(true);
    // F09：移动端将抽屉提升为 body 级顶层容器，避免被 main 的层叠上下文压住。
    const originalParent = panel.parentElement;
    const originalNext = panel.nextSibling;
    const placePanel = () => {
        if (window.matchMedia('(max-width: 900px)').matches) {
            if (panel.parentElement !== document.body) {
                document.body.appendChild(panel);
                panel.classList.add('drawer-mode');
            }
        } else {
            panel.classList.remove('drawer-mode');
            if (panel.parentElement !== originalParent) {
                originalParent.insertBefore(panel, originalNext);
            }
            close();
        }
    };
    placePanel();
    desktopMq.addEventListener('change', placePanel);
    // 移动端显示抽屉开关，桌面隐藏
    const syncVis = () => btn.classList.toggle('hidden', desktopMq.matches);
    syncVis();
    desktopMq.addEventListener('change', syncVis);
}

async function checkBootstrapSync() {
    const status = await loadSyncStatus();
    if (!status?.sync?.running) return;
    document.getElementById('stats-info').textContent = '首批内容正在同步，完成后会自动刷新';
    document.getElementById('titles-grid').innerHTML = '<div class="empty-state"><span class="spinner"></span><div class="empty-title">正在建立内容库</div><p>第一次同步需要几分钟，可以稍后回来查看</p></div>';
    if (!bootstrapPollTimer) {
        bootstrapPollTimer = setInterval(async () => {
            const next = await loadSyncStatus();
            if (next && !next.sync?.running) {
                clearInterval(bootstrapPollTimer);
                bootstrapPollTimer = null;
                await Promise.allSettled([loadStats()]);
                resetAndLoad();
            }
        }, 6000);
    }
}

function closeStatusMenus(except = null) {
    document.querySelectorAll('.status-menu:not(.hidden)').forEach(menu => {
        if (menu === except) return;
        const trigger = menu.parentElement.querySelector('.status-menu-trigger');
        trigger?.setAttribute('aria-expanded', 'false');
        menu.style.opacity = '0';
        menu.style.transform = 'scale(.94) translateY(-4px)';
        setTimeout(() => { menu.classList.add('hidden'); menu.style.opacity = ''; menu.style.transform = ''; }, 140);
    });
}

/* 5.2 管理菜单：同步 / 导出 / 恢复 / 数据健康收进一处，减少与搜片竞争 */
function closeAdminMenu(restoreFocus = false) {
    const menu = document.getElementById('admin-menu');
    const button = document.getElementById('admin-menu-btn');
    if (!menu || menu.classList.contains('hidden')) return;
    menu.classList.add('hidden');
    button?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button?.focus();
}

function setupAdminMenu() {
    const button = document.getElementById('admin-menu-btn');
    const menu = document.getElementById('admin-menu');
    if (!button || !menu) return;
    button.addEventListener('click', event => {
        event.stopPropagation();
        if (menu.classList.contains('hidden')) {
            menu.classList.remove('hidden');
            button.setAttribute('aria-expanded', 'true');
            menu.querySelector('button:not(.hidden), a[href]')?.focus();
        } else {
            closeAdminMenu(true);
        }
    });
    menu.addEventListener('click', event => {
        if (event.target.closest('[role="menuitem"]')) closeAdminMenu();
    });
}

function setupEvents() {
    document.addEventListener('click', event => {
        const sectionTab = event.target.closest('#primary-nav [data-section]');
        if (sectionTab) { setSection(sectionTab.dataset.section); return; }

        const modeBtn = event.target.closest('#mode-filters [data-mode]');
        if (modeBtn) { applyMode(modeBtn.dataset.mode); return; }

        const batchToggle = event.target.closest('#batch-toggle');
        if (batchToggle) { setBatchMode(!state.batchMode); return; }

        const type = event.target.closest('#type-filters [data-type]');
        if (type) { state.type = type.dataset.type; resetAndLoad(); return; }

        const unwatchedToggle = event.target.closest('#unwatched-toggle');
        if (unwatchedToggle) { state.excludeWatched = !state.excludeWatched; resetAndLoad(); return; }

        const statusTab = event.target.closest('#status-filters [data-status]');
        if (statusTab) { state.watchStatus = statusTab.dataset.status; resetAndLoad(); return; }

        const cardMain = event.target.closest('.card-main');
        if (cardMain) {
            const card = cardMain.closest('.title-card');
            // 批量模式下点击卡片改为选中/取消，不打开详情
            if (state.batchMode && state.section === 'library') {
                toggleBatchSelection(card.dataset.titleId);
                return;
            }
            showDetail(card.dataset.titleId);
            return;
        }

        const menuTrigger = event.target.closest('.status-menu-trigger');
        if (menuTrigger) {
            const menu = menuTrigger.parentElement.querySelector('.status-menu');
            const opening = menu.classList.contains('hidden');
            closeStatusMenus(menu);
            if (opening) {
                menu.style.opacity = '0';
                menu.style.transform = 'scale(.94) translateY(-4px)';
                menu.classList.remove('hidden');
                requestAnimationFrame(() => { menu.style.opacity = ''; menu.style.transform = ''; });
            } else {
                menu.classList.add('hidden');
            }
            menuTrigger.setAttribute('aria-expanded', String(opening));
            if (opening) menu.querySelector('button.active, button')?.focus();
            return;
        }

        const statusOption = event.target.closest('[data-set-status]');
        if (statusOption) {
            const holder = statusOption.closest('[data-title-id], .title-card');
            setTitleStatus(holder.dataset.titleId, statusOption.dataset.setStatus, statusOption);
            return;
        }

        const recentChip = event.target.closest('[data-recent-id]');
        if (recentChip) { showDetail(recentChip.dataset.recentId); return; }

        const action = event.target.closest('[data-action]');
        if (action?.dataset.action === 'clear-filters') clearAllFilters();
        if (action?.dataset.action === 'goto-discover') { setSection('discover'); return; }
        if (action?.dataset.action === 'surprise-again') { surprisePick(); return; }
        if (action?.dataset.action === 'surprise-tonight') {
            if (state.surpriseLastId != null) setTitleStatus(state.surpriseLastId, 'watching');
            return;
        }
        if (action?.dataset.action === 'surprise-close') { hideSurpriseBar(); return; }
        if (action?.dataset.action === 'batch-select-all') {
            state.loadedTitleIds.forEach(id => state.selectedIds.add(Number(id)));
            refreshBatchSelectionUI();
            syncBatchUI();
            return;
        }
        if (action?.dataset.action === 'batch-exit') { setBatchMode(false); return; }
        if (action?.dataset.action === 'batch-watchlist') { applyBatchAction({ watch_status: 'watchlist' }, '已设为想看'); return; }
        if (action?.dataset.action === 'batch-watching') { applyBatchAction({ watch_status: 'watching' }, '已设为在看'); return; }
        if (action?.dataset.action === 'batch-watched') { applyBatchAction({ watch_status: 'watched' }, '已设为已看'); return; }
        if (action?.dataset.action === 'batch-remove') { applyBatchAction({ watch_status: '' }, '已移出片单'); return; }
        if (action?.dataset.action === 'copy-link') { copyCurrentLink(state.activeDetailId); return; }
        if (action?.dataset.action === 'expand-cast') {
            const full = parseJsonList(currentDetail?.cast_json);
            if (full.length) action.closest('.modal-cast').innerHTML = `主演：${full.map(name => escapeHtml(name)).join(' / ')}`;
            return;
        }
        if (action?.dataset.action === 'goto-channels') {
            event.preventDefault();
            document.getElementById('channels')?.scrollIntoView({ behavior: reduceMotionQuery.matches ? 'auto' : 'smooth', block: 'start' });
            return;
        }
        if (action?.dataset.action === 'retry') resetAndLoad();
        if (action?.dataset.action === 'retry-more') { state.hasMore = true; loadTitles(); }
        if (action?.dataset.action === 'retry-detail') showDetail(action.dataset.titleId);
        if (action?.dataset.action === 'clear-recent') {
            setRecentViewed([]);
            renderRecentRow();
            showToast('已清空最近浏览');
        }
        if (action?.dataset.action === 'imdb-import') importByImdb(state.search);
        if (!event.target.closest('.admin-menu-wrap')) closeAdminMenu();
        if (!event.target.closest('.status-menu-wrap')) closeStatusMenus();
    });

    // 6.1：详情内切换观看地区 → 持久化并重渲染渠道列表；6.7：个人记录字段即时保存
    document.addEventListener('change', event => {
        if (event.target.id === 'viewing-region-select') {
            state.viewingRegion = /^[A-Z]{2}$/.test(event.target.value) ? event.target.value : '';
            try { localStorage.setItem('viewing_region_v1', state.viewingRegion); } catch (_) { /* 忽略存储失败 */ }
            if (currentDetail) renderDetail(currentDetail);
            return;
        }
        if (event.target.id === 'surprise-scope') {
            state.surpriseScope = ['filters', 'watchlist', 'watching'].includes(event.target.value) ? event.target.value : 'filters';
            try { localStorage.setItem('surprise_scope_v1', state.surpriseScope); } catch (_) { /* 忽略 */ }
            return;
        }
        const field = event.target.closest('[data-personal]');
        if (field) {
            const titleId = field.closest('.personal-fields')?.dataset.titleId || state.activeDetailId;
            if (titleId) savePersonalField(Number(titleId), field.dataset.personal, field.value);
            return;
        }
        if (event.target.id === 'batch-priority') {
            if (event.target.value !== '') {
                applyBatchAction({ priority: Number(event.target.value) }, '优先级已更新');
            }
        }
    });

    document.getElementById('sync-button').addEventListener('click', triggerSync);
    document.getElementById('export-button')?.addEventListener('click', exportWatchlist);
    document.getElementById('import-button')?.addEventListener('click', () => document.getElementById('import-file')?.click());
    document.getElementById('import-file')?.addEventListener('change', importWatchlistFile);
    document.getElementById('imdb-import-btn')?.addEventListener('click', importByImdb);
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('surprise-btn')?.addEventListener('click', surprisePick);
    document.getElementById('nav-prev')?.addEventListener('click', () => navigateDetail(-1));
    document.getElementById('nav-next')?.addEventListener('click', () => navigateDetail(1));
    document.getElementById('close-shortcuts')?.addEventListener('click', () => toggleShortcuts(false));
    document.getElementById('recent-toggle')?.addEventListener('click', () => toggleRecentRow());
    // F14：点击帮助浮层空白处也可关闭
    document.getElementById('shortcuts-overlay')?.addEventListener('click', event => {
        if (event.target === event.currentTarget) toggleShortcuts(false);
    });
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        button.addEventListener('click', () => setViewMode(button.dataset.view));
    });
    document.getElementById('detail-modal').addEventListener('pointerdown', event => {
        // 移动端为底部 sheet 形态，禁用外部点击关闭，避免误关；保留 ESC 和关闭按钮
        if (window.matchMedia('(max-width: 680px)').matches) return;
        if (event.target.id === 'detail-modal') closeModal();
    });

    let searchTimer;
    const searchInput = document.getElementById('search-input');
    searchInput.addEventListener('input', () => {
        document.getElementById('clear-search').classList.toggle('hidden', !searchInput.value);
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            const value = searchInput.value.trim();
            if (value !== state.search) { state.search = value; resetAndLoad(); }
        }, 320);
    });
    document.getElementById('clear-search').addEventListener('click', () => {
        clearTimeout(searchTimer);
        state.search = '';
        searchInput.value = '';
        searchInput.focus();
        resetAndLoad();
    });
    document.getElementById('region-filter').addEventListener('change', event => { state.region = event.target.value; resetAndLoad(); });
    document.getElementById('rating-filter').addEventListener('change', event => { state.rating = Number(event.target.value); resetAndLoad(); });
    document.getElementById('genre-filter')?.addEventListener('change', event => { state.genre = event.target.value; resetAndLoad(); });
    document.getElementById('runtime-filter')?.addEventListener('change', event => { state.maxRuntime = Number(event.target.value) || 0; resetAndLoad(); });
    document.getElementById('sort-filter').addEventListener('change', event => { state.sort_by = event.target.value; resetAndLoad(); });
    document.getElementById('sort-order-btn')?.addEventListener('click', () => {
        state.order = state.order === 'asc' ? 'desc' : 'asc';
        syncControlsFromState();
        resetAndLoad();
    });

    document.addEventListener('keydown', event => {
        if (event.isComposing) return;
        const modalOpen = !document.getElementById('detail-modal').classList.contains('hidden');
        const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
        const shortcutsOpen = !document.getElementById('shortcuts-overlay')?.classList.contains('hidden');
        // F14：详情或快捷键浮层打开时禁用全局快捷键，避免操作逃出当前浮层
        if (event.key === '/' && !typing && !modalOpen && !shortcutsOpen) {
            event.preventDefault();
            searchInput.focus();
        }
        // 键盘帮助浮层：? 打开，Esc 关闭（优先于模态关闭）
        if (event.key === '?' && !typing && !modalOpen && !shortcutsOpen) {
            event.preventDefault();
            toggleShortcuts(true);
            return;
        }
        if (event.key === 'Escape') {
            if (shortcutsOpen) { toggleShortcuts(false); return; }
            if (!document.getElementById('admin-menu')?.classList.contains('hidden')) { closeAdminMenu(true); return; }
            if (document.body.classList.contains('sidebar-open')) { window.__closeSidebar?.(); return; }
            if (document.body.classList.contains('wall-open')) { closeWall(); return; }
            if (document.querySelector('.status-menu:not(.hidden)')) closeStatusMenus();
            else closeModal();
        }
        // 海报墙锁屏开关 W
        if ((event.key === 'w' || event.key === 'W') && !typing && !modalOpen && !shortcutsOpen) {
            event.preventDefault();
            if (document.body.classList.contains('wall-open')) closeWall();
            else openWall();
        }
        // 视图切换快捷键 G/L（打开帮助浮层时也生效，方便直接试用）
        if ((event.key === 'g' || event.key === 'G' || event.key === 'l' || event.key === 'L') && !typing && !modalOpen) {
            event.preventDefault();
            setViewMode(state.viewMode === 'grid' ? 'list' : 'grid');
        }
        if (modalOpen && (event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !typing) {
            event.preventDefault();
            navigateDetail(event.key === 'ArrowLeft' ? -1 : 1);
        }
        if (!modalOpen && (event.key === 'r' || event.key === 'R') && !typing && !shortcutsOpen) {
            event.preventDefault();
            surprisePick();
        }
        if (event.key === 'Tab') {
            if (shortcutsOpen) trapFocusWithin(document.getElementById('shortcuts-overlay'), event);
            else if (document.body.classList.contains('sidebar-open')) trapFocusWithin(document.querySelector('body > .filter-panel.drawer-mode'), event);
            else if (modalOpen) trapModalFocus(event);
        }
    });

    window.addEventListener('online', () => {
        document.getElementById('offline-banner').classList.add('hidden');
        showToast('网络连接已恢复');
        clearAllCaches(); // 离线期间数据可能过期，清空缓存让下次请求拉取最新
    });
    window.addEventListener('offline', () => document.getElementById('offline-banner').classList.remove('hidden'));
    if (!navigator.onLine) document.getElementById('offline-banner').classList.remove('hidden');

    // 前进/后退导航的滚动位置恢复
    window.addEventListener('popstate', () => {
        // F12：URL 是详情状态的唯一真实来源；后退关闭、前进重开。
        const t = new URLSearchParams(window.location.search).get('t');
        if (t && /^\d+$/.test(t)) {
            if (String(state.detailId) !== t) showDetail(Number(t), { fromHistory: true });
        } else if (!document.getElementById('detail-modal').classList.contains('hidden')) {
            closeModal({ fromHistory: true });
        }
    });
    window.addEventListener('pageshow', event => {
        // bfcache 恢复：DOM 未重建，无需等待列表加载，直接恢复
        if (event.persisted && history.state?.scrollY) {
            requestAnimationFrame(() => window.scrollTo({ top: history.state.scrollY, behavior: 'auto' }));
        }
    });
    // 普通 back_forward（非 bfcache）：页面重建，标记待恢复位置，loadTitles 完成后消费
    const navEntry = performance.getEntriesByType?.('navigation')[0];
    if (navEntry?.type === 'back_forward') {
        state.restoreScroll = history.state?.scrollY ?? null;
    }
    setupCardHoverPrefetch();

    // 6.10 PWA：注册 Service Worker（离线外壳），注册失败不影响正常使用
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js').catch(() => { /* 忽略注册失败 */ });
        });
    }
}

/* F14：通用焦点陷阱 —— 详情弹层、快捷键帮助、移动端筛选抽屉共用 */
function trapFocusWithin(container, event) {
    if (!container) return;
    const focusable = [...container.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')]
        .filter(el => el.offsetParent !== null || el === document.activeElement);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function trapModalFocus(event) {
    // F14：导航按钮位于 panel 外，需纳入同一焦点循环
    trapFocusWithin(document.getElementById('detail-modal'), event);
}

function setupInfiniteScroll() {
    const observer = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && state.hasMore && !state.loading) loadTitles();
    }, { rootMargin: '650px 0px' });
    observer.observe(document.getElementById('scroll-sentinel'));
}

function setupBackToTop() {
    const button = document.getElementById('back-to-top');
    const header = document.querySelector('.app-header');
    const progress = document.querySelector('.scroll-progress');
    let ticking = false;
    window.addEventListener('scroll', () => {
        if (ticking) return;
        requestAnimationFrame(() => {
            const scrollY = window.scrollY;
            button.classList.toggle('visible', scrollY > 700);
            header?.classList.toggle('is-scrolled', scrollY > 10);
            if (progress) {
                const max = document.documentElement.scrollHeight - window.innerHeight;
                progress.style.transform = `scaleX(${max > 0 ? Math.min(scrollY / max, 1) : 0})`;
            }
            ticking = false;
        });
        ticking = true;
    }, { passive: true });
    button.addEventListener('click', () => window.scrollTo({ top: 0, behavior: reduceMotionQuery.matches ? 'auto' : 'smooth' }));
}

/* 卡片 hover 预取详情：仅桌面精确指针启用，hover 250ms 后预取，离开取消；命中后不重复请求 */
function setupCardHoverPrefetch() {
    if (!window.matchMedia?.('(pointer: fine)').matches) return;
    const grid = document.getElementById('titles-grid');
    if (!grid) return;
    let hoverTimer = null;
    let hoveredId = null;
    grid.addEventListener('mouseover', event => {
        const card = event.target.closest('.title-card');
        if (!card) return;
        if (state.batchMode) return; // 批量整理时不预取
        const id = card.dataset.titleId;
        if (id === hoveredId) return; // 同一张卡内移动，不重置计时
        hoveredId = id;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => prefetchDetail(id), 250);
    });
    grid.addEventListener('mouseleave', () => {
        hoveredId = null;
        clearTimeout(hoverTimer);
    });
}

function showToast(message, type = 'success') {
    const region = document.getElementById('toast-region');
    const backToTop = document.getElementById('back-to-top');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    region.appendChild(toast);
    if (backToTop?.classList.contains('visible')) backToTop.classList.add('shifted');
    setTimeout(() => {
        toast.remove();
        if (!region.children.length) backToTop?.classList.remove('shifted');
    }, 3800);
}

/* ---------- 海报墙锁屏（Netflix 屏保风格） ---------- */
const WALL_BATCH_SIZE = 30;
const WALL_SWITCH_MS = 15000;
let wallTitles = [];
let wallIndex = 0;
let wallTimer = null;
let wallRequestId = 0;

function shuffleArray(array) {
    for (let i = array.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [array[i], array[j]] = [array[j], array[i]];
    }
    return array;
}

function preloadWallImages(urls) {
    urls.forEach(url => { const img = new Image(); img.src = url; });
}

function nextWallBatch() {
    if (!wallTitles.length) return [];
    const batch = [];
    for (let i = 0; i < WALL_BATCH_SIZE; i++) {
        batch.push(wallTitles[wallIndex % wallTitles.length]);
        wallIndex += 1;
    }
    return batch;
}

function renderWallBatch() {
    const grid = document.getElementById('wall-grid');
    const batch = nextWallBatch();
    preloadWallImages(batch);
    grid.innerHTML = batch.map((src, i) =>
        `<img class="wall-item" src="${escapeHtml(src)}" alt="" loading="eager" decoding="async" onerror="this.remove()" style="animation-delay:${Math.min(i * 45, 800)}ms">`
    ).join('');
    // 重启整墙缓慢平移（Ken Burns）
    grid.classList.remove('is-panning');
    void grid.offsetWidth;
    grid.classList.add('is-panning');
}

function startWallRotation() {
    stopWallRotation();
    wallTimer = setInterval(() => {
        // 页面不可见时暂停轮换（CSS 降动态不能覆盖 JS 计时器）
        if (document.hidden || reduceMotionQuery.matches) return;
        const overlay = document.getElementById('wall-overlay');
        overlay.classList.add('is-switching');
        setTimeout(() => {
            renderWallBatch();
            overlay.classList.remove('is-switching');
        }, 260);
    }, WALL_SWITCH_MS);
}

function stopWallRotation() {
    if (wallTimer) { clearInterval(wallTimer); wallTimer = null; }
}

async function enterWall() {
    const overlay = document.getElementById('wall-overlay');
    const grid = document.getElementById('wall-grid');
    const requestId = ++wallRequestId;
    overlay.classList.remove('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('wall-open');
    if (!wallTitles.length) {
        grid.innerHTML = '<p class="wall-status">正在加载海报…</p>';
        try {
            const data = await api('/api/titles?limit=100&sort_by=rating&order=desc');
            // 关闭后返回的迟到响应不再启动轮播
            if (requestId !== wallRequestId || !document.body.classList.contains('wall-open')) return;
            wallTitles = shuffleArray((data.titles || []).map(t => sanitizeUrl(t.poster_url)).filter(Boolean));
            wallIndex = 0;
        } catch {
            if (requestId !== wallRequestId || !document.body.classList.contains('wall-open')) return;
            wallTitles = [];
            grid.innerHTML = '<div class="wall-status"><p>海报墙加载失败，请检查网络后重试</p><button type="button" class="btn-retry" id="wall-retry">重新加载</button><button type="button" class="btn-retry" id="wall-exit">返回浏览</button></div>';
            document.getElementById('wall-retry')?.addEventListener('click', (e) => { e.stopPropagation(); enterWall(); });
            document.getElementById('wall-exit')?.addEventListener('click', (e) => { e.stopPropagation(); closeWall(); });
            return;
        }
    }
    if (!wallTitles.length) {
        grid.innerHTML = '<div class="wall-status"><p>暂无海报可展示</p><button type="button" class="btn-retry" id="wall-exit-empty">返回浏览</button></div>';
        document.getElementById('wall-exit-empty')?.addEventListener('click', (e) => { e.stopPropagation(); closeWall(); });
        return;
    }
    renderWallBatch();
    startWallRotation();
}

function exitWall() {
    wallRequestId += 1; // 使在途请求失效
    const overlay = document.getElementById('wall-overlay');
    overlay.classList.add('hidden');
    overlay.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('wall-open');
    stopWallRotation();
}

function openWall() {
    if (location.hash !== '#wall') history.replaceState(null, '', '#wall');
    enterWall();
}

function closeWall() {
    if (location.hash === '#wall') history.replaceState(null, '', location.pathname + location.search);
    exitWall();
}

function setupWall() {
    const overlay = document.getElementById('wall-overlay');
    const closeBtn = document.getElementById('wall-close');
    if (!overlay) return;
    overlay.addEventListener('click', closeWall);
    closeBtn.addEventListener('click', (e) => { e.stopPropagation(); closeWall(); });
    window.addEventListener('hashchange', () => {
        if (location.hash === '#wall') enterWall();
        else exitWall();
    });
    // 入口：/wall 路径或 #wall hash 都直接进入锁屏，并归一化 URL
    if (location.hash === '#wall' || location.pathname === '/wall') {
        history.replaceState(null, '', location.pathname === '/wall' ? '/#wall' : location.pathname + location.search + '#wall');
        enterWall();
    }
}
