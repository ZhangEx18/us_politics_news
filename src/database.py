#!/usr/bin/env python3
"""SQLite 存储层：去重、增量更新、查询、幂等迁移"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo
from urls import normalize_url

LEGACY_STORAGE_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Article:
    url: str
    title: str
    summary: str
    source: str
    source_type: str
    published_at: Optional[datetime]
    fetched_at: datetime
    category: str = ""
    score: float = 0.0
    topic: str = ""
    reason: str = ""
    level: str = ""
    # v2 新增字段
    column: str = ""
    source_tier: int = 4
    event_key: str = ""
    source_url_normalized: str = ""
    llm_score: Optional[float] = None
    llm_summary: str = ""
    llm_tags: str = ""  # 逗号分隔
    llm_reason: str = ""


# v2 新增列列表（用于幂等迁移）
_V2_COLUMNS = [
    ("column", "TEXT DEFAULT ''"),
    ("source_tier", "INTEGER DEFAULT 4"),
    ("event_key", "TEXT DEFAULT ''"),
    ("source_url_normalized", "TEXT DEFAULT ''"),
    ("llm_score", "REAL"),
    ("llm_summary", "TEXT DEFAULT ''"),
    ("llm_tags", "TEXT DEFAULT ''"),
    ("llm_reason", "TEXT DEFAULT ''"),
]


def _to_utc_storage(dt: Optional[datetime]) -> str | None:
    """数据库统一存带 +00:00 的 UTC ISO，避免新写入数据再混入本地时区。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_utc_storage(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_naive_iso_datetime(value: str | None) -> bool:
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return False
    return dt.tzinfo is None


def _legacy_local_to_utc_storage(value: str | None) -> str | None:
    if not _is_naive_iso_datetime(value):
        return value
    local_dt = datetime.fromisoformat(value).replace(tzinfo=LEGACY_STORAGE_TZ)
    return _to_utc_storage(local_dt)


def article_to_content_item(article: "Article", url_hash_fn=None) -> "ContentItem":
    """将数据库 Article 转换为 ContentItem，消除 run_pipeline / run_weekly 的重复转换逻辑。"""
    from models import ContentItem, SourceType

    url_norm = article.source_url_normalized or ""
    if not url_norm and url_hash_fn:
        url_norm = normalize_url(article.url)

    return ContentItem(
        id=f"db:{url_hash_fn(normalize_url(article.url))}" if url_hash_fn else f"db:{article.url}",
        source_type=SourceType(article.source_type) if article.source_type else SourceType.RSS,
        title=article.title,
        url=article.url,
        content=article.summary or "",
        source_name=article.source,
        published_at=article.published_at,
        fetched_at=article.fetched_at,
        column=article.column or "",
        source_tier=article.source_tier or 4,
        event_key=article.event_key or "",
        source_url_normalized=url_norm,
        topic=article.topic or "",
        score=article.score or 0.0,
        reason=article.reason or "",
        level=article.level or "",
    )


class NewsDatabase:
    def __init__(self, db_path: str = "data/news.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()
        self._migrate_v2()
        self._migrate_legacy_datetimes()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url_hash TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    score REAL DEFAULT 0.0,
                    topic TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    level TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_fetched ON articles(fetched_at);
                CREATE INDEX IF NOT EXISTS idx_category ON articles(category);
                CREATE INDEX IF NOT EXISTS idx_source ON articles(source_type);
                CREATE INDEX IF NOT EXISTS idx_level ON articles(level);
                CREATE TABLE IF NOT EXISTS fetch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'ok'
                );
            """)

    def _migrate_v2(self):
        """幂等迁移：为旧数据库添加 v2 新列"""
        with self._connect() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
            for col_name, col_def in _V2_COLUMNS:
                if col_name not in existing:
                    conn.execute(f"ALTER TABLE articles ADD COLUMN {col_name} {col_def}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_column ON articles(\"column\")")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_key ON articles(event_key)")

    def _migrate_legacy_datetimes(self):
        """把旧版本写入的本地 naive 时间迁移为显式 UTC ISO。"""
        with self._connect() as conn:
            article_rows = conn.execute(
                "SELECT id, published_at, fetched_at FROM articles"
            ).fetchall()
            for row in article_rows:
                published_at = _legacy_local_to_utc_storage(row["published_at"])
                fetched_at = _legacy_local_to_utc_storage(row["fetched_at"])
                if published_at != row["published_at"] or fetched_at != row["fetched_at"]:
                    conn.execute(
                        "UPDATE articles SET published_at = ?, fetched_at = ? WHERE id = ?",
                        (published_at, fetched_at, row["id"]),
                    )
            fetch_log_rows = conn.execute(
                "SELECT id, fetched_at FROM fetch_log"
            ).fetchall()
            for row in fetch_log_rows:
                fetched_at = _legacy_local_to_utc_storage(row["fetched_at"])
                if fetched_at != row["fetched_at"]:
                    conn.execute(
                        "UPDATE fetch_log SET fetched_at = ? WHERE id = ?",
                        (fetched_at, row["id"]),
                    )

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def url_exists(self, url: str) -> bool:
        """查询 URL 是否已存在（只读辅助，不参与写入判定）"""
        normalized = normalize_url(url)
        h = self.url_hash(normalized)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE url_hash = ?",
                (h,),
            ).fetchone()
            return row is not None

    def insert(self, article: Article) -> bool:
        """单条插入，依赖 url_hash UNIQUE 索引幂等"""
        normalized = normalize_url(article.url)
        h = self.url_hash(normalized)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO articles
                (url_hash, url, title, summary, source, source_type,
                 published_at, fetched_at, category, score, topic, reason, level,
                 "column", source_tier, event_key, source_url_normalized,
                 llm_score, llm_summary, llm_tags, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    h, article.url, article.title, article.summary,
                    article.source, article.source_type,
                    _to_utc_storage(article.published_at),
                    _to_utc_storage(article.fetched_at),
                    article.category, article.score, article.topic,
                    article.reason, article.level,
                    article.column, article.source_tier,
                    article.event_key, article.source_url_normalized,
                    article.llm_score, article.llm_summary,
                    article.llm_tags, article.llm_reason,
                ),
            )
            return conn.total_changes > 0

    def insert_many(self, articles: List[Article]) -> int:
        """单连接、单事务批量插入，依赖 url_hash UNIQUE 索引幂等"""
        if not articles:
            return 0
        rows = []
        for article in articles:
            normalized = normalize_url(article.url)
            h = self.url_hash(normalized)
            rows.append((
                h, article.url, article.title, article.summary,
                article.source, article.source_type,
                _to_utc_storage(article.published_at),
                _to_utc_storage(article.fetched_at),
                article.category, article.score, article.topic,
                article.reason, article.level,
                article.column, article.source_tier,
                article.event_key, article.source_url_normalized,
                article.llm_score, article.llm_summary,
                article.llm_tags, article.llm_reason,
            ))
        with self._connect() as conn:
            cursor = conn.executemany(
                """INSERT OR IGNORE INTO articles
                (url_hash, url, title, summary, source, source_type,
                 published_at, fetched_at, category, score, topic, reason, level,
                 "column", source_tier, event_key, source_url_normalized,
                 llm_score, llm_summary, llm_tags, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return cursor.rowcount

    def fetch_since(self, since: datetime) -> List[Article]:
        cutoff = _to_utc_storage(since)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (cutoff,),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_today(self) -> List[Article]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (today,),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_by_column(self, column: str, since: datetime) -> List[Article]:
        cutoff = _to_utc_storage(since)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE "column" = ? AND fetched_at >= ?
                ORDER BY llm_score DESC, score DESC, fetched_at DESC""",
                (column, cutoff),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_by_category(self, category: str, days: int = 1) -> List[Article]:
        cutoff = _to_utc_storage(datetime.now(timezone.utc) - timedelta(days=days))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE category = ? AND fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (category, cutoff),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def get_stats(self, days: int = 1) -> dict:
        cutoff = _to_utc_storage(datetime.now(timezone.utc) - timedelta(days=days))
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE fetched_at >= ?", (cutoff,)
            ).fetchone()[0]
            by_source = conn.execute(
                """SELECT source_type, COUNT(*) as cnt
                FROM articles WHERE fetched_at >= ?
                GROUP BY source_type ORDER BY cnt DESC""",
                (cutoff,),
            ).fetchall()
            by_column = conn.execute(
                """SELECT "column", COUNT(*) as cnt
                FROM articles WHERE fetched_at >= ? AND "column" != ''
                GROUP BY "column" ORDER BY cnt DESC""",
                (cutoff,),
            ).fetchall()
        return {
            "total": total,
            "by_source": dict(by_source),
            "by_column": dict(by_column),
        }

    def log_fetch(self, source: str, count: int, status: str = "ok"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO fetch_log (source, fetched_at, count, status) VALUES (?, ?, ?, ?)",
                (source, _to_utc_storage(datetime.now(timezone.utc)), count, status),
            )

    def cleanup_old(self, days: int = 30):
        cutoff = _to_utc_storage(datetime.now(timezone.utc) - timedelta(days=days))
        with self._connect() as conn:
            conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
            conn.execute("DELETE FROM fetch_log WHERE fetched_at < ?", (cutoff,))

    def update_llm_scores(self, scored_items: list[dict]) -> int:
        """批量更新 LLM 评分，直接消费 score_batch 返回的 dict 列表"""
        count = 0
        with self._connect() as conn:
            for item in scored_items:
                link = item.get("link", "")
                if not link:
                    continue
                normalized = normalize_url(link)
                h = self.url_hash(normalized)
                tags = item.get("tags", [])
                tags_str = ",".join(tags) if isinstance(tags, list) else str(tags)
                cursor = conn.execute(
                    """UPDATE articles
                    SET llm_score = ?, llm_summary = ?, llm_tags = ?,
                        "column" = ?, event_key = ?, source_tier = ?
                    WHERE url_hash = ?""",
                    (
                        item.get("score"),
                        item.get("summary", ""),
                        tags_str,
                        item.get("column", ""),
                        item.get("event_key", ""),
                        item.get("source_tier", 4),
                        h,
                    ),
                )
                count += cursor.rowcount
        return count

    def update_scores(self, scored_articles: list) -> int:
        count = 0
        with self._connect() as conn:
            for article in scored_articles:
                normalized = normalize_url(article.url)
                h = self.url_hash(normalized)
                cursor = conn.execute(
                    """UPDATE articles
                    SET score = ?, topic = ?, reason = ?, level = ?
                    WHERE url_hash = ?""",
                    (article.score, article.topic, article.reason, article.level, h),
                )
                count += cursor.rowcount
        return count

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> Article:
        return Article(
            url=row["url"],
            title=row["title"],
            summary=row["summary"] or "",
            source=row["source"],
            source_type=row["source_type"],
            published_at=_from_utc_storage(row["published_at"]),
            fetched_at=_from_utc_storage(row["fetched_at"]) or datetime.now(timezone.utc),
            category=row["category"] or "",
            score=row["score"] or 0.0,
            topic=row["topic"] or "",
            reason=row["reason"] or "",
            level=row["level"] or "",
            column=row["column"] or "",
            source_tier=row["source_tier"] or 4,
            event_key=row["event_key"] or "",
            source_url_normalized=row["source_url_normalized"] or "",
            llm_score=row["llm_score"],
            llm_summary=row["llm_summary"] or "",
            llm_tags=row["llm_tags"] or "",
            llm_reason=row["llm_reason"] or "",
        )
