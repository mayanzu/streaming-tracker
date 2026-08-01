"""app.db 包：SQLite 数据访问层。

按职责拆分自原 app/database.py：
- connection: 连接与全局查询条件常量
- utils: 时间戳/评分/国家代码归一化、重试间隔
- schema: 建表、补列、索引与历史迁移（init_db）
- titles: 作品写入（insert_title 等）与缓存读取
- queries: 只读查询（列表/详情/统计/健康检查）
- pending: 待处理重试队列与同步批量落库（persist_sync_batch）
- sync_runs: 同步运行记录
"""

from app.db.connection import (
    TRUSTED_RATING_CONDITION,
    TRUSTED_RATING_CONDITION_T,
    UNTRUSTED_RATING_CONDITION,
    get_db,
    get_db_connection,
)
from app.db.pending import (
    claim_catalog_window,
    get_due_pending_titles,
    persist_sync_batch,
)
from app.db.queries import (
    check_database,
    count_titles,
    count_untrusted_titles,
    get_providers,
    get_stats,
    get_title_detail,
    get_titles,
    get_titles_missing_countries,
    purge_all_titles,
    purge_untrusted_titles,
    update_title_status,
)
from app.db.schema import init_db
from app.db.sync_runs import (
    create_sync_run,
    finish_sync_run,
    get_latest_finished_sync_run,
    get_latest_sync_run,
    mark_sync_run_abandoned,
    record_sync_error,
    update_sync_run_progress,
)
from app.db.titles import (
    get_title_cache,
    insert_title,
    persist_title_countries,
    update_title_imdb_id,
)
