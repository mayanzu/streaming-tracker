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
| `RATING_PRIOR_VOTES` / `RATING_PRIOR_MEAN` | `3000` / `7.5` | IMDb Top 250 同款贝叶斯加权先验；只影响“评分最高”排序，展示仍是原始分；`RATING_PRIOR_VOTES=0` 退化为原始分排序 |
| `TRANSLATE_PROVIDER` | `google` | 中文翻译源：`google` / `deepl` / `none`；`none` 时不翻译并把 zh_quality 标为 none |
| `DEEPL_API_KEY` | 空 | `TRANSLATE_PROVIDER=deepl` 时必填 |
| `TRACKER_DATA_DIR` | `./data` | compose 数据卷宿主机目录 |

## 界面约定

- 发现页默认按加权评分排序（“综合口碑”：结合 IMDb 评分与评价人数，结果栏附一句解释）；
  “高分精选”模式 = 评分 7.5+ 的同一加权排序；
  “近期新片”支持 30 / 90 / 180 天窗口，窗口为闭区间 `[起始日, 今天]`，不再返回未来上映作品；
  原“近期上映”一级 tab 已并入该模式，旧链接 `?view=releases` 会自动重定向为近 30 天新片。
- 首屏只保留标题与一句说明，收录统计移至主导航计数与页脚；“最近浏览”默认折叠为一行。
- 题材筛选来自数据（`/api/stats.genres`），按作品数排序、支持多选（并集）；英文剧集题材会归并为中文
  （如 `Sci-Fi & Fantasy` → 科幻 / 奇幻），旧数据可用 `python -m app.backfill_genres` 回填。
  年代、时长、产地与最低评分收在“更多条件”中（手机筛选抽屉可折叠），底部主动作显示真实结果数。
- 手机端筛选抽屉为单列分组，底部固定“重置 / 查看结果（N 部）”。
- 管理菜单 → “数据状态”提供应用内面板：先给“当前状态”结论，再列最近同步、评分库时效、
  渠道核验进度（近 30 天已核验 / 未核验 / 确认暂无渠道）、中文资料待回填数量；技术细节可展开，
  原始 JSON 保留在 `/ready`（含 `data_health` 摘要）。
- 详情渠道按“平台 × 地区 × 观看方式”展示：超过 2 个地区折叠为“查看全部 N 个地区”可点击展开；
  无渠道时区分“已核验暂无”与“尚未核验”，并给最近核验时间。

## 数据来源

- 作品元数据：TMDB；评分：IMDb（含 OMDb 回填）。
- 中文资料质量：详情页区分“已补全 / 简介机器翻译 / 仅片名中文 / 暂无中文”，
  旧数据没有质量标记时按未标注处理，下次同步或回填后补全。
- 观看渠道：TMDB 观看信息按「平台 × 地区 × 观看方式」逐条存储，**只在作品详情中展示**（列表不按平台筛选）。
  六大主流平台显示中文品牌名；其他平台显示具体名称（如 MUBI、爱奇艺）。
  选择观看地区后只展示该地区已核验的 offer；历史遗留数据无法还原跨地区对应关系，
  会标注“待重新核验”，重新核验后自动替换。TMDB 观看指南链接仅作指南，不拼凑平台直达链接。

## 备份与恢复

- 顶部“导出片单”下载 JSON（`schema_version: 2`，含稳定 identity `tmdb_id + type`、个人记录与作品快照）。
- “恢复片单”选择备份文件：后端统一校验并预览新增 / 更新 / 无变化 / 保护较新记录 / 无效 / 重复，
  确认后按 identity 幂等合并，可重复执行。默认保留本地较新的个人记录，可选择以备份覆盖。
  目录缺失的条目会先恢复个人记录并显示“资料待补全”，补全 TMDB 资料后自动关联，不会丢失。
- SQLite 在线备份建议用 `sqlite3 data/tracker.db ".backup 'tracker-backup.db'"`，恢复前先停服务并演练一次。

## 常见故障

| 现象 | 处理 |
|---|---|
| 详情打不开 | 点“重新加载”会发新请求；hover 预取失败不会阻断点击 |
| 片单数量刚改又变回去 | 已修统计缓存失效；若复现请附 `PATCH` 与 `GET /api/stats` 顺序提 issue |
| 片单出现“资料待补全” | 该条目所属作品暂不在目录中；个人记录已保存，可在卡片中重试补全或移除 |
| 首屏无内容 | 空库且同步中会提示“正在建立内容库”并自动刷新；同步禁用时需手动触发或导入 |
| 离线/服务不可达时数据偏旧 | 页面会提示数据来自缓存及获取时间；恢复联网后自动重取当前列表 |

## 回归测试

```bash
python -m pytest tests/test_review_regressions.py -q
# 无 pytest 时也可直接运行（内置 __main__ 收集器）
python tests/test_review_regressions.py
```

覆盖：片单计数与列表一致、统计缓存失效、过期收藏可访问/可移除、空目录恢复与快照往返、
导入计数与本地记录保护、备份恢复往返、逐地区观看 offer 隔离与核验否定、
题材/时长/年代筛选与题材别名并集、加权评分排序、推荐打分多样性与无外网快速返回、
IMDb 精确检索、近期上映时间边界、隐藏已看、近期新片、个人记录、批量操作、
中文资料质量标注与 CJK 占比判断。前端改动用 `node --check static/js/app.js` 与
`node --check static/sw.js` 校验；样式改动用 `python scripts/check_css_tokens.py` 守门。

## 性能基线

```bash
# 对运行中的实例采样（warmup 3 次 + 计时 20 次，输出 Markdown）
python scripts/bench_api.py --base-url http://127.0.0.1:8000 --iterations 20 \
    --markdown docs/benchmarks/2026-09-11.md
```

结果记录在 `docs/benchmarks/`，包含各端点 p50 / p95 / 均值 / 最大值。
列表接口的过滤总数带 60 秒进程内缓存（写入时失效），首次请求约 80–115ms，
缓存命中后约 10ms；变更后端查询后建议重跑一次对比。
“评分最高”排序使用表达式索引 `idx_titles_weighted_rating`（启动时自动创建，
`RATING_PRIOR_VOTES/MEAN` 变化时自动重建），评分排序不再做全表扫描 + 临时排序。

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
- 定时任务已接入：每夜同步后一小时自动回填 500 部（`providers_checked_at` 缺失或超过 45 天）。

## 数据回填与维护

```bash
# 中文资料质量标记（zh_quality / overview_cjk_ratio）
python -m app.backfill_zh_quality [--dry-run]

# 无中文片名 / 中英混排简介重译（遵守 TRANSLATE_PROVIDER；none 时只标注）
python -m app.backfill_translations --titles --overviews --concurrency 4

# 英文题材名归并为中文规范值
python -m app.backfill_genres [--dry-run]

# 日志与无界数据清理（availability 删除前会校验可见作品数不变）
python -m app.maintenance prune-errors
python -m app.maintenance prune-pending --days 180 --dry-run
python -m app.maintenance prune-availability --dry-run
python -m app.maintenance vacuum          # 需停服务

# CSS 设计令牌守门（新增裸色值/字号会失败；清理完成后可 --update-baseline）
python scripts/check_css_tokens.py
```

## 发布

```bash
# 同步 index.html / sw.js 的资源版本号，写入 data/version.json（/ready、/api/stats 会暴露）
python scripts/release.py

# 部署完成后核对线上版本（对比 /ready.app_version、/sw.js、首页资源版本）
python scripts/release.py --check-only --check-url http://192.168.31.3:8000
```

`scripts/release.py` 默认以 `APP_VERSION` 环境变量或现有 `data/version.json` 的版本号发布，
用 git short sha 作为 build id 与资源版本；三条核对全部 PASS 才算发布完成。
