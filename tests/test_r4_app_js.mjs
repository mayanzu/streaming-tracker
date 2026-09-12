// R4-05/R4-06/R4-02 生产函数探针：在 node:vm 隔离环境执行 static/js/app.js 的真实代码。
// 运行：node tests/test_r4_app_js.mjs
// 说明：不加载真实 DOM/网络；只切取目标函数并注入最小 stub，断言行为而非实现。
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const src = fs.readFileSync('static/js/app.js', 'utf8');

function part(start, end) {
    const from = src.indexOf(start);
    const to = src.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `未找到代码片段: ${start} .. ${end}`);
    return src.slice(from, to);
}

const passed = [];
async function check(name, fn) {
    await fn();
    passed.push(name);
    console.log(`PASS ${name}`);
}

// ── 1. R4-05：统一引用 getTitleRef / titleRefKey / openTitleRef / setTitleStatusByRef ──
const refCtx = vm.createContext({
    console,
    state: { snapshotEntries: new Map() },
    opened: { detail: [], missing: [], status: [] },
});
vm.runInContext(`
    function refForId(id) { return { id: Number(id) }; }
    function refForIdentity(identity) {
        return { key: identity.type + ':' + Number(identity.tmdbId), type: identity.type, tmdbId: Number(identity.tmdbId) };
    }
    function missingEntryKey(entry) { return entry.type + ':' + Number(entry.tmdb_id); }
    function showDetail(id) { opened.detail.push(Number(id)); }
    function openMissingDetail(key) { opened.missing.push(key); }
    function setTitleStatus(id, status, source, identity) { opened.status.push({ id, status, identity: identity || null }); }
    ${part('function statusMenuId(', 'function statusMenuHtml(')}
    ${part('function getTitleRef(', 'function cardForRef(')}
`, refCtx);

await check('catalog 引用与稳定键', () => {
    const ref = vm.runInContext('getTitleRef({ id: 42, type: "movie" })', refCtx);
    assert.equal(ref.kind, 'catalog');
    assert.equal(ref.id, 42);
    assert.equal(vm.runInContext('titleRefKey(getTitleRef({ id: 42 }))', refCtx), 'catalog:42');
});

await check('快照引用使用 type:tmdb_id 稳定键', () => {
    const ref = vm.runInContext(
        'getTitleRef({ id: null, tmdb_id: 278, type: "movie", catalog_available: false })', refCtx);
    assert.equal(ref.kind, 'snapshot');
    assert.equal(ref.key, 'movie:278');
    assert.equal(
        vm.runInContext('titleRefKey(getTitleRef({ id: null, tmdb_id: 278, type: "movie", catalog_available: false }))', refCtx),
        'snapshot:movie:278');
});

await check('无身份行返回 null，不与 catalog/snapshot 冲突', () => {
    assert.equal(vm.runInContext('getTitleRef({})', refCtx), null);
    assert.equal(vm.runInContext('getTitleRef(null)', refCtx), null);
    assert.equal(vm.runInContext('titleRefKey(null)', refCtx), '');
    assert.notEqual(
        vm.runInContext('titleRefKey(getTitleRef({ id: 278 }))', refCtx),
        vm.runInContext('titleRefKey(getTitleRef({ id: null, tmdb_id: 278, type: "movie" }))', refCtx));
});

await check('openTitleRef：catalog 走 showDetail，快照走 openMissingDetail 而非 /titles/0', () => {
    refCtx.opened.detail.length = 0;
    refCtx.opened.missing.length = 0;
    vm.runInContext('openTitleRef(getTitleRef({ id: 7 }))', refCtx);
    assert.deepEqual(refCtx.opened.detail, [7]);
    assert.deepEqual(refCtx.opened.missing, []);
    vm.runInContext(`
        openTitleRef(
            getTitleRef({ id: null, tmdb_id: 123456, type: 'movie', catalog_available: false }),
            { id: null, tmdb_id: 123456, type: 'movie', title: '快照作品' })
    `, refCtx);
    assert.deepEqual(refCtx.opened.missing, ['movie:123456']);
    assert.deepEqual(refCtx.opened.detail, [7], '快照不得调用 showDetail(0/null)');
    assert.ok(refCtx.state.snapshotEntries.has('movie:123456'));
});

await check('setTitleStatusByRef：快照走 watchlist 身份写入', () => {
    refCtx.opened.status.length = 0;
    vm.runInContext(
        'setTitleStatusByRef(getTitleRef({ id: null, tmdb_id: 278, type: "movie" }), "watching")', refCtx);
    assert.deepEqual(JSON.parse(JSON.stringify(refCtx.opened.status)), [
        { id: null, status: 'watching', identity: { type: 'movie', tmdbId: 278 } },
    ]);
});

// ── 2. R4-02：released_before 与 released_after 成对生成 ──
const filterCtx = vm.createContext({
    console,
    state: {
        sort_by: 'rating', order: 'desc', type: '', search: '', region: '', rating: 0,
        genres: [], years: '', maxRuntime: 0, releasedDays: 30,
        excludeWatched: false, watchStatus: '',
    },
    URLSearchParams,
    yearRangeParams: () => null,
});
vm.runInContext(
    part('function releasedAfterDate(', 'function updateUrl()') + '\n'
    + part('function buildFilterParams(', '/* 6.6 '),
    filterCtx);

await check('released_before 为本地今天，released_after 为 N 天前', () => {
    const today = vm.runInContext('todayDateString()', filterCtx);
    assert.match(today, /^\d{4}-\d{2}-\d{2}$/);
    const params = new URLSearchParams(vm.runInContext('buildFilterParams().toString()', filterCtx));
    assert.equal(params.get('released_before'), today);
    assert.equal(params.get('released_after'), new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10));
});

await check('非近期窗口不传日期上界', () => {
    filterCtx.state.releasedDays = 0;
    const params = new URLSearchParams(vm.runInContext('buildFilterParams().toString()', filterCtx));
    assert.equal(params.get('released_before'), null);
    assert.equal(params.get('released_after'), null);
    filterCtx.state.releasedDays = 30;
});

// ── 3. R4-05：surprisePick 快照抽选不产生空 ID ──
const surpriseCtx = vm.createContext({
    console,
    state: {
        surpriseScope: 'watchlist', limit: 40, surpriseRecent: [],
        surpriseLastId: null, surpriseLastRef: null, snapshotEntries: new Map(),
    },
    opened: { detail: [], missing: [], toasts: [] },
});
vm.runInContext(`
    const btn = { disabled: false, classList: { add() {}, remove() {} } };
    const document = { getElementById: () => btn };
    async function surpriseFetch() {
        return { total: 1, data: [{ id: null, tmdb_id: 123456, type: 'movie', title: '快照作品', catalog_available: false }] };
    }
    function showToast(message, level) { opened.toasts.push([message, level]); }
    function userMessage(error) { return error.message; }
    function missingEntryKey(entry) { return entry.type + ':' + Number(entry.tmdb_id); }
    function refForId(id) { return { id: Number(id) }; }
    function refForIdentity(identity) {
        return { key: identity.type + ':' + Number(identity.tmdbId), type: identity.type, tmdbId: Number(identity.tmdbId) };
    }
    function renderSurpriseBar() {}
    function showDetail(id) { opened.detail.push(Number(id)); }
    function openMissingDetail(key) { opened.missing.push(key); }
    ${part('function getTitleRef(', 'function cardForRef(')}
    ${part('async function surprisePick()', 'function parseJsonList(')}
    function rememberSurprise(ref) { state.surpriseRecent = [titleRefKey(ref)]; }
`, surpriseCtx);

await check('surprisePick 仅含快照片单：打开快照详情、不产生 0 号 ID', async () => {
    await vm.runInContext('surprisePick()', surpriseCtx);
    assert.deepEqual(surpriseCtx.opened.toasts, [], `抽选出现异常提示: ${JSON.stringify(surpriseCtx.opened.toasts)}`);
    assert.deepEqual(surpriseCtx.opened.missing, ['movie:123456']);
    assert.deepEqual(surpriseCtx.opened.detail, []);
    assert.equal(surpriseCtx.state.surpriseLastId, null);
    assert.equal(surpriseCtx.state.surpriseLastRef.kind, 'snapshot');
    assert.equal(surpriseCtx.state.surpriseRecent[0], 'snapshot:movie:123456');
});

// ── 4. R4-06：菜单触发器接线（静态断言，浏览器行为由人工/端到端验证） ──
await check('状态菜单触发器带 data-status-menu-trigger 与 aria-controls', () => {
    assert.ok(src.includes('data-status-menu-trigger'));
    assert.ok(src.includes('aria-controls="${escapeHtml(statusMenuId(title.id))}"'));
    assert.ok(src.includes('aria-controls="${escapeHtml(detailMenuId)}"'));
    assert.ok(src.includes('function openStatusMenu(menu, trigger'));
    assert.ok(src.includes('function statusMenuTriggerFor(menu)'));
    assert.ok(src.includes('closeStatusMenus(null, { restoreFocus: true })'));
    assert.ok(src.includes('function statusMenuId(id, key = \'\')'));
    assert.notEqual(
        vm.runInContext('statusMenuId(1)', refCtx),
        vm.runInContext('statusMenuId("", "movie:1")', refCtx));
});

await check('快照卡片不渲染假批量复选框，批量全选只含目录 id', () => {
    const cardSlice = part('function createMissingCatalogCard(', 'async function openMissingDetail(');
    assert.ok(!cardSlice.includes('<span class="batch-check"'));
    assert.ok(src.includes('快照暂不支持批量状态操作'));
    const selectAllSlice = part("if (action?.dataset.action === 'batch-select-all')", "if (action?.dataset.action === 'batch-exit')");
    assert.ok(selectAllSlice.includes('state.loadedTitleIds'));
});

console.log(`\n${passed.length} 项探针全部通过`);
