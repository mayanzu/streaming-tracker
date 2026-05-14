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
    netflix: 'Netflix', disney: 'Disney+', max: 'Max', hbo: 'Max',
    amazon: 'Prime Video', apple: 'Apple TV+', hulu: 'Hulu'
};

const ratingSourceNames = {
    imdb: 'IMDb',
    omdb: 'IMDb'
};

let providerCounts = {};
let bootstrapPollTimer = null;
let bootstrapPartialShown = false;
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

document.addEventListener('DOMContentLoaded', async () => {
    await loadSyncStatus();
    await loadProviders();
    await loadYears();
    loadTitles();
    setupEvents();
    setupInfiniteScroll();
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

    const sync = status.sync || {};
    const latestFinished = status.latest_finished_sync || {};
    const last = sync.last_result || latestFinished || {};

    if (sync.running) {
        el.textContent = sync.current_reason === 'untrusted_rating_rebuild'
            ? 'IMDb 重建中'
            : '同步中';
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

        const ordered = data.available.sort((a, b) => (providerCounts[b] || 0) - (providerCounts[a] || 0));
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
                container.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.provider = btn.dataset.provider;
                resetAndLoad();
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

async function loadStatsSnapshot() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) return null;
        return await res.json();
    } catch (e) {
        return null;
    }
}

async function checkBootstrapSync() {
    try {
        const status = await loadSyncStatus();
        if (!status) return;
        const sync = status.sync || {};
        if (!sync.running) return;

        const rebuilding = sync.current_reason === 'untrusted_rating_rebuild';
        document.getElementById('stats-info').textContent = rebuilding
            ? '正在按 IMDb 评分重建数据...'
            : '首次部署正在抓取数据...';
        document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
            <div class="spinner"></div>
            <p>${rebuilding ? '正在清理非 IMDb 评分并重新入库' : '正在抓取首批作品，稍后会自动刷新'}</p>
        </div>`;

        if (!bootstrapPollTimer) {
            bootstrapPollTimer = setInterval(async () => {
                const next = await loadSyncStatus();
                if (next?.sync?.running) {
                    if (!bootstrapPartialShown && !hasActiveFilters()) {
                        const stats = await loadStatsSnapshot();
                        if ((stats?.total || 0) > 0) {
                            bootstrapPartialShown = true;
                            await loadProviders();
                            await loadYears();
                            resetAndLoad();
                        }
                    }
                    return;
                }

                if (!next?.sync?.running) {
                    clearInterval(bootstrapPollTimer);
                    bootstrapPollTimer = null;
                    bootstrapPartialShown = false;
                    await loadProviders();
                    await loadYears();
                    resetAndLoad();
                }
            }, 10000);
        }
    } catch (e) {}
}

function renderTitles(titles, clear) {
    const grid = document.getElementById('titles-grid');
    if (clear) {
        grid.innerHTML = '';
        if (titles.length === 0) {
            grid.innerHTML = `<div class="empty-state">
                <div class="empty-icon">—</div>
                <p>没有找到符合条件的作品</p>
            </div>`;
        }
    }

    const frag = document.createDocumentFragment();

    titles.forEach(t => {
        const card = document.createElement('div');
        card.className = 'title-card';
        card.dataset.titleId = t.id;

        const rating = t.imdb_rating || 0;
        const ratingCls = rating > 0 ? 'card-rating' : 'card-rating no-rating';
        const ratingText = rating > 0 ? rating.toFixed(1) : '—';
        const sourceText = t.rating_source ? ratingSourceNames[t.rating_source] || 'IMDb' : '';
        const ratingLabel = sourceText ? `${ratingText} ${sourceText}` : ratingText;
        const poster = t.poster_url || posterFallback;
        const typeLabel = t.type === 'movie' ? '电影' : '电视剧';
        const title = escapeHtml(t.title);
        const overview = escapeHtml(t.overview || '');
        const releaseDate = escapeHtml(t.release_date || '');

        const providersHtml = (t.providers || []).map(p => {
            const color = providerColors[p] || '#666';
            return `<span class="card-provider">
                <span class="p-dot" style="background:${color}"></span>${escapeHtml(providerNames[p] || p)}</span>`;
        }).join('');

        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-label', `查看 ${t.title || ''} 详情`);
        card.innerHTML = `
            <div class="poster-wrap">
                <img src="${escapeHtml(poster)}" alt="${title}" loading="lazy"
                     onerror="this.src=window.posterFallback">
                <span class="type-tag">${typeLabel}</span>
            </div>
            <div class="card-info">
                <div class="card-title">${title}</div>
                <div class="card-meta">
                    <span class="${ratingCls}" title="${escapeHtml(sourceText || '评分来源待更新')}">${escapeHtml(ratingLabel)}</span>
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

function renderError() {
    document.getElementById('titles-grid').innerHTML = `<div class="empty-state">
        <div class="empty-icon">!</div>
        <p>数据加载失败，请刷新页面重试</p>
    </div>`;
}

async function showDetail(id) {
    const modal = document.getElementById('detail-modal');
    const body = document.getElementById('detail-content');
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    body.innerHTML = `<div style="display:flex;justify-content:center;align-items:center;height:200px">
        <div class="spinner"></div></div>`;

    try {
        const res = await fetch(`/api/titles/${id}`);
        if (!res.ok) throw new Error('');
        renderDetail(await res.json());
    } catch (e) {
        body.innerHTML = '<div style="text-align:center;color:var(--text-tertiary);padding:48px">加载失败，请重试</div>';
    }
}

function renderDetail(t) {
    const rating = t.imdb_rating || 0;
    const ratingText = rating > 0 ? rating.toFixed(1) : '暂无评分';
    const sourceText = t.rating_source
        ? ratingSourceNames[t.rating_source] || 'IMDb'
        : '评分来源待更新';
    const votesText = t.rating_votes ? `IMDb ${Number(t.rating_votes).toLocaleString()} 票` : 'IMDb 票数待更新';
    const poster = t.poster_url || posterFallback;
    const typeLabel = t.type === 'movie' ? '电影' : '电视剧';
    const title = escapeHtml(t.title);
    const originalTitle = escapeHtml(t.original_title || '');
    const releaseDate = escapeHtml(t.release_date || '—');
    const overview = escapeHtml(t.overview || '暂无简介');
    const tmdbType = t.type === 'movie' ? 'movie' : 'tv';
    const tmdbId = encodeURIComponent(t.tmdb_id);

    const providersHtml = (t.providers || []).map(p => {
        const color = providerColors[p] || '#666';
        return `<span class="modal-provider">
            <span class="p-dot" style="background:${color}"></span>${escapeHtml(providerNames[p] || p)}</span>`;
    }).join('');

    document.getElementById('detail-content').innerHTML = `
        <div class="modal-poster">
            <img src="${escapeHtml(poster)}" alt="${title}"
                 onerror="this.src=window.posterFallback">
        </div>
        <div class="modal-info">
            <h2>${title}</h2>
            ${originalTitle ? `<p class="original-title">${originalTitle}</p>` : ''}
            <div class="meta-tags">
                <span class="meta-tag rating-tag">${ratingText}</span>
                <span class="meta-tag">${sourceText}</span>
                <span class="meta-tag">${votesText}</span>
                <span class="meta-tag">${typeLabel}</span>
                <span class="meta-tag">${releaseDate}</span>
            </div>
            <div class="modal-providers">${providersHtml}</div>
            <h3>剧情简介</h3>
            <p class="modal-overview">${overview}</p>
            <a href="https://www.themoviedb.org/${tmdbType}/${tmdbId}" target="_blank" rel="noopener noreferrer" class="modal-link">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                在 TMDB 查看
            </a>
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
    loadTitles();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setupEvents() {
    // Sort
    document.querySelectorAll('#sort-filters .filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#sort-filters .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.sort_by = btn.dataset.sort;
            resetAndLoad();
        });
    });

    // Type
    document.querySelectorAll('#type-filters .filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#type-filters .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.type = btn.dataset.type;
            resetAndLoad();
        });
    });

    // Modal close
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('detail-modal').addEventListener('click', e => {
        if (e.target.id === 'detail-modal') closeModal();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeModal();
    });

    // Search (debounced)
    let timer;
    document.getElementById('search-input').addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            state.search = document.getElementById('search-input').value.trim();
            resetAndLoad();
        }, 350);
    });

    // Year
    document.getElementById('year-filter').addEventListener('change', e => {
        state.year = e.target.value; resetAndLoad();
    });

    // Rating
    document.getElementById('rating-filter').addEventListener('change', e => {
        state.rating = parseFloat(e.target.value) || 0; resetAndLoad();
    });
}

function setupInfiniteScroll() {
    new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && state.hasMore && !state.loading) loadTitles();
    }, { threshold: 0, rootMargin: '600px 0px' }).observe(document.getElementById('scroll-sentinel'));
}
