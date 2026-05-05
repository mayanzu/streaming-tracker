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

let providerCounts = {};

document.addEventListener('DOMContentLoaded', async () => {
    await loadProviders();
    await loadYears();
    loadTitles();
    setupEvents();
    setupInfiniteScroll();
});

async function loadProviders() {
    try {
        const res = await fetch('/api/providers');
        const data = await res.json();
        data.providers.forEach(p => { providerCounts[p.provider_name] = p.count; });

        const ordered = data.available.sort((a, b) => (providerCounts[b] || 0) - (providerCounts[a] || 0));
        const total = Object.values(providerCounts).reduce((s, c) => s + c, 0);

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
        const data = await res.json();
        const select = document.getElementById('year-filter');
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
    loader.classList.remove('hidden');
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
        const data = await res.json();

        renderTitles(data.titles, state.page === 1);

        const typeLabel = state.type === 'movie' ? '部电影' : state.type === 'tv' ? '部电视剧' : '部作品';
        document.getElementById('stats-info').innerHTML =
            `共 <span>${data.total}</span> ${typeLabel}，已加载 <span>${Math.min(state.page * state.limit, data.total)}</span> 部`;

        state.hasMore = (state.page * state.limit) < data.total;
        if (!state.hasMore && data.total > 0) {
            loader.classList.add('hidden');
            end.classList.remove('hidden');
        }
        state.page++;
    } catch (e) {
        console.error('titles:', e);
    } finally {
        state.loading = false;
        loader.classList.add('hidden');
    }
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
        const poster = t.poster_url || 'https://placehold.co/500x750/1a1a1e/5c5c66?text=No+Poster';
        const typeLabel = t.type === 'movie' ? '电影' : '电视剧';
        const releaseYear = (t.release_date || '').substring(0, 4);

        const providersHtml = (t.providers || []).map(p => {
            const color = providerColors[p] || '#666';
            return `<span class="card-provider">
                <span class="p-dot" style="background:${color}"></span>${providerNames[p] || p}</span>`;
        }).join('');

        card.innerHTML = `
            <div class="poster-wrap">
                <img src="${poster}" alt="${t.title}" loading="lazy"
                     onerror="this.src='https://placehold.co/500x750/1a1a1e/5c5c66?text=No+Poster'">
                <span class="type-tag">${typeLabel}</span>
            </div>
            <div class="card-info">
                <div class="card-title">${t.title}</div>
                <div class="card-meta">
                    <span class="${ratingCls}">${ratingText}</span>
                    <span class="card-date">${releaseYear}</span>
                </div>
                <div class="card-overview">${t.overview || ''}</div>
                <div class="card-providers">${providersHtml}</div>
            </div>`;

        card.addEventListener('click', () => showDetail(t.id));
        frag.appendChild(card);
    });

    grid.appendChild(frag);
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
    const poster = t.poster_url || 'https://placehold.co/500x750/1a1a1e/5c5c66?text=No+Poster';
    const typeLabel = t.type === 'movie' ? '电影' : '电视剧';

    const providersHtml = (t.providers || []).map(p => {
        const color = providerColors[p] || '#666';
        return `<span class="modal-provider">
            <span class="p-dot" style="background:${color}"></span>${providerNames[p] || p}</span>`;
    }).join('');

    document.getElementById('detail-content').innerHTML = `
        <div class="modal-poster">
            <img src="${poster}" alt="${t.title}"
                 onerror="this.src='https://placehold.co/500x750/1a1a1e/5c5c66?text=No+Poster'">
        </div>
        <div class="modal-info">
            <h2>${t.title}</h2>
            ${t.original_title ? `<p class="original-title">${t.original_title}</p>` : ''}
            <div class="meta-tags">
                <span class="meta-tag rating-tag">${ratingText}</span>
                <span class="meta-tag">${typeLabel}</span>
                <span class="meta-tag">${t.release_date || '—'}</span>
            </div>
            <div class="modal-providers">${providersHtml}</div>
            <h3>剧情简介</h3>
            <p class="modal-overview">${t.overview || '暂无简介'}</p>
            <a href="https://www.themoviedb.org/${t.type}/${t.tmdb_id}" target="_blank" class="modal-link">
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
    document.getElementById('scroll-end').classList.add('hidden');
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
    }, { threshold: 0 }).observe(document.getElementById('scroll-loader'));
}
