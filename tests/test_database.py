"""数据库存储层测试"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import (
    Article,
    ArticleCandidate,
    NewsDatabase,
    ReportEvent,
    _to_utc_storage,
    migrate_legacy_news_db,
)


def test_to_utc_storage_writes_explicit_utc_offset():
    stored = _to_utc_storage(datetime(2026, 6, 18, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert stored == "2026-06-18T00:30:00+00:00"


def test_database_stores_and_queries_datetimes_as_utc(tmp_path):
    db = NewsDatabase(str(tmp_path / "news.db"))
    published = datetime(2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    fetched = datetime(2026, 6, 18, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    db.insert(Article(
        url="https://example.com/a",
        title="测试文章",
        summary="摘要",
        source="Example",
        source_type="rss",
        published_at=published,
        fetched_at=fetched,
    ))

    before = db.fetch_since(datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc))
    after = db.fetch_since(datetime(2026, 6, 18, 1, 0, tzinfo=timezone.utc))

    assert len(before) == 1
    assert before[0].fetched_at == datetime(2026, 6, 18, 0, 30, tzinfo=timezone.utc)
    assert before[0].published_at == datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    assert after == []


def test_database_migrates_legacy_local_naive_datetimes(tmp_path):
    db_path = tmp_path / "legacy.db"
    db = NewsDatabase(str(db_path))
    with db._connect() as conn:
        conn.execute("DELETE FROM articles")
        conn.execute(
            """INSERT INTO articles
            (url_hash, url, title, summary, source, source_type, published_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "abc",
                "https://example.com/legacy",
                "旧文章",
                "摘要",
                "Example",
                "rss",
                "2026-06-18T08:00:00",
                "2026-06-18T08:30:00",
            ),
        )
        conn.execute(
            "INSERT INTO fetch_log (source, fetched_at, count, status) VALUES (?, ?, ?, ?)",
            ("rss", "2026-06-18T08:30:00", 1, "ok"),
        )

    NewsDatabase(str(db_path))
    with db._connect() as conn:
        row = conn.execute("SELECT published_at, fetched_at FROM articles").fetchone()
        log_row = conn.execute("SELECT fetched_at FROM fetch_log").fetchone()

    assert row["published_at"] == "2026-06-18T00:00:00+00:00"
    assert row["fetched_at"] == "2026-06-18T00:30:00+00:00"
    assert log_row["fetched_at"] == "2026-06-18T00:30:00+00:00"

    NewsDatabase(str(db_path))
    with db._connect() as conn:
        row_after_second_run = conn.execute("SELECT fetched_at FROM articles").fetchone()
    assert row_after_second_run["fetched_at"] == "2026-06-18T00:30:00+00:00"


def test_migrate_legacy_news_db_moves_articles_and_fetch_log(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    target_path = tmp_path / "products" / "news.db"

    legacy = NewsDatabase(str(legacy_path))
    legacy.insert(Article(
        url="https://example.com/a",
        title="测试文章",
        summary="摘要",
        source="Example",
        source_type="rss",
        published_at=datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 18, 0, 30, tzinfo=timezone.utc),
    ))
    legacy.log_fetch("Example", 1, "ok")

    result = migrate_legacy_news_db(str(target_path), str(legacy_path))
    target = NewsDatabase(str(target_path))

    assert result["legacy_exists"] is True
    assert result["migrated_articles"] == 1
    assert result["migrated_fetch_logs"] == 1
    assert target.article_count() == 1
    assert target.fetch_log_count() == 1

    second = migrate_legacy_news_db(str(target_path), str(legacy_path))
    assert second["migrated_articles"] == 0


def test_database_candidate_event_and_run_layers_are_idempotent(tmp_path):
    db = NewsDatabase(str(tmp_path / "news.db"))
    published = datetime(2026, 6, 27, 0, 30, tzinfo=timezone.utc)

    candidate = ArticleCandidate(
        report_key="2026-06-27",
        report_type="daily",
        url="https://example.com/a?utm_source=x",
        title="White House announces policy update",
        source="Example",
        column="us_politics",
        candidate_score=92,
        source_tier=1,
        reason="official source",
        status="selected",
        event_key="white_house_policy_20260627",
        published_at=published,
        fetched_at=published,
        freshness_date="2026-06-27",
        event_date="2026-06-27",
        freshness_status="today",
    )

    assert db.upsert_article_candidates([candidate]) == 1
    assert db.upsert_article_candidates([candidate]) >= 1

    candidates = db.fetch_article_candidates("2026-06-27", status="selected")
    assert len(candidates) == 1
    assert candidates[0].event_key == "white_house_policy_20260627"
    assert candidates[0].freshness_date == "2026-06-27"
    assert candidates[0].freshness_status == "today"

    event = ReportEvent(
        report_key="2026-06-27",
        report_type="daily",
        event_key="white_house_policy_20260627",
        column="us_politics",
        title_zh="白宫宣布政策更新",
        summary_zh="白宫发布新的政策安排。",
        score=91,
        source_links=[{"title": "Example", "url": "https://example.com/a"}],
        tags="official,policy",
        published_at=published,
        freshness_date="2026-06-27",
        event_date="2026-06-27",
        freshness_status="today",
    )

    assert db.upsert_report_events([event]) == 1
    assert db.upsert_report_events([event]) >= 1

    events = db.fetch_report_events("2026-06-27", report_type="daily")
    assert len(events) == 1
    assert events[0].title_zh == "白宫宣布政策更新"
    assert events[0].source_links[0]["url"] == "https://example.com/a"
    assert events[0].freshness_date == "2026-06-27"
    assert events[0].freshness_status == "today"

    run_id = db.log_report_run(
        "2026-06-27",
        "daily",
        "ok",
        window_since=published,
        window_until=published,
        input_count=10,
        candidate_count=1,
        selected_count=1,
        output_md_path="docs/news/daily/2026-06-27.md",
        metrics={"ok": True},
    )
    assert run_id > 0
