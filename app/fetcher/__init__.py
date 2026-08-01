"""app.fetcher 包：外部数据获取层。

按职责拆分自原 app/fetcher.py：
- common: 共享常量与工具（正则、翻译、海报、日期窗口、候选合并、统计）
- tmdb: TMDB 基础 API 调用（fetch_tmdb）
- ratings: IMDb 评分获取（OMDb API + 本地 IMDb 数据集）
- providers: 平台发现（按提供商/地区 discover）
- discover: 聚合发现入口（IMDb 单条导入、全平台发现编排）
- enrich: 作品富化与整体编排（fetch_all_providers / enrich_titles 等）
"""

from app.fetcher.common import (
    TRUSTED_RATING_SOURCES,
    ExternalRequestError,
    empty_fetch_stats,
    merge_fetch_stats,
    normalize_imdb_id,
    translate_to_chinese,
)
from app.fetcher.discover import discover_all_providers, discover_imdb_title
from app.fetcher.enrich import (
    enrich_titles,
    enrich_with_imdb,
    fetch_all_providers,
    fetch_provider_titles,
)
from app.fetcher.providers import (
    discover_provider,
    discover_unconfigured_providers,
)
from app.fetcher.ratings import get_imdb_rating, get_imdb_ratings
from app.fetcher.tmdb import fetch_tmdb
