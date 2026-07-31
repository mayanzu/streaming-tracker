# 片流前端系统性全面优化方案

## Context（为什么做）

`streaming-tracker` 前端是纯 vanilla 三件套（`index.html` / `style.css` / `app.js`），已具备懒加载、骨架屏、无限滚动、防抖搜索、rAF 批处理、URL 状态同步、模态焦点陷阱等基础优化。但对照用户提出的硬指标仍有真实差距：

- **性能**：无客户端缓存，切换筛选往返时重复 fetch；详情模态每次都重新请求；状态变更同步等待 API；这些都让交互响应落在 300ms 目标之外。
- **触控**：移动端多处按钮 < 44×44px（filter-btn 38px、sort-order-btn 38px、surprise-btn 32px、clear-search 26px、modal-nav 40px 等），不达标。
- **功能**：用户确认要新增详情预取+乐观更新、列表视图、最近浏览+滚动恢复、键盘帮助浮层；并删除"最近收录(added_date)"排序。
- **部署**：当前 docker-compose 未挂载 static/，前端迭代必须重建镜像；用户选 bind mount 方案。
- **质量**：需出优化前后实测对比报告。

后端已充分优化（WAL + 索引 + GZip + immutable 缓存），本方案聚焦前端，仅触及 `app/api.py` 一行 sort pattern 清理。

预期产出：交互响应 ≤300ms、移动端触控 100% ≥44px、关键任务步骤减少 ≥15%、部署到 192.168.31.3 并实测对比。

## 关键文件

- [static/js/app.js](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js) — 缓存层、乐观更新、新功能主体
- [static/css/style.css](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css) — 触控目标、按钮层级、列表视图、新组件样式
- [static/index.html](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/index.html) — 删 added_date option、新增视图切换/快捷键浮层/最近浏览容器、版本号 bump
- [app/api.py](file:///c:/Users/mzj/Desktop/code/streaming-tracker/app/api.py#L110) — sort pattern 移除 added_date
- [docker-compose.yml](file:///c:/Users/mzj/Desktop/code/streaming-tracker/docker-compose.yml) — static/ bind mount

## 核心架构设计

### 缓存层（app.js，state 声明后新增）

```
const cache = {
  detail: new Map(),   // id -> { data, ts, promise, prefetched }
  pages:  new Map(),   // cacheKey -> { pages: Map<pageNum, titles>, total, has_next, ts, promise }
};
const DETAIL_TTL = 5*60*1000, PAGE_TTL = 2*60*1000;
const DETAIL_CACHE_MAX = 50, PAGE_CACHE_MAX = 5;
```

- `pageCacheKey()`：`provider|type|region|rating|sort_by|order|watch_status|search`（search 归一化 trim+lowercase），与 `buildFilterParams` 同源。
- `getDetail(id)`：命中且未超 TTL → 立即返回，`prefetched=false` 时后台 `revalidateDetail`；超 TTL → 仍返回旧值避免闪烁 + 后台刷新；未命中 → 复用 in-flight promise 或发请求。LRU 超限删最旧。
- `getPage(key, page)`：命中未超 TTL → 直接渲染，page===1 时后台静默刷新仅在新 total 变化时 patch；miss → 请求并写入，并发用 promise 合并。
- 写入时机：`showDetail`、`prefetchDetail`、`setTitleStatus` 成功后用 API 返回值覆盖。
- 失效：`invalidatePageCache(key)`（视图内改状态导致结果集变化时）、`clearAllCaches()`（sync 完成、online 恢复）。

改造点：`loadTitles`（[L552](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L552)）fetch 前查 cache；`showDetail`（[L722](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L722)）先查 detail cache；`surprisePick`（[L769](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L769)）走缓存。`requestVersion` 竞态保护保留。

### 乐观更新 + 回滚（替换 [setTitleStatus L867-891](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L867)）

snapshot/rollback 模式：
1. `readCurrentStatus(id)` 从 currentDetail 或卡片 dataset 派生旧状态；幂等短路。
2. 快照 `{ id, oldStatus, newStatus, statsSnapshot: {从 DOM 抓 #status-count-* 文本}, detailRef: cache.detail.get(id) }`。
3. `applyStatusOptimistic(id, newStatus)`：复用现有 `updateCardStatus`（[L893](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L893)）+ patchDetailInCache + 本地计数增减 + 同步模态 status-picker active。
4. `await api(PATCH)`：成功 → 用权威数据覆盖缓存 + currentDetail；`loadStats()` 后台校正计数；若 `state.watchStatus` 非空且 ≠ newStatus 则 `invalidatePageCache` + `resetAndLoad`。失败 → `rollbackStatus(snapshot)` 反向操作 + 还原 DOM 计数文本 + toast 报错。
5. 冲突保护：`state.optimisticPending` 非空时同 id 点击忽略并 toast"操作进行中"。

## 分阶段实现

### 阶段 0 — 基线测量（不改代码）
用 `TRAE-browseruse` skill 访问 `http://192.168.31.3:8000`，DevTools Console 注入 Performance API 采集：TTFB / FCP / LCP / TTI / DOMContentLoaded / JS heap / 图片请求数 / 缓存命中率；用 `performance.mark/measure` 包裹"点卡片→模态渲染"、"切平台→首卡渲染"、"输入搜索→列表更新"、"点状态→toast"四条路径；rAF 计数滚动 FPS。基线数据记录到对话，不写文件。

### 阶段 1 — 触控目标 + 按钮布局（CSS 主导）
全部改动落在 `@media (max-width: 680px)`（[L741-818](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L741)）块内，不污染桌面端：
- `.filter-btn` 38→44px（[L771](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L771)）
- `.sort-order-btn` 38→44px（[L772](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L772)）
- `.surprise-btn` 32→44px（[L813](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L813)）
- `.library-tab` 42→44px（移动端覆盖）
- `.filter-select` 移动端补 `height:44px`
- `.status-menu-trigger` 38→44px（[L779](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L779)）
- `.status-menu button` min-height 34→44px（[L477](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L477) 桌面端也提升）
- `.clear-search` 扩点击区到 44×44：`min-width/min-height:44px` + `padding:9px` + `top:50%; transform:translateY(-50%)`，svg 仍 14px（[L172-178](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L172)）
- `.modal-nav` 40→44px（[L815](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/css/style.css#L815)）
- `.status-picker button` 移动端 36→44px

按钮层级：在 `:root` 加 `--btn-primary` / `--btn-secondary` 变量；主操作（`.card-main`、加入片单）保留 accent 权重，次操作维持低权重。补平板 768-1199px 区间：`.filter-controls` 单行防折行高低不齐。

验证：browser_use 在 390×844 视口实测每项目标 `getBoundingClientRect()` ≥44。版本号 css v26→v27（[index.html L11,L13](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/index.html#L11)）。

### 阶段 2 — 清理 added_date + 缓存层 + 详情预取（JS 主导）
清理 added_date：
- [index.html L118](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/index.html#L118) 删 `<option value="added_date">最近收录</option>`
- [app.js L345](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L345) `valid(...,['rating','release_date','added_date'],...)` 去掉 `added_date`
- [app.js L1038-1041](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/js/app.js#L1038) `updateFilterSummary` 删 added_date 分支
- [api.py L110](file:///c:/Users/mzj/Desktop/code/streaming-tracker/app/api.py#L110) pattern → `^(rating|release_date)$`

缓存层：按"核心架构"落地 `cache` 对象 + `pageCacheKey/getPage/setPage/invalidatePageCache/clearAllCaches/getDetail/setDetail/patchDetailInCache/revalidateDetail/prefetchDetail`。改造 `loadTitles`、`showDetail`、`surprisePick`。

详情预取：新增 `setupCardHoverPrefetch()`，在 `setupEvents` 内对 `.titles-grid` 用 mouseover 委托，hover 250ms 后 `prefetchDetail(id)`；仅 `matchMedia('(pointer: fine)')` 启用；mouseleave 清 setTimeout。命中后不重复请求。

风险：缓存陈旧配 stale-while-revalidate + sync 完成清缓存；预取流量用 250ms 阈值 + pointer:fine + 离开取消控制。验证：browser_use 实测切平台再切回应秒开且 Network 无新请求；hover 250ms 后离开再点详情秒开；`pytest tests/test_database.py` 确认 sort 仍正常。版本号 js v29→v30（[index.html L12,L182](file:///c:/Users/mzj/Desktop/code/streaming-tracker/static/index.html#L12)）。

### 阶段 3 — 乐观更新 + 滚动恢复 + 最近浏览
乐观更新：按"核心架构"替换 `setTitleStatus`。

滚动恢复：`updateUrl` 中 `history.replaceState({ scrollY }, ...)`；`loadTitles` 完成后若 `performance.navigation.type===back_forward` 且有待恢复 scrollY，rAF 内 `window.scrollTo`；用户主动改 filter 不恢复（用 `state.userInitiatedFilter` 标记区分）。

最近浏览：`showDetail` 写 sessionStorage `recent_viewed`（unshift {id,title,poster}，去重，截断 8 条，try/catch 静默失败）；在 `.results-bar` 上方加 `#recent-row` 横向滚动条（有数据才显示），点击调 `showDetail` 复用缓存；清空按钮 `data-action="clear-recent"`。CSS 新增 `.recent-row` / `.recent-chip`（60×90 海报 + 标题截断）。

验证：browser_use 实测点详情→返回列表滚动位置恢复；最近浏览条出现且可点击。版本号 css v27→v28，js v30→v31。

### 阶段 4 — 列表视图 + 键盘帮助浮层
列表视图（桌面端）：`.results-bar` 加 `.view-toggle`（`data-view="grid"`/`data-view="list"`）；state 加 `viewMode` + localStorage 持久化；`renderTitles` 按 viewMode 选 `createTitleCard` 或新增 `createTitleListItem`（横向：左海报 80×120，中标题/简介/元数据，右评分大字+平台+状态菜单）；CSS `.titles-grid[data-view="list"]` 单列 + `.title-card.is-list` 横向 flex，用属性选择器隔离不污染网格。

键盘帮助浮层：index.html 末尾加 `#shortcuts-overlay.hidden` 列出 `/`搜索、`R`随机、`?`帮助、`Esc`关闭、`←/→`详情导航、`G/L`视图切换；setupEvents keydown 加 `?` 显示、`Esc` 关闭、`G/L` 切视图。

验证：browser_use 桌面视口切视图、按 ? 弹浮层。版本号 css v28→v29，js v31→v32。

### 阶段 5 — docker-compose bind mount + 部署
[docker-compose.yml](file:///c:/Users/mzj/Desktop/code/streaming-tracker/docker-compose.yml) `volumes` 在 `./data:/app/data` 后加 `- ./static:/app/static:ro`。

部署步骤（iStoreOS ash，禁 `&&`，分命令执行）：
1. SSH `root@192.168.31.3`，`cd` 到项目目录（部署时先 `find / -name docker-compose.yml -path '*streaming*' 2>/dev/null` 定位）
2. `git pull`
3. `docker compose up -d --build`（Compose 自动重建变更容器，无需 down，避免停机；首次 build 因 Dockerfile `COPY . .` 已烤旧 static，bind mount 会以只读覆盖）
4. `docker compose ps` 确认 healthy
5. `docker exec streaming-tracker ls /app/static` 验证挂载生效

回滚：compose 改回 + `up -d`。后续前端迭代只需 `docker cp` 新 static 到宿主 `./static` + 浏览器强刷（版本号已 bump），无需重建。

### 阶段 6 — 复测 + 对比报告
browser_use 同阶段 0 指标复测，输出对比报告：
- 元信息：日期/URL/设备/网络/浏览器
- 加载性能表：TTFB / FCP / LCP / TTI / JS heap（基线 vs 优化后 vs 变化%）
- 交互性能表：详情延迟 / 筛选响应 / 状态切换响应 / 搜索响应 / 滚动 FPS
- 资源表：JS 传输字节 / CSS 传输字节 / 图片请求数 / 缓存命中率
- 功能核对表：触控达标率 / 列表视图 / 最近浏览 / 滚动恢复 / 键盘帮助 / 乐观更新回滚
- 异常项清单

## 版本号 bump 策略
`index.html` css 引用 2 处（L11 preload + L13 stylesheet）、js 引用 2 处（L12 preload + L182 script）。改 css 两处同步 +1，改 js 两处同步 +1，单调递增不复用。累计：阶段1 css v27；阶段2 js v30；阶段3 css v28 js v31；阶段4 css v29 js v32。

## 风险与回滚
| 风险 | 触发 | 回滚 |
|---|---|---|
| 缓存陈旧状态错位 | 多端操作 | stale-while-revalidate + sync 完成清缓存 + PATCH 后 patchDetailInCache |
| 乐观更新回滚不彻底 | snapshot 漏字段 | snapshot 含 DOM 计数文本 + currentDetail + cache 引用；rollback 反向 + loadStats 校正 |
| 移动端按钮变高溢出 | 阶段1 | 用 min-height 而非 height；单条 CSS 还原 |
| bind mount 路径错 | 阶段5 | 容器内 ls 验证；compose 改回 + up -d |
| added_date 旧书签 422 | 阶段2 | hydrateStateFromUrl 已 fallback release_date，不触发 |
| 列表视图污染网格 | 阶段4 | `[data-view="list"]` 属性选择器隔离 |
| 预取流量翻倍 | 阶段2 | 250ms 阈值 + pointer:fine + 离开取消 + 命中后不重复 |

## 验证清单
- `pytest tests/` 全绿（added_date 移除后 sort 仍正常）
- browser_use 桌面/平板/移动三视口走查：触控 ≥44px、列表视图、最近浏览、滚动恢复、键盘帮助、乐观更新成功/失败回滚
- 生产 192.168.31.3:8000 健康检查 200，bind mount 生效
- 性能对比报告各项指标达标或给出未达标说明
