"""个人偏好快照与逐地区观看 offer 的持久化辅助（供 titles/queries/backfill 共用）。"""

from app.db.utils import _utc_now

_SNAPSHOT_FIELDS = ("title", "original_title", "imdb_id", "poster_url", "release_date")


def upsert_preference_snapshot(cursor, tmdb_id, media_type, fields=None, now=None):
    """写入/更新偏好快照：目录存在时取目录快照，导入时可用备份字段补齐。

    只更新非空字段，避免用缺失数据覆盖已有快照；快照仅用于展示与恢复。
    """
    now = now or _utc_now()
    source = {key: (fields or {}).get(key) for key in _SNAPSHOT_FIELDS}
    cursor.execute("""
        INSERT INTO title_preference_snapshots
            (tmdb_id, type, title, original_title, imdb_id, poster_url,
             release_date, snapshot_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tmdb_id, type) DO UPDATE SET
            title=COALESCE(NULLIF(excluded.title, ''), title_preference_snapshots.title),
            original_title=COALESCE(NULLIF(excluded.original_title, ''),
                                    title_preference_snapshots.original_title),
            imdb_id=COALESCE(NULLIF(excluded.imdb_id, ''), title_preference_snapshots.imdb_id),
            poster_url=COALESCE(NULLIF(excluded.poster_url, ''),
                                title_preference_snapshots.poster_url),
            release_date=COALESCE(NULLIF(excluded.release_date, ''),
                                  title_preference_snapshots.release_date),
            snapshot_updated_at=excluded.snapshot_updated_at
    """, (tmdb_id, media_type, source["title"], source["original_title"],
          source["imdb_id"], source["poster_url"], source["release_date"], now))

    # 目录存在时以目录字段补全快照（导入字段优先，缺失部分回退目录）。
    cursor.execute("""
        INSERT INTO title_preference_snapshots
            (tmdb_id, type, title, original_title, imdb_id, poster_url,
             release_date, snapshot_updated_at)
        SELECT tmdb_id, type, title, original_title, imdb_id, poster_url,
               release_date, ?
        FROM titles
        WHERE tmdb_id=? AND type=?
          AND COALESCE(title, '') <> ''
        ON CONFLICT(tmdb_id, type) DO UPDATE SET
            title=COALESCE(NULLIF(title_preference_snapshots.title, ''), excluded.title),
            original_title=COALESCE(NULLIF(title_preference_snapshots.original_title, ''),
                                    excluded.original_title),
            imdb_id=COALESCE(NULLIF(title_preference_snapshots.imdb_id, ''), excluded.imdb_id),
            poster_url=COALESCE(NULLIF(title_preference_snapshots.poster_url, ''),
                                excluded.poster_url),
            release_date=COALESCE(NULLIF(title_preference_snapshots.release_date, ''),
                                  excluded.release_date),
            snapshot_updated_at=excluded.snapshot_updated_at
    """, (now, tmdb_id, media_type))


def touch_preference_snapshot(cursor, tmdb_id, media_type, now=None):
    """偏好写入后用目录数据补全快照（快照本身不会被目录变更清空）。"""
    upsert_preference_snapshot(cursor, tmdb_id, media_type, fields=None, now=now)


def write_provider_offers(cursor, title_id, offers, observed_at):
    """写入逐地区 offer；同一（平台、地区、观看方式）幂等 upsert。"""
    count = 0
    for offer in offers or []:
        provider_name = str(offer.get("provider_name") or "").strip()[:200]
        if not provider_name:
            continue
        try:
            provider_id = int(offer.get("provider_id") or 0)
        except (TypeError, ValueError):
            provider_id = 0
        provider_group = str(offer.get("provider_group") or "others").strip()[:40] or "others"
        region = str(offer.get("region") or "").strip().upper()[:2]
        monetization = str(offer.get("monetization") or "flatrate").strip()[:20] or "flatrate"
        cursor.execute("""
            INSERT INTO title_watch_offers
                (title_id, provider_id, provider_group, provider_name, region,
                 monetization, first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(title_id, provider_id, provider_name, region, monetization)
            DO UPDATE SET last_seen_at=excluded.last_seen_at, is_active=1,
                provider_group=excluded.provider_group
        """, (title_id, provider_id, provider_group, provider_name, region,
              monetization, observed_at, observed_at))
        count += 1
    return count
