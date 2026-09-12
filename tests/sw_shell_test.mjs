/* R4-01 Service Worker 外壳提交语义测试。
 *
 * 在 node:vm 中执行 static/sw.js 源码，用内存 Cache 模拟 install/activate/fetch
 * 生命周期，验证：
 *   a) 首次安装全部成功 → 核心资源入缓存并 skipWaiting；
 *   b) 已有旧版本、新 CSS 失败 → 安装拒绝、不 skipWaiting、清理新缓存、旧缓存保留；
 *   c) 新 JS / 入口 HTML 失败 → 同上；
 *   d) 仅可选图标失败 → 安装成功、核心完整、skipWaiting；
 *   e) 完整升级 activate 清理旧 stream- 缓存；外壳不完整时保留旧缓存；
 *   f) fetch 逻辑保留：静态资源离线可命中缓存。
 *
 * 运行：node tests/sw_shell_test.mjs（任一断言失败以退出码 1 结束）
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SW_SOURCE = fs.readFileSync(path.join(REPO_ROOT, 'static', 'sw.js'), 'utf8');
const ORIGIN = 'http://127.0.0.1:8000';

const absoluteUrl = input =>
    new URL(typeof input === 'string' ? input : input.url, ORIGIN).href;

/* 从 sw.js 源码提取版本与资源清单：release.py 更新版本后测试无需跟着改 */
const versionMatch = SW_SOURCE.match(/const SW_VERSION = '([^']+)'/);
assert.ok(versionMatch, '未能从 static/sw.js 提取 SW_VERSION');
const SW_VERSION = versionMatch[1];
const SHELL_CACHE = `stream-shell-${SW_VERSION}`;
const DATA_CACHE = `stream-data-${SW_VERSION}`;

function extractAssetList(name) {
    const match = SW_SOURCE.match(new RegExp(`const ${name} = \\[([\\s\\S]*?)\\];`));
    assert.ok(match, `未能从 static/sw.js 提取 ${name}`);
    const urls = [...match[1].matchAll(/['"`]([^'"`]+)['"`]/g)]
        .map(item => item[1].replace(/\$\{SW_VERSION\}/g, SW_VERSION));
    assert.ok(urls.length > 0, `${name} 不应为空`);
    return urls;
}

const CORE_SHELL_ASSETS = extractAssetList('CORE_SHELL_ASSETS');
const OPTIONAL_SHELL_ASSETS = extractAssetList('OPTIONAL_SHELL_ASSETS');

assert.deepEqual(
    CORE_SHELL_ASSETS,
    ['/', `/static/css/style.css?v=${SW_VERSION}`, `/static/js/app.js?v=${SW_VERSION}`],
    '核心外壳必须固定为入口 HTML 与版本化 CSS/JS',
);

/* 浏览器中的相对 URL 以 SW 作用域解析；测试用固定 origin 模拟 */
class ScopedRequest extends Request {
    constructor(input, init = {}) {
        if (input instanceof Request) {
            super(input, init);
            return;
        }
        const { cache: _cache, ...rest } = init; // Node 的 Request 不实现 cache: 'reload'
        super(new URL(String(input), ORIGIN).href, rest);
    }
}

function createHarness({ failAdd = [], seed = {} } = {}) {
    const handlers = {};
    const events = [];
    const warnings = [];
    const stores = new Map();

    for (const [name, urls] of Object.entries(seed)) {
        const store = new Map();
        for (const url of urls) {
            store.set(absoluteUrl(url), new Response(`seed:${url}`));
        }
        stores.set(name, store);
    }

    const storeOf = name => {
        if (!stores.has(name)) stores.set(name, new Map());
        return stores.get(name);
    };
    const shouldFailAdd = url => failAdd.some(rule =>
        (rule instanceof RegExp ? rule.test(url) : absoluteUrl(rule) === url));

    const makeCache = name => ({
        async add(request) {
            const url = absoluteUrl(request);
            if (shouldFailAdd(url)) throw new Error(`模拟 add 失败：${url}`);
            storeOf(name).set(url, new Response(`cached:${url}`));
        },
        async match(request) {
            return storeOf(name).get(absoluteUrl(request));
        },
        async put(request, response) {
            storeOf(name).set(absoluteUrl(request), response);
        },
        async delete(request) {
            return storeOf(name).delete(absoluteUrl(request));
        },
        async keys() {
            return [...storeOf(name).keys()].map(url => new ScopedRequest(url));
        },
    });

    const caches = {
        async open(name) {
            storeOf(name);
            return makeCache(name);
        },
        async keys() {
            return [...stores.keys()];
        },
        async delete(name) {
            return stores.delete(name);
        },
        async match(request) {
            const url = absoluteUrl(request);
            for (const store of stores.values()) {
                if (store.has(url)) return store.get(url);
            }
            return undefined;
        },
    };

    const sandbox = {
        console: {
            log() {},
            error() {},
            warn: (...args) => warnings.push(args.map(String).join(' ')),
        },
        URL,
        Headers,
        Request: ScopedRequest,
        Response,
        fetch: async request => {
            throw new Error(`模拟离线：${absoluteUrl(request)}`);
        },
        caches,
        self: {
            addEventListener(type, handler) {
                handlers[type] = handler;
            },
            skipWaiting: async () => {
                events.push('skipWaiting');
            },
            clients: {
                claim: async () => {
                    events.push('claim');
                },
            },
            location: { origin: ORIGIN },
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(SW_SOURCE, sandbox, { filename: 'static/sw.js' });

    const runLifecycle = handlerName => async () => {
        let pending = null;
        handlers[handlerName]({ waitUntil: promise => { pending = promise; } });
        assert.ok(pending, `${handlerName} 应通过 waitUntil 注册生命周期 Promise`);
        return pending;
    };

    return {
        handlers,
        events,
        warnings,
        stores,
        runInstall: runLifecycle('install'),
        runActivate: runLifecycle('activate'),
        hasCache: name => stores.has(name),
        cacheUrls: name => [...(stores.get(name)?.keys() ?? [])],
    };
}

async function caseFirstInstallAllCached() {
    const harness = createHarness();
    for (const name of ['install', 'activate', 'fetch', 'message']) {
        assert.equal(typeof harness.handlers[name], 'function', `应注册 ${name} 处理器`);
    }
    await harness.runInstall();
    assert.ok(harness.events.includes('skipWaiting'), '首次安装成功应调用 skipWaiting');
    const cached = harness.cacheUrls(SHELL_CACHE);
    for (const url of CORE_SHELL_ASSETS) {
        assert.ok(cached.includes(absoluteUrl(url)), `核心资源应已缓存：${url}`);
    }
    for (const url of OPTIONAL_SHELL_ASSETS) {
        assert.ok(cached.includes(absoluteUrl(url)), `可选资源应尽力缓存：${url}`);
    }
}

async function caseCssFailureKeepsOldVersion() {
    const harness = createHarness({
        failAdd: [/\.css/],
        seed: {
            'stream-shell-OLD': ['/', '/static/css/style.css?v=OLD'],
            'stream-data-OLD': ['/api/titles'],
        },
    });
    await assert.rejects(harness.runInstall(), /核心外壳资源缓存失败/);
    assert.ok(!harness.events.includes('skipWaiting'), '安装失败不得调用 skipWaiting');
    assert.equal(harness.hasCache(SHELL_CACHE), false, '失败后应清理本次新外壳缓存');
    assert.ok(harness.hasCache('stream-shell-OLD'), '旧外壳缓存必须保留');
    assert.ok(harness.hasCache('stream-data-OLD'), '旧数据缓存必须保留');
}

async function caseJsAndEntryFailureKeepOldVersion() {
    const scenarios = [
        ['新 JS 失败', [/\/static\/js\/app\.js/]],
        ['入口 HTML 失败', ['/']],
    ];
    for (const [label, failAdd] of scenarios) {
        const harness = createHarness({
            failAdd,
            seed: { 'stream-shell-OLD': ['/'] },
        });
        await assert.rejects(harness.runInstall(), /核心外壳资源缓存失败/, `${label} 时安装应拒绝`);
        assert.ok(!harness.events.includes('skipWaiting'), `${label} 时不得调用 skipWaiting`);
        assert.equal(harness.hasCache(SHELL_CACHE), false, `${label} 时应清理新外壳缓存`);
        assert.ok(harness.hasCache('stream-shell-OLD'), `${label} 时旧缓存必须保留`);
    }
}

async function caseOptionalAssetFailureStillCommits() {
    const failedOptional = OPTIONAL_SHELL_ASSETS.find(url => url.includes('icon-192'))
        || OPTIONAL_SHELL_ASSETS[0];
    const harness = createHarness({ failAdd: [failedOptional] });
    await harness.runInstall(); // 可选失败不应拒绝安装
    assert.ok(harness.events.includes('skipWaiting'), '可选资源失败不应阻止安装提交');
    const cached = harness.cacheUrls(SHELL_CACHE);
    for (const url of CORE_SHELL_ASSETS) {
        assert.ok(cached.includes(absoluteUrl(url)), `核心资源必须完整：${url}`);
    }
    assert.ok(!cached.includes(absoluteUrl(failedOptional)), '失败的可选资源不应留在缓存中');
    assert.ok(
        harness.warnings.some(text => text.includes(failedOptional)),
        '可选资源失败应记录日志',
    );
}

async function caseActivateCleansOnlyWhenShellComplete() {
    // 完整升级：install 成功后 activate 才清理旧版本
    const upgraded = createHarness({
        seed: {
            'stream-shell-OLD': ['/'],
            'stream-data-OLD': ['/api/titles'],
            'unrelated-cache': ['/x'],
        },
    });
    await upgraded.runInstall();
    await upgraded.runActivate();
    assert.ok(upgraded.events.includes('claim'), 'activate 后应 claim 客户端');
    assert.equal(upgraded.hasCache('stream-shell-OLD'), false, '完整升级后应删除旧外壳缓存');
    assert.equal(upgraded.hasCache('stream-data-OLD'), false, '完整升级后应删除旧数据缓存');
    assert.ok(upgraded.hasCache(SHELL_CACHE), '当前外壳缓存必须保留');
    assert.ok(upgraded.hasCache('unrelated-cache'), '非 stream- 缓存不应被清理');

    // 外壳不完整（只有入口，缺 CSS/JS）：保留旧版本以便回退
    const incomplete = createHarness({
        seed: {
            [SHELL_CACHE]: ['/'],
            'stream-shell-OLD': ['/'],
            'stream-data-OLD': ['/api/titles'],
        },
    });
    await incomplete.runActivate();
    assert.ok(incomplete.events.includes('claim'), '外壳不完整也应 claim 客户端');
    assert.ok(incomplete.hasCache('stream-shell-OLD'), '外壳不完整必须保留旧外壳缓存');
    assert.ok(incomplete.hasCache('stream-data-OLD'), '外壳不完整必须保留旧数据缓存');

    // 当前外壳缓存完全缺失：同样不清理
    const missing = createHarness({ seed: { 'stream-shell-OLD': ['/'] } });
    await missing.runActivate();
    assert.ok(missing.hasCache('stream-shell-OLD'), '无当前外壳时必须保留旧缓存');
}

async function caseStaticFetchFallsBackToCache() {
    const harness = createHarness();
    await harness.runInstall();
    let responded = null;
    harness.handlers.fetch({
        request: new ScopedRequest(absoluteUrl(CORE_SHELL_ASSETS[1])),
        waitUntil() {},
        respondWith(promise) { responded = promise; },
    });
    assert.ok(responded, '静态资源请求应 respondWith');
    const response = await responded;
    assert.ok(response, '离线时应返回缓存响应');
    assert.match(await response.text(), /^cached:/);
}

const CASES = [
    ['a) 首次安装全部成功：核心资源入缓存并 skipWaiting', caseFirstInstallAllCached],
    ['b) 已有旧版本且新 CSS 失败：安装不提交、旧缓存保留', caseCssFailureKeepsOldVersion],
    ['c) 新 JS / 入口 HTML 失败：安装不提交、旧缓存保留', caseJsAndEntryFailureKeepOldVersion],
    ['d) 仅可选图标失败：安装成功且核心完整', caseOptionalAssetFailureStillCommits],
    ['e) activate 仅在当前外壳完整时清理旧版本缓存', caseActivateCleansOnlyWhenShellComplete],
    ['f) fetch 逻辑保留：静态资源离线命中缓存', caseStaticFetchFallsBackToCache],
];

let failed = 0;
for (const [name, fn] of CASES) {
    try {
        await fn();
        console.log(`[PASS] ${name}`);
    } catch (error) {
        failed += 1;
        console.error(`[FAIL] ${name}`);
        console.error(error);
    }
}
if (failed) {
    console.error(`sw_shell_test: ${CASES.length - failed}/${CASES.length} 通过`);
    process.exit(1);
}
console.log(`sw_shell_test: ${CASES.length}/${CASES.length} 通过`);
