#!/usr/bin/env python3
"""SQLite 存储层：去重、增量更新、查询、幂等迁移"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from collections import defaultdict
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


@dataclass
class ArticleCandidate:
    report_key: str
    report_type: str
    url: str
    title: str
    source: str
    column: str
    candidate_score: float
    source_tier: int
    reason: str = ""
    status: str = "pending"
    event_key: str = ""
    published_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    source_url_normalized: str = ""
    freshness_date: str = ""
    event_date: str = ""
    freshness_status: str = ""


@dataclass
class ReportEvent:
    report_key: str
    report_type: str
    event_key: str
    column: str
    title_zh: str
    summary_zh: str
    score: float
    source_links: list[dict]
    quality_status: str = "ok"
    tags: str = ""
    published_at: Optional[datetime] = None
    freshness_date: str = ""
    event_date: str = ""
    freshness_status: str = ""


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

_REPORT_LAYER_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "article_candidates": [
        ("freshness_date", "TEXT DEFAULT ''"),
        ("event_date", "TEXT DEFAULT ''"),
        ("freshness_status", "TEXT DEFAULT ''"),
    ],
    "report_events": [
        ("freshness_date", "TEXT DEFAULT ''"),
        ("event_date", "TEXT DEFAULT ''"),
        ("freshness_status", "TEXT DEFAULT ''"),
    ],
}


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
    def __init__(self, db_path: str = "data/products/news/news.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()
        self._migrate_v2()
        self._migrate_report_layers()
        self._migrate_legacy_datetimes()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def article_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])

    def source_health_rows(
        self,
        window_since: datetime | None = None,
        window_days: int = 30,
        source_defs: list[dict] | None = None,
    ) -> list[dict]:
        """汇总源健康数据：按 source 聚合最近窗口的入库量、评分量与栏目分布。"""
        if window_since is None:
            window_since = datetime.now(timezone.utc) - timedelta(days=window_days)
        cutoff = _to_utc_storage(window_since)
        source_meta = {
            str(item.get("name") or "").strip(): dict(item)
            for item in (source_defs or [])
            if str(item.get("name") or "").strip()
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source,
                    source_type,
                    source_tier,
                    "column",
                    COUNT(*) AS article_count,
                    SUM(CASE WHEN llm_score IS NOT NULL THEN 1 ELSE 0 END) AS scored_count,
                    SUM(CASE WHEN llm_score IS NOT NULL AND llm_score >= 65 THEN 1 ELSE 0 END) AS strong_scored_count,
                    COUNT(DISTINCT substr(COALESCE(published_at, fetched_at), 1, 10)) AS active_days,
                    MAX(COALESCE(published_at, fetched_at)) AS latest_seen_at
                FROM articles
                WHERE COALESCE(published_at, fetched_at) >= ?
                GROUP BY source, source_type, source_tier, "column"
                ORDER BY article_count DESC, source ASC, "column" ASC
                """,
                (cutoff,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            row_dict = dict(row)
            meta = source_meta.get(str(row_dict.get("source") or "").strip(), {})
            row_dict["fetch_mode"] = meta.get("fetch_mode") or row_dict.get("source_type") or "rss"
            row_dict["configured_column"] = meta.get("column", row_dict.get("column") or "")
            row_dict["configured_source_tier"] = int(meta.get("source_tier") or row_dict.get("source_tier") or 4)
            row_dict["source_enabled"] = bool(meta.get("enabled", True))
            result.append(row_dict)
        return result

    def fetch_log_rows(self, window_since: datetime | None = None, window_days: int = 30) -> list[dict]:
        """返回抓取日志汇总，供健康报告使用。"""
        if window_since is None:
            window_since = datetime.now(timezone.utc) - timedelta(days=window_days)
        cutoff = _to_utc_storage(window_since)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, status, count, fetched_at
                FROM fetch_log
                WHERE fetched_at >= ?
                ORDER BY fetched_at DESC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_log_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0])

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
                CREATE TABLE IF NOT EXISTS article_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_key TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    url_hash TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    "column" TEXT DEFAULT '',
                    candidate_score REAL DEFAULT 0.0,
                    source_tier INTEGER DEFAULT 4,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    event_key TEXT DEFAULT '',
                    published_at TEXT,
                    fetched_at TEXT,
                    source_url_normalized TEXT DEFAULT '',
                    freshness_date TEXT DEFAULT '',
                    event_date TEXT DEFAULT '',
                    freshness_status TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(report_key, report_type, url_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_report
                    ON article_candidates(report_type, report_key, "column", candidate_score);
                CREATE TABLE IF NOT EXISTS report_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_key TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    "column" TEXT DEFAULT '',
                    title_zh TEXT NOT NULL,
                    summary_zh TEXT DEFAULT '',
                    score REAL DEFAULT 0.0,
                    source_links_json TEXT DEFAULT '[]',
                    quality_status TEXT DEFAULT 'ok',
                    tags TEXT DEFAULT '',
                    published_at TEXT,
                    freshness_date TEXT DEFAULT '',
                    event_date TEXT DEFAULT '',
                    freshness_status TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(report_key, report_type, event_key, "column")
                );
                CREATE INDEX IF NOT EXISTS idx_report_events_window
                    ON report_events(report_type, report_key, "column", score);
                CREATE TABLE IF NOT EXISTS report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_key TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    window_since TEXT,
                    window_until TEXT,
                    input_count INTEGER DEFAULT 0,
                    candidate_count INTEGER DEFAULT 0,
                    selected_count INTEGER DEFAULT 0,
                    ai_duration_seconds REAL DEFAULT 0.0,
                    error_count INTEGER DEFAULT 0,
                    output_md_path TEXT DEFAULT '',
                    output_html_path TEXT DEFAULT '',
                    metrics_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_report_runs
                    ON report_runs(report_type, report_key, created_at);
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

    def _migrate_report_layers(self):
        """幂等迁移：为候选池和事件库补充今日性字段。"""
        with self._connect() as conn:
            for table, columns in _REPORT_LAYER_COLUMNS.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_name, col_def in columns:
                    if col_name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

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

    def upsert_article_candidates(self, candidates: list[ArticleCandidate]) -> int:
        if not candidates:
            return 0
        now = _to_utc_storage(datetime.now(timezone.utc))
        rows = []
        for candidate in candidates:
            normalized = candidate.source_url_normalized or normalize_url(candidate.url)
            rows.append((
                candidate.report_key,
                candidate.report_type,
                self.url_hash(normalized),
                candidate.url,
                candidate.title,
                candidate.source,
                candidate.column,
                candidate.candidate_score,
                candidate.source_tier,
                candidate.reason,
                candidate.status,
                candidate.event_key,
                _to_utc_storage(candidate.published_at),
                _to_utc_storage(candidate.fetched_at),
                normalized,
                candidate.freshness_date,
                candidate.event_date,
                candidate.freshness_status,
                now,
            ))
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT INTO article_candidates
                (report_key, report_type, url_hash, url, title, source, "column",
                 candidate_score, source_tier, reason, status, event_key,
                 published_at, fetched_at, source_url_normalized,
                 freshness_date, event_date, freshness_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_key, report_type, url_hash) DO UPDATE SET
                    title = excluded.title,
                    source = excluded.source,
                    "column" = excluded."column",
                    candidate_score = excluded.candidate_score,
                    source_tier = excluded.source_tier,
                    reason = excluded.reason,
                    status = excluded.status,
                    event_key = excluded.event_key,
                    published_at = excluded.published_at,
                    fetched_at = excluded.fetched_at,
                    source_url_normalized = excluded.source_url_normalized,
                    freshness_date = excluded.freshness_date,
                    event_date = excluded.event_date,
                    freshness_status = excluded.freshness_status""",
                rows,
            )
            return conn.total_changes - before

    def fetch_article_candidates(
        self,
        report_key: str,
        report_type: str = "daily",
        status: str | None = None,
    ) -> list[ArticleCandidate]:
        sql = """SELECT * FROM article_candidates
                 WHERE report_key = ? AND report_type = ?"""
        params: list[object] = [report_key, report_type]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += ' ORDER BY "column", candidate_score DESC, source_tier ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            ArticleCandidate(
                report_key=row["report_key"],
                report_type=row["report_type"],
                url=row["url"],
                title=row["title"],
                source=row["source"],
                column=row["column"] or "",
                candidate_score=row["candidate_score"] or 0.0,
                source_tier=row["source_tier"] or 4,
                reason=row["reason"] or "",
                status=row["status"] or "pending",
                event_key=row["event_key"] or "",
                published_at=_from_utc_storage(row["published_at"]),
                fetched_at=_from_utc_storage(row["fetched_at"]),
                source_url_normalized=row["source_url_normalized"] or "",
                freshness_date=row["freshness_date"] or "",
                event_date=row["event_date"] or "",
                freshness_status=row["freshness_status"] or "",
            )
            for row in rows
        ]

    def upsert_report_events(self, events: list[ReportEvent]) -> int:
        if not events:
            return 0
        now = _to_utc_storage(datetime.now(timezone.utc))
        rows = [
            (
                event.report_key,
                event.report_type,
                event.event_key,
                event.column,
                event.title_zh,
                event.summary_zh,
                event.score,
                json.dumps(event.source_links, ensure_ascii=False),
                event.quality_status,
                event.tags,
                _to_utc_storage(event.published_at),
                event.freshness_date,
                event.event_date,
                event.freshness_status,
                now,
            )
            for event in events
            if event.event_key and event.title_zh
        ]
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT INTO report_events
                (report_key, report_type, event_key, "column", title_zh, summary_zh,
                 score, source_links_json, quality_status, tags, published_at,
                 freshness_date, event_date, freshness_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_key, report_type, event_key, "column") DO UPDATE SET
                    title_zh = excluded.title_zh,
                    summary_zh = excluded.summary_zh,
                    score = excluded.score,
                    source_links_json = excluded.source_links_json,
                    quality_status = excluded.quality_status,
                    tags = excluded.tags,
                    published_at = excluded.published_at,
                    freshness_date = excluded.freshness_date,
                    event_date = excluded.event_date,
                    freshness_status = excluded.freshness_status""",
                rows,
            )
            return conn.total_changes - before

    def fetch_report_events(
        self,
        since_key: str,
        until_key: str | None = None,
        report_type: str = "daily",
        quality_status: str = "ok",
    ) -> list[ReportEvent]:
        sql = """SELECT * FROM report_events
                 WHERE report_type = ? AND report_key >= ?"""
        params: list[object] = [report_type, since_key]
        if until_key is not None:
            sql += " AND report_key < ?"
            params.append(until_key)
        if quality_status:
            sql += " AND quality_status = ?"
            params.append(quality_status)
        sql += ' ORDER BY report_key DESC, "column", score DESC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        events: list[ReportEvent] = []
        for row in rows:
            try:
                source_links = json.loads(row["source_links_json"] or "[]")
            except json.JSONDecodeError:
                source_links = []
            events.append(ReportEvent(
                report_key=row["report_key"],
                report_type=row["report_type"],
                event_key=row["event_key"],
                column=row["column"] or "",
                title_zh=row["title_zh"],
                summary_zh=row["summary_zh"] or "",
                score=row["score"] or 0.0,
                source_links=source_links if isinstance(source_links, list) else [],
                quality_status=row["quality_status"] or "ok",
                tags=row["tags"] or "",
                published_at=_from_utc_storage(row["published_at"]),
                freshness_date=row["freshness_date"] or "",
                event_date=row["event_date"] or "",
                freshness_status=row["freshness_status"] or "",
            ))
        return events

    def log_report_run(
        self,
        report_key: str,
        report_type: str,
        status: str,
        *,
        window_since: datetime | None = None,
        window_until: datetime | None = None,
        input_count: int = 0,
        candidate_count: int = 0,
        selected_count: int = 0,
        ai_duration_seconds: float = 0.0,
        error_count: int = 0,
        output_md_path: str = "",
        output_html_path: str = "",
        metrics: dict | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO report_runs
                (report_key, report_type, status, window_since, window_until,
                 input_count, candidate_count, selected_count, ai_duration_seconds,
                 error_count, output_md_path, output_html_path, metrics_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_key,
                    report_type,
                    status,
                    _to_utc_storage(window_since),
                    _to_utc_storage(window_until),
                    input_count,
                    candidate_count,
                    selected_count,
                    ai_duration_seconds,
                    error_count,
                    output_md_path,
                    output_html_path,
                    json.dumps(metrics or {}, ensure_ascii=False),
                    _to_utc_storage(datetime.now(timezone.utc)),
                ),
            )
            return int(cursor.lastrowid)

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


def db_health_check(
    db_path: str,
    window_since: datetime | None = None,
    source_defs: list[dict] | None = None,
) -> dict:
    """数据库健康检查：返回文章总数、最晚抓取时间、窗口命中数等状态信息。

    用于在 digest-only 或完整流程前打印可观测状态，帮助排查"停更无感"问题。
    """
    from pathlib import Path as _Path

    result = {
        "db_exists": False,
        "db_path": db_path,
        "article_count": 0,
        "latest_fetched_at": None,
        "window_count": 0,
        "window_since": None,
        "fetch_log_count": 0,
        "source_health_rows": [],
        "source_health_summary": {},
    }

    if not _Path(db_path).exists():
        return result

    result["db_exists"] = True

    try:
        db = NewsDatabase(db_path)
        result["article_count"] = db.article_count()
        result["fetch_log_count"] = db.fetch_log_count()
    except Exception:
        return result

    window_since_utc = None
    if window_since is not None:
        window_since_utc = window_since if window_since.tzinfo else window_since.replace(tzinfo=timezone.utc)
        window_since_utc = window_since_utc.astimezone(timezone.utc)

    import sqlite3 as _sqlite3

    try:
        with _sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT MAX(fetched_at) FROM articles").fetchone()
            if row and row[0]:
                result["latest_fetched_at"] = row[0]

            if window_since_utc is not None:
                cutoff = _to_utc_storage(window_since_utc)
                if cutoff:
                    count_row = conn.execute(
                        "SELECT COUNT(*) FROM articles WHERE fetched_at >= ?", (cutoff,)
                    ).fetchone()
                    result["window_count"] = count_row[0] if count_row else 0
                    result["window_since"] = cutoff
        try:
            result["source_health_rows"] = db.source_health_rows(
                window_since=window_since,
                source_defs=source_defs,
            )
            result["source_health_summary"] = build_source_health_summary(
                result["source_health_rows"],
                fetch_log_rows=db.fetch_log_rows(window_since=window_since_utc) if window_since_utc else [],
                window_days=max(
                    1,
                    int((datetime.now(timezone.utc) - window_since_utc).days) if window_since_utc else 30,
                ),
                source_defs=source_defs,
            )
        except Exception:
            result["source_health_rows"] = []
            result["source_health_summary"] = {}
    except Exception:
        pass

    return result


def format_health_report(check: dict) -> str:
    """将健康检查结果格式化为人类可读的状态报告。"""
    lines = []
    lines.append(f"  数据库路径: {check['db_path']}")

    if not check["db_exists"]:
        lines.append("  状态: 数据库文件不存在")
        return "\n".join(lines)

    lines.append(f"  文章总数: {check['article_count']}")
    lines.append(f"  抓取日志: {check.get('fetch_log_count', 0)} 条")

    if check["latest_fetched_at"]:
        lines.append(f"  最晚抓取时间: {check['latest_fetched_at']}")
    else:
        lines.append("  最晚抓取时间: 无数据")

    if check.get("window_since"):
        lines.append(f"  窗口起始: {check['window_since']}")
        lines.append(f"  窗口内文章数: {check['window_count']}")

    summary = check.get("source_health_summary") or {}
    if summary:
        lines.append(
            f"  源覆盖: {summary.get('source_count', 0)} 个源 / {summary.get('column_count', 0)} 个栏目"
        )
        column_parts = []
        for col_key in ("us_politics", "global_affairs", "technology", "economy"):
            col = summary.get("columns", {}).get(col_key, {})
            if not col:
                continue
            column_parts.append(
                f"{col_key}:{col.get('article_count', 0)}"
                f"/源{col.get('source_count', 0)}"
                f"/{col.get('status', 'unknown')}"
            )
        if column_parts:
            lines.append("  栏目覆盖: " + " | ".join(column_parts))
        top_sources = summary.get("sources", [])[:3]
        if top_sources:
            rendered = []
            for source in top_sources:
                rendered.append(
                    f"{source.get('source', '')}({source.get('health_status', 'unknown')},"
                    f"{source.get('article_count', 0)}篇)"
                )
            lines.append("  重点源: " + " | ".join(rendered))

    # 状态判定
    if check["article_count"] == 0:
        lines.append("  判定: 空库，需要先抓取或同步远端状态")
    elif check.get("window_count", -1) == 0:
        lines.append("  判定: 有历史数据但当前窗口为空，库可能已停更")
    elif check.get("window_count", -1) > 0:
        if summary and summary.get("column_health_state") == "single_source_bias":
            lines.append("  判定: 有数据，但栏目来源偏单一")
        elif summary and summary.get("window_state") == "stale":
            lines.append("  判定: 有数据，但最近窗口偏旧")
        else:
            lines.append("  判定: 正常")

    return "\n".join(lines)


def _parse_health_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _health_status_for_source(
    article_count: int,
    active_days: int,
    latest_seen_at: str,
    fetch_success_rate: float | None,
    source_enabled: bool,
) -> str:
    if not source_enabled:
        return "disabled"
    if article_count <= 0:
        return "empty"
    if fetch_success_rate is not None and fetch_success_rate <= 0:
        return "fetch_failed"
    latest_dt = _parse_health_timestamp(latest_seen_at)
    if latest_dt is not None:
        age_days = max(0.0, (datetime.now(timezone.utc) - latest_dt).total_seconds() / 86400)
        if age_days >= 7:
            return "stale"
    if article_count >= 5 and active_days >= 2:
        return "healthy"
    if article_count >= 2:
        return "watch"
    return "thin"


def _column_status(article_count: int, source_count: int) -> str:
    if article_count <= 0:
        return "empty"
    if article_count < 3:
        return "thin"
    if source_count <= 1:
        return "single_source"
    return "healthy"


def build_source_health_summary(
    rows: list[dict],
    fetch_log_rows: list[dict] | None = None,
    window_days: int = 30,
    source_defs: list[dict] | None = None,
) -> dict:
    """把源级别健康行聚合成可读摘要，兼顾源健康与栏目覆盖。"""
    source_meta = {
        str(item.get("name") or "").strip(): dict(item)
        for item in (source_defs or [])
        if str(item.get("name") or "").strip()
    }
    source_logs: dict[str, dict[str, object]] = defaultdict(lambda: {"total": 0, "ok": 0, "latest": ""})
    for row in fetch_log_rows or []:
        source = str(row.get("source") or "").strip()
        if not source:
            continue
        entry = source_logs[source]
        entry["total"] = int(entry["total"]) + 1
        if str(row.get("status") or "").strip().lower() in {"ok", "success", "succeeded"}:
            entry["ok"] = int(entry["ok"]) + 1
        fetched_at = str(row.get("fetched_at") or "").strip()
        if fetched_at and fetched_at > str(entry["latest"] or ""):
            entry["latest"] = fetched_at

    sources: dict[str, dict] = {}
    columns: dict[str, dict] = {}
    total_articles = 0
    total_scored = 0
    total_strong_scored = 0
    latest_seen_at = ""

    for row in rows:
        source = str(row.get("source") or "").strip() or "unknown"
        column = str(row.get("column") or "").strip() or "unknown"
        tier = int(row.get("configured_source_tier") or row.get("source_tier") or 4)
        article_count = int(row.get("article_count") or 0)
        scored_count = int(row.get("scored_count") or 0)
        strong_scored_count = int(row.get("strong_scored_count") or 0)
        active_days = int(row.get("active_days") or 0)
        seen_at = str(row.get("latest_seen_at") or "").strip()
        fetch_mode = str(row.get("fetch_mode") or source_meta.get(source, {}).get("fetch_mode") or row.get("source_type") or "rss").strip()
        expected_column = str(row.get("configured_column") or source_meta.get(source, {}).get("column") or "").strip()
        source_enabled = bool(row.get("source_enabled", True))

        total_articles += article_count
        total_scored += scored_count
        total_strong_scored += strong_scored_count
        if seen_at and (not latest_seen_at or seen_at > latest_seen_at):
            latest_seen_at = seen_at

        source_entry = sources.setdefault(source, {
            "source": source,
            "fetch_mode": fetch_mode,
            "source_tier": tier,
            "expected_column": expected_column,
            "source_enabled": source_enabled,
            "article_count": 0,
            "scored_count": 0,
            "strong_scored_count": 0,
            "active_days": 0,
            "latest_seen_at": "",
            "columns": {},
            "health_status": "thin",
            "coverage_status": "empty",
            "fetch_success_rate": None,
            "fetch_log_count": 0,
            "fetch_log_ok_count": 0,
        })
        source_entry["article_count"] += article_count
        source_entry["scored_count"] += scored_count
        source_entry["strong_scored_count"] += strong_scored_count
        source_entry["active_days"] = max(int(source_entry["active_days"]), active_days)
        if seen_at and (not source_entry["latest_seen_at"] or seen_at > source_entry["latest_seen_at"]):
            source_entry["latest_seen_at"] = seen_at
        source_entry["columns"][column] = source_entry["columns"].get(column, 0) + article_count

        log_info = source_logs.get(source, {"total": 0, "ok": 0, "latest": ""})
        source_entry["fetch_log_count"] = int(log_info.get("total") or 0)
        source_entry["fetch_log_ok_count"] = int(log_info.get("ok") or 0)
        if int(log_info.get("total") or 0) > 0:
            source_entry["fetch_success_rate"] = round(
                int(log_info.get("ok") or 0) / int(log_info.get("total") or 1),
                3,
            )
        source_entry["health_status"] = _health_status_for_source(
            article_count=int(source_entry["article_count"]),
            active_days=int(source_entry["active_days"]),
            latest_seen_at=str(source_entry["latest_seen_at"]),
            fetch_success_rate=source_entry["fetch_success_rate"],
            source_enabled=source_enabled,
        )
        if expected_column and article_count > 0:
            observed_columns = {key for key, value in source_entry["columns"].items() if value > 0}
            if expected_column not in observed_columns:
                source_entry["coverage_status"] = "column_mismatch"
            elif len(observed_columns) > 1:
                source_entry["coverage_status"] = "multi_column"
            else:
                source_entry["coverage_status"] = "covered"
        elif article_count > 0:
            source_entry["coverage_status"] = "covered"

        column_entry = columns.setdefault(column, {
            "column": column,
            "article_count": 0,
            "scored_count": 0,
            "strong_scored_count": 0,
            "source_count": 0,
            "source_tier_counts": {},
            "source_names": [],
            "status": "empty",
        })
        column_entry["article_count"] += article_count
        column_entry["scored_count"] += scored_count
        column_entry["strong_scored_count"] += strong_scored_count
        column_entry.setdefault("source_names", [])
        if source not in column_entry["source_names"]:
            column_entry["source_names"].append(source)
        column_entry["source_count"] = len(column_entry["source_names"])
        tier_counts = column_entry.setdefault("source_tier_counts", {})
        tier_counts[str(tier)] = tier_counts.get(str(tier), 0) + article_count
        column_entry["status"] = _column_status(
            article_count=int(column_entry["article_count"]),
            source_count=int(column_entry["source_count"]),
        )

    for source, meta in source_meta.items():
        if source in sources:
            continue
        source_enabled = bool(meta.get("enabled", True))
        log_info = source_logs.get(source, {"total": 0, "ok": 0, "latest": ""})
        fetch_success_rate = None
        if int(log_info.get("total") or 0) > 0:
            fetch_success_rate = round(
                int(log_info.get("ok") or 0) / int(log_info.get("total") or 1),
                3,
            )
        sources[source] = {
            "source": source,
            "fetch_mode": str(meta.get("fetch_mode") or "rss"),
            "source_tier": int(meta.get("source_tier") or 4),
            "expected_column": str(meta.get("column") or ""),
            "source_enabled": source_enabled,
            "article_count": 0,
            "scored_count": 0,
            "strong_scored_count": 0,
            "active_days": 0,
            "latest_seen_at": "",
            "columns": {},
            "health_status": _health_status_for_source(
                article_count=0,
                active_days=0,
                latest_seen_at="",
                fetch_success_rate=fetch_success_rate,
                source_enabled=source_enabled,
            ),
            "coverage_status": "empty",
            "fetch_success_rate": fetch_success_rate,
            "fetch_log_count": int(log_info.get("total") or 0),
            "fetch_log_ok_count": int(log_info.get("ok") or 0),
        }

    source_list = sorted(
        sources.values(),
        key=lambda item: (-int(item.get("article_count") or 0), str(item.get("source") or "")),
    )
    column_list = [columns[key] for key in sorted(columns)]

    source_count = len(source_list)
    column_count = len(column_list)
    if not column_list:
        column_health_state = "empty"
    elif any(item["status"] == "single_source" for item in column_list):
        column_health_state = "single_source_bias"
    elif any(item["status"] == "empty" for item in column_list):
        column_health_state = "missing_column"
    elif any(item["status"] == "thin" for item in column_list):
        column_health_state = "thin"
    else:
        column_health_state = "healthy"

    if latest_seen_at:
        latest_dt = _parse_health_timestamp(latest_seen_at)
        if latest_dt is not None:
            window_age_days = max(0.0, (datetime.now(timezone.utc) - latest_dt).total_seconds() / 86400)
            if window_age_days >= max(7, window_days * 0.5):
                window_state = "stale"
            else:
                window_state = "fresh"
        else:
            window_state = "unknown"
    else:
        window_state = "empty"

    return {
        "window_days": window_days,
        "source_count": source_count,
        "column_count": column_count,
        "total_articles": total_articles,
        "total_scored": total_scored,
        "total_strong_scored": total_strong_scored,
        "latest_seen_at": latest_seen_at,
        "window_state": window_state,
        "column_health_state": column_health_state,
        "sources": source_list,
        "columns": {item["column"]: item for item in column_list},
    }


def migrate_legacy_news_db(
    target_db_path: str,
    legacy_db_path: str = "data/news.db",
) -> dict[str, int | str | bool]:
    """把旧 news 库的数据幂等迁入当前正式库。"""
    target = NewsDatabase(target_db_path)
    legacy_path = Path(legacy_db_path)
    if not legacy_path.exists():
        return {
            "legacy_exists": False,
            "migrated_articles": 0,
            "migrated_fetch_logs": 0,
            "target_db_path": str(target.db_path),
            "legacy_db_path": str(legacy_path),
        }

    legacy = NewsDatabase(str(legacy_path))
    article_columns = [
        "url_hash", "url", "title", "summary", "source", "source_type",
        "published_at", "fetched_at", "category", "score", "topic", "reason", "level",
        "column", "source_tier", "event_key", "source_url_normalized",
        "llm_score", "llm_summary", "llm_tags", "llm_reason",
    ]
    migrated_fetch_logs = 0

    with legacy._connect() as src, target._connect() as dst:
        before_articles = int(dst.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
        article_rows = src.execute(
            f'SELECT {", ".join(f"""\"{col}\"""" for col in article_columns)} FROM articles ORDER BY id ASC'
        ).fetchall()
        dst.executemany(
            f'''INSERT OR IGNORE INTO articles ({", ".join(f'"{col}"' for col in article_columns)})
                VALUES ({", ".join("?" for _ in article_columns)})''',
            [tuple(row[col] for col in article_columns) for row in article_rows],
        )

        if int(dst.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]) == 0:
            fetch_rows = src.execute(
                "SELECT source, fetched_at, count, status FROM fetch_log ORDER BY id ASC"
            ).fetchall()
            dst.executemany(
                "INSERT INTO fetch_log (source, fetched_at, count, status) VALUES (?, ?, ?, ?)",
                [(row["source"], row["fetched_at"], row["count"], row["status"]) for row in fetch_rows],
            )
            migrated_fetch_logs = len(fetch_rows)

    return {
        "legacy_exists": True,
        "migrated_articles": max(0, target.article_count() - before_articles),
        "migrated_fetch_logs": migrated_fetch_logs,
        "target_db_path": str(target.db_path),
        "legacy_db_path": str(legacy_path),
    }
