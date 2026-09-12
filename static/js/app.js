const state = {
    page: 1,
    limit: 40,
    sort_by: 'rating',
    order: 'desc',
    type: '',
    search: '',
    region: '',
    rating: 0,
    genres: [],            // WP-5 题材 facet：多选并集
    years: '',             // WP-5 年代筛选：'' | '2020-2029' | '2010-2019' | '-2009'
    maxRuntime: 0,
    watchStatus: '',
    excludeWatched: false, // 6.5：隐藏已看（仅浏览目录时生效，不影响“已看”片单）
    section: 'discover',   // 5.2 主导航：discover | library
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
    restorePages: 1,            // 待恢复的已加载页数（深滚动恢复）
    restoreScrollTarget: null,  // 补载完成后要滚动到的位置
    restorePagesTarget: 1,
    viewMode: 'grid',           // 列表展示模式：grid | list
    snapshotEntries: new Map(), // R01：目录缺失的个人条目（key -> 行数据）
    activeMissingKey: null,     // 当前打开的目录缺失条目
    usedOfflineCache: false,    // R07：本会话是否读取过离线缓存
};

/* D/R06：各页面保留自己的筛选状态；进入片单不继承发现页条件，返回时恢复。
 * WP-3：默认发现页按加权评分排序（imdb_rating 排序实际使用贝叶斯加权分）。 */
const SECTION_DEFAULTS = {
    discover: { type: '', search: '', region: '', rating: 0, genres: [], years: '', maxRuntime: 0, excludeWatched: false, releasedDays: 0, sort_by: 'rating', order: 'desc' },
    library: { type: '', search: '', region: '', rating: 0, genres: [], years: '', maxRuntime: 0, excludeWatched: false, releasedDays: 0, sort_by: 'updated_at', order: 'desc' },
};
// 列表排序标签：chip、手机摘要与下拉统一使用（R10）
const SORT_LABELS = {
    release_date: '最近上映', rating: '评分最高',
    updated_at: '最近加入', priority: '优先级',
};
const MONETIZATION_LABELS = {
    flatrate: '订阅', ads: '广告', free: '免费',
    rent: '租赁', buy: '购买', mixed: '渠道',
};
// 资料语言质量：避免 has_zh 布尔值承担全部质量承诺（R-maintenance）
const ZH_QUALITY_NOTES = {
    machine: '简介由机器翻译，仅供参考',
    overview_only: '片名为原文，简介已中文化',
    title_only: '暂缺中文简介，显示原文',
    none: '暂无中文简介与译名',
};
if (!state.sectionSnapshots) {
    state.sectionSnapshots = {
        discover: { ...SECTION_DEFAULTS.discover },
        library: { ...SECTION_DEFAULTS.library, watchStatus: 'watchlist' },
    };
}

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
const GENRE_ZH = {
    Action: '动作', Adventure: '冒险', Animation: '动画', Comedy: '喜剧',
    Crime: '犯罪', Documentary: '纪录', Drama: '剧情', Family: '家庭',
    Fantasy: '奇幻', History: '历史', Horror: '恐怖', Music: '音乐',
    Mystery: '悬疑', Romance: '爱情', 'Science Fiction': '科幻',
    Thriller: '惊悚', War: '战争', Western: '西部',
};
// 真实库 genres_json 以中文为主，题材选项来自 /api/stats 的 facet（按作品数排序）
const YEAR_FILTER_VALUES = ['', '2020-2029', '2010-2019', '-2009'];

function yearRangeParams(value) {
    if (!value) return null;
    const older = String(value).match(/^-(\d{4})$/);
    if (older) return { year_to: Number(older[1]) };
    const range = String(value).match(/^(\d{4})-(\d{4})$/);
    if (range) return { year_from: Number(range[1]), year_to: Number(range[2]) };
    return null;
}

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
    // N05：片名命中已通过高亮表达，不再占用卡片元数据宽度
    if (!reason || reason === 'title') return '';
    const label = MATCH_REASON_LABELS[reason];
    return label ? `<span class="match-reason">${label}</span>` : '';
}
const SKELETON_COUNT = 10;

let statsData = null;
let syncStatusData = null;
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
        if (response.headers.get('X-Stream-Cache') === 'hit') {
            // R07：SW 离线回退，明确告知数据来自缓存与获取时间
            state.usedOfflineCache = true;
            showCacheNotice(response.headers.get('X-Stream-Cached-At'));
        }
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
        state.genres.join(','), state.years, state.maxRuntime, state.releasedDays,
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
                updateDetailActions(data);
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
    // 7.1：跨过手机断点时重新摆放详情标题（模态未打开时为空操作）
    mobileDetailQuery?.addEventListener?.('change', placeDetailTitle);
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
    captureSectionSnapshot(state.section);
    // WP-5 F4：旧“近期上映”链接归一化为 ?fresh=30，避免 URL 与界面状态不一致
    if (new URLSearchParams(window.location.search).get('view') === 'releases') updateUrl();
    setupDisplayAdaptation();
    setupHeaderHeight();
    setupIntroCompact();
    setupEvents();
    setupAdminMenu();
    setupFilterToggle();
    setupFilterPopover();
    setupModalSwipe();
    setupWall();
    setupInfiniteScroll();
    setupBackToTop();
    setupDataStatus();
    syncControlsFromState();
    renderActiveFilters();
    renderSkeletons();
    renderRecentRow();
    // 首屏主列表立即加载，不被统计/同步状态阻塞
    loadTitles();
    // F12：直接以 ?t=作品ID 打开时恢复详情（不重复压入历史）
    const initialDetailId = Number(new URLSearchParams(window.location.search).get('t') || 0);
    if (Number.isInteger(initialDetailId) && initialDetailId > 0) {
        showDetail(initialDetailId, { fromHistory: true });
    }
    await Promise.allSettled([loadStats(), loadSyncStatus()]);
    // 列表已先行加载，此处仅补全计数与状态，不再阻塞首屏
});

/* N02：顶栏高度写入 CSS 变量，供 scroll-margin-top 与滚动判断使用 */
function setupHeaderHeight() {
    const header = document.querySelector('.app-header');
    if (!header) return;
    const sync = () => {
        document.documentElement.style.setProperty('--header-h', `${header.offsetHeight}px`);
    };
    sync();
    if ('ResizeObserver' in window) new ResizeObserver(sync).observe(header);
    else window.addEventListener('resize', sync, { passive: true });
}

/* WP-3：介绍区首次访问保留两行，之后折叠为一行（localStorage.introSeen） */
function setupIntroCompact() {
    const intro = document.querySelector('.intro');
    if (!intro) return;
    let seen = false;
    try { seen = localStorage.getItem('intro_seen') === '1'; } catch (_) { seen = false; }
    if (seen) intro.classList.add('is-compact');
    try { localStorage.setItem('intro_seen', '1'); } catch (_) { /* 忽略存储失败 */ }
}

function hydrateStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const valid = (value, allowed, fallback = '') => allowed.includes(value) ? value : fallback;
    state.type = valid(params.get('type') || '', ['', 'movie', 'tv']);
    state.search = (params.get('q') || '').slice(0, 100);
    state.region = /^[A-Za-z]{2}$/.test(params.get('region') || '') ? params.get('region').toUpperCase() : '';
    state.genres = [...new Set(params.getAll('genre').map(value => value.slice(0, 40)).filter(Boolean))].slice(0, 20);
    const years = params.get('years') || '';
    state.years = yearRangeParams(years) ? years : '';
    const runtimeParam = Number(params.get('runtime') || params.get('max_runtime') || 0);
    state.maxRuntime = [0, 90, 120].includes(runtimeParam) ? runtimeParam : 0;
    state.rating = valid(params.get('rating') || '0', ['0', '7', '7.5', '8'], '0');
    state.rating = Number(state.rating);
    const view = params.get('view') || '';
    // WP-5 F4：合并“近期上映”入口；旧的 ?view=releases 重定向为“近期新片（30 天）”
    const legacyReleases = view === 'releases';
    state.section = view === 'library' ? 'library' : 'discover';
    // 6.7 我的片单专属排序只在 library 视图生效
    const sortAllowed = state.section === 'library'
        ? ['rating', 'release_date', 'updated_at', 'priority']
        : ['rating', 'release_date'];
    const defaultSort = state.section === 'library'
        ? SECTION_DEFAULTS.library.sort_by
        : SECTION_DEFAULTS.discover.sort_by;
    state.sort_by = valid(params.get('sort') || defaultSort, sortAllowed, defaultSort);
    state.order = params.get('order') === 'asc' ? 'asc' : 'desc';
    if (state.section === 'library') {
        state.libraryStatus = valid(params.get('status') || '', ['watchlist', 'watching', 'watched']) || 'watchlist';
    }
    state.watchStatus = state.section === 'library' ? state.libraryStatus : '';
    state.excludeWatched = params.get('unwatched') === '1';
    const fresh = Number(params.get('fresh') || 0);
    const validFresh = [0, 30, 90, 180].includes(fresh) ? fresh : 0;
    state.releasedDays = legacyReleases && !validFresh ? 30 : validFresh;
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
    state.genres.forEach(name => params.append('genre', name));
    if (state.years) params.set('years', state.years);
    if (state.maxRuntime) params.set('max_runtime', String(state.maxRuntime));
    const defaultSort = SECTION_DEFAULTS[state.section]?.sort_by || 'rating';
    if (state.sort_by !== defaultSort) params.set('sort', state.sort_by);
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
    // 将当前滚动位置与已加载页数存入 history state，供后退/前进导航时恢复
    const loadedPages = Math.max(1, Math.ceil((state.loadedTitleIds.length || state.limit) / state.limit));
    history.replaceState(
        { ...(history.state || {}), scrollY: window.scrollY, loadedPages },
        '', `${window.location.pathname}${query ? `?${query}` : ''}`,
    );
}

function syncControlsFromState() {
    document.getElementById('search-input').value = state.search;
    document.getElementById('clear-search').classList.toggle('hidden', !state.search);
    document.getElementById('region-filter').value = state.region;
    document.getElementById('rating-filter').value = String(state.rating);
    renderGenreChips();
    const runtimeSelect = document.getElementById('runtime-filter');
    if (runtimeSelect) runtimeSelect.value = String(state.maxRuntime || 0);
    document.querySelectorAll('#year-filters [data-years]').forEach(button => {
        const active = button.dataset.years === state.years;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
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
    document.querySelectorAll('#fresh-menu [data-fresh-days]').forEach(button => {
        const active = state.releasedDays > 0 && Number(button.dataset.freshDays) === state.releasedDays;
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    const surpriseScopeLabelEl = document.getElementById('surprise-scope-label');
    if (surpriseScopeLabelEl) surpriseScopeLabelEl.textContent = `从${surpriseScopeLabel()}`;
    document.querySelectorAll('#surprise-scope-menu [data-surprise-scope]').forEach(button => {
        const active = button.dataset.surpriseScope === state.surpriseScope;
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
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

let statsRendered = false;
let renderedRegionSignature = null;

async function loadStats() {
    try {
        statsData = await api('/api/stats');
        renderStats(statsData);
    } catch (error) {
        showToast('概览数据暂时无法加载', 'warn');
    }
}

/* N11：统计局部更新 —— 值未变化不动 DOM，非首次不重放数字动画，下拉只在选项集合变化时重建 */
function renderStats(data) {
    const byStatus = data.by_status || {};
    const listTotal = Object.values(byStatus).reduce((sum, count) => sum + Number(count || 0), 0);
    const setNumber = (selector, value, decimals = 0) => {
        const el = document.querySelector(selector);
        if (!el) return;
        const text = decimals ? Number(value).toFixed(decimals) : Math.round(Number(value)).toLocaleString();
        if (el.textContent === text) return;
        if (statsRendered) el.textContent = text;
        else animateNumber(el, value, decimals);
    };
    setNumber('[data-stat="total"]', Number(data.total || 0));
    setNumber('[data-stat="added"]', Number(data.added_this_week || 0));
    setNumber('[data-stat="list"]', listTotal);
    const footerTotal = document.getElementById('footer-total');
    if (footerTotal) footerTotal.textContent = Number(data.total || 0).toLocaleString();
    const introTotal = document.getElementById('intro-total');
    if (introTotal) introTotal.textContent = Number(data.total || 0).toLocaleString();
    const introUpdated = document.getElementById('intro-updated');
    if (introUpdated) {
        introUpdated.textContent = data.last_synced_at ? ` ${formatRelativeDate(new Date(data.last_synced_at))}` : ' 今天';
    }
    const freshness = document.getElementById('footer-freshness');
    if (freshness) {
        const parts = [];
        if (data.last_synced_at) {
            const d = new Date(data.last_synced_at);
            parts.push(`数据更新于 ${formatRelativeDate(d)}`);
        }
        const ds = data.ratings_dataset || {};
        if (ds.stale) {
            parts.push(`评分库${ds.age_hours != null ? `${Math.round(ds.age_hours / 24)}天前` : '缺失'} · 可能漏掉新开分作品`);
            freshness.className = 'freshness-stale';
        } else if (ds.age_hours != null) {
            parts.push('评分库新鲜');
            freshness.className = '';
        }
        freshness.textContent = parts.length ? `（${parts.join('，')}）` : '';
    }
    setNumber('#status-count-all', Number(data.total || 0));
    const libraryCount = document.getElementById('status-count-library');
    if (libraryCount) {
        const text = listTotal.toLocaleString();
        if (libraryCount.textContent !== text) libraryCount.textContent = text;
        const pending = Number(data.library_pending || 0);
        libraryCount.title = pending > 0
            ? `${listTotal} 条个人记录，其中 ${pending} 条目录待补全`
            : `${listTotal} 条个人记录`;
    }
    const versionEl = document.getElementById('footer-version');
    if (versionEl && data.app_version) {
        versionEl.textContent = ` · v${data.app_version}`;
    }
    ['watchlist', 'watching', 'watched'].forEach(status => {
        setNumber(`#status-count-${status}`, Number(byStatus[status] || 0));
    });

    // 产地下拉：只在 count 集合变化时重建（95 项 HTML 不必每次片单操作重建）
    const regions = [...(data.regions || [])].sort((a, b) => {
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
    const regionSignature = regions.map(region => `${region.country_code}:${region.count}`).join('|');
    if (regionSignature !== renderedRegionSignature) {
        const select = document.getElementById('region-filter');
        const current = state.region;
        select.innerHTML = '<option value="">全部地区</option>';
        const regionLabelCounts = {};
        regions.forEach(region => {
            const label = displayRegionName(region.country_code);
            regionLabelCounts[label] = (regionLabelCounts[label] || 0) + 1;
        });
        regions.forEach(region => {
            const option = document.createElement('option');
            option.value = region.country_code;
            const label = displayRegionName(region.country_code);
            // 历史地区名称可能重复显示（如两个“俄罗斯”）：重名时附带地区代码
            const suffix = regionLabelCounts[label] > 1 ? `（${region.country_code}）` : '';
            option.textContent = `${label}${suffix} · ${Number(region.count || 0).toLocaleString()}`;
            select.appendChild(option);
        });
        if (current && !regions.some(region => region.country_code === current)) {
            select.appendChild(new Option(displayRegionName(current), current));
        }
        select.value = current;
        renderedRegionSignature = regionSignature;
    }
    renderGenreChips();
    statsRendered = true;
}

/* WP-5 题材 facet：按作品数排序、只显示 count > 0 的题材；选中项不在 facet 中也保留 */
function renderGenreChips() {
    const container = document.getElementById('genre-filters');
    if (!container) return;
    const facets = (statsData?.genres || []).filter(item => Number(item.count) > 0);
    const selected = new Set(state.genres);
    const items = [...facets];
    state.genres.forEach(name => {
        if (!items.some(item => item.name === name)) items.push({ name, count: null });
    });
    container.innerHTML = items.map(item => `
        <button type="button" class="filter-btn genre-chip${selected.has(item.name) ? ' active' : ''}"
            data-genre="${escapeHtml(item.name)}" aria-pressed="${selected.has(item.name) ? 'true' : 'false'}">
            ${escapeHtml(genreZh(item.name))}${item.count != null ? `<span class="genre-count">${Number(item.count).toLocaleString()}</span>` : ''}
        </button>`).join('');
}

async function loadSyncStatus() {
    try {
        const status = await api('/api/sync/status');
        syncStatusData = status;
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

/* R07：Service Worker 用缓存兜底 API 时，告知数据来源与获取时间 */
let cacheNoticeShown = false;
function showCacheNotice(cachedAt) {
    const banner = document.getElementById('offline-banner');
    if (!banner) return;
    const time = cachedAt ? new Date(cachedAt) : null;
    const when = time && !Number.isNaN(time.getTime())
        ? `（获取于 ${formatRelativeDate(time)}）`
        : '';
    banner.textContent = `服务暂时不可达，当前展示缓存数据${when}`;
    banner.classList.remove('hidden');
    if (!cacheNoticeShown) {
        cacheNoticeShown = true;
        showToast('服务暂不可达，当前显示缓存数据', 'warn');
    }
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

/* N16：桌面显示完整计数，手机只显示“N 部”；“已显示 N”在手机上收进加载提示 */
function formatStatsHtml(total, loaded, noun, extraHtml = '') {
    const totalText = Number(total).toLocaleString();
    const full = `<span class="stats-full">找到 <strong>${totalText}</strong> ${noun}${total ? ` · 已显示 ${loaded}` : ''}${extraHtml}</span>`;
    const compact = `<span class="stats-compact"><strong>${totalText}</strong> 部</span>`;
    return full + compact;
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
    // 弱设备（软路由/USB 盘）首次请求含 COUNT，给 20s 余量避免直接超时
    const fetcher = () => api(
        `/api/titles?${buildFilterParams({ page: String(currentPage), limit: String(state.limit) })}`,
        { timeout: 20000 },
    );
    try {
        const result = await getCachedPage(cacheKey, currentPage, fetcher);
        if (version !== state.requestVersion) return;
        const titles = result.data || [];
        const isFirstPage = currentPage === 1;
        renderTitles(titles, isFirstPage, (currentPage - 1) * state.limit);
        const loaded = Math.min((currentPage - 1) * state.limit + titles.length, result.total);
        const noun = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部剧集' : '部作品';
        let extraHtml = '';
        const libraryPending = Number(statsData?.library_pending || 0);
        if (state.section === 'library' && libraryPending > 0) {
            extraHtml += ` · 其中 <strong>${libraryPending}</strong> 部资料待补全`;
        }
        document.getElementById('stats-info').innerHTML = formatStatsHtml(result.total, loaded, noun, extraHtml);
        state.hasMore = Boolean(result.has_next);
        document.getElementById('scroll-sentinel').classList.toggle('hidden', !state.hasMore);
        // N16：只有确实还有没加载完的作品时才提示“已浏览全部”
        if (!state.hasMore && result.total > state.limit) end.classList.remove('hidden');
        if (titles.length) state.page += 1;
        // F12：用请求页而非递增后的 state.page 判断首屏
        if (isFirstPage && result.total === 0 && !hasActiveFilters()) checkBootstrapSync();
        // 后退/前进导航恢复滚动位置（仅首页加载路径，用户主动筛选不触发）
        // F12+：目标超出首批 DOM 高度时，按已加载页数补载后再滚动。
        if (isFirstPage && state.restoreScroll != null && !state.userInitiatedFilter) {
            const target = state.restoreScroll;
            const pages = Math.max(1, Number(state.restorePages || 1));
            state.restoreScroll = null;
            state.restorePages = 1;
            if (pages > 1 && state.hasMore) {
                state.restoreScrollTarget = target;
                state.restorePagesTarget = pages;
            } else {
                requestAnimationFrame(() => window.scrollTo({ top: target, behavior: 'auto' }));
            }
        }
        if (state.restoreScrollTarget != null && !state.userInitiatedFilter) {
            const targetPages = state.restorePagesTarget || 1;
            const represented = Math.min(
                (currentPage - 1) * state.limit + titles.length,
                result.total,
            );
            if (represented >= Math.min(targetPages * state.limit, result.total)) {
                const top = state.restoreScrollTarget;
                state.restoreScrollTarget = null;
                state.restorePagesTarget = 1;
                requestAnimationFrame(() => window.scrollTo({ top, behavior: 'auto' }));
            }
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
        // 便于定位线上“内容没有加载出来”：把筛选条件与错误一起打到控制台
        console.error('[loadTitles] 列表加载失败', pageCacheKey(), error);
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
            // 深滚动恢复：目标页数未达到时继续补载（仅在导航恢复场景）
            if (state.restoreScrollTarget != null && state.hasMore && !state.userInitiatedFilter) {
                loadTitles();
            }
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
        state.snapshotEntries = new Map();
    }
    if (clear && !titles.length) {
        renderEmptyState();
        return;
    }
    const showRank = state.sort_by === 'rating';
    // 按当前视图模式选择卡片工厂（网格卡 / 横向列表项）；
    // R01：目录缺失的个人条目使用“资料待补全”卡片，保留状态与记录操作。
    const createCard = state.viewMode === 'list' ? createTitleListItem : createTitleCard;
    const fragment = document.createDocumentFragment();
    titles.forEach((title, index) => {
        if (title.catalog_available === false || title.id == null) {
            const key = missingEntryKey(title);
            state.snapshotEntries.set(key, title);
            const card = createMissingCatalogCard(title);
            prepareCardEntrance(card, index);
            fragment.appendChild(card);
            return;
        }
        state.loadedTitleIds.push(Number(title.id));
        const position = rankBase + index + 1;
        // N13/5.2：排名徽章只在前 10 显示，减少海报覆盖物
        const card = createCard(title, showRank && position <= 10 ? position : null, rankBase + index);
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

/* 5.5 我的片单辅助信息：已看优先显示观看日期 / 个人评分 */
function libraryCardMeta(title) {
    if (state.section !== 'library') return '';
    if (title.watched_at) return `观看于 ${formatVerifiedDate(title.watched_at)}`;
    if (title.personal_rating != null) return `我的评分 ${Number(title.personal_rating).toFixed(1)}`;
    return '';
}

/* 播放平台只在详情中展示，卡片默认只回答：叫什么、是什么类型、口碑怎样。 */
function createTitleCard(title, rank = null, index = 0) {
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
    const libraryMeta = libraryCardMeta(title);
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                ${cardPosterMarkup(title, CARD_POSTER_SIZES, typeLabel, index)}
                <span class="poster-loading-text" aria-hidden="true">${escapeHtml(title.title)}</span>
                <span class="batch-check" aria-hidden="true"></span>
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
                ${rank ? `<span class="rank-badge" data-rank="${rank}">${rank}</span>` : ''}
                <span class="poster-rating${rating ? '' : ' is-unrated'}"${tier ? ` data-tier="${tier}"` : ''}><span class="r-num">${rating ? rating.toFixed(1) : '待开分'}</span>${rating ? '<small>IMDb</small>' : ''}</span>
                ${priority > 0 ? `<span class="priority-badge" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>` : ''}
            </div>
            <div class="card-info">
                <h2 class="card-title">${highlightSearch(title.title, state.search)}</h2>
                <div class="card-meta"><span>${typeLabel}</span>${rating ? '' : '<span class="meta-new">新片</span>'}<span>${escapeHtml(cardYear(title.release_date))}</span>${genre ? `<span class="meta-genre">${escapeHtml(genre)}</span>` : ''}${libraryMeta ? `<span>${escapeHtml(libraryMeta)}</span>` : ''}${state.search ? matchReasonHtml(title.match_reason) : ''}</div>
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
function createTitleListItem(title, rank = null, index = 0) {
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
    const libraryMeta = libraryCardMeta(title);
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                ${cardPosterMarkup(title, LIST_POSTER_SIZES, typeLabel, index)}
                <span class="poster-loading-text" aria-hidden="true">${escapeHtml(title.title)}</span>
                <span class="batch-check" aria-hidden="true"></span>
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
            </div>
            <div class="list-info">
                <h2 class="card-title">${highlightSearch(title.title, state.search)}</h2>
                <p class="card-overview">${highlightSearch(title.overview || '暂无剧情简介', state.search)}</p>
                <div class="card-meta">
                    <span class="meta-rating${rating ? '' : ' is-unrated'}"${tier ? ` data-tier="${tier}"` : ''}>${rating ? `${rating.toFixed(1)} IMDb` : '新片'}</span>
                    <span>${typeLabel}</span><span>${escapeHtml(title.release_date || '日期待定')}</span>${region ? `<span>${escapeHtml(region)}</span>` : ''}${libraryMeta ? `<span>${escapeHtml(libraryMeta)}</span>` : ''}${state.search ? matchReasonHtml(title.match_reason) : ''}
                </div>
            </div>
            <div class="list-side">
                ${priority > 0 ? `<span class="list-priority" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>` : ''}
                ${rank ? `<span class="list-rank" aria-label="评分排名第 ${rank}">#${rank}</span>` : ''}
                <span class="list-rating"${tier ? ` data-tier="${tier}"` : ''}>${rating ? `${rating.toFixed(1)}<small>IMDb</small>` : '<span class="list-unrated">待开分</span>'}</span>
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

/* 卡片用（网格/列表）：有海报走响应式图片，无海报走文字封面。
 * P03：前 10 张 eager、前 2 张 high 优先级，慢网首屏不是一堵灰墙。 */
function cardPosterMarkup(title, sizes, typeLabel, index = 0) {
    const poster = sanitizeUrl(title.poster_url);
    if (poster && !poster.startsWith('data:image/')) {
        const loading = index < 10 ? 'eager' : 'lazy';
        const priority = index < 2 ? ' fetchpriority="high"' : '';
        return `<img ${responsivePosterAttributes(poster, sizes)} alt="${escapeHtml(title.title)} 海报" loading="${loading}"${priority} decoding="async" onload="window.handleGridPosterLoad(this)" onerror="window.handlePosterError(this)">`;
    }
    return posterTextCover(title.title, `${typeLabel} · ${cardYear(title.release_date)}`);
}

/* 切换网格/列表视图：localStorage 持久化；保留当前首屏锚点与已加载页数 */
function setViewMode(mode) {
    if (!['grid', 'list'].includes(mode) || state.viewMode === mode) return;
    const firstCard = document.querySelector('#titles-grid .title-card');
    const anchorId = firstCard?.dataset.titleId ? Number(firstCard.dataset.titleId) : null;
    const loadedPages = Math.max(1, Math.ceil((state.loadedTitleIds.length || state.limit) / state.limit));
    state.viewMode = mode;
    try { localStorage.setItem('view_mode', mode); } catch (_) { /* 存储不可用时仅本次生效 */ }
    document.querySelectorAll('.view-toggle-btn').forEach(button => {
        const active = button.dataset.view === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.getElementById('titles-grid')?.setAttribute('data-view', mode);
    // 重载已加载页数（分页缓存命中，秒级返回），渲染后滚回原锚点
    state.requestVersion += 1;
    const version = state.requestVersion;
    state.page = 1;
    state.loading = false;
    state.hasMore = true;
    updateUrl();
    syncControlsFromState();
    renderActiveFilters();
    document.getElementById('scroll-end').classList.add('hidden');
    document.getElementById('scroll-sentinel').classList.remove('hidden');
    renderSkeletons();
    (async () => {
        for (let index = 0; index < loadedPages; index += 1) {
            if (version !== state.requestVersion || !state.hasMore) break;
            await loadTitles();
        }
        if (anchorId != null) {
            document.querySelector(`.title-card[data-title-id="${CSS.escape(String(anchorId))}"]`)
                ?.scrollIntoView({ block: 'start', behavior: 'auto' });
        }
    })();
}

/* 6.2 内容模式：高分精选 = 评分 7.5+ 且按加权评分排序；近期新片 = 时间窗口 + 最新排序。
 * 按钮状态由筛选条件推导，用户手动改筛选后自然回到“自定义”，不会假装仍在某个模式。 */
function premiumActive() {
    return state.releasedDays === 0 && state.sort_by === 'rating' && state.rating >= 7.5;
}

function applyMode(mode) {
    if (mode === 'premium') {
        if (premiumActive()) {
            state.rating = 0;
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
    resetAndLoad({ scroll: false });
}

/* D：每个 section 保留自己的筛选状态；进入片单不继承发现页条件 */
function captureSectionSnapshot(section) {
    if (!SECTION_DEFAULTS[section]) return;
    state.sectionSnapshots = state.sectionSnapshots || {};
    state.sectionSnapshots[section] = {
        type: state.type,
        search: state.search,
        region: state.region,
        rating: state.rating,
        genres: [...state.genres],
        years: state.years,
        maxRuntime: state.maxRuntime,
        excludeWatched: state.excludeWatched,
        releasedDays: state.releasedDays,
        sort_by: state.sort_by,
        order: state.order,
        watchStatus: section === 'library'
            ? (state.watchStatus || state.libraryStatus || 'watchlist')
            : state.watchStatus,
    };
}

function restoreSectionSnapshot(section) {
    const base = SECTION_DEFAULTS[section];
    const snapshot = state.sectionSnapshots?.[section] || base;
    state.type = snapshot.type ?? base.type;
    state.search = snapshot.search ?? base.search;
    state.region = snapshot.region ?? base.region;
    state.rating = Number(snapshot.rating ?? base.rating) || 0;
    state.genres = Array.isArray(snapshot.genres) ? [...snapshot.genres] : [...(base.genres || [])];
    state.years = snapshot.years ?? base.years ?? '';
    state.maxRuntime = Number(snapshot.maxRuntime ?? base.maxRuntime) || 0;
    state.excludeWatched = Boolean(snapshot.excludeWatched);
    state.releasedDays = Number(snapshot.releasedDays ?? base.releasedDays) || 0;
    state.sort_by = snapshot.sort_by || base.sort_by;
    state.order = snapshot.order === 'asc' ? 'asc' : 'desc';
    if (section === 'library') {
        state.libraryStatus = ['watchlist', 'watching', 'watched'].includes(snapshot.watchStatus)
            ? snapshot.watchStatus
            : (state.libraryStatus || 'watchlist');
        state.watchStatus = state.libraryStatus;
    } else {
        state.watchStatus = '';
    }
}

/* 5.2 主导航：发现 / 我的片单（WP-5 F4：原“近期上映”并入“近期新片”模式） */
function setSection(section, options = {}) {
    if (!['discover', 'library'].includes(section) || state.section === section) return;
    captureSectionSnapshot(state.section);
    state.section = section;
    restoreSectionSnapshot(section);
    if (options.search !== undefined) {
        state.search = String(options.search || '').trim();
        if (state.sectionSnapshots[section]) state.sectionSnapshots[section].search = state.search;
    }
    if (section !== 'library') setBatchMode(false);
    window.__closeSidebar?.(false);
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
    if (ids.length > 200) {
        showToast('批量操作单次上限 200 部，请减少选择', 'warn');
        return;
    }
    try {
        const result = await api('/api/titles/batch', {
            method: 'PATCH',
            body: JSON.stringify({ ids, ...payload }),
        });
        showToast(`${successText} ${result.updated} 部${result.skipped ? ` · 跳过 ${result.skipped} 部` : ''}`);
        clearAllCaches();
        invalidatePersistentDataCache();
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

function statusMenuHtml(id, current, key = '') {
    const options = [
        ['', '不在片单'], ['watchlist', '想看'], ['watching', '在看'], ['watched', '已看'],
    ];
    return `<div class="status-menu hidden" role="menu" aria-label="选择片单状态">
        ${options.map(([value, label]) => `<button type="button" role="menuitem" data-title-id="${id}"${key ? ` data-title-key="${key}"` : ''} data-set-status="${value}" class="${current === value ? 'active' : ''}">${label}</button>`).join('')}
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
    state.activeMissingKey = null;
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
    const footerPrev = document.querySelector('.modal-footer-nav [data-action="detail-prev"]');
    const footerNext = document.querySelector('.modal-footer-nav [data-action="detail-next"]');
    const i = state.currentDetailIndex;
    const list = state.loadedTitleIds;
    const has = i >= 0 && list.length > 1;
    const prevDisabled = !has || i <= 0;
    const nextDisabled = !has || i >= list.length - 1;
    if (prev) prev.disabled = prevDisabled;
    if (next) next.disabled = nextDisabled;
    if (footerPrev) footerPrev.disabled = prevDisabled;
    if (footerNext) footerNext.disabled = nextDisabled;
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
    state.genres.forEach(name => params.append('genre', name));
    const years = yearRangeParams(state.years);
    if (years?.year_from) params.set('year_from', String(years.year_from));
    if (years?.year_to) params.set('year_to', String(years.year_to));
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
    return scope === 'filters' ? '当前结果' : `我的${watchStatusNames[scope]}`;
}

async function surpriseFetch(scope, page) {
    if (scope === 'filters') {
        return getCachedPage(pageCacheKey(), page, () => api(
            `/api/titles?${buildFilterParams({ page: String(page), limit: String(state.limit) })}`,
            { timeout: 20000 },
        ));
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

/* ── R03：我的记录区域独立渲染，状态/字段保存后局部替换，不整屏重渲染 ── */
function personalRatingOptionHtml(current) {
    const values = [];
    for (let value = 10; value >= 0.5; value -= 0.5) {
        values.push(Math.round(value * 2) / 2);
    }
    const normalized = current == null || current === '' ? null : Number(current);
    if (normalized != null && Number.isFinite(normalized) && !values.includes(normalized)) {
        values.push(normalized);
        values.sort((a, b) => b - a);
    }
    return values.map(value => (
        `<option value="${value}"${normalized === value ? ' selected' : ''}>${value} 分</option>`
    )).join('');
}

function personalSectionInnerHtml(title, status, options = {}) {
    const entryAttrs = options.entryKey
        ? ` data-entry-key="${escapeHtml(options.entryKey)}"`
        : '';
    if (!status) return '<p class="provider-note">加入片单后，可记录优先级、个人评分与备注。</p>';
    return `<div class="modal-section-title">我的记录</div>
        <div class="personal-fields" data-title-id="${title.id ?? ''}"${entryAttrs}>
            <label class="personal-field"><span>优先级</span>
                <select class="personal-select" data-personal="priority" data-saved-value="${Number(title.priority || 0)}">
                    ${[[0, '普通'], [1, '优先'], [2, '必看']].map(([value, label]) => `<option value="${value}"${Number(title.priority || 0) === value ? ' selected' : ''}>${label}</option>`).join('')}
                </select></label>
            <label class="personal-field"><span>我的评分</span>
                <select class="personal-select" data-personal="personal_rating" data-saved-value="${title.personal_rating == null ? '' : Number(title.personal_rating)}">
                    <option value=""${title.personal_rating == null ? ' selected' : ''}>未评分</option>
                    ${personalRatingOptionHtml(title.personal_rating)}
                </select></label>
            <label class="personal-field personal-field-note"><span>备注</span>
                <input class="personal-note-input" type="text" maxlength="200" data-personal="note" data-saved-value="${escapeHtml(title.note || '')}" placeholder="例如：周末陪家人看" value="${escapeHtml(title.note || '')}"></label>
            ${title.watched_at ? `<p class="provider-note personal-watched">观看于 ${escapeHtml(formatVerifiedDate(title.watched_at))}</p>` : ''}
        </div>`;
}

function updatePersonalSection(title) {
    const container = document.getElementById('personal-section');
    if (!container) return;
    const options = state.activeMissingKey
        ? { entryKey: state.activeMissingKey }
        : {};
    container.innerHTML = personalSectionInnerHtml(title, title.watch_status || '', options);
}

/* ── R02：观看渠道逐地区 offer 渲染 ── */
function providerChannelName(offer) {
    const group = offer.provider_group || 'others';
    if (group === 'others') {
        return OTHER_PROVIDER_ZH[offer.provider_name] || offer.provider_name;
    }
    return providerNames[group] || offer.provider_name;
}

function renderOfferList(offers) {
    const viewingRegion = state.viewingRegion;
    // R02：先按所选地区过滤 offer，再按平台合并；观看方式不跨地区串台
    const scopedOffers = viewingRegion
        ? offers.filter(offer => offer.region === viewingRegion)
        : offers;
    const groups = new Map();
    scopedOffers.forEach(offer => {
        const key = `${offer.provider_group || 'others'}|${offer.provider_name}`;
        const item = groups.get(key) || {
            name: providerChannelName(offer), group: offer.provider_group || 'others',
            monetizations: new Set(), regions: new Set(), latest: '',
        };
        item.monetizations.add(MONETIZATION_LABELS[offer.monetization] || offer.monetization || '渠道');
        if (offer.region) item.regions.add(offer.region);
        if (offer.verified_at && offer.verified_at > item.latest) item.latest = offer.verified_at;
        groups.set(key, item);
    });
    const displayed = [...groups.values()];
    const verified = scopedOffers.map(offer => offer.verified_at).filter(Boolean).sort().pop() || '';
    const chips = displayed.map(item => {
        const regions = [...item.regions];
        const regionText = viewingRegion
            ? ''
            : regions.length > 2
                ? ` · ${regions.slice(0, 2).map(code => displayRegionName(code, true)).join('、')} 等 ${regions.length} 个地区`
                : ` · ${regions.map(code => displayRegionName(code, true)).join('、')}`;
        const monetization = [...item.monetizations].filter(Boolean).join(' / ');
        return `<span class="modal-provider">
            <span class="p-dot" style="background:${providerColors[item.group] || '#7f7d75'}"></span>
            <strong>${escapeHtml(item.name)}</strong>${monetization ? ` · ${escapeHtml(monetization)}` : ''}${escapeHtml(regionText)}</span>`;
    });
    let note = '';
    if (viewingRegion && !displayed.length) {
        chips.push(`<p class="provider-note provider-note-strong">在${escapeHtml(viewingRegionName(viewingRegion))}暂无已核验的观看渠道。</p>`);
        note = '该地区尚未核验，或已核验但暂无渠道。';
    } else if (viewingRegion) {
        note = `已按${escapeHtml(viewingRegionName(viewingRegion))}过滤，核验时间取自对应地区。`;
    } else {
        note = '未选择地区：以下为已核验的平台与地区对应关系。';
    }
    return { listHtml: chips.join(''), note, verified };
}

/* N15/4.3：历史聚合渠道的紧凑展示 —— 紧凑地区名、>2 个地区折叠、无标签 others 合并 */
function renderChannelChip(d, pending = false) {
    const isUnlabeledOthers = d.provider === 'others' && !(d.labels || []).length;
    const regionCodes = d.regions || [];
    const shown = regionCodes.slice(0, 2).map(code => displayRegionName(code, true)).join('、');
    const regionText = pending || !regionCodes.length
        ? ''
        : regionCodes.length > 2
            ? ` · ${shown} 等 ${regionCodes.length} 个地区`
            : ` · ${shown}`;
    const name = isUnlabeledOthers
        ? `其他平台 · ${Math.max(regionCodes.length, 1)} 个地区（待核验）`
        : `${d.provider === 'others' ? otherProviderLabel(d.labels) : (providerNames[d.provider] || d.provider)}${regionText}`;
    const tip = pending || !regionCodes.length
        ? '地区待确认'
        : `覆盖地区：${regionCodes.map(code => displayRegionName(code, true)).join('、')}`;
    return `<span class="modal-provider${pending ? ' is-pending' : ''}" title="${escapeHtml(tip)}">
        <span class="p-dot" style="background:${providerColors[d.provider] || '#7f7d75'}"></span>${escapeHtml(name)}</span>`;
}

function buildChannelView(title) {
    const details = title.provider_details || [];
    const structuredOffers = (title.offers || []).filter(offer => offer && !offer.legacy && offer.provider_name);
    const legacyOffers = (title.offers || []).filter(offer => offer && offer.legacy);
    const viewingRegion = state.viewingRegion;
    let listHtml = '';
    let noteHtml = '';
    let verified = '';
    if (structuredOffers.length) {
        // R02：有逐地区结构化 offer 时按地区过滤、按平台合并展示
        const offerView = renderOfferList(structuredOffers);
        listHtml = offerView.listHtml;
        noteHtml = offerView.note ? `<p class="provider-note">${offerView.note}</p>` : '';
        verified = offerView.verified;
        if (legacyOffers.length) {
            const legacyNames = new Set(legacyOffers.map(offer => offer.provider_name).filter(Boolean));
            noteHtml += `<p class="provider-note">另有 ${legacyNames.size} 个历史渠道记录待重新核验。</p>`;
        }
    } else if (details.length) {
        const matchedChannels = [];
        const pendingChannels = [];
        details.forEach(d => {
            const regions = d.regions || [];
            if (!viewingRegion) matchedChannels.push(d);
            else if (!regions.length) pendingChannels.push(d);
            else if (regions.includes(viewingRegion)) matchedChannels.push(d);
        });
        listHtml = matchedChannels.map(d => renderChannelChip(d)).join('');
        if (viewingRegion && !matchedChannels.length) {
            listHtml += `<p class="provider-note provider-note-strong">在${escapeHtml(viewingRegionName(viewingRegion))}暂无已核验的观看渠道。</p>`;
        }
        if (viewingRegion && pendingChannels.length) {
            listHtml += `<span class="provider-pending-label">尚未核验该地区：</span>${pendingChannels.map(d => renderChannelChip(d, true)).join('')}`;
        }
        // 长免责声明收进 ⓘ 提示，避免每条渠道后都挂一句开发者文案
        noteHtml = '<p class="provider-note provider-note-hint" title="历史渠道按平台聚合，地区对应可能不精确；重新核验后按 平台 × 地区 × 观看方式 逐条展示"><span class="hint-icon" aria-hidden="true">i</span>渠道信息可能不完整</p>';
        verified = details.map(d => d.last_seen_at).filter(Boolean).sort().pop() || '';
    }
    return { listHtml, noteHtml, verified };
}

function channelsSectionHtml(title) {
    const view = buildChannelView(title);
    if (!view.listHtml && !view.noteHtml) return '';
    return `<div class="channels-head"><div class="modal-section-title" id="channels">观看渠道</div>${viewingRegionSelectHtml()}</div>
        <div class="modal-providers">${view.listHtml}</div>
        ${view.noteHtml}
        ${view.verified ? `<p class="provider-note">最近核验：${escapeHtml(formatVerifiedDate(view.verified))}</p>` : ''}`;
}

/* N13/4.2：详情动作层级 —— 未加入只有“加入想看”一个主按钮；已加入用分段控件；
 * “观看渠道”是页内锚点，降为文本链接；复制链接改图标按钮。 */
function actionAreaHtml(title, status, options = {}) {
    const channelLink = options.hasChannels
        ? '<a class="link-inline" href="#channels" data-action="goto-channels">观看渠道 <span aria-hidden="true">↓</span></a>'
        : '';
    const copyButton = '<button type="button" class="icon-btn" data-action="copy-link" aria-label="复制这部作品的链接" title="复制链接">⧉</button>';
    if (!status) {
        return `<div class="split-btn" data-title-id="${title.id}">
            <button type="button" class="btn-primary" data-set-status="watchlist">加入想看</button>
            <button type="button" class="btn-primary-more" data-action="toggle-status-menu" aria-haspopup="menu" aria-expanded="false" aria-label="更多状态">▾</button>
            <div class="status-menu hidden" role="menu" aria-label="选择片单状态">
                <button type="button" role="menuitem" data-set-status="watching">在看</button>
                <button type="button" role="menuitem" data-set-status="watched">已看</button>
            </div>
        </div>${channelLink}${copyButton}`;
    }
    return `<div class="status-picker status-picker-compact" data-title-id="${title.id}">
        ${[['watchlist', '想看'], ['watching', '在看'], ['watched', '已看']].map(([value, label]) => `<button type="button" data-set-status="${value}" class="${status === value ? 'active' : ''}">${label}</button>`).join('')}
        <button type="button" class="icon-btn" data-set-status="" aria-label="移出片单" title="移出片单">×</button>
    </div>${channelLink}${copyButton}`;
}

function updateDetailActions(title) {
    const container = document.getElementById('detail-actions');
    if (!container) return;
    const hasChannels = Boolean(document.getElementById('channels'))
        || Boolean((title.provider_details || []).length || (title.offers || []).length);
    container.innerHTML = actionAreaHtml(title, title.watch_status || '', { hasChannels });
}

/* 7.1：手机端详情标题移出装饰区、跨整行显示；桌面保持标题在顶部氛围层 */
const mobileDetailQuery = typeof window.matchMedia === 'function'
    ? window.matchMedia('(max-width: 680px)')
    : null;

function placeDetailTitle() {
    const heroTitle = document.querySelector('#detail-content .modal-hero-title');
    const heroContent = document.querySelector('#detail-content .modal-hero-content');
    const summary = document.querySelector('#detail-content .modal-summary');
    if (!heroTitle || !heroContent || !summary) return;
    if (mobileDetailQuery?.matches) {
        if (heroTitle.parentElement !== summary) summary.insertBefore(heroTitle, summary.firstChild);
    } else if (heroTitle.parentElement !== heroContent) {
        heroContent.appendChild(heroTitle);
    }
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
    // 4.3/4.4：渠道区抽成独立片段，切换观看地区只替换这一段
    const channelSection = channelsSectionHtml(title);
    const status = title.watch_status || '';
    const zhQualityNote = ZH_QUALITY_NOTES[title.zh_quality] || '';
    const imdbLink = title.imdb_id ? `<a class="modal-link" href="https://www.imdb.com/title/${encodeURIComponent(title.imdb_id)}/" target="_blank" rel="noopener noreferrer">在 IMDb 查看 ${externalIcon()}</a>` : '';
    const tmdbType = title.type === 'movie' ? 'movie' : 'tv';
    const watchLink = `<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}/watch" target="_blank" rel="noopener noreferrer">打开观看指南（TMDB） ${externalIcon()}</a>`;
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
    // 4.1：评分并入 meta 信息行，hero 不再放大方块
    const scoreTag = rating
        ? `<span class="meta-tag meta-score"${tier ? ` data-tier="${tier}"` : ''} role="img" aria-label="IMDb 评分 ${rating.toFixed(1)}，${escapeHtml(votesText)}">${rating.toFixed(1)} <small>IMDb · ${escapeHtml(votesText)}</small></span>`
        : '<span class="meta-tag meta-score is-unrated">新片 · 待开分</span>';
    // 6.7 我的记录：优先级 / 个人评分 / 备注 / 观看日期（未加入片单时给出提示）
    const personalSection = `<div class="personal-section" id="personal-section">${personalSectionInnerHtml(title, status)}</div>`;
    // R13：由惊喜发现打开的详情，不关闭模态即可换一部或标记今晚看
    const isSurprise = Number(title.id) === Number(state.surpriseLastId);
    const surpriseRow = isSurprise ? `<div class="surprise-row" role="status">
        <p class="surprise-row-text">来自${escapeHtml(surpriseScopeLabel())}的随机推荐${title.runtime ? ` · ${title.type === 'movie' ? '' : '单集约 '}${Number(title.runtime)} 分钟` : ''}${genres.length ? ` · ${escapeHtml(genres[0])}` : ''}</p>
        <div class="surprise-row-buttons">
            <button type="button" class="btn-retry" data-action="surprise-again">换一部</button>
            <button type="button" class="btn-primary" data-action="surprise-tonight">今晚看这个</button>
        </div>
    </div>` : '';
    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" ${heroBgStyle}></div>
            <div class="modal-hero-content">
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
                    ${scoreTag}
                    <span class="meta-tag">${title.type === 'movie' ? '电影' : '剧集'}</span>
                    ${region ? `<span class="meta-tag">${escapeHtml(region)}</span>` : ''}
                    <span class="meta-tag">${escapeHtml(title.release_date || '日期待定')}</span>
                    ${metaExtra}
                </div>
            </div>
            <div class="modal-actions-row">
                <div class="modal-actions" id="detail-actions">
                    ${actionAreaHtml(title, status, { hasChannels: Boolean(channelSection) })}
                </div>
                ${surpriseRow}
            </div>
            <div class="modal-details">
                ${personalSection}
                <div class="modal-section-title">剧情简介${zhQualityNote ? `<span class="zh-quality-note">${zhQualityNote}</span>` : ''}</div>
                <div class="modal-overview">${escapeHtml(title.overview || '暂无剧情简介')}</div>
                ${cast.length ? `<div class="modal-cast">主演：${cast.map(name => escapeHtml(name)).join(' / ')} ${castExtra}</div>` : ''}
                ${genres.length ? `<div class="modal-genres">${genres.map(g => `<span class="genre-tag">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
                ${channelSection ? `<div id="channels-section">${channelSection}</div>` : ''}
                <div class="modal-section-title">资料与观看指南</div>
                <div class="modal-links">${imdbLink}<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}" target="_blank" rel="noopener noreferrer">在 TMDB 查看 ${externalIcon()}</a>${watchLink}${trailer}</div>
                <div class="modal-section-title">同类推荐</div>
                <div class="related-row" id="related-row"><span class="related-loading">正在加载推荐…</span></div>
                <div class="modal-footer-nav">
                    <button type="button" class="btn-retry" data-action="detail-prev" disabled>‹ 上一部</button>
                    <button type="button" class="btn-retry" data-action="detail-next" disabled>下一部 ›</button>
                </div>
            </div>
        </div>`;
    placeDetailTitle(); // 7.1：按视口把标题放在装饰层（桌面）或正文顶部（手机）
    updateDetailNav();  // N06：底部“上一部/下一部”按钮与当前列表位置同步
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

/* F-restore / R05：读取备份 → 后端统一校验预览 → 用户确认后合并导入 */
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
    const schemaVersion = Number(Array.isArray(parsed) ? 1 : (parsed.schema_version || 1)) || 1;
    let preview;
    try {
        preview = await api('/api/watchlist/import', {
            method: 'POST',
            body: JSON.stringify({ items, schema_version: schemaVersion, dry_run: true }),
        });
    } catch (error) {
        showToast(userMessage(error), 'error');
        return;
    }
    const fieldsChanged = preview.field_changes || {};
    const changedLabels = Object.entries(fieldsChanged)
        .filter(([, count]) => count > 0)
        .map(([key, count]) => `${{ watch_status: '状态', priority: '优先级', note: '备注', personal_rating: '评分', watched_at: '观看日期' }[key] || key} ${count}`)
        .join(' · ');
    const lines = [
        '从备份恢复片单？',
        `新增 ${preview.added} · 更新 ${preview.updated} · 无变化 ${preview.unchanged}`
        + (preview.protected ? ` · 保护较新记录 ${preview.protected}` : '')
        + (preview.invalid ? ` · 无效 ${preview.invalid}` : '')
        + (preview.duplicate ? ` · 重复 ${preview.duplicate}` : ''),
    ];
    if (changedLabels) lines.push(`更新内容：${changedLabels}`);
    if (preview.catalog_pending) lines.push(`目录待补全 ${preview.catalog_pending} 部（个人记录先恢复，之后自动补全）`);
    lines.push('更新会覆盖备份中已有的状态 / 备注 / 评分 / 优先级。');
    if (!window.confirm(lines.join('\n'))) return;
    let force = false;
    if (preview.protected > 0) {
        force = window.confirm(
            `检测到 ${preview.protected} 条本地记录比备份更新。\n`
            + '确定：按备份覆盖这些较新记录；取消：保留本地较新记录。',
        );
    }
    try {
        const result = await api('/api/watchlist/import', {
            method: 'POST',
            body: JSON.stringify({ items, schema_version: schemaVersion, force }),
        });
        showToast(
            `恢复完成：新增 ${result.added} · 更新 ${result.updated}`
            + ` · 无变化 ${result.unchanged}`
            + (result.protected ? ` · 保护 ${result.protected}` : '')
            + (result.skipped ? ` · 跳过 ${result.skipped}` : ''),
        );
        clearAllCaches();
        invalidatePersistentDataCache();
        await Promise.allSettled([loadStats()]);
        reloadListSilently();
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
        invalidatePersistentDataCache();
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

/* ── 乐观更新：状态切换先改本地 UI，失败按本次增量回滚（F15）──
 * 不再用全局计数快照还原（会覆盖并发成功项），只撤销本次增减。
 */

/* 统一作品引用：目录作品 {id}；目录缺失的个人条目 {key,type,tmdbId} */
function refForId(id) {
    return { id: Number(id) };
}
function refForIdentity(identity) {
    return {
        key: `${identity.type}:${Number(identity.tmdbId)}`,
        type: identity.type,
        tmdbId: Number(identity.tmdbId),
    };
}
function refStorageKey(ref) {
    return ref.key || String(ref.id);
}
function cardForRef(ref) {
    if (ref.key) {
        return document.querySelector(`.title-card[data-title-key="${CSS.escape(ref.key)}"]`);
    }
    if (ref.id == null) return null;
    return document.querySelector(`.title-card[data-title-id="${CSS.escape(String(ref.id))}"]`);
}
function pickerForRef(ref) {
    if (ref.key) return document.querySelector(`.status-picker[data-entry-key="${CSS.escape(ref.key)}"]`);
    return document.querySelector(`.status-picker[data-title-id="${CSS.escape(String(ref.id))}"]`);
}

/* 从当前详情或卡片 dataset 派生作品当前状态 */
function readCurrentStatus(ref) {
    if (!ref.key && currentDetail?.id === ref.id) return currentDetail.watch_status || '';
    return cardForRef(ref)?.dataset.watchStatus || '';
}

/* 构建回滚快照：旧状态、目标状态与详情缓存引用 */
function buildStatusSnapshot(ref, newStatus) {
    return {
        ref,
        oldStatus: readCurrentStatus(ref),
        newStatus,
        detailRef: ref.key ? null : cache.detail.get(ref.id),
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

/* 同步模态内状态选择器的高亮（目录详情与目录缺失详情共用） */
function syncPickerActive(ref, status) {
    pickerForRef(ref)?.querySelectorAll('[data-set-status]').forEach(button => {
        button.classList.toggle('active', button.dataset.setStatus === status);
    });
}

/* 乐观应用新状态：卡片、详情缓存、计数、模态选择器一次同步 */
function applyStatusOptimistic(ref, newStatus) {
    const oldStatus = readCurrentStatus(ref); // 必须在 updateCardStatus 改 dataset 之前读取
    updateCardStatus(ref, newStatus);
    if (!ref.key) {
        const cached = cache.detail.get(ref.id)?.data;
        if (cached) cached.watch_status = newStatus;
    }
    syncPickerActive(ref, newStatus);
    if (!ref.key && currentDetail?.id === ref.id) {
        updateDetailActions({ ...currentDetail, watch_status: newStatus });
    }
    applyStatusCounts(oldStatus, newStatus);
}

/* 失败回滚：反向还原本次改动的视觉与数据 */
function rollbackStatus(snapshot) {
    updateCardStatus(snapshot.ref, snapshot.oldStatus);
    if (snapshot.detailRef?.data) snapshot.detailRef.data.watch_status = snapshot.oldStatus;
    if (!snapshot.ref.key && currentDetail?.id === snapshot.ref.id) {
        currentDetail = { ...currentDetail, watch_status: snapshot.oldStatus };
        updateDetailActions(currentDetail);
        updatePersonalSection(currentDetail);
    }
    applyStatusCounts(snapshot.newStatus, snapshot.oldStatus);
}

/* R07：写入后通知 Service Worker 清空持久数据缓存，避免离线看到旧状态 */
function invalidatePersistentDataCache() {
    try { navigator.serviceWorker?.controller?.postMessage({ type: 'invalidate-data' }); } catch (_) { /* 忽略 */ }
}

/* 当前排序依赖被修改字段时静默重取列表（保留滚动位置与模态输入） */
function fieldAffectsLibrarySort(field) {
    if (state.section !== 'library') return false;
    if (state.sort_by === 'priority') return field === 'priority';
    if (state.sort_by === 'updated_at') return ['priority', 'note', 'personal_rating'].includes(field);
    return false;
}

function reloadListSilently() {
    state.requestVersion += 1;
    state.page = 1;
    state.loading = false;
    state.hasMore = true;
    loadTitles();
}

/* 卡片优先级徽标即时同步（网格/列表/目录缺失三种结构） */
function updateCardPriority(idOrRef, priority) {
    const ref = typeof idOrRef === 'object' && idOrRef !== null ? idOrRef : refForId(idOrRef);
    const card = cardForRef(ref);
    if (!card) return;
    card.querySelectorAll('.priority-badge, .list-priority').forEach(el => el.remove());
    if (priority > 0) {
        const isList = card.classList.contains('is-list');
        const badge = `<span class="${isList ? 'list-priority' : 'priority-badge'}" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>`;
        const host = isList ? card.querySelector('.list-side') : card.querySelector('.poster-wrap');
        host?.insertAdjacentHTML(isList ? 'afterbegin' : 'beforeend', badge);
    }
}

async function setTitleStatus(id, watchStatus, sourceButton, identity = null) {
    const ref = identity ? refForIdentity(identity) : refForId(id);
    const pendingKey = refStorageKey(ref);
    // 幂等短路：目标状态与当前一致时不发请求
    if (readCurrentStatus(ref) === watchStatus) { closeStatusMenus(); return; }
    // F15：按作品跟踪并发，A 未完成时 B 仍可操作
    if (state.pendingStatuses.has(pendingKey)) {
        showToast('操作进行中，请稍候', 'warn');
        return;
    }
    const snapshot = buildStatusSnapshot(ref, watchStatus);
    applyStatusOptimistic(ref, watchStatus);
    state.pendingStatuses.add(pendingKey);
    state.optimisticPending = pendingKey; // 兼容旧调试字段
    const scope = sourceButton?.closest('.status-picker, .status-menu');
    scope?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
        const title = ref.key
            ? await api(`/api/watchlist/${ref.type}/${ref.tmdbId}/status`, {
                method: 'PATCH',
                body: JSON.stringify({ watch_status: watchStatus }),
            })
            : await api(`/api/titles/${ref.id}/status`, {
                method: 'PATCH',
                body: JSON.stringify({ watch_status: watchStatus }),
            });
        state.pendingStatuses.delete(pendingKey);
        state.optimisticPending = state.pendingStatuses.size ? [...state.pendingStatuses][0] : null;
        if (ref.key) {
            state.snapshotEntries.set(ref.key, { ...(state.snapshotEntries.get(ref.key) || {}), ...title });
            if (state.activeMissingKey === ref.key) {
                currentDetail = title;
                syncPickerActive(ref, watchStatus);
                updatePersonalSection(title);
            }
        } else {
            // 以权威数据覆盖详情缓存（含最新 watch_status 与时间戳）
            setDetail(ref.id, title);
            if (currentDetail?.id === ref.id) {
                currentDetail = title;
                updateDetailActions(title);
                updatePersonalSection(title); // R03：状态成功后立即刷新“我的记录”
            }
        }
        // F04：状态写成功后失效全部分页缓存，避免其他视图/排序沿用旧成员集合
        invalidatePageCache();
        invalidatePersistentDataCache();
        showToast(watchStatus ? `已加入“${watchStatusNames[watchStatus]}”` : '已从片单移除');
        // 后台校正统计计数（乐观值通常已一致，仅防并发漂移）
        loadStats().catch(() => {});
        // 当前视图按状态过滤且成员集合变化 → 静默刷新列表
        if (state.watchStatus || state.section === 'library') {
            reloadListSilently();
        } else if (state.excludeWatched && watchStatus === 'watched') {
            reloadListSilently();
        }
    } catch (error) {
        state.pendingStatuses.delete(pendingKey);
        state.optimisticPending = state.pendingStatuses.size ? [...state.pendingStatuses][0] : null;
        rollbackStatus(snapshot);
        // F15：失败后以权威统计校正，避免与并发成功项叠加
        loadStats().catch(() => {});
        showToast(userMessage(error), 'error');
    } finally {
        scope?.querySelectorAll('button').forEach(button => { button.disabled = false; });
        closeStatusMenus();
    }
}

/* 6.7 保存个人记录：同作品写入串行化；成功后局部更新表单、徽标与缓存 */
const preferenceSaveChains = new Map();

function enqueuePreferenceSave(key, task) {
    const previous = preferenceSaveChains.get(key) || Promise.resolve();
    const next = previous.then(task, task);
    preferenceSaveChains.set(key, next.catch(() => {}));
    return next;
}

function applyPreferenceSuccess(ref, entry, field) {
    if (ref.key) {
        state.snapshotEntries.set(ref.key, { ...(state.snapshotEntries.get(ref.key) || {}), ...entry });
        if (state.activeMissingKey === ref.key) {
            currentDetail = entry;
            updatePersonalSection(entry);
        }
        updateCardPriority(ref, Number(entry.priority) || 0);
    } else {
        setDetail(ref.id, entry);
        if (currentDetail?.id === ref.id) {
            currentDetail = entry;
            updatePersonalSection(entry);
        }
        updateCardPriority(ref.id, Number(entry.priority) || 0);
    }
    invalidatePageCache();
    invalidatePersistentDataCache();
    showToast(field === 'note' ? '备注已保存' : '已保存');
    if (fieldAffectsLibrarySort(field)) reloadListSilently();
}

async function savePersonalField(id, field, rawValue, control = null, identity = null) {
    const ref = identity ? refForIdentity(identity) : refForId(id);
    const savedValue = control?.dataset.savedValue;
    if (savedValue !== undefined && String(rawValue) === String(savedValue)) return;
    let value;
    if (field === 'priority') value = Number(rawValue) || 0;
    else if (field === 'personal_rating') value = rawValue === '' ? 0 : Number(rawValue);
    else value = String(rawValue ?? '').slice(0, 200);

    control?.classList.remove('is-unsaved');
    control?.classList.add('is-saving');
    const path = ref.key
        ? `/api/watchlist/${ref.type}/${ref.tmdbId}/preference`
        : `/api/titles/${ref.id}/preference`;
    await enqueuePreferenceSave(refStorageKey(ref), async () => {
        try {
            const entry = await api(path, {
                method: 'PATCH',
                body: JSON.stringify({ [field]: value }),
            });
            control?.classList.remove('is-saving');
            if (control) control.dataset.savedValue = String(rawValue);
            applyPreferenceSuccess(ref, entry, field);
        } catch (error) {
            control?.classList.remove('is-saving');
            control?.classList.add('is-unsaved');
            showToast(`${userMessage(error)}（未保存）`, 'error');
        }
    });
}

function updateCardStatus(idOrRef, status) {
    const ref = typeof idOrRef === 'object' && idOrRef !== null ? idOrRef : refForId(idOrRef);
    const card = cardForRef(ref);
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

/* ── R01：目录缺失的个人条目：卡片 + 快照详情 ── */
function missingEntryKey(entry) {
    return `${entry.type}:${Number(entry.tmdb_id)}`;
}

function createMissingCatalogCard(entry) {
    const key = missingEntryKey(entry);
    const card = document.createElement('article');
    card.className = 'title-card is-list is-missing-catalog';
    card.dataset.titleKey = key;
    card.dataset.watchStatus = entry.watch_status || '';
    const status = entry.watch_status || '';
    const priority = Number(entry.priority) || 0;
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(entry.title || '资料待补全')} 的保存记录">
            <div class="poster-wrap">
                ${posterTextCover(entry.title || '资料待补全', '待补全')}
                <span class="batch-check" aria-hidden="true"></span>
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
            </div>
            <div class="list-info">
                <h2 class="card-title">${escapeHtml(entry.title || '资料待补全')}</h2>
                <p class="card-overview">目录中暂无这部作品，你的个人记录仍然完整保存；补全 TMDB 资料后会自动关联。</p>
                <div class="card-meta"><span>${entry.type === 'movie' ? '电影' : '剧集'}</span><span>资料待补全</span>${entry.release_date ? `<span>${escapeHtml(entry.release_date)}</span>` : ''}</div>
            </div>
            <div class="list-side">
                ${priority > 0 ? `<span class="list-priority" data-priority="${priority}">${priority >= 2 ? '必看' : '优先'}</span>` : ''}
                <span class="list-status-note">待补全</span>
            </div>
        </button>
        <div class="status-menu-wrap">
            <button class="status-menu-trigger ${status ? 'has-status' : ''}" type="button" aria-label="设置保存记录的状态" aria-haspopup="menu" aria-expanded="false">
                ${bookmarkIcon(status)}
            </button>
            ${statusMenuHtml('', status, key)}
        </div>`;
    return card;
}

async function openMissingDetail(key) {
    const entry = state.snapshotEntries.get(key);
    if (!entry) return;
    state.activeMissingKey = key;
    state.activeDetailId = null;
    state.currentDetailIndex = -1;
    currentDetail = null;
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) previousFocus = document.activeElement;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    document.getElementById('close-modal').focus();
    document.getElementById('detail-content').innerHTML = '<div class="detail-loading"><span class="spinner" aria-label="详情加载中"></span></div>';
    updateDetailNav();
    let detail = entry;
    try {
        const fresh = await api(`/api/watchlist/${entry.type}/${entry.tmdb_id}`);
        detail = { ...entry, ...fresh };
        state.snapshotEntries.set(key, detail);
    } catch (_) { /* 读取失败时用列表中的本地数据展示 */ }
    if (state.activeMissingKey !== key) return;
    renderSnapshotDetail(detail);
}

function renderSnapshotDetail(entry) {
    const key = missingEntryKey(entry);
    state.activeMissingKey = key;
    currentDetail = entry;
    const title = entry.title || '资料待补全';
    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" style="background: linear-gradient(135deg, var(--surface-raised), var(--surface) 70%)"></div>
            <div class="modal-hero-content">
                <div class="modal-hero-title"><h2 id="modal-title">${escapeHtml(title)}</h2>${entry.original_title ? `<p>${escapeHtml(entry.original_title)}</p>` : ''}</div>
            </div>
        </div>
        <div class="modal-body snapshot-body">
            <div class="snapshot-notice" role="status">
                <strong>资料待补全</strong>
                <p>目录中暂无这部作品（TMDB ID ${Number(entry.tmdb_id)}），你的个人记录仍然完整保存。补全成功后会自动与目录关联。</p>
            </div>
            <div class="modal-actions">
                <div class="status-picker" data-entry-key="${key}">
                    ${[['watchlist', '想看'], ['watching', '在看'], ['watched', '已看']].map(([value, label]) => `<button type="button" data-set-status="${value}" data-entry-key="${key}" class="${entry.watch_status === value ? 'active' : ''}">${label}</button>`).join('')}
                </div>
                <button type="button" class="btn-copy-link" data-action="snapshot-refresh" data-entry-key="${key}">重试补全</button>
                <button type="button" class="btn-copy-link snapshot-remove" data-action="snapshot-remove" data-entry-key="${key}">移出片单</button>
            </div>
            <div class="modal-details">
                <div class="personal-section" id="personal-section">${personalSectionInnerHtml({ ...entry, id: null }, entry.watch_status, { entryKey: key })}</div>
                <div class="modal-section-title">保存的资料</div>
                <div class="modal-links">
                    ${entry.imdb_id ? `<a class="modal-link" href="https://www.imdb.com/title/${encodeURIComponent(entry.imdb_id)}/" target="_blank" rel="noopener noreferrer">在 IMDb 查看 ${externalIcon()}</a>` : ''}
                    <a class="modal-link" href="https://www.themoviedb.org/${entry.type}/${encodeURIComponent(entry.tmdb_id)}" target="_blank" rel="noopener noreferrer">在 TMDB 查看 ${externalIcon()}</a>
                </div>
            </div>
        </div>`;
}

async function refreshMissingEntry(key) {
    const entry = state.snapshotEntries.get(key);
    if (!entry) return;
    const [type, tmdbId] = key.split(':');
    showToast('正在重新核验资料…');
    try {
        const detail = await api(`/api/watchlist/${type}/${tmdbId}/refresh`, { method: 'POST', timeout: 30000 });
        state.snapshotEntries.set(key, { ...entry, ...detail });
        if (detail.catalog_available) {
            showToast('资料已补全并关联到目录');
            clearAllCaches();
            await Promise.allSettled([loadStats()]);
            reloadListSilently();
            closeModal();
            if (detail.id) showDetail(detail.id);
        } else {
            renderSnapshotDetail({ ...entry, ...detail });
            showToast('本次未补全（可能尚未开分或无中文资料），稍后会自动重试', 'warn');
        }
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

async function removeMissingEntry(key, sourceButton) {
    const entry = state.snapshotEntries.get(key);
    if (!entry) return;
    if (!window.confirm('把这部作品移出片单？保存的个人记录会一并删除。')) return;
    const [type, tmdbId] = key.split(':');
    try {
        await api(`/api/watchlist/${type}/${tmdbId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ watch_status: '' }),
        });
        state.snapshotEntries.delete(key);
        applyStatusCounts(entry.watch_status, '');
        invalidatePageCache();
        invalidatePersistentDataCache();
        showToast('已从片单移除');
        loadStats().catch(() => {});
        closeModal({ force: true });
        reloadListSilently();
    } catch (error) {
        showToast(userMessage(error), 'error');
    }
}

function finalizeCloseModal() {
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    document.body.style.overflow = '';
    currentDetail = null;
    state.currentDetailIndex = -1;
    state.activeMissingKey = null;
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
    if (!options.fromHistory && !options.force && state.detailId != null && history.state?.detailView) {
        history.back();
        return;
    }
    if (!options.fromHistory && !options.force && state.detailId != null) {
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

/* F8：应用内“数据状态”面板，替代 /ready 原始 JSON */
function setupDataStatus() {
    document.getElementById('data-status-btn')?.addEventListener('click', () => openDataStatus());
    document.getElementById('close-data-status')?.addEventListener('click', () => toggleDataStatus(false));
    document.getElementById('data-status-overlay')?.addEventListener('click', event => {
        if (event.target === event.currentTarget) toggleDataStatus(false);
    });
}

function toggleDataStatus(show) {
    const overlay = document.getElementById('data-status-overlay');
    if (!overlay) return;
    overlay.classList.toggle('hidden', !show);
    if (show) {
        renderDataStatusGrid();
        previousFocus = document.activeElement;
        document.getElementById('close-data-status')?.focus();
    } else {
        previousFocus?.focus?.();
    }
}

function openDataStatus() {
    toggleDataStatus(true);
    Promise.allSettled([loadStats(), loadSyncStatus()]).then(() => {
        if (!document.getElementById('data-status-overlay')?.classList.contains('hidden')) {
            renderDataStatusGrid();
        }
    });
}

function renderDataStatusGrid() {
    const grid = document.getElementById('data-status-grid');
    if (!grid) return;
    const stats = statsData || {};
    const sync = syncStatusData?.latest_finished_sync || {};
    const ds = stats.ratings_dataset || {};
    const channels = stats.channels_verified || {};
    const rows = [
        ['应用版本', stats.app_version ? `v${stats.app_version} · build ${stats.build_id || '—'}` : '未知'],
        ['数据 schema', stats.schema_version != null ? `v${stats.schema_version}` : '未知'],
        ['收录作品', `${Number(stats.total || 0).toLocaleString()} 部`],
        ['最近同步', sync.finished_at
            ? `${sync.status === 'partial' ? '部分成功' : sync.status === 'failed' ? '失败' : '成功'} · ${formatRelativeDate(new Date(sync.finished_at))}${sync.request_failed ? ` · 失败请求 ${Number(sync.request_failed).toLocaleString()}` : ''}`
            : '暂无记录'],
        ['下次同步', syncStatusData?.next_run_time
            ? new Date(syncStatusData.next_run_time).toLocaleString('zh-CN')
            : '未启用'],
        ['评分库', ds.age_hours != null
            ? `${Math.round(ds.age_hours)} 小时前${ds.stale ? ' · 可能漏掉新开分作品' : ''}`
            : '缺失'],
        ['片单待补全', `${Number(stats.library_pending || 0).toLocaleString()} 部`],
        ['渠道核验', channels.total
            ? `${Number(channels.done || 0).toLocaleString()} / ${Number(channels.total).toLocaleString()}`
            : '—'],
        ['待处理队列', `${Number(stats.pending || 0).toLocaleString()} 条`],
    ];
    grid.innerHTML = rows.map(([label, value]) => (
        `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`
    )).join('');
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
        swipeState = {
            startX: t.clientX, startY: t.clientY, startTime: Date.now(),
            dy: 0, dx: 0, dragging: false, horizontal: false,
        };
    }, { passive: true });
    panel.addEventListener('touchmove', (e) => {
        if (!swipeState) return;
        const t = e.touches[0];
        const dx = t.clientX - swipeState.startX;
        const dy = t.clientY - swipeState.startY;
        swipeState.dx = dx;
        // 水平滑动意图优先：用于详情上一部/下一部，不参与下滑关闭
        if (!swipeState.horizontal && Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.2) {
            swipeState.horizontal = true;
        }
        if (swipeState.horizontal) return;
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
        const { dy, dx, dragging, horizontal, startTime } = swipeState;
        const elapsed = Date.now() - startTime;
        const velocity = dy / Math.max(elapsed, 1);
        panel.style.transition = '';
        panel.style.transform = '';
        swipeState = null;
        if (horizontal) {
            if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                navigateDetail(dx < 0 ? 1 : -1);
            }
            return;
        }
        if (dragging && (dy > SWIPE_THRESHOLD || velocity > SWIPE_VELOCITY)) {
            closeModal();
        }
    }, { passive: true });
}

function hasActiveFilters() {
    return Boolean(state.type || state.search || state.region || state.rating
        || state.genres.length || state.years || state.maxRuntime
        || state.watchStatus || state.excludeWatched || state.releasedDays);
}

/* R06：搜索是全站作品搜索；输入过程中不滚动页面（N02） */
function commitSearch(value) {
    const trimmed = String(value || '').trim();
    if (trimmed === state.search) return;
    state.search = trimmed;
    resetAndLoad({ scroll: false });
}

/* N02：只有目标已经滚到顶栏下面时才滚动，且留出顶栏高度 */
function scrollWorkspaceIntoView(targetSelector = '.workspace') {
    const target = document.querySelector(targetSelector) || document.querySelector('.workspace');
    if (!target) return;
    const headerH = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--header-h')) || 64;
    if (target.getBoundingClientRect().top >= headerH) return;
    target.scrollIntoView({
        behavior: reduceMotionQuery.matches ? 'auto' : 'smooth',
        block: 'start',
    });
}

function resetAndLoad(options = {}) {
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
    // 筛选变更后回到内容区顶部；搜索输入用 { scroll:false } 保持视口稳定
    if (options.scroll !== false) scrollWorkspaceIntoView(options.target);
    loadTitles();
}

function resetSectionFilters() {
    state.type = '';
    state.search = '';
    state.region = '';
    state.rating = 0;
    state.genres = [];
    state.years = '';
    state.maxRuntime = 0;
    state.excludeWatched = false;
    state.releasedDays = 0;
    state.sort_by = SECTION_DEFAULTS[state.section]?.sort_by || 'rating';
    state.order = 'desc';
    resetAndLoad();
}

function clearAllFilters() {
    resetSectionFilters();
}

function yearsLabel(value = state.years) {
    if (value === '2020-2029') return '2020 年代';
    if (value === '2010-2019') return '2010 年代';
    if (value === '-2009') return '2009 及更早';
    return '';
}

/* WP-5 题材 facet：多选并集，点按立即生效 */
function toggleGenre(name) {
    const index = state.genres.indexOf(name);
    if (index >= 0) state.genres.splice(index, 1);
    else state.genres.push(name);
    resetAndLoad({ scroll: false });
}

/* F7 搜索直达：关键词等于题材/地区名时，给出可点击的筛选建议 */
function searchSuggestion() {
    const query = (state.search || '').trim();
    if (!query) return null;
    const genre = (statsData?.genres || []).find(item => item.name === query && Number(item.count) > 0);
    if (genre) {
        return {
            label: `按题材筛选：${genreZh(genre.name)}（${Number(genre.count).toLocaleString()}）`,
            apply: () => {
                state.search = '';
                if (!state.genres.includes(genre.name)) state.genres.push(genre.name);
            },
        };
    }
    for (const code of Object.keys(regionShortNames)) {
        if (regionShortNames[code] === query || regionNames[code] === query) {
            return {
                label: `按地区筛选：${regionShortNames[code]}`,
                apply: () => { state.search = ''; state.region = code; },
            };
        }
    }
    return null;
}

function renderActiveFilters() {
    const container = document.getElementById('active-filters');
    const chips = [];
    if (state.search) chips.push(['关键词', state.search, '', () => { state.search = ''; resetAndLoad(); }]);
    if (state.type) chips.push(['类型', state.type === 'movie' ? '电影' : '剧集', '', () => { state.type = ''; resetAndLoad(); }]);
    if (state.region) chips.push(['地区', displayRegionName(state.region), '', () => { state.region = ''; resetAndLoad(); }]);
    if (state.rating) chips.push(['评分', `${state.rating} 分以上`, '', () => { state.rating = 0; resetAndLoad(); }]);
    state.genres.forEach(name => {
        chips.push(['题材', genreZh(name), '', () => {
            state.genres = state.genres.filter(item => item !== name);
            resetAndLoad();
        }]);
    });
    if (state.years) chips.push(['年代', yearsLabel(), '', () => { state.years = ''; resetAndLoad(); }]);
    if (state.maxRuntime) chips.push(['时长', `${state.maxRuntime} 分钟内（含时长未知）`, '', () => { state.maxRuntime = 0; resetAndLoad(); }]);
    if (state.excludeWatched && !state.watchStatus) chips.push(['片单', '隐藏已看', '', () => { state.excludeWatched = false; resetAndLoad(); }]);
    if (state.releasedDays > 0) chips.push(['上映', `近 ${state.releasedDays} 天`, '', () => { state.releasedDays = 0; resetAndLoad(); }]);
    const defaultSort = SECTION_DEFAULTS[state.section]?.sort_by || 'rating';
    if (state.sort_by !== defaultSort) {
        chips.push(['排序', SORT_LABELS[state.sort_by] || state.sort_by, '', () => {
            state.sort_by = defaultSort;
            state.order = 'desc';
            resetAndLoad();
        }]);
    }
    const suggestion = searchSuggestion();
    if (!chips.length && !suggestion) {
        container.classList.add('hidden');
        container.innerHTML = '';
        updateFilterSummary();
        return;
    }
    container.classList.remove('hidden');
    container.innerHTML = '';
    if (suggestion) {
        const hint = document.createElement('button');
        hint.type = 'button';
        hint.className = 'active-filter-chip is-suggestion';
        hint.innerHTML = `<span>${escapeHtml(suggestion.label)}</span><span class="chip-x">→</span>`;
        hint.addEventListener('click', () => {
            suggestion.apply();
            resetAndLoad({ scroll: false });
        });
        container.appendChild(hint);
    }
    if (chips.length) {
        const label = document.createElement('span');
        label.className = 'active-filters-label';
        label.textContent = '已筛选';
        container.appendChild(label);
        chips.forEach(([key, labelText, color, clear]) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'active-filter-chip';
            button.setAttribute('aria-label', `移除${key}筛选：${labelText}`);
            if (color) button.style.setProperty('--chip-color', color);
            button.innerHTML = `${color ? '<span class="chip-dot"></span>' : ''}<span><span class="chip-label-key">${key}</span>${escapeHtml(labelText)}</span><span class="chip-x">×</span>`;
            button.addEventListener('click', clear);
            container.appendChild(button);
        });
    }
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
    state.genres.forEach(name => parts.push(genreZh(name)));
    if (state.years) parts.push(yearsLabel());
    if (state.maxRuntime > 0) parts.push(`≤${state.maxRuntime}分`);
    if (state.excludeWatched && !state.watchStatus) parts.push('隐藏已看');
    if (state.releasedDays > 0) parts.push(`近${state.releasedDays}天`);
    const defaultSort = SECTION_DEFAULTS[state.section]?.sort_by || 'rating';
    if (state.sort_by !== defaultSort) {
        const sortLabel = SORT_LABELS[state.sort_by] || state.sort_by;
        parts.push(state.order === 'asc' ? `${sortLabel}↑` : `${sortLabel}↓`);
    }
    el.textContent = parts.join(' · ');
    // WP-3：筛选按钮上的计数
    const count = document.getElementById('filter-more-count');
    if (count) {
        const total = state.genres.length + (state.years ? 1 : 0) + (state.region ? 1 : 0)
            + (state.rating ? 1 : 0) + (state.maxRuntime ? 1 : 0);
        count.textContent = total ? `(${total})` : '';
        count.classList.toggle('hidden', !total);
    }
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
    // 选择控件 change 后保持打开，支持连续设置类型、题材、时长、产地与评分。
    const desktopMq = window.matchMedia('(min-width: 901px)');
    desktopMq.addEventListener('change', (e) => { if (e.matches) close(); });
    window.__closeSidebar = (restoreFocus = true) => close(restoreFocus);
    // F09：移动端将抽屉提升为 body 级顶层容器，避免被 main 的层叠上下文压住。
    const originalParent = panel.parentElement;
    const originalNext = panel.nextSibling;
    const popover = document.getElementById('filter-popover');
    const placePanel = () => {
        if (window.matchMedia('(max-width: 900px)').matches) {
            if (panel.parentElement !== document.body) {
                document.body.appendChild(panel);
                panel.classList.add('drawer-mode');
            }
            // 抽屉里没有“筛选”按钮，弹出面板常显
            popover?.classList.remove('hidden');
        } else {
            panel.classList.remove('drawer-mode');
            if (panel.parentElement !== originalParent) {
                originalParent.insertBefore(panel, originalNext);
            }
            popover?.classList.add('hidden');
            document.getElementById('filter-more-btn')?.setAttribute('aria-expanded', 'false');
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

/* 5.1：惊喜范围收进惊喜入口自身的附属菜单，主页面只留一个动作入口 */
function toggleSurpriseScopeMenu() {
    const menu = document.getElementById('surprise-scope-menu');
    const button = document.getElementById('surprise-scope-btn');
    if (!menu || !button) return;
    const opening = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !opening);
    button.setAttribute('aria-expanded', String(opening));
    if (opening) menu.querySelector('[data-surprise-scope].active, [data-surprise-scope]')?.focus();
}

/* WP-5 F4：“近期新片”时间窗口收进 chip 内的小菜单 */
function toggleFreshMenu() {
    const menu = document.getElementById('fresh-menu');
    const button = document.getElementById('mode-fresh-more');
    if (!menu || !button) return;
    const opening = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !opening);
    button.setAttribute('aria-expanded', String(opening));
    if (opening) menu.querySelector('[data-fresh-days]')?.focus();
}

function closeFreshMenu(restoreFocus = false) {
    const menu = document.getElementById('fresh-menu');
    const button = document.getElementById('mode-fresh-more');
    if (!menu || menu.classList.contains('hidden')) return;
    menu.classList.add('hidden');
    button?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button?.focus();
}

/* WP-3：桌面四个下拉收进“筛选”弹出面板（手机是抽屉，由 placePanel 搬家） */
function toggleFilterPopover() {
    const popover = document.getElementById('filter-popover');
    const button = document.getElementById('filter-more-btn');
    if (!popover || !button) return;
    const opening = popover.classList.contains('hidden');
    popover.classList.toggle('hidden', !opening);
    button.setAttribute('aria-expanded', String(opening));
    if (opening) popover.querySelector('button, select')?.focus();
}

function closeFilterPopover(restoreFocus = false) {
    // 抽屉模式下四个下拉常显，没有可关闭的弹出层
    if (window.matchMedia?.('(max-width: 900px)').matches) return;
    const popover = document.getElementById('filter-popover');
    const button = document.getElementById('filter-more-btn');
    if (!popover || popover.classList.contains('hidden')) return;
    popover.classList.add('hidden');
    button?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button?.focus();
}

function setupFilterPopover() {
    document.getElementById('filter-more-btn')?.addEventListener('click', event => {
        event.stopPropagation();
        toggleFilterPopover();
    });
}

function closeSurpriseScopeMenu(restoreFocus = false) {
    const menu = document.getElementById('surprise-scope-menu');
    const button = document.getElementById('surprise-scope-btn');
    if (!menu || menu.classList.contains('hidden')) return;
    menu.classList.add('hidden');
    button?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button?.focus();
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

        if (event.target.closest('#mode-fresh-more')) { toggleFreshMenu(); return; }

        const freshOption = event.target.closest('#fresh-menu [data-fresh-days]');
        if (freshOption) {
            state.releasedDays = Number(freshOption.dataset.freshDays) || 0;
            state.rating = 0;
            state.sort_by = 'release_date';
            state.order = 'desc';
            closeFreshMenu();
            resetAndLoad({ scroll: false });
            return;
        }

        const genreChip = event.target.closest('#genre-filters [data-genre]');
        if (genreChip) { toggleGenre(genreChip.dataset.genre); return; }

        const yearChip = event.target.closest('#year-filters [data-years]');
        if (yearChip) {
            state.years = yearChip.dataset.years || '';
            resetAndLoad({ scroll: false });
            return;
        }

        const batchToggle = event.target.closest('#batch-toggle');
        if (batchToggle) { setBatchMode(!state.batchMode); return; }

        const type = event.target.closest('#type-filters [data-type]');
        if (type) { state.type = type.dataset.type; resetAndLoad(); return; }

        const unwatchedToggle = event.target.closest('#unwatched-toggle');
        if (unwatchedToggle) { state.excludeWatched = !state.excludeWatched; resetAndLoad(); return; }

        const statusTab = event.target.closest('#status-filters [data-status]');
        if (statusTab) {
            state.watchStatus = statusTab.dataset.status;
            state.libraryStatus = state.watchStatus;
            captureSectionSnapshot('library');
            resetAndLoad();
            return;
        }

        const cardMain = event.target.closest('.card-main');
        if (cardMain) {
            const card = cardMain.closest('.title-card');
            // R01：目录缺失的个人条目打开快照详情
            if (card.dataset.titleKey) { openMissingDetail(card.dataset.titleKey); return; }
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
            const entryKey = statusOption.dataset.entryKey || statusOption.dataset.titleKey;
            if (entryKey) {
                const [entryType, entryTmdb] = entryKey.split(':');
                setTitleStatus(null, statusOption.dataset.setStatus, statusOption, {
                    type: entryType, tmdbId: Number(entryTmdb),
                });
                return;
            }
            const holder = statusOption.closest('[data-title-id], .title-card');
            setTitleStatus(holder.dataset.titleId, statusOption.dataset.setStatus, statusOption);
            return;
        }

        const scopeOption = event.target.closest('[data-surprise-scope]');
        if (scopeOption) {
            state.surpriseScope = scopeOption.dataset.surpriseScope;
            try { localStorage.setItem('surprise_scope_v1', state.surpriseScope); } catch (_) { /* 忽略 */ }
            closeSurpriseScopeMenu();
            syncControlsFromState();
            showToast(`惊喜发现范围：${surpriseScopeLabel()}`);
            return;
        }
        if (event.target.closest('#surprise-scope-btn')) { toggleSurpriseScopeMenu(); return; }

        const recentChip = event.target.closest('[data-recent-id]');
        if (recentChip) { showDetail(recentChip.dataset.recentId); return; }

        const action = event.target.closest('[data-action]');
        if (action?.dataset.action === 'clear-filters') clearAllFilters();
        if (action?.dataset.action === 'reset-filters') { resetSectionFilters(); return; }
        if (action?.dataset.action === 'close-drawer') { window.__closeSidebar?.(); return; }
        if (action?.dataset.action === 'apply-filters') {
            window.__closeSidebar?.(false);
            // N02：滚到主导航（而不是内容网格），保证 tab 与子 tab 不被顶栏遮住
            requestAnimationFrame(() => scrollWorkspaceIntoView('.primary-nav'));
            return;
        }
        if (action?.dataset.action === 'toggle-status-menu') {
            const menu = action.parentElement?.querySelector('.status-menu');
            if (menu) {
                const opening = menu.classList.contains('hidden');
                menu.classList.toggle('hidden', !opening);
                action.setAttribute('aria-expanded', String(opening));
                if (opening) menu.querySelector('button')?.focus();
            }
            return;
        }
        if (action?.dataset.action === 'detail-prev') { navigateDetail(-1); return; }
        if (action?.dataset.action === 'detail-next') { navigateDetail(1); return; }
        if (action?.dataset.action === 'goto-discover') { setSection('discover'); return; }
        if (action?.dataset.action === 'surprise-again') { surprisePick(); return; }
        if (action?.dataset.action === 'surprise-tonight') {
            if (state.surpriseLastId != null) setTitleStatus(state.surpriseLastId, 'watching');
            return;
        }
        if (action?.dataset.action === 'surprise-close') { hideSurpriseBar(); return; }
        if (action?.dataset.action === 'batch-select-all') {
            const cap = 200;
            const candidates = state.loadedTitleIds.map(Number);
            state.selectedIds = new Set(candidates.slice(0, cap));
            if (candidates.length > cap) {
                showToast(`批量操作单次上限 ${cap} 部，已选中前 ${cap} 部`, 'warn');
            }
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
        if (action?.dataset.action === 'snapshot-refresh') { refreshMissingEntry(action.dataset.entryKey); return; }
        if (action?.dataset.action === 'snapshot-remove') { removeMissingEntry(action.dataset.entryKey, action); return; }
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
        if (!event.target.closest('.status-menu-wrap') && !event.target.closest('.split-btn')) closeStatusMenus();
        if (!event.target.closest('.surprise-split')) closeSurpriseScopeMenu();
        if (!event.target.closest('.filter-popover-wrap')) closeFilterPopover();
        if (!event.target.closest('.mode-split')) closeFreshMenu();
    });

    // 6.1：详情内切换观看地区 → 持久化并重渲染渠道列表；6.7：个人记录字段即时保存
    document.addEventListener('change', event => {
        if (event.target.id === 'viewing-region-select') {
            state.viewingRegion = /^[A-Z]{2}$/.test(event.target.value) ? event.target.value : '';
            try { localStorage.setItem('viewing_region_v1', state.viewingRegion); } catch (_) { /* 忽略存储失败 */ }
            // 4.4：只刷新渠道区，不整屏重渲染（不重取推荐、不丢焦点、不重复记最近浏览）
            if (currentDetail && state.activeMissingKey) renderSnapshotDetail(currentDetail);
            else if (currentDetail) {
                const container = document.getElementById('channels-section');
                if (container) container.innerHTML = channelsSectionHtml(currentDetail);
                else renderDetail(currentDetail);
            }
            return;
        }
        const field = event.target.closest('[data-personal]');
        if (field) {
            const fields = field.closest('.personal-fields');
            const entryKey = fields?.dataset.entryKey;
            if (entryKey) {
                const [entryType, entryTmdb] = entryKey.split(':');
                savePersonalField(null, field.dataset.personal, field.value, field, {
                    type: entryType, tmdbId: Number(entryTmdb),
                });
                return;
            }
            const titleId = fields?.dataset.titleId || state.activeDetailId;
            if (titleId) savePersonalField(Number(titleId), field.dataset.personal, field.value, field);
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
    document.getElementById('imdb-import-btn')?.addEventListener('click', () => importByImdb());
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
        searchTimer = setTimeout(() => commitSearch(searchInput.value), 320);
    });
    document.getElementById('clear-search').addEventListener('click', () => {
        clearTimeout(searchTimer);
        searchInput.value = '';
        commitSearch('');
        searchInput.focus();
    });
    document.getElementById('region-filter').addEventListener('change', event => { state.region = event.target.value; resetAndLoad(); });
    document.getElementById('rating-filter').addEventListener('change', event => { state.rating = Number(event.target.value); resetAndLoad(); });
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
        const dataStatusOpen = !document.getElementById('data-status-overlay')?.classList.contains('hidden');
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
            if (dataStatusOpen) { toggleDataStatus(false); return; }
            if (!document.getElementById('admin-menu')?.classList.contains('hidden')) { closeAdminMenu(true); return; }
            if (document.body.classList.contains('sidebar-open')) { window.__closeSidebar?.(); return; }
            if (document.body.classList.contains('wall-open')) { closeWall(); return; }
            if (!document.getElementById('fresh-menu')?.classList.contains('hidden')) { closeFreshMenu(true); return; }
            if (!document.getElementById('filter-popover')?.classList.contains('hidden')) { closeFilterPopover(true); return; }
            if (!document.getElementById('surprise-scope-menu')?.classList.contains('hidden')) { closeSurpriseScopeMenu(true); return; }
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
            else if (dataStatusOpen) trapFocusWithin(document.getElementById('data-status-overlay'), event);
            else if (document.body.classList.contains('sidebar-open')) trapFocusWithin(document.querySelector('body > .filter-panel.drawer-mode'), event);
            else if (modalOpen) trapModalFocus(event);
        }
    });

    window.addEventListener('online', () => {
        document.getElementById('offline-banner').classList.add('hidden');
        showToast('网络连接已恢复');
        clearAllCaches(); // 离线期间数据可能过期，清空缓存让下次请求拉取最新
        invalidatePersistentDataCache();
        // R07：恢复联网后主动重取当前屏幕数据，避免界面停留在缓存副本
        if (state.usedOfflineCache) {
            state.usedOfflineCache = false;
            reloadListSilently();
            if (statsData == null) loadStats().catch(() => {});
        }
    });
    window.addEventListener('offline', () => {
        const banner = document.getElementById('offline-banner');
        banner.textContent = '当前处于离线状态，已加载内容仍可浏览';
        banner.classList.remove('hidden');
    });
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
    // 普通 back_forward（非 bfcache）：页面重建，标记待恢复位置与已加载页数
    const navEntry = performance.getEntriesByType?.('navigation')[0];
    if (navEntry?.type === 'back_forward') {
        state.restoreScroll = history.state?.scrollY ?? null;
        state.restorePages = Math.max(1, Number(history.state?.loadedPages || 1));
    }
    setupCardHoverPrefetch();

    // 6.10 PWA：注册 Service Worker（离线外壳），注册失败不影响正常使用
    if ('serviceWorker' in navigator) {
        // N01：首次安装时 clients.claim() 也会触发 controllerchange，不能误报“应用已更新”
        const hadController = Boolean(navigator.serviceWorker.controller);
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' })
                .catch(() => { /* 忽略注册失败 */ });
        });
        let refreshedController = false;
        navigator.serviceWorker.addEventListener?.('controllerchange', () => {
            if (!hadController || refreshedController) return;
            refreshedController = true;
            // 新外壳接管后提示刷新，避免用户停留在旧版本资源组合
            showToast('已有新版本，点击刷新', 'success', {
                action: '刷新',
                onAction: () => location.reload(),
            });
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

function showToast(message, type = 'success', options = {}) {
    const region = document.getElementById('toast-region');
    const backToTop = document.getElementById('back-to-top');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    toast.appendChild(text);
    // N01：支持一个可操作按钮（如“已有新版本，点击刷新”）
    if (options.action && typeof options.onAction === 'function') {
        toast.classList.add('has-action');
        const actionButton = document.createElement('button');
        actionButton.type = 'button';
        actionButton.className = 'toast-action';
        actionButton.textContent = options.action;
        actionButton.addEventListener('click', () => {
            toast.remove();
            options.onAction();
        });
        toast.appendChild(actionButton);
    }
    region.appendChild(toast);
    if (backToTop?.classList.contains('visible')) backToTop.classList.add('shifted');
    setTimeout(() => {
        toast.remove();
        if (!region.children.length) backToTop?.classList.remove('shifted');
    }, options.duration || 3800);
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
