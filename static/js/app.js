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
};

const providerColors = {
    netflix: '#e06060', disney: '#7196dc', max: '#7483d9',
    amazon: '#55a9cf', apple: '#d8d8d2', hulu: '#67b98a',
};
const providerNames = {
    netflix: 'Netflix', disney: 'Disney+', max: 'Max',
    amazon: 'Prime Video', apple: 'Apple TV+', hulu: 'Hulu',
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
const DETAIL_POSTER_SIZES = '(max-width: 680px) 1px, (max-width: 900px) 150px, 180px';

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
    window.setInterval(() => {
        const currentDpr = Math.max(1, Math.round((window.devicePixelRatio || 1) * 100) / 100);
        if (currentDpr !== observedDisplayDpr) scheduleDisplayAdaptation(true);
    }, 750);
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
    setupInfiniteScroll();
    setupBackToTop();
    syncControlsFromState();
    renderSkeletons();

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
    if (state.watchStatus) params.set('status', state.watchStatus);
    const query = params.toString();
    history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
}

function syncControlsFromState() {
    document.getElementById('search-input').value = state.search;
    document.getElementById('clear-search').classList.toggle('hidden', !state.search);
    document.getElementById('region-filter').value = state.region;
    document.getElementById('rating-filter').value = String(state.rating);
    document.getElementById('sort-filter').value = state.sort_by;
    document.querySelectorAll('#type-filters [data-type]').forEach(button => {
        button.classList.toggle('active', button.dataset.type === state.type);
    });
    document.querySelectorAll('#status-filters [data-status]').forEach(button => {
        button.classList.toggle('active', button.dataset.status === state.watchStatus);
    });
    document.querySelectorAll('#provider-filters [data-provider]').forEach(button => {
        button.classList.toggle('active', button.dataset.provider === state.provider);
    });
}

async function loadStats() {
    try {
        statsData = await api('/api/stats');
        const byStatus = statsData.by_status || {};
        const listTotal = Object.values(byStatus).reduce((sum, count) => sum + Number(count || 0), 0);
        document.getElementById('overview-stats').innerHTML = `
            <div><dt>收录</dt><dd>${Number(statsData.total || 0).toLocaleString()}</dd></div>
            <div><dt>平均分</dt><dd>${Number(statsData.avg_rating || 0).toFixed(1)}</dd></div>
            <div><dt>我的片单</dt><dd>${listTotal.toLocaleString()}</dd></div>`;
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
        container.innerHTML = providerButtonHtml('', '全部平台', data.total || 0, '');
        providers.forEach(key => {
            container.insertAdjacentHTML('beforeend', providerButtonHtml(
                key, providerNames[key] || key, providerCounts[key] || 0, providerColors[key],
            ));
        });
        syncControlsFromState();
    } catch (error) {
        container.innerHTML = '<span class="filter-label">平台列表加载失败</span>';
    }
}

function providerButtonHtml(key, name, count, color) {
    return `<button class="filter-btn" type="button" data-provider="${escapeHtml(key)}">
        ${color ? `<span class="provider-dot" style="background:${color}"></span>` : ''}
        ${escapeHtml(name)} <span class="count-badge">${Number(count).toLocaleString()}</span>
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
            await Promise.allSettled([loadStats(), loadProviders()]);
            resetAndLoad();
        }
    }, 4000);
}

async function loadTitles() {
    if (state.loading || !state.hasMore) return;
    const version = state.requestVersion;
    state.loading = true;
    const loader = document.getElementById('scroll-loader');
    const end = document.getElementById('scroll-end');
    const grid = document.getElementById('titles-grid');
    grid.setAttribute('aria-busy', 'true');
    if (state.page > 1) loader.classList.remove('hidden');
    end.classList.add('hidden');

    try {
        const params = new URLSearchParams({
            page: String(state.page), limit: String(state.limit),
            sort_by: state.sort_by, order: state.order,
        });
        if (state.provider) params.set('provider', state.provider);
        if (state.type) params.set('type', state.type);
        if (state.search) params.set('search', state.search);
        if (state.region) params.set('region', state.region);
        if (state.rating > 0) params.set('min_rating', String(state.rating));
        if (state.watchStatus) params.set('watch_status', state.watchStatus);

        const data = await api(`/api/titles?${params}`);
        if (version !== state.requestVersion) return;
        renderTitles(data.titles || [], state.page === 1);
        const loaded = Math.min((state.page - 1) * state.limit + data.titles.length, data.total);
        const noun = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部剧集' : '部作品';
        document.getElementById('stats-info').innerHTML = `找到 <strong>${Number(data.total).toLocaleString()}</strong> ${noun}${data.total ? ` · 已显示 ${loaded}` : ''}`;
        state.hasMore = Boolean(data.has_next);
        document.getElementById('scroll-sentinel').classList.toggle('hidden', !state.hasMore);
        if (!state.hasMore && data.total > 0) end.classList.remove('hidden');
        if (data.titles.length) state.page += 1;
        if (state.page === 1 && data.total === 0 && !hasActiveFilters()) checkBootstrapSync();
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
        }
    }
}

function renderSkeletons() {
    const grid = document.getElementById('titles-grid');
    grid.setAttribute('aria-busy', 'true');
    grid.innerHTML = Array.from({ length: SKELETON_COUNT }, () => `
        <div class="skeleton-card" aria-hidden="true">
            <div class="skeleton-poster"></div>
            <div class="skeleton-info"><div class="skeleton-line medium"></div><div class="skeleton-line short"></div><div class="skeleton-line tiny"></div></div>
        </div>`).join('');
}

function renderTitles(titles, clear) {
    const grid = document.getElementById('titles-grid');
    if (clear) grid.innerHTML = '';
    if (clear && !titles.length) {
        renderEmptyState();
        return;
    }
    const fragment = document.createDocumentFragment();
    titles.forEach(title => fragment.appendChild(createTitleCard(title)));
    grid.appendChild(fragment);
}

function createTitleCard(title) {
    const card = document.createElement('article');
    card.className = 'title-card';
    card.dataset.titleId = title.id;
    card.dataset.watchStatus = title.watch_status || '';
    const rating = Number(title.imdb_rating) || 0;
    const poster = sanitizeUrl(title.poster_url) || posterFallback;
    const providers = (title.providers || []).map(provider => `
        <span class="card-provider"><span class="p-dot" style="background:${providerColors[provider] || '#7f7d75'}"></span>${escapeHtml(providerNames[provider] || provider)}</span>`).join('');
    const status = title.watch_status || '';
    const region = primaryRegionLabel(title.origin_countries, true);
    card.innerHTML = `
        <button class="card-main" type="button" aria-label="查看 ${escapeHtml(title.title)} 详情">
            <div class="poster-wrap">
                <img ${responsivePosterAttributes(poster, CARD_POSTER_SIZES)} alt="${escapeHtml(title.title)} 海报" loading="lazy" decoding="async" onerror="window.handlePosterError(this)">
                ${status ? `<span class="status-badge" data-status="${status}">${watchStatusNames[status]}</span>` : ''}
                <span class="poster-rating">${rating ? rating.toFixed(1) : '—'}<small>IMDb</small></span>
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
    return card;
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
    content.innerHTML = '<div class="detail-loading"><span class="spinner" aria-label="详情加载中"></span></div>';
    document.getElementById('close-modal').focus();
    try {
        currentDetail = await api(`/api/titles/${id}`);
        renderDetail(currentDetail);
    } catch (error) {
        content.innerHTML = `<div class="detail-error"><div><p>${escapeHtml(userMessage(error))}</p><button type="button" class="btn-retry" data-action="retry-detail" data-title-id="${id}">重新加载</button></div></div>`;
    }
}

function renderDetail(title) {
    const rating = Number(title.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const poster = sanitizeUrl(title.poster_url) || posterFallback;
    const detailBackdrop = tmdbPosterUrl(poster, 'w780');
    const original = title.original_title && title.original_title !== title.title ? title.original_title : '';
    const providers = (title.providers || []).map(provider => `
        <span class="modal-provider"><span class="p-dot" style="background:${providerColors[provider] || '#7f7d75'}"></span>${escapeHtml(providerNames[provider] || provider)}</span>`).join('');
    const status = title.watch_status || '';
    const imdbLink = title.imdb_id ? `<a class="modal-link" href="https://www.imdb.com/title/${encodeURIComponent(title.imdb_id)}/" target="_blank" rel="noopener noreferrer">在 IMDb 查看 ${externalIcon()}</a>` : '';
    const tmdbType = title.type === 'movie' ? 'movie' : 'tv';
    const region = primaryRegionLabel(title.origin_countries, true);
    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" style="background-image:url('${escapeHtml(detailBackdrop)}')"></div>
            <div class="modal-hero-content">
                <div class="modal-hero-rating"><div class="rating-num">${rating ? rating.toFixed(1) : '—'}</div>${tier ? `<div class="rating-tier" data-tier="${tier}">IMDb · ${ratingTierLabels[tier]}</div>` : ''}</div>
                <div class="modal-hero-title"><h2 id="modal-title">${escapeHtml(title.title)}</h2>${original ? `<p>${escapeHtml(original)}</p>` : ''}</div>
            </div>
        </div>
        <div class="modal-body">
            <div class="modal-poster"><img ${responsivePosterAttributes(poster, DETAIL_POSTER_SIZES)} alt="${escapeHtml(title.title)} 海报" decoding="async" onerror="window.handlePosterError(this)"></div>
            <div class="modal-info">
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
                <div class="modal-section-title">剧情简介</div>
                <p class="modal-overview">${escapeHtml(title.overview || '暂无剧情简介')}</p>
                <div class="modal-links">${imdbLink}<a class="modal-link" href="https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(title.tmdb_id)}" target="_blank" rel="noopener noreferrer">在 TMDB 查看 ${externalIcon()}</a></div>
            </div>
        </div>`;
}

function externalIcon() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M14 5h5v5M19 5l-8 8M17 13v5H6V7h5"/></svg>';
}

async function setTitleStatus(id, watchStatus, sourceButton) {
    const scope = sourceButton?.closest('.status-picker, .status-menu');
    scope?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
        const title = await api(`/api/titles/${id}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ watch_status: watchStatus }),
        });
        updateCardStatus(id, watchStatus);
        if (currentDetail?.id === Number(id)) {
            currentDetail = title;
            document.querySelectorAll('.status-picker [data-set-status]').forEach(button => {
                button.classList.toggle('active', button.dataset.setStatus === watchStatus);
            });
        }
        showToast(watchStatus ? `已加入“${watchStatusNames[watchStatus]}”` : '已从片单移除');
        await loadStats();
        if (state.watchStatus && state.watchStatus !== watchStatus) resetAndLoad();
    } catch (error) {
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
    previousFocus?.focus?.();
}

function hasActiveFilters() {
    return Boolean(state.provider || state.type || state.search || state.region || state.rating || state.watchStatus);
}

function resetAndLoad() {
    state.requestVersion += 1;
    state.page = 1;
    state.loading = false;
    state.hasMore = true;
    updateUrl();
    syncControlsFromState();
    renderActiveFilters();
    document.getElementById('scroll-end').classList.add('hidden');
    document.getElementById('scroll-sentinel').classList.remove('hidden');
    renderSkeletons();
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
        menu.classList.add('hidden');
        const trigger = menu.parentElement.querySelector('.status-menu-trigger');
        trigger?.setAttribute('aria-expanded', 'false');
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
            menu.classList.toggle('hidden', !opening);
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

        const action = event.target.closest('[data-action]');
        if (action?.dataset.action === 'clear-filters') clearAllFilters();
        if (action?.dataset.action === 'retry') resetAndLoad();
        if (action?.dataset.action === 'retry-more') { state.hasMore = true; loadTitles(); }
        if (action?.dataset.action === 'retry-detail') showDetail(action.dataset.titleId);
        if (!event.target.closest('.status-menu-wrap')) closeStatusMenus();
    });

    document.getElementById('sync-button').addEventListener('click', triggerSync);
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('detail-modal').addEventListener('mousedown', event => {
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

    document.addEventListener('keydown', event => {
        if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
            event.preventDefault();
            searchInput.focus();
        }
        if (event.key === 'Escape') {
            if (document.querySelector('.status-menu:not(.hidden)')) closeStatusMenus();
            else closeModal();
        }
        if (event.key === 'Tab' && !document.getElementById('detail-modal').classList.contains('hidden')) trapModalFocus(event);
    });

    window.addEventListener('online', () => {
        document.getElementById('offline-banner').classList.add('hidden');
        showToast('网络连接已恢复');
    });
    window.addEventListener('offline', () => document.getElementById('offline-banner').classList.remove('hidden'));
    if (!navigator.onLine) document.getElementById('offline-banner').classList.remove('hidden');
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
    let ticking = false;
    window.addEventListener('scroll', () => {
        if (ticking) return;
        requestAnimationFrame(() => {
            button.classList.toggle('visible', window.scrollY > 700);
            ticking = false;
        });
        ticking = true;
    }, { passive: true });
    button.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
}

function showToast(message, type = 'success') {
    const region = document.getElementById('toast-region');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    region.appendChild(toast);
    setTimeout(() => toast.remove(), 3800);
}
