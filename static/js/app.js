const state = {
    page: 1,
    limit: 40,
    provider: '',
    sort_by: 'release_date',
    order: 'desc',
    type: '',
    search: '',
    region: '',
    rating: 0,
    watchStatus: '',
    loading: false,
    hasMore: true,
    requestVersion: 0,
    loadedTitleIds: [],
    currentDetailIndex: -1,
    optimisticPending: null,   // 正在乐观提交状态的作品 id，防同 id 并发操作
    userInitiatedFilter: false, // 用户主动变更筛选（区别于后退/前进恢复）
    restoreScroll: null,        // 待恢复的滚动位置（后退/前进导航）
    viewMode: 'grid',           // 列表展示模式：grid | list
};

// 视图模式持久化：localStorage 优先，无记录或存储不可用时回退网格
try { state.viewMode = localStorage.getItem('view_mode') === 'list' ? 'list' : 'grid'; } catch (_) { state.viewMode = 'grid'; }

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
const hiddenMainFilterProviders = new Set(['hulu']);
const SKELETON_COUNT = 10;

let providerCounts = {};
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
  <path d="M190 286h120v178H190z" fill="none" stroke="#4a4942" stroke-width="6"/>
  <path d="m228 335 70 40-70 40z" fill="#7f7d75"/>
  <text x="250" y="520" text-anchor="middle" fill="#7f7d75" font-family="Arial,sans-serif" font-size="24">暂无海报</text>
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

/* LRU 淘汰：超限时删 Map 中最旧条目（按插入序） */
function evictOldest(map, max) {
    if (map.size > max) map.delete(map.keys().next().value);
}

/* 分页缓存键：与 buildFilterParams 同源，search 归一化为 trim+lowercase */
function pageCacheKey() {
    return [
        state.provider, state.type, state.region, state.rating,
        state.sort_by, state.order, state.watchStatus,
        (state.search || '').trim().toLowerCase(),
    ].join('|');
}

/* 读取/写入分页缓存。fetcher 由调用方提供（构建 params + 调 api）。
 * 返回 { data, total, has_next, fromCache, stale, refreshPromise }。
 * refreshPromise 非空时，调用方可 .then 在后台刷新完成后 patch UI（如 total 变化）。 */
async function getCachedPage(key, page, fetcher) {
    const now = Date.now();
    let entry = cache.pages.get(key);

    if (entry) {
        const pageData = entry.pages.get(page);
        if (pageData) {
            const fresh = now - entry.ts <= PAGE_TTL;
            if (fresh) {
                return { data: pageData, total: entry.total, has_next: entry.has_next, fromCache: true, stale: false, refreshPromise: null };
            }
            // stale：立即返回旧值，page 1 后台静默刷新（限 page 1 控制流量）
            let refreshPromise = null;
            if (page === 1 && !entry.revalidating) {
                entry.revalidating = true;
                refreshPromise = (async () => {
                    try {
                        const data = await fetcher();
                        entry.pages.set(1, data.titles || []);
                        entry.total = data.total;
                        entry.has_next = Boolean(data.has_next);
                        entry.ts = Date.now();
                    } catch (_) { /* 保留旧值，不改 ts 以便下次仍可刷新 */ }
                    finally { entry.revalidating = false; }
                })();
            }
            return { data: pageData, total: entry.total, has_next: entry.has_next, fromCache: true, stale: true, refreshPromise };
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

    // miss：发请求并写入，in-flight 期间记录 promise 供并发合并
    const promise = (async () => {
        try {
            const data = await fetcher();
            const titles = data.titles || [];
            entry.pages.set(page, titles);
            entry.total = data.total;
            entry.has_next = Boolean(data.has_next);
            entry.ts = Date.now();
            return { data: titles, total: data.total, has_next: Boolean(data.has_next) };
        } finally {
            entry.inflight.delete(page);
        }
    })();
    entry.inflight.set(page, promise);
    const result = await promise;
    return { ...result, fromCache: false, stale: false, refreshPromise: null };
}

/* 读取/写入详情缓存。prefetched=true 表示预取来源（hover 预取标记） */
async function getCachedDetail(id, { prefetched = false } = {}) {
    const numId = Number(id);
    const now = Date.now();
    const entry = cache.detail.get(numId);

    if (entry) {
        if (entry.data) {
            const fresh = now - entry.ts <= DETAIL_TTL;
            if (fresh) return entry.data;
            // stale：返回旧值并后台刷新
            if (!entry.revalidating) {
                entry.revalidating = true;
                revalidateDetail(numId).finally(() => { entry.revalidating = false; });
            }
            return entry.data;
        }
        if (entry.promise) return entry.promise; // in-flight：复用同一请求
    }

    const promise = (async () => {
        const data = await api(`/api/titles/${numId}`);
        setDetail(numId, data, { prefetched });
        return data;
    })();

    if (entry) {
        entry.promise = promise;
    } else {
        cache.detail.set(numId, { data: null, ts: now, promise, prefetched, revalidating: false });
        evictOldest(cache.detail, DETAIL_CACHE_MAX);
    }
    return promise;
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

document.addEventListener('DOMContentLoaded', async () => {
    hydrateStateFromUrl();
    setupDisplayAdaptation();
    setupEvents();
    setupFilterToggle();
    relocateSearch();
    window.matchMedia('(max-width: 900px)').addEventListener('change', relocateSearch);
    setupModalSwipe();
    setupInfiniteScroll();
    setupBackToTop();
    syncControlsFromState();
    renderSkeletons();
    renderRecentRow();

    await Promise.allSettled([loadStats(), loadProviders(), loadSyncStatus()]);
    await loadTitles();
});

function hydrateStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const valid = (value, allowed, fallback = '') => allowed.includes(value) ? value : fallback;
    state.provider = params.get('provider') || '';
    state.type = valid(params.get('type') || '', ['', 'movie', 'tv']);
    state.search = (params.get('q') || '').slice(0, 100);
    state.region = /^[A-Za-z]{2}$/.test(params.get('region') || '') ? params.get('region').toUpperCase() : '';
    state.rating = valid(params.get('rating') || '0', ['0', '7', '7.5', '8'], '0');
    state.rating = Number(state.rating);
    state.sort_by = valid(params.get('sort') || 'release_date', ['rating', 'release_date'], 'release_date');
    state.order = params.get('order') === 'asc' ? 'asc' : 'desc';
    state.watchStatus = valid(params.get('status') || '', ['', 'watchlist', 'watching', 'watched']);
}

function updateUrl() {
    const params = new URLSearchParams();
    if (state.search) params.set('q', state.search);
    if (state.provider) params.set('provider', state.provider);
    if (state.type) params.set('type', state.type);
    if (state.region) params.set('region', state.region);
    if (state.rating) params.set('rating', String(state.rating));
    if (state.sort_by !== 'release_date') params.set('sort', state.sort_by);
    if (state.order === 'asc') params.set('order', 'asc');
    if (state.watchStatus) params.set('status', state.watchStatus);
    const query = params.toString();
    // 将当前滚动位置存入 history state，供后退/前进导航时恢复
    history.replaceState({ scrollY: window.scrollY }, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
}

function syncControlsFromState() {
    document.getElementById('search-input').value = state.search;
    document.getElementById('clear-search').classList.toggle('hidden', !state.search);
    document.getElementById('region-filter').value = state.region;
    document.getElementById('rating-filter').value = String(state.rating);
    document.getElementById('sort-filter').value = state.sort_by;
    const orderBtn = document.getElementById('sort-order-btn');
    if (orderBtn) {
        orderBtn.dataset.order = state.order;
        orderBtn.setAttribute('aria-label', state.order === 'asc' ? '当前为升序，点击切换为降序' : '当前为降序，点击切换为升序');
    }
    document.querySelectorAll('#type-filters [data-type]').forEach(button => {
        button.classList.toggle('active', button.dataset.type === state.type);
    });
    document.querySelectorAll('#status-filters [data-status]').forEach(button => {
        button.classList.toggle('active', button.dataset.status === state.watchStatus);
    });
    document.querySelectorAll('#provider-filters [data-provider]').forEach(button => {
        button.classList.toggle('active', button.dataset.provider === state.provider);
    });
    document.getElementById('titles-grid')?.setAttribute('data-view', state.viewMode);
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        button.classList.toggle('active', button.dataset.view === state.viewMode);
    });
}

async function loadStats() {
    try {
        statsData = await api('/api/stats');
        const byStatus = statsData.by_status || {};
        const listTotal = Object.values(byStatus).reduce((sum, count) => sum + Number(count || 0), 0);
        document.getElementById('overview-stats').innerHTML = `
            <div><dt>收录</dt><dd data-stat="total">—</dd></div>
            <div><dt>平均分</dt><dd data-stat="avg">—</dd></div>
            <div><dt>我的片单</dt><dd data-stat="list">—</dd></div>`;
        animateNumber(document.querySelector('[data-stat="total"]'), Number(statsData.total || 0));
        animateNumber(document.querySelector('[data-stat="avg"]'), Number(statsData.avg_rating || 0), 1);
        animateNumber(document.querySelector('[data-stat="list"]'), listTotal);
        const footerTotal = document.getElementById('footer-total');
        if (footerTotal) footerTotal.textContent = Number(statsData.total || 0).toLocaleString();
        document.getElementById('status-count-all').textContent = Number(statsData.total || 0).toLocaleString();
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

async function loadProviders() {
    const container = document.getElementById('provider-filters');
    try {
        const data = await api('/api/providers');
        providerCounts = Object.fromEntries((data.providers || []).map(item => [item.provider_name, item.count]));
        const providers = (data.available || [])
            .filter(key => !hiddenMainFilterProviders.has(key))
            .sort((a, b) => (providerCounts[b] || 0) - (providerCounts[a] || 0));
        const html = providerButtonHtml('', '全部平台', data.total || 0, '')
            + providers.map(key => providerButtonHtml(
                key, providerNames[key] || key, providerCounts[key] || 0, providerColors[key],
            )).join('');
        container.innerHTML = html;
        syncControlsFromState();
    } catch (error) {
        container.innerHTML = '<span class="filter-label">平台列表加载失败</span>';
    }
}

function providerButtonHtml(key, name, count, color) {
    return `<button class="filter-btn" type="button" data-provider="${escapeHtml(key)}">
        <span class="btn-left">
            ${color ? `<span class="provider-dot" style="background:${color}"></span>` : ''}
            <span class="btn-text">${escapeHtml(name)}</span>
        </span>
        <span class="count-badge">${Number(count).toLocaleString()}</span>
    </button>`;
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
            await Promise.allSettled([loadStats(), loadProviders()]);
            resetAndLoad();
        }
    }, 4000);
}

async function loadTitles() {
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
        renderTitles(titles, state.page === 1, (state.page - 1) * state.limit);
        const loaded = Math.min((state.page - 1) * state.limit + titles.length, result.total);
        const noun = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部剧集' : '部作品';
        document.getElementById('stats-info').innerHTML = `找到 <strong>${Number(result.total).toLocaleString()}</strong> ${noun}${result.total ? ` · 已显示 ${loaded}` : ''}`;
        state.hasMore = Boolean(result.has_next);
        document.getElementById('scroll-sentinel').classList.toggle('hidden', !state.hasMore);
        if (!state.hasMore && result.total > 0) end.classList.remove('hidden');
        if (titles.length) state.page += 1;
        if (state.page === 1 && result.total === 0 && !hasActiveFilters()) checkBootstrapSync();
        // 后退/前进导航恢复滚动位置（仅首页加载路径，用户主动筛选不触发）
        if (state.page === 1 && state.restoreScroll != null && !state.userInitiatedFilter) {
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
            if (!document.getElementById('detail-modal').classList.contains('hidden')) updateDetailNav();
        }
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
    const showRank = state.sort_by === 'rating';
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
}

function createTitleCard(title, rank = null) {
    const card = document.createElement('article');
    card.className = 'title-card';
    card.dataset.titleId = title.id;
    card.dataset.watchStatus = title.watch_status || '';
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const poster = sanitizeUrl(title.poster_url) || posterFallback;
    const providers = (title.providers || []).map(provider => `
        <span class="card-provider"><span class="p-dot" style="background:${providerColors[provider] || '#7f7d75'}"></span>${escapeHtml(providerNames[provider] || provider)}</span>`).join('');
    const status = title.watch_status || '';
    const region = primaryRegionLabel(title.origin_countries, true);
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                <img ${responsivePosterAttributes(poster, CARD_POSTER_SIZES)} alt="${escapeHtml(title.title)} 海报" loading="lazy" decoding="async" onload="window.handleGridPosterLoad(this)" onerror="window.handlePosterError(this)">
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
                ${rank ? `<span class="rank-badge" data-rank="${rank}">${rank}</span>` : ''}
                <span class="poster-rating"${tier ? ` data-tier="${tier}"` : ''}><span class="r-num">${rating ? rating.toFixed(1) : '—'}</span><small>IMDb</small></span>
                <span class="type-tag">${title.type === 'movie' ? '电影' : '剧集'}</span>
            </div>
            <div class="card-info">
                <h2 class="card-title">${escapeHtml(title.title)}</h2>
                <div class="card-meta"><span>${escapeHtml(title.release_date || '日期待定')}</span>${region ? `<span>${escapeHtml(region)}</span>` : ''}</div>
                <p class="card-overview">${escapeHtml(title.overview || '暂无剧情简介')}</p>
                <div class="card-providers">${providers}</div>
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

/* 横向列表卡片：左海报 / 中标题+简介+元数据 / 右评分+平台。
 * 保留 .poster-wrap / .status-menu-wrap 结构，使 updateCardStatus 等既有逻辑直接复用。 */
function createTitleListItem(title, rank = null) {
    const card = document.createElement('article');
    card.className = 'title-card is-list';
    card.dataset.titleId = title.id;
    card.dataset.watchStatus = title.watch_status || '';
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const poster = sanitizeUrl(title.poster_url) || posterFallback;
    const providers = (title.providers || []).map(provider => `
        <span class="card-provider"><span class="p-dot" style="background:${providerColors[provider] || '#7f7d75'}"></span>${escapeHtml(providerNames[provider] || provider)}</span>`).join('');
    const status = title.watch_status || '';
    const region = primaryRegionLabel(title.origin_countries, true);
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                <img ${responsivePosterAttributes(poster, LIST_POSTER_SIZES)} alt="${escapeHtml(title.title)} 海报" loading="lazy" decoding="async" onload="window.handleGridPosterLoad(this)" onerror="window.handlePosterError(this)">
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
                ${rank ? `<span class="rank-badge" data-rank="${rank}">${rank}</span>` : ''}
                <span class="poster-rating"${tier ? ` data-tier="${tier}"` : ''}><span class="r-num">${rating ? rating.toFixed(1) : '—'}</span><small>IMDb</small></span>
                <span class="type-tag">${title.type === 'movie' ? '电影' : '剧集'}</span>
            </div>
            <div class="list-info">
                <h2 class="card-title">${escapeHtml(title.title)}</h2>
                <p class="card-overview">${escapeHtml(title.overview || '暂无剧情简介')}</p>
                <div class="card-meta">${escapeHtml(title.release_date || '日期待定')}${region ? `<span> · ${escapeHtml(region)}</span>` : ''}</div>
            </div>
            <div class="list-side">
                <span class="list-rating"${tier ? ` data-tier="${tier}"` : ''}>${rating ? rating.toFixed(1) : '—'}<small>IMDb</small></span>
                <div class="list-providers">${providers || '<span class="list-provider-empty">暂无平台信息</span>'}</div>
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

/* 切换网格/列表视图：localStorage 持久化，重载列表以应用新布局（分页缓存命中，秒级返回） */
function setViewMode(mode) {
    if (!['grid', 'list'].includes(mode) || state.viewMode === mode) return;
    state.viewMode = mode;
    try { localStorage.setItem('view_mode', mode); } catch (_) { /* 存储不可用时仅本次生效 */ }
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        button.classList.toggle('active', button.dataset.view === mode);
    });
    document.getElementById('titles-grid')?.setAttribute('data-view', mode);
    resetAndLoad();
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
    document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
        <div class="empty-icon-wrap">${searchIcon()}</div>
        <div class="empty-title">${title}</div><p>${copy}</p>
        ${filtered ? '<button type="button" class="btn-clear-filters" data-action="clear-filters">查看全部作品</button>' : ''}
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

async function showDetail(id) {
    const modal = document.getElementById('detail-modal');
    const content = document.getElementById('detail-content');
    if (modal.classList.contains('hidden')) previousFocus = document.activeElement;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    state.currentDetailIndex = state.loadedTitleIds.indexOf(Number(id));
    updateDetailNav();
    document.querySelector('.modal-panel')?.scrollTo({ top: 0 });
    document.getElementById('close-modal').focus();
    // 缓存命中（fresh 或 stale）时跳过 loading 占位，显著降低详情打开延迟
    const hasCached = Boolean(cache.detail.get(Number(id))?.data);
    if (!hasCached) {
        content.innerHTML = '<div class="detail-loading"><span class="spinner" aria-label="详情加载中"></span></div>';
    }
    try {
        currentDetail = await getCachedDetail(id);
        if (!modal.classList.contains('hidden')) renderDetail(currentDetail);
    } catch (error) {
        content.innerHTML = `<div class="detail-error"><div><p>${escapeHtml(userMessage(error))}</p><button type="button" class="btn-retry" data-action="retry-detail" data-title-id="${id}">重新加载</button></div></div>`;
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
    showDetail(list[target]);
}

function buildFilterParams(extra = {}) {
    const params = new URLSearchParams({ sort_by: state.sort_by, order: state.order, ...extra });
    if (state.provider) params.set('provider', state.provider);
    if (state.type) params.set('type', state.type);
    if (state.search) params.set('search', state.search);
    if (state.region) params.set('region', state.region);
    if (state.rating > 0) params.set('min_rating', String(state.rating));
    if (state.watchStatus) params.set('watch_status', state.watchStatus);
    return params;
}

async function surprisePick() {
    const btn = document.getElementById('surprise-btn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('loading');
    try {
        // 复用列表分页缓存：用户已浏览过的筛选范围可零请求拿到 total 与作品
        const key = pageCacheKey();
        const first = await getCachedPage(key, 1, () => api(`/api/titles?${buildFilterParams({ page: '1', limit: String(state.limit) })}`));
        const total = Number(first.total || 0);
        if (!total) { showToast('当前筛选范围内没有可挑选的作品', 'warn'); return; }
        const idx = Math.floor(Math.random() * total);
        const page = Math.floor(idx / state.limit) + 1;
        const pos = idx % state.limit;
        const result = page === 1
            ? first
            : await getCachedPage(key, page, () => api(`/api/titles?${buildFilterParams({ page: String(page), limit: String(state.limit) })}`));
        const title = (result.data || [])[pos] || (result.data || [])[0];
        if (!title) { showToast('未找到作品，再试一次', 'warn'); return; }
        showDetail(title.id);
        showToast('为你随机挑了一部');
    } catch (error) {
        showToast(userMessage(error), 'error');
    } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
    }
}

function renderDetail(title) {
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const safePoster = sanitizeUrl(title.poster_url);
    const poster = safePoster || posterFallback;
    const hasRealPoster = Boolean(safePoster) && !safePoster.startsWith('data:image/');
    const detailBackdrop = hasRealPoster ? tmdbPosterUrl(safePoster, 'w780') : '';
    const heroBgStyle = detailBackdrop
        ? `style="background-image:url('${escapeHtml(detailBackdrop)}')"`
        : 'style="background: linear-gradient(135deg, var(--surface-raised), var(--surface) 70%)"';
    const original = title.original_title && title.original_title !== title.title ? title.original_title : '';
    const providers = (title.providers || []).map(provider => `
        <span class="modal-provider"><span class="p-dot" style="background:${providerColors[provider] || '#7f7d75'}"></span>${escapeHtml(providerNames[provider] || provider)}</span>`).join('');
    const status = title.watch_status || '';
    const imdbLink = title.imdb_id ? `<a class="modal-link" href="https://www.imdb.com/title/${encodeURIComponent(title.imdb_id)}/" target="_blank" rel="noopener noreferrer">在 IMDb 查看 ${externalIcon()}</a>` : '';
    const tmdbType = title.type === 'movie' ? 'movie' : 'tv';
    const region = primaryRegionLabel(title.origin_countries, true);
    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" ${heroBgStyle}></div>
            <div class="modal-hero-content">
                <div class="modal-hero-rating">
                    <div class="rating-ring"${tier ? ` data-tier="${tier}"` : ''}>
                        <svg viewBox="0 0 88 88" aria-hidden="true">
                            <circle class="ring-bg" cx="44" cy="44" r="34"/>
                            <circle class="ring-val" cx="44" cy="44" r="34"/>
                        </svg>
                        <div class="rating-num">${rating ? rating.toFixed(1) : '—'}</div>
                    </div>
                    <div class="rating-tier"${tier ? ` data-tier="${tier}"` : ''}>${tier ? `IMDb · ${ratingTierLabels[tier]}` : '暂无评分'}</div>
                </div>
                <div class="modal-hero-title"><h2 id="modal-title">${escapeHtml(title.title)}</h2>${original ? `<p>${escapeHtml(original)}</p>` : ''}</div>
            </div>
        </div>
        <div class="modal-body">
            <div class="modal-poster-shell">
                <div class="modal-poster${safePoster ? '' : ' is-fallback'}">
                    <img class="modal-poster-image" ${responsivePosterAttributes(poster, DETAIL_POSTER_SIZES)} width="500" height="750" alt="${escapeHtml(title.title)} 海报" decoding="async" fetchpriority="high" onload="window.handlePosterLoad(this)" onerror="window.handlePosterError(this)">
                </div>
            </div>
            <div class="modal-summary">
                <div class="meta-tags">
                    <span class="meta-tag">${title.type === 'movie' ? '电影' : '剧集'}</span>
                    ${region ? `<span class="meta-tag">${escapeHtml(region)}</span>` : ''}
                    <span class="meta-tag">${escapeHtml(title.release_date || '日期待定')}</span>
                    <span class="meta-tag">${Number(title.rating_votes || 0).toLocaleString()} 票</span>
                </div>
                <div class="modal-section-title">我的片单</div>
                <div class="status-picker" data-title-id="${title.id}">
                    ${[['', '未加入'], ['watchlist', '想看'], ['watching', '在看'], ['watched', '已看']].map(([value, label]) => `<button type="button" data-set-status="${value}" class="${status === value ? 'active' : ''}">${label}</button>`).join('')}
                </div>
                ${providers ? `<div class="modal-section-title">可观看平台</div><div class="modal-providers">${providers}</div>` : ''}
            </div>
            <div class="modal-details">
                <div class="modal-section-title">剧情简介</div>
                <p class="modal-overview">${escapeHtml(title.overview || '暂无剧情简介')}</p>
                <div class="modal-links">${imdbLink}<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}" target="_blank" rel="noopener noreferrer">在 TMDB 查看 ${externalIcon()}</a></div>
            </div>
        </div>`;
    const ringValue = document.querySelector('#detail-content .ring-val');
    if (ringValue && rating > 0) {
        const circumference = 2 * Math.PI * 34;
        const target = circumference * (1 - Math.min(rating, 10) / 10);
        requestAnimationFrame(() => requestAnimationFrame(() => {
            ringValue.style.strokeDashoffset = target.toFixed(1);
        }));
    }
    recordRecentView(title); // 详情渲染即计入最近浏览
}

function externalIcon() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M14 5h5v5M19 5l-8 8M17 13v5H6V7h5"/></svg>';
}

/* ── 最近浏览：sessionStorage 持久化，跨刷新保留，上限 8 条 ── */
const RECENT_MAX = 8;

function getRecentViewed() {
    try { return JSON.parse(sessionStorage.getItem('recent_viewed') || '[]'); }
    catch (_) { return []; } // 数据损坏时视为空
}

function setRecentViewed(list) {
    try { sessionStorage.setItem('recent_viewed', JSON.stringify(list)); }
    catch (_) { /* 隐私模式等场景静默失败 */ }
}

/* 记录浏览：同 id 去重后置顶，截断上限 */
function recordRecentView(title) {
    const list = getRecentViewed().filter(item => Number(item.id) !== Number(title.id));
    list.unshift({ id: Number(title.id), title: title.title, poster: sanitizeUrl(title.poster_url) || '' });
    setRecentViewed(list.slice(0, RECENT_MAX));
    renderRecentRow();
}

/* 渲染最近浏览横条：有数据才显示，点击复用详情缓存 */
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
    scroller.innerHTML = list.map(item => `
        <button type="button" class="recent-chip" data-recent-id="${item.id}" aria-label="查看 ${escapeHtml(item.title)}">
            ${item.poster ? `<img src="${escapeHtml(item.poster)}" alt="" loading="lazy" decoding="async" width="60" height="90">` : ''}
            <span>${escapeHtml(item.title)}</span>
        </button>`).join('');
    row.classList.remove('hidden');
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
    // 冲突保护：同一作品请求进行中时忽略再次点击
    if (state.optimisticPending === numId) {
        showToast('操作进行中，请稍候', 'warn');
        return;
    }
    const snapshot = buildStatusSnapshot(numId, watchStatus);
    applyStatusOptimistic(numId, watchStatus);
    state.optimisticPending = numId;
    const scope = sourceButton?.closest('.status-picker, .status-menu');
    scope?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
        const title = await api(`/api/titles/${numId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ watch_status: watchStatus }),
        });
        state.optimisticPending = null;
        // 以权威数据覆盖详情缓存（含最新 watch_status 与时间戳）
        setDetail(numId, title);
        if (currentDetail?.id === numId) {
            currentDetail = title;
            syncPickerActive(numId, watchStatus);
        }
        showToast(watchStatus ? `已加入“${watchStatusNames[watchStatus]}”` : '已从片单移除');
        // 后台校正统计计数（乐观值通常已一致，仅防并发漂移）
        loadStats().catch(() => {});
        // 当前视图按状态过滤且该作品已移出结果集 → 刷新列表
        if (state.watchStatus && state.watchStatus !== watchStatus) {
            invalidatePageCache(pageCacheKey());
            resetAndLoad();
        }
    } catch (error) {
        state.optimisticPending = null;
        rollbackStatus(snapshot);
        showToast(userMessage(error), 'error');
    } finally {
        scope?.querySelectorAll('button').forEach(button => { button.disabled = false; });
        closeStatusMenus();
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

function closeModal() {
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    document.body.style.overflow = '';
    currentDetail = null;
    state.currentDetailIndex = -1;
    previousFocus?.focus?.();
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
    return Boolean(state.provider || state.type || state.search || state.region || state.rating || state.watchStatus);
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
    // 筛选变更后平滑滚回内容区顶部，确保用户从第一行结果开始看
    const workspace = document.querySelector('.workspace');
    if (workspace) workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
    loadTitles();
}

function clearAllFilters() {
    state.provider = '';
    state.type = '';
    state.search = '';
    state.region = '';
    state.rating = 0;
    state.watchStatus = '';
    resetAndLoad();
}

function renderActiveFilters() {
    const container = document.getElementById('active-filters');
    const chips = [];
    if (state.search) chips.push(['关键词', state.search, '', () => { state.search = ''; resetAndLoad(); }]);
    if (state.provider) chips.push(['平台', providerNames[state.provider] || state.provider, providerColors[state.provider], () => { state.provider = ''; resetAndLoad(); }]);
    if (state.type) chips.push(['类型', state.type === 'movie' ? '电影' : '剧集', '', () => { state.type = ''; resetAndLoad(); }]);
    if (state.region) chips.push(['地区', displayRegionName(state.region), '', () => { state.region = ''; resetAndLoad(); }]);
    if (state.rating) chips.push(['评分', `${state.rating} 分以上`, '', () => { state.rating = 0; resetAndLoad(); }]);
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
    if (state.provider) parts.push(providerNames[state.provider] || state.provider);
    if (state.type) parts.push(state.type === 'movie' ? '电影' : '剧集');
    if (state.region) parts.push(displayRegionName(state.region));
    if (state.rating > 0) parts.push(`≥${state.rating}`);
    if (state.sort_by !== 'release_date') {
        const sortLabel = '评分最高';
        parts.push(state.order === 'asc' ? `${sortLabel}↑` : sortLabel);
    }
    el.textContent = parts.length ? parts.join(' · ') : '全部作品';
}

function setupFilterToggle() {
    const btn = document.getElementById('filter-toggle');
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (!btn || !sidebar || !backdrop) return;
    const close = () => {
        document.body.classList.remove('sidebar-open');
        backdrop.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
    };
    const open = () => {
        document.body.classList.add('sidebar-open');
        backdrop.classList.remove('hidden');
        btn.setAttribute('aria-expanded', 'true');
    };
    btn.addEventListener('click', () => {
        if (document.body.classList.contains('sidebar-open')) close();
        else open();
    });
    backdrop.addEventListener('click', close);
    document.addEventListener('click', (e) => {
        if (document.body.classList.contains('sidebar-open') && e.target.closest('#sidebar')) close();
    });
    const desktopMq = window.matchMedia('(min-width: 901px)');
    desktopMq.addEventListener('change', (e) => { if (e.matches) close(); });
    window.__closeSidebar = close;
    // 移动端显示抽屉开关，桌面隐藏
    const syncVis = () => btn.classList.toggle('hidden', desktopMq.matches);
    syncVis();
    desktopMq.addEventListener('change', syncVis);
}

function relocateSearch() {
    const wrap = document.getElementById('search-wrap');
    const headerSlot = document.getElementById('header-search-slot');
    const sidebarSlot = document.getElementById('sidebar-search-slot');
    if (!wrap || !headerSlot || !sidebarSlot) return;
    const toHeader = window.matchMedia('(max-width: 900px)').matches;
    const target = toHeader ? headerSlot : sidebarSlot;
    if (wrap.parentElement !== target) target.appendChild(wrap);
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
                await Promise.allSettled([loadStats(), loadProviders()]);
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

function setupEvents() {
    document.addEventListener('click', event => {
        const provider = event.target.closest('[data-provider]');
        if (provider) { state.provider = provider.dataset.provider; resetAndLoad(); return; }

        const type = event.target.closest('#type-filters [data-type]');
        if (type) { state.type = type.dataset.type; resetAndLoad(); return; }

        const statusTab = event.target.closest('#status-filters [data-status]');
        if (statusTab) { state.watchStatus = statusTab.dataset.status; resetAndLoad(); return; }

        const cardMain = event.target.closest('.card-main');
        if (cardMain) { showDetail(cardMain.closest('.title-card').dataset.titleId); return; }

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
        if (action?.dataset.action === 'retry') resetAndLoad();
        if (action?.dataset.action === 'retry-more') { state.hasMore = true; loadTitles(); }
        if (action?.dataset.action === 'retry-detail') showDetail(action.dataset.titleId);
        if (action?.dataset.action === 'clear-recent') {
            setRecentViewed([]);
            renderRecentRow();
            showToast('已清空最近浏览');
        }
        if (!event.target.closest('.status-menu-wrap')) closeStatusMenus();
    });

    document.getElementById('sync-button').addEventListener('click', triggerSync);
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('surprise-btn')?.addEventListener('click', surprisePick);
    document.getElementById('nav-prev')?.addEventListener('click', () => navigateDetail(-1));
    document.getElementById('nav-next')?.addEventListener('click', () => navigateDetail(1));
    document.getElementById('close-shortcuts')?.addEventListener('click', () => toggleShortcuts(false));
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
        if (event.key === '/' && !typing) {
            event.preventDefault();
            searchInput.focus();
        }
        // 键盘帮助浮层：? 打开，Esc 关闭（优先于模态关闭）
        if (event.key === '?' && !typing && !modalOpen) {
            event.preventDefault();
            toggleShortcuts(true);
            return;
        }
        if (event.key === 'Escape') {
            if (shortcutsOpen) { toggleShortcuts(false); return; }
            if (document.body.classList.contains('sidebar-open')) { window.__closeSidebar?.(); return; }
            if (document.querySelector('.status-menu:not(.hidden)')) closeStatusMenus();
            else closeModal();
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
        if (!modalOpen && (event.key === 'r' || event.key === 'R') && !typing) {
            event.preventDefault();
            surprisePick();
        }
        if (event.key === 'Tab' && modalOpen) trapModalFocus(event);
    });

    window.addEventListener('online', () => {
        document.getElementById('offline-banner').classList.add('hidden');
        showToast('网络连接已恢复');
        clearAllCaches(); // 离线期间数据可能过期，清空缓存让下次请求拉取最新
    });
    window.addEventListener('offline', () => document.getElementById('offline-banner').classList.remove('hidden'));
    if (!navigator.onLine) document.getElementById('offline-banner').classList.remove('hidden');

    // 前进/后退导航的滚动位置恢复
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
}

function trapModalFocus(event) {
    const panel = document.querySelector('.modal-panel');
    const focusable = [...panel.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
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
    button.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
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
