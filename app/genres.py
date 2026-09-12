"""题材归一：把 TMDB 英文/混合题材名映射为中文标签，供同步、回填与查询共用。

约定：
- 中文标签是唯一规范值（与现有库数据一致）；
- 一个别名可以展开为多个规范题材（如 "Sci-Fi & Fantasy" → 科幻 + 奇幻）；
- 查询时把规范题材反向展开为全部别名，兼容尚未回填的历史数据。
"""

from __future__ import annotations

# 别名 → 规范题材（可一对多）。键为 TMDB 常见英文名或历史混合名。
GENRE_ALIASES: dict[str, list[str]] = {
    "Action": ["动作"],
    "Action & Adventure": ["动作", "冒险"],
    "动作冒险": ["动作", "冒险"],
    "Adventure": ["冒险"],
    "Animation": ["动画"],
    "Comedy": ["喜剧"],
    "Crime": ["犯罪"],
    "Documentary": ["纪录"],
    "Drama": ["剧情"],
    "Family": ["家庭"],
    "Fantasy": ["奇幻"],
    "History": ["历史"],
    "Horror": ["恐怖"],
    "Kids": ["儿童"],
    "Music": ["音乐"],
    "Mystery": ["悬疑"],
    "Reality": ["真人秀"],
    "Romance": ["爱情"],
    "Science Fiction": ["科幻"],
    "Sci-Fi & Fantasy": ["科幻", "奇幻"],
    "Soap": ["肥皂剧"],
    "Talk": ["脱口秀"],
    "TV Movie": ["电视电影"],
    "Thriller": ["惊悚"],
    "War": ["战争"],
    "War & Politics": ["战争"],
    "Western": ["西部"],
}

# TMDB 剧集题材偶尔带 & 前缀（如 "& Fantasy"），先剥掉再查表
_STRIP_PREFIXES = ("&",)


def normalize_genre(name) -> list[str]:
    """把单个题材名归一化为规范中文题材列表（未知名称原样保留）。"""
    value = str(name or "").strip()
    if not value:
        return []
    aliases = GENRE_ALIASES.get(value)
    if aliases:
        return list(aliases)
    stripped = value.lstrip("".join(_STRIP_PREFIXES)).strip()
    if stripped and stripped != value and stripped in GENRE_ALIASES:
        return list(GENRE_ALIASES[stripped])
    return [value]


def normalize_genres(values) -> list[str]:
    """把题材列表归一化为去重后的规范题材列表，保持出现顺序。"""
    result: list[str] = []
    for value in values or []:
        for genre in normalize_genre(value):
            if genre not in result:
                result.append(genre)
    return result


# 规范题材 → 全部别名（含自身），用于兼容未回填的历史数据
GENRE_QUERY_VARIANTS: dict[str, list[str]] = {}
for _alias, _canonical_list in GENRE_ALIASES.items():
    for _canonical in _canonical_list:
        variants = GENRE_QUERY_VARIANTS.setdefault(_canonical, [_canonical])
        if _alias not in variants:
            variants.append(_alias)


def genre_query_variants(name) -> list[str]:
    """查询时展开为全部等价写法；未知题材返回原名。"""
    value = str(name or "").strip()
    if not value:
        return []
    return GENRE_QUERY_VARIANTS.get(value, [value])
