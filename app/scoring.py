"""加权评分 SQL：查询排序与表达式索引共用同一份表达式，保证索引可被命中。

IMDb Top 250 同款贝叶斯加权：w = v/(v+m)·R + m/(v+m)·C
- 只用于排序；详情与卡片继续展示原始 imdb_rating；
- m=0 时退化为原始分排序；
- 无评分作品保持 NULL（配合 NULLS LAST 排到最后）。
"""

from app.config import RATING_PRIOR_MEAN, RATING_PRIOR_VOTES


def weighted_rating_sql(alias="t", null_fallback=None):
    """生成加权评分表达式。

    alias: 表别名前缀（查询用 "t"，表达式索引用 ""）；
    null_fallback: 无评分时的取值（片单路径传 -1 保持旧排序行为，默认 NULL）。
    """
    prefix = f"{alias}." if alias else ""
    null_value = "NULL" if null_fallback is None else str(null_fallback)
    votes = f"COALESCE({prefix}rating_votes, 0)"
    return (
        f"CASE WHEN {prefix}imdb_rating IS NULL THEN {null_value} ELSE"
        f" ({votes} * 1.0 / ({votes} + {RATING_PRIOR_VOTES})) * {prefix}imdb_rating"
        f" + ({RATING_PRIOR_VOTES} * 1.0 / ({votes} + {RATING_PRIOR_VOTES})) * {RATING_PRIOR_MEAN}"
        " END"
    )


def weighted_rating_index_expression():
    """表达式索引使用的无别名版本（必须与查询表达式同构）。"""
    return weighted_rating_sql(alias="")
