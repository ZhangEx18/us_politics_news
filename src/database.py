#!/usr/bin/env python3
"""SQLite 存储层：去重、增量更新、查询、幂等迁移"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse


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


class NewsDatabase:
    def __init__(self, db_path: str = "data/news.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()
        self._migrate_v2()

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

    @staticmethod
    def normalize_url(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/")
        return f"{host}{path}"

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def url_exists(self, url: str) -> bool:
        """查询 URL 是否已存在（只读辅助，不参与写入判定）"""
        normalized = self.normalize_url(url)
        h = self.url_hash(normalized)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE url_hash = ?",
                (h,),
            ).fetchone()
            return row is not None

    def insert(self, article: Article) -> bool:
        """单条插入，依赖 url_hash UNIQUE 索引幂等"""
        normalized = self.normalize_url(article.url)
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
                    article.published_at.isoformat() if article.published_at else None,
                    article.fetched_at.isoformat(),
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
            normalized = self.normalize_url(article.url)
            h = self.url_hash(normalized)
            rows.append((
                h, article.url, article.title, article.summary,
                article.source, article.source_type,
                article.published_at.isoformat() if article.published_at else None,
                article.fetched_at.isoformat(),
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
        cutoff = since.isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (cutoff,),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_today(self) -> List[Article]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (today,),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_by_column(self, column: str, since: datetime) -> List[Article]:
        cutoff = since.isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE "column" = ? AND fetched_at >= ?
                ORDER BY llm_score DESC, score DESC, fetched_at DESC""",
                (column, cutoff),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def fetch_by_category(self, category: str, days: int = 1) -> List[Article]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles
                WHERE category = ? AND fetched_at >= ?
                ORDER BY score DESC, fetched_at DESC""",
                (category, cutoff),
            ).fetchall()
            return [self._row_to_article(r) for r in rows]

    def get_stats(self, days: int = 1) -> dict:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
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
                (source, datetime.now().isoformat(), count, status),
            )

    def cleanup_old(self, days: int = 30):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
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
                normalized = self.normalize_url(link)
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
                normalized = self.normalize_url(article.url)
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
            published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
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
