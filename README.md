# 片流 · STREAM INDEX

聚合海外流媒体高分新片，帮中文用户快速挑选值得看、在目标地区有观看渠道的影视作品，并维护个人观影安排。

## 启动

```bash
# 本地
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Docker
docker compose up -d --build
```

浏览器打开 `http://localhost:8000`。

## 配置

复制 `.env.example` 为 `.env` 后修改。关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TMDB_API_KEY` | 空 | 内容抓取与 IMDb 补录必需；缺失时同步与补录返回明确错误 |
| `SYNC_ENABLED` | `true`（代码默认） | compose 默认 `false`：部署不自动同步，本地运行默认开启。两处不一致是刻意的：本地首次运行需要建库，部署由外部调度触发 |
| `DATABASE_URL` | `data/tracker.db` | SQLite 路径；测试用临时文件覆盖此变量 |
| `MIN_IMDB_VOTES` / `MIN_IMDB_VOTES_GRACE` | `1000` / `20` | 可信评分票数门槛；宽限期内（`NEW_TITLE_GRACE_DAYS`，默认 30 天）新片用宽松门槛 |
| `TRACKER_DATA_DIR` | `./data` | compose 数据卷宿主机目录 |

## 数据来源

- 作品元数据：TMDB；评分：IMDb（含 OMDb 回填）。
- 观看渠道：TMDB 观看信息聚合，**只在作品详情中展示**（列表不再按平台筛选）。
  六大主流平台显示中文品牌名；其他平台显示具体名称（如 MUBI、爱奇艺），
  附覆盖地区与最近核验时间。历史数据的具体名称会在下次同步命中同一渠道时自动回填，
  回填前显示“其他平台”。TMDB 观看指南链接仅作指南，不拼凑平台直达链接。

## 备份与恢复

- 顶部“导出片单”下载 JSON（含 `schema_version`、稳定 identity `tmdb_id + type` 与作品快照）。
- “恢复片单”选择备份文件：先预览新增 / 已存在 / 无效条目，确认后按 identity 幂等合并，可重复执行。偏好独立于作品表存储，重建目录不会丢失片单。
- SQLite 在线备份建议用 `sqlite3 data/tracker.db ".backup 'tracker-backup.db'"`，恢复前先停服务并演练一次。

## 常见故障

| 现象 | 处理 |
|---|---|
| 平台筛选显示失败 | 基础平台入口仍可用，点“重试”单独恢复；不影响主列表 |
| 详情打不开 | 点“重新加载”会发新请求；hover 预取失败不会阻断点击 |
| 片单数量刚改又变回去 | 已修统计缓存失效；若复现请附 `PATCH` 与 `GET /api/stats` 顺序提 issue |
| 首屏无内容 | 空库且同步中会提示“正在建立内容库”并自动刷新；同步禁用时需手动触发或导入 |

## 回归测试

```bash
.venv/Scripts/python tests/test_review_regressions.py
```

覆盖：片单计数与列表一致、统计缓存失效、过期收藏可访问/可移除、备份恢复往返、题材/时长筛选、搜索精确优先、隐藏已看、近期新片、个人记录、批量操作。前端改动用 `node --check static/js/app.js` 校验。

## 性能基线

```bash
# 对运行中的实例采样（warmup 3 次 + 计时 20 次，输出 Markdown）
python scripts/bench_api.py --base-url http://127.0.0.1:8000 --iterations 20 \
    --markdown docs/benchmarks/2026-09-11.md
```

结果记录在 `docs/benchmarks/`，包含各端点 p50 / p95 / 均值 / 最大值。
列表接口的过滤总数带 60 秒进程内缓存（写入时失效），首次请求约 80–115ms，
缓存命中后约 10ms；变更后端查询后建议重跑一次对比。

## 渠道核验与回填

```bash
# 先看量（dry-run 不写库）
python -m app.backfill_channels --limit 200 --newest-first --dry-run
# 分批核验具体平台名称与覆盖地区（可断点续跑）
python -m app.backfill_channels --limit 500 --concurrency 5 --newest-first
```

核验语义（7.5）：
- **只有单片核验成功后才发布否定结论**：请求成功时 upsert 本次命中的渠道，
  并把该片本次未出现的活跃行标记失效；请求失败不写库、不改动任何行；
- 同步（`persist_sync_batch`）只做渠道合并 upsert，不再按时钟全局过期，
  避免长期未被同步覆盖的老片被误停用；
- 无论是否命中渠道都记录 `providers_checked_at`（空结果 30 天内不重复请求）；
- 候选集同时覆盖“未标注渠道”和“已无活跃渠道（可能被旧策略误停用）”的作品。
  本库当前约 2 万条候选、其中约 858 部零活跃渠道，可分批执行或挂 cron。
