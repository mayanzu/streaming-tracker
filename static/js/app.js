const state = {
    page: 1, limit: 50, provider: '', sort_by: 'rating',
    order: 'desc', type: '', search: '', year: '', rating: 0,
    loading: false, hasMore: true
};

const providerColors = {
    netflix: '#E50914', disney: '#113CCF', max: '#002BE7',
    amazon: '#00A8E1', apple: '#F5F5F7', hulu: '#1CE783'
};

const providerNames = {
    netflix: 'Netflix', disney: 'Disney+', max: 'Max',
    amazon: 'Prime Video', apple: 'Apple TV+', hulu: 'Hulu'
};

const hiddenMainFilterProviders = new Set(['hulu']);

const ratingSourceNames = {
    imdb: 'IMDb',
    omdb: 'IMDb'
};

const ratingTierLabels = {
    great: '极佳',
    good: '优秀',
    fair: '良好'
};

const SKELETON_COUNT = 12;

let providerCounts = {};
let bootstrapPollTimer = null;
const posterFallback = `data:image/svg+xml,${encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="500" height="750" viewBox="0 0 500 750">
  <rect width="500" height="750" fill="#18181e"/>
  <rect x="1" y="1" width="498" height="748" rx="16" fill="none" stroke="#2a2a34" stroke-width="2"/>
  <circle cx="250" cy="315" r="42" fill="#2a2a34"/>
  <path d="M192 414h116M214 456h72" stroke="#6e6e7a" stroke-width="16" stroke-linecap="round"/>
  <text x="250" y="535" text-anchor="middle" fill="#6e6e7a" font-family="Arial, sans-serif" font-size="28" font-weight="700">NO POSTER</text>
</svg>
`)}`;
window.posterFallback = posterFallback;

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
}

function sanitizeUrl(url) {
    if (!url) return '';
    try {
        const parsed = new URL(url, window.location.origin);
        const allowed = ['image.tmdb.org', 'api.image.tmdb.org'];
        if (allowed.includes(parsed.hostname)) return url;
        if (url.startsWith('data:image/')) return url;
    } catch (_) { /* invalid URL */ }
    return '';
}

function ratingTier(rating) {
    if (!rating || rating <= 0) return null;
    if (rating >= 8) return 'great';
    if (rating >= 7.5) return 'good';
    return 'fair';
}

document.addEventListener('DOMContentLoaded', async () => {
    setupEvents();
    setupInfiniteScroll();
    setupBackToTop();
    renderSkeletons();
    await loadSyncStatus();
    await loadProviders();
    await loadYears();
    loadTitles();
});

async function loadSyncStatus() {
    try {
        const res = await fetch('/api/sync/status');
        if (!res.ok) return null;
        const status = await res.json();
        renderSyncStatus(status);
        return status;
    } catch (e) {
        return null;
    }
}

function renderSyncStatus(status) {
    const el = document.getElementById('sync-info');
    if (!el) return;

    // SYNC_ENABLED=false 时（如路由器只读部署），同步在本地脚本跑，
    // 前端不应 surface 任何同步状态（包括历史 failed 记录）
    if (status.enabled === false) {
        el.textContent = '';
        el.className = '';
        return;
    }

    const sync = status.sync || {};
    const latestFinished = status.latest_finished_sync || {};
    const last = sync.last_result || latestFinished || {};
    const latestRun = status.latest_run || {};
    const progress = Object.keys(last).length ? last : latestRun;

    if (sync.running) {
        const currentProvider = progress.current_provider;
        const current = currentProvider ? ` · ${providerNames[currentProvider] || currentProvider}` : '';
        const providerStep = progress.provider_total
            ? ` ${progress.current_provider_index || '?'} / ${progress.provider_total}`
            : '';
        const discovered = progress.discovered ? ` · 已发现 ${progress.discovered}` : '';
        el.textContent = sync.current_reason === 'untrusted_rating_rebuild'
            ? `IMDb 重建中${providerStep}${current}${discovered}`
            : `同步中${providerStep}${current}${discovered}`;
        el.className = 'sync-pill active';
        return;
    }

    if (last.reason === 'missing_tmdb_api_key') {
        el.textContent = '缺少 TMDB Key';
        el.className = 'sync-pill danger';
        return;
    }

    if (last.reason === 'sync_failed') {
        el.textContent = '同步失败';
        el.className = 'sync-pill danger';
        return;
    }

    if (latestFinished.status === 'failed') {
        el.textContent = '上次同步失败';
        el.className = 'sync-pill danger';
        return;
    }

    if (latestFinished.finished_at) {
        const finishedAt = new Date(latestFinished.finished_at);
        if (!Number.isNaN(finishedAt.getTime())) {
            if (latestFinished.status === 'partial') {
                el.textContent = `部分同步 ${finishedAt.toLocaleDateString('zh-CN')}`;
                el.className = 'sync-pill warn';
                return;
            }
            el.textContent = `上次同步 ${finishedAt.toLocaleDateString('zh-CN')}`;
            el.className = 'sync-pill';
            return;
        }
    }

    el.textContent = '';
    el.className = '';
}

async function loadProviders() {
    try {
        const res = await fetch('/api/providers');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        providerCounts = {};
        data.providers.forEach(p => { providerCounts[p.provider_name] = p.count; });

        const availableProviders = (data.available || [])
            .filter(key => !hiddenMainFilterProviders.has(key));
        const ordered = availableProviders.sort((a, b) => (providerCounts[b] || 0) - (providerCounts[a] || 0));
        const total = data.total ?? Object.values(providerCounts).reduce((s, c) => s + c, 0);

        const container = document.getElementById('provider-filters');
        container.innerHTML = `<button class="filter-btn active" data-provider="">
            全部<span class="count-badge">${total}</span></button>`;

        ordered.forEach(key => {
            const btn = document.createElement('button');
            btn.className = 'filter-btn';
            btn.dataset.provider = key;
            const count = providerCounts[key] || 0;
            const color = providerColors[key] || '#666';
            btn.innerHTML = `<span class="provider-dot" style="background:${color}"></span>
                ${providerNames[key] || key}
                <span class="count-badge">${count}</span>`;
            container.appendChild(btn);
        });

        container.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                setProvider(btn.dataset.provider);
            });
        });
    } catch (e) { console.error('providers:', e); }
}

async function loadYears() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const select = document.getElementById('year-filter');
        select.innerHTML = '<option value="">全部年份</option>';
        (data.years || []).forEach(y => {
            const o = document.createElement('option'); o.value = y; o.textContent = y;
            select.appendChild(o);
        });
    } catch (e) {}
}

async function loadTitles() {
    if (state.loading || !state.hasMore) return;
    state.loading = true;

    const loader = document.getElementById('scroll-loader');
    const end = document.getElementById('scroll-end');
    if (state.page > 1) loader.classList.remove('hidden');
    end.classList.add('hidden');

    try {
        const params = new URLSearchParams({
            page: state.page, limit: state.limit,
            sort_by: state.sort_by, order: state.order
        });
        if (state.provider) params.append('provider', state.provider);
        if (state.type) params.append('type', state.type);
        if (state.search) params.append('search', state.search);
        if (state.year) params.append('year', state.year);
        if (state.rating > 0) params.append('min_rating', state.rating);

        const res = await fetch(`/api/titles?${params}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        renderTitles(data.titles, state.page === 1);

        const typeLabel = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部电视剧' : '部作品';
        const loaded = Math.min((state.page - 1) * state.limit + data.titles.length, data.total);
        document.getElementById('stats-info').innerHTML =
            `共 <span>${data.total}</span> ${typeLabel}，已加载 <span>${loaded}</span> 部`;

        state.hasMore = data.titles.length > 0 && loaded < data.total;
        document.getElementById('scroll-sentinel').classList.toggle('hidden', !state.hasMore);
        if (state.page === 1 && data.total === 0 && !hasActiveFilters()) {
            checkBootstrapSync();
        }
        if (!state.hasMore && data.total > 0) {
            loader.classList.add('hidden');
            end.classList.remove('hidden');
        }
        if (data.titles.length > 0) state.page++;
    } catch (e) {
        console.error('titles:', e);
        state.hasMore = false;
        document.getElementById('scroll-sentinel').classList.add('hidden');
        if (state.page === 1) {
            renderError();
            document.getElementById('stats-info').textContent = '加载失败，请稍后重试';
        } else {
            end.textContent = '加载失败，请刷新重试';
            end.classList.remove('hidden');
        }
    } finally {
        state.loading = false;
        loader.classList.add('hidden');
    }
}

function hasActiveFilters() {
    return Boolean(state.provider || state.type || state.search || state.year || state.rating > 0);
}

async function checkBootstrapSync() {
    try {
        const status = await loadSyncStatus();
        if (!status) return;
        const sync = status.sync || {};
        if (!sync.running) return;

        const rebuilding = sync.current_reason === 'untrusted_rating_rebuild';
        const progress = sync.last_result || status.latest_run || {};
        const current = progress.current_provider;
        const discovered = progress.discovered || 0;
        const step = progress.provider_total
            ? `${progress.current_provider_index || '?'} / ${progress.provider_total} · `
            : '';
        const progressText = current
            ? `当前平台：${step}${providerNames[current] || current}${discovered ? `，已发现 ${discovered} 部候选` : ''}`
            : '';
        document.getElementById('stats-info').textContent = rebuilding
            ? '正在按 IMDb 评分重建数据...'
            : '首次部署正在抓取数据...';
        document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
            <div class="spinner"></div>
            <div class="empty-title">${rebuilding ? '正在重建数据' : '首批数据抓取中'}</div>
            <p>${escapeHtml(progressText) || (rebuilding ? '正在清理非 IMDb 评分并重新入库' : '正在抓取首批作品，稍后会自动刷新')}</p>
        </div>`;

        if (!bootstrapPollTimer) {
            let pollCount = 0;
            const POLL_MAX = 180;
            bootstrapPollTimer = setInterval(async () => {
                pollCount++;
                const next = await loadSyncStatus();
                const stillRunning = next?.sync?.running;
                if (!stillRunning) {
                    clearInterval(bootstrapPollTimer);
                    bootstrapPollTimer = null;
                    await loadProviders();
                    await loadYears();
                    resetAndLoad();
                } else if (pollCount >= POLL_MAX) {
                    clearInterval(bootstrapPollTimer);
                    bootstrapPollTimer = null;
                    document.getElementById('stats-info').textContent =
                        '同步仍在后台进行，请稍后手动刷新页面';
                }
            }, 10000);
        }
    } catch (e) {}
}

function renderSkeletons() {
    const grid = document.getElementById('titles-grid');
    let html = '';
    for (let i = 0; i < SKELETON_COUNT; i++) {
        html += `<div class="skeleton-card" aria-hidden="true">
            <div class="skeleton-poster"></div>
            <div class="skeleton-info">
                <div class="skeleton-line medium"></div>
                <div class="skeleton-line short"></div>
                <div class="skeleton-line tiny"></div>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}

function renderTitles(titles, clear) {
    const grid = document.getElementById('titles-grid');
    if (clear) {
        grid.innerHTML = '';
        if (titles.length === 0) {
            renderEmptyState();
            return;
        }
    }

    const frag = document.createDocumentFragment();

    titles.forEach(t => {
        const card = document.createElement('div');
        card.className = 'title-card';
        card.dataset.titleId = t.id;

        const rating = Number(t.imdb_rating) || 0;
        const tier = ratingTier(rating);
        const ratingText = rating > 0 ? rating.toFixed(1) : '—';
        const sourceText = t.rating_source ? ratingSourceNames[t.rating_source] || 'IMDb' : '';
        const poster = sanitizeUrl(t.poster_url) || posterFallback;
        const typeLabel = t.type === 'movie' ? '电影' : '电视剧';
        const title = escapeHtml(t.title);
        const overview = escapeHtml(t.overview || '');
        const releaseDate = escapeHtml(t.release_date || '');

        const providersHtml = (t.providers || []).map(p => {
            const color = providerColors[p] || '#666';
            return `<span class="card-provider">
                <span class="p-dot" style="background:${color}"></span>${escapeHtml(providerNames[p] || p)}</span>`;
        }).join('');

        const ratingHtml = rating > 0
            ? `<div class="poster-rating" data-tier="${tier}" title="${escapeHtml(sourceText || '评分来源')} ${escapeHtml(ratingText)}">${escapeHtml(ratingText)}</div>`
            : `<div class="poster-rating no-rating" title="评分待更新">—</div>`;

        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-label', `查看 ${t.title || ''} 详情，评分 ${ratingText}`);
        card.innerHTML = `
            <div class="poster-wrap">
                <img src="${escapeHtml(poster)}" alt="${title}" loading="lazy"
                     onerror="this.src=window.posterFallback">
                ${ratingHtml}
                <span class="type-tag">${typeLabel}</span>
            </div>
            <div class="card-info">
                <div class="card-title">${title}</div>
                <div class="card-meta">
                    <span class="card-date">${releaseDate}</span>
                </div>
                <div class="card-overview">${overview}</div>
                <div class="card-providers">${providersHtml}</div>
            </div>`;

        card.addEventListener('click', () => showDetail(t.id));
        card.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                showDetail(t.id);
            }
        });
        frag.appendChild(card);
    });

    grid.appendChild(frag);
}

function renderEmptyState() {
    const grid = document.getElementById('titles-grid');
    const hasFilters = hasActiveFilters();
    const icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>`;
    grid.innerHTML = `<div class="empty-state">
        <div class="empty-icon-wrap">${icon}</div>
        <div class="empty-title">没有匹配的作品</div>
        <p>${hasFilters ? '当前筛选条件下没有结果，可以尝试清除筛选或换个关键词' : '数据库似乎是空的，等待同步完成后再试'}</p>
        ${hasFilters ? '<button type="button" class="btn-clear-filters" onclick="clearAllFilters()">清除所有筛选</button>' : ''}
    </div>`;
}

function renderError() {
    const grid = document.getElementById('titles-grid');
    const icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>`;
    grid.innerHTML = `<div class="empty-state">
        <div class="empty-icon-wrap">${icon}</div>
        <div class="empty-title">加载失败</div>
        <p>无法获取作品列表，请检查网络后重试</p>
        <button type="button" class="btn-retry" onclick="resetAndLoad()">重新加载</button>
    </div>`;
}

async function showDetail(id) {
    const modal = document.getElementById('detail-modal');
    const body = document.getElementById('detail-content');
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    body.innerHTML = `<div style="display:flex;justify-content:center;align-items:center;min-height:240px;padding:40px">
        <div class="spinner"></div></div>`;

    try {
        const res = await fetch(`/api/titles/${id}`);
        if (!res.ok) throw new Error('');
        renderDetail(await res.json());
    } catch (e) {
        body.innerHTML = '<div style="text-align:center;color:var(--text-tertiary);padding:64px 24px">加载失败，请重试</div>';
    }
}

function renderDetail(t) {
    const rating = Number(t.imdb_rating) || 0;
    const tier = ratingTier(rating);
    const ratingNum = rating > 0 ? rating.toFixed(1) : '—';
    const tierLabel = tier ? ratingTierLabels[tier] : '';
    const votesText = t.rating_votes ? `${Number(t.rating_votes).toLocaleString()} 票` : '票数待更新';
    const sourceText = t.rating_source ? ratingSourceNames[t.rating_source] || 'IMDb' : 'IMDb';
    const poster = sanitizeUrl(t.poster_url) || posterFallback;
    const typeLabel = t.type === 'movie' ? '电影' : '电视剧';
    const title = escapeHtml(t.title);
    const originalTitle = escapeHtml(t.original_title || '');
    const showOriginal = originalTitle && originalTitle !== title;
    const releaseDate = escapeHtml(t.release_date || '—');
    const overview = escapeHtml(t.overview || '暂无简介');
    const tmdbType = t.type === 'movie' ? 'movie' : 'tv';
    const tmdbId = encodeURIComponent(t.tmdb_id);
    const imdbId = t.imdb_id ? encodeURIComponent(t.imdb_id) : '';

    const providersHtml = (t.providers || []).map(p => {
        const color = providerColors[p] || '#666';
        return `<span class="modal-provider">
            <span class="p-dot" style="background:${color}"></span>${escapeHtml(providerNames[p] || p)}</span>`;
    }).join('');
    const imdbLinkHtml = imdbId
        ? `<a href="https://www.imdb.com/title/${imdbId}/" target="_blank" rel="noopener noreferrer" class="modal-link imdb-link">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 17L17 7"/><path d="M8 7h9v9"/></svg>
                在 IMDb 查看
            </a>`
        : '';

    const ratingTierHtml = tier
        ? `<div class="rating-tier" data-tier="${tier}">${tierLabel}</div>`
        : '';

    document.getElementById('detail-content').innerHTML = `
        <div class="modal-hero">
            <div class="modal-hero-bg" style="background-image:url('${escapeHtml(poster)}')"></div>
            <div class="modal-hero-content">
                <div class="modal-hero-rating">
                    <div class="rating-num ${rating > 0 ? '' : 'no-rating'}">${escapeHtml(ratingNum)}</div>
                    ${ratingTierHtml}
                </div>
                <div class="modal-hero-title">
                    <h2>${title}</h2>
                    ${showOriginal ? `<p class="original-title">${originalTitle}</p>` : ''}
                </div>
            </div>
        </div>
        <div class="modal-body">
            <div class="modal-poster">
                <img src="${escapeHtml(poster)}" alt="${title}"
                     onerror="this.src=window.posterFallback">
            </div>
            <div class="modal-info">
                <div class="meta-tags">
                    <span class="meta-tag">${typeLabel}</span>
                    <span class="meta-tag">${releaseDate}</span>
                    <span class="meta-tag votes-tag">${escapeHtml(sourceText)} · ${escapeHtml(votesText)}</span>
                </div>
                ${providersHtml ? `<div class="modal-section-title">可观看平台</div>
                <div class="modal-providers">${providersHtml}</div>` : ''}
                <div class="modal-section-title">剧情简介</div>
                <p class="modal-overview">${overview}</p>
                <div class="modal-links">
                    ${imdbLinkHtml}
                    <a href="https://www.themoviedb.org/${tmdbType}/${tmdbId}" target="_blank" rel="noopener noreferrer" class="modal-link">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                        在 TMDB 查看
                    </a>
                </div>
            </div>
        </div>`;
}

function closeModal() {
    document.getElementById('detail-modal').classList.add('hidden');
    document.body.style.overflow = '';
}

function resetAndLoad() {
    state.page = 1; state.hasMore = true;
    const end = document.getElementById('scroll-end');
    end.textContent = '已加载全部作品';
    end.classList.add('hidden');
    document.getElementById('scroll-sentinel').classList.remove('hidden');
    renderSkeletons();
    renderActiveFilters();
    loadTitles();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---- 状态修改与 active filter chips ---- */

function setProvider(value) {
    state.provider = value || '';
    const container = document.getElementById('provider-filters');
    container.querySelectorAll('.filter-btn').forEach(b => {
        b.classList.toggle('active', (b.dataset.provider || '') === state.provider);
    });
    resetAndLoad();
}

function setType(value) {
    state.type = value || '';
    document.querySelectorAll('#type-filters .filter-btn').forEach(b => {
        b.classList.toggle('active', (b.dataset.type || '') === state.type);
    });
    resetAndLoad();
}

function setSearch(value) {
    state.search = value || '';
    const input = document.getElementById('search-input');
    if (input.value !== state.search) input.value = state.search;
    resetAndLoad();
}

function setYear(value) {
    state.year = value || '';
    const select = document.getElementById('year-filter');
    if (select.value !== state.year) select.value = state.year;
    resetAndLoad();
}

function setRating(value) {
    state.rating = Number(value) || 0;
    const select = document.getElementById('rating-filter');
    const target = state.rating > 0 ? String(state.rating) : '0';
    if (select.value !== target) select.value = target;
    resetAndLoad();
}

function clearAllFilters() {
    state.provider = '';
    state.type = '';
    state.search = '';
    state.year = '';
    state.rating = 0;
    document.getElementById('search-input').value = '';
    document.getElementById('year-filter').value = '';
    document.getElementById('rating-filter').value = '0';
    document.querySelectorAll('#provider-filters .filter-btn').forEach(b => {
        b.classList.toggle('active', !b.dataset.provider);
    });
    document.querySelectorAll('#type-filters .filter-btn').forEach(b => {
        b.classList.toggle('active', !b.dataset.type);
    });
    document.querySelectorAll('#sort-filters .filter-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.sort === state.sort_by);
    });
    resetAndLoad();
}

window.clearAllFilters = clearAllFilters;

function renderActiveFilters() {
    const container = document.getElementById('active-filters');
    if (!container) return;

    const chips = [];

    if (state.search) {
        chips.push({
            kind: 'search',
            keyLabel: '关键词',
            label: state.search,
            onClear: () => setSearch(''),
        });
    }
    if (state.provider) {
        chips.push({
            kind: 'provider',
            keyLabel: '平台',
            label: providerNames[state.provider] || state.provider,
            color: providerColors[state.provider],
            onClear: () => setProvider(''),
        });
    }
    if (state.type) {
        chips.push({
            kind: 'type',
            keyLabel: '类型',
            label: state.type === 'movie' ? '电影' : '电视剧',
            onClear: () => setType(''),
        });
    }
    if (state.year) {
        chips.push({
            kind: 'year',
            keyLabel: '年份',
            label: state.year,
            onClear: () => setYear(''),
        });
    }
    if (state.rating > 0) {
        chips.push({
            kind: 'rating',
            keyLabel: '评分',
            label: `≥ ${state.rating}`,
            onClear: () => setRating(0),
        });
    }

    if (chips.length === 0) {
        container.classList.add('hidden');
        container.innerHTML = '';
        return;
    }

    container.classList.remove('hidden');
    container.innerHTML = '<span class="active-filters-label">已筛选</span>';

    chips.forEach((chip, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'active-filter-chip';
        btn.dataset.kind = chip.kind;
        if (chip.color) btn.style.setProperty('--chip-color', chip.color);
        btn.setAttribute('aria-label', `移除筛选 ${chip.keyLabel}：${chip.label}`);
        btn.innerHTML = `
            ${chip.kind === 'provider' ? '<span class="chip-dot"></span>' : ''}
            <span class="chip-label"><span class="chip-label-key">${escapeHtml(chip.keyLabel)}</span>${escapeHtml(chip.label)}</span>
            <span class="chip-x" aria-hidden="true">
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="1.5" y1="1.5" x2="8.5" y2="8.5"/><line x1="8.5" y1="1.5" x2="1.5" y2="8.5"/></svg>
            </span>`;
        btn.addEventListener('click', chip.onClear);
        container.appendChild(btn);
    });

    if (chips.length > 1) {
        const clearAll = document.createElement('button');
        clearAll.type = 'button';
        clearAll.className = 'btn-clear-all-filters';
        clearAll.textContent = '全部清除';
        clearAll.addEventListener('click', clearAllFilters);
        container.appendChild(clearAll);
    }
}

function setupEvents() {
    document.querySelectorAll('#sort-filters .filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#sort-filters .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.sort_by = btn.dataset.sort;
            resetAndLoad();
        });
    });

    document.querySelectorAll('#type-filters .filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            setType(btn.dataset.type);
        });
    });

    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('detail-modal').addEventListener('click', e => {
        if (e.target.id === 'detail-modal') closeModal();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeModal();
    });

    let timer;
    document.getElementById('search-input').addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            setSearch(document.getElementById('search-input').value.trim());
        }, 350);
    });

    document.getElementById('year-filter').addEventListener('change', e => {
        setYear(e.target.value);
    });

    document.getElementById('rating-filter').addEventListener('change', e => {
        setRating(parseFloat(e.target.value) || 0);
    });
}

function setupInfiniteScroll() {
    new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && state.hasMore && !state.loading) loadTitles();
    }, { threshold: 0, rootMargin: '600px 0px' }).observe(document.getElementById('scroll-sentinel'));
}

function setupBackToTop() {
    const btn = document.getElementById('back-to-top');
    if (!btn) return;
    let ticking = false;
    const threshold = 600;
    const update = () => {
        const visible = window.scrollY > threshold;
        btn.classList.toggle('visible', visible);
        ticking = false;
    };
    window.addEventListener('scroll', () => {
        if (!ticking) {
            requestAnimationFrame(update);
            ticking = true;
        }
    }, { passive: true });
    btn.addEventListener('click', () => {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
}
