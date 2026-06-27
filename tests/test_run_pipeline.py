"""run_pipeline 回归测试"""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import Article
from models import ContentItem
from run_pipeline import (
    main,
    _augment_ai_config_with_runtime,
    _count_scored_entries,
    _content_item_to_report_candidate,
    _filter_articles_to_window,
    _filter_items_by_freshness,
    _get_report_publish_time,
    _get_report_window,
    _is_cn_source_entry,
    _is_cn_source_item,
    _load_schedule_config,
    _open_news_db,
    _is_hard_news_entry,
    run_digest_only,
)
from report_engine import build_reader_highlights


def test_database_article_can_map_to_content_item_without_id_field():
    article = Article(
        url="https://example.com/a",
        title="测试文章",
        summary="测试摘要",
        source="Example",
        source_type="rss",
        published_at=datetime.now(),
        fetched_at=datetime.now(),
        column="us_politics",
        source_tier=2,
        event_key="test_event_20260618",
        source_url_normalized="example.com/a",
        topic="测试主题",
        score=88,
        reason="测试原因",
        level="重点",
    )

    item = ContentItem(
        id="db:example.com/a",
        source_type=article.source_type,
        title=article.title,
        url=article.url,
        content=article.summary,
        source_name=article.source,
        published_at=article.published_at,
        column=article.column,
        source_tier=article.source_tier,
        event_key=article.event_key,
        source_url_normalized=article.source_url_normalized,
        topic=article.topic,
        score=article.score,
        reason=article.reason,
        level=article.level,
    )

    assert item.id.startswith("db:")
    assert item.column == "us_politics"
    assert item.event_key == "test_event_20260618"
    assert item.source_tier == 2


def test_main_digest_only_exits_when_no_content_generated(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--digest-only"])
    monkeypatch.setattr("run_pipeline.run_digest_only", lambda hours=24, report_type="daily": {"total_selected": 0})

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1


def test_build_reader_highlights_prefers_titles_and_deduplicates():
    columns = {
        "us_politics": [
            {"title_zh": "华为案证据裁定：孟晚舟供述可被美国检方使用", "core_facts": "事实一"},
            {"title_zh": "华为案证据裁定：孟晚舟供述可被美国检方使用", "core_facts": "重复标题"},
        ],
        "global_affairs": [
            {"title_zh": "", "core_facts": "美国首次公开与伊朗达成的 14 点谅解备忘录全文"},
        ],
    }

    highlights = build_reader_highlights(columns, limit=8)

    assert highlights[0].startswith("华为案证据裁定")
    assert len(highlights) == 2
    assert any("伊朗" in item for item in highlights)


def test_count_scored_entries_only_counts_real_ai_results():
    scored = [
        {"link": "https://example.com/1", "column": "us_politics", "summary": "摘要", "event_key": "a_20260618"},
        {"link": "https://example.com/2", "column": "", "summary": "摘要", "event_key": "b_20260618"},
        {"link": "https://example.com/3", "column": "technology", "summary": "", "event_key": "c_20260618"},
        {"link": "https://example.com/4", "column": "economy", "summary": "摘要", "event_key": ""},
    ]

    assert _count_scored_entries(scored) == 1


def test_is_hard_news_entry_requires_true_flag():
    assert _is_hard_news_entry({"is_hard_news": True}) is True
    assert _is_hard_news_entry({"is_hard_news": False}) is False
    assert _is_hard_news_entry({}) is False


def test_content_item_to_report_candidate_uses_content_as_summary():
    item = ContentItem(
        id="1",
        source_type="rss",
        title="英文标题",
        url="https://example.com/item",
        content="数据库摘要正文",
        source_name="Example",
        metadata={"language": "en", "tags": ["macro"]},
        column="economy",
    )

    candidate = _content_item_to_report_candidate(item)

    assert candidate["summary"] == "数据库摘要正文"
    assert candidate["content"] == "数据库摘要正文"
    assert candidate["source_links"][0]["url"] == "https://example.com/item"


def test_cn_source_helpers_detect_language_and_tag():
    item = ContentItem(
        id="1",
        source_type="rss",
        title="中文源",
        url="https://example.com/cn",
        source_name="Example CN",
        metadata={"language": "zh", "tags": ["cn_source", "macro"]},
    )

    assert _is_cn_source_item(item) is True
    assert _is_cn_source_entry({"language": "zh", "tags": []}) is True
    assert _is_cn_source_entry({"language": "en", "tags": ["cn_source"]}) is True
    assert _is_cn_source_entry({"language": "en", "tags": ["policy"]}) is False


def test_augment_ai_config_with_runtime_applies_llm_limits():
    config = {
        "llm": {
            "max_concurrent": 5,
            "max_prompt_chars": 120000,
            "timeout_seconds": 180,
            "score_max_concurrent": 2,
            "score_max_prompt_chars": 9000,
            "score_timeout_seconds": 120,
            "score_wall_timeout_seconds": 420,
            "score_content_chars": 400,
            "score_retry_split_depth": 3,
            "digest_timeout_seconds": 240,
            "digest_content_chars": 1000,
            "meta_timeout_seconds": 120,
        }
    }

    ai_config = _augment_ai_config_with_runtime(
        {"api_key": "k", "base_url": "https://example.com", "model": "m"},
        config,
    )

    assert ai_config["score_max_concurrent"] == 2
    assert ai_config["score_max_prompt_chars"] == 9000
    assert ai_config["score_timeout_seconds"] == 120
    assert ai_config["score_wall_timeout_seconds"] == 420
    assert ai_config["score_content_chars"] == 400
    assert ai_config["score_retry_split_depth"] == 3
    assert ai_config["digest_timeout_seconds"] == 240
    assert ai_config["digest_content_chars"] == 1000
    assert ai_config["meta_timeout_seconds"] == 120


def test_get_report_window_locks_to_beijing_7am_cutoff():
    tz = ZoneInfo("Asia/Shanghai")
    config = {"schedule": {"timezone": "Asia/Shanghai", "cutoff_hour": 7, "publish_at": "07:45"}}

    since, until, report_date = _get_report_window(datetime(2026, 6, 19, 8, 30, tzinfo=tz), config=config)
    assert since == datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    assert until == datetime(2026, 6, 19, 7, 0, tzinfo=tz)
    assert report_date == "2026-06-19"

    since, until, report_date = _get_report_window(datetime(2026, 6, 19, 6, 30, tzinfo=tz), config=config)
    assert since == datetime(2026, 6, 17, 7, 0, tzinfo=tz)
    assert until == datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    assert report_date == "2026-06-18"


def test_get_report_window_converts_github_runner_utc_time_to_beijing():
    tz = ZoneInfo("Asia/Shanghai")
    config = {"schedule": {"timezone": "Asia/Shanghai", "cutoff_hour": 7, "publish_at": "07:45"}}

    now = datetime(2026, 6, 20, 1, 55, tzinfo=timezone.utc).astimezone(tz)
    since, until, report_date = _get_report_window(now, config=config)

    assert since == datetime(2026, 6, 19, 7, 0, tzinfo=tz)
    assert until == datetime(2026, 6, 20, 7, 0, tzinfo=tz)
    assert report_date == "2026-06-20"


def test_digest_only_uses_converted_schedule_timezone_for_report_window(monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            utc_now = datetime(2026, 6, 20, 1, 55, tzinfo=timezone.utc)
            if tz is None:
                return utc_now.replace(tzinfo=None)
            return utc_now.astimezone(tz)

    class EmptyDatabase:
        def __init__(self, db_path):
            self.db_path = db_path

        def article_count(self):
            return 0

        def fetch_since(self, since):
            assert since == datetime(2026, 6, 18, 23, 0)
            return []

    monkeypatch.setattr("run_pipeline.datetime", FixedDatetime)
    monkeypatch.setattr("run_pipeline._load_config", lambda: {"schedule": {"timezone": "Asia/Shanghai"}})
    monkeypatch.setattr("run_pipeline._load_ai_config", lambda: {"api_key": "k"})
    monkeypatch.setattr("run_pipeline._augment_ai_config_with_runtime", lambda ai_config, config: ai_config)
    monkeypatch.setattr(
        "run_pipeline.migrate_legacy_news_db",
        lambda db_path: {
            "legacy_exists": False,
            "migrated_articles": 0,
            "migrated_fetch_logs": 0,
        },
    )
    monkeypatch.setattr("run_pipeline.NewsDatabase", EmptyDatabase)
    monkeypatch.setattr(
        "sync_state_db.sync_product_db",
        lambda product_key: {"restored": False, "error": "远端分支不存在"},
    )

    stats = run_digest_only()

    assert stats == {"total_selected": 0}


def test_open_news_db_runs_legacy_migration(monkeypatch):
    called = {}

    class DummyDb:
        def __init__(self, db_path):
            called["db_path"] = db_path

        def article_count(self):
            return 3

    monkeypatch.setattr(
        "run_pipeline.migrate_legacy_news_db",
        lambda db_path: (
            called.setdefault("migration_path", db_path),
            {
                "legacy_exists": True,
                "migrated_articles": 3,
                "migrated_fetch_logs": 1,
            },
        )[1],
    )
    monkeypatch.setattr("run_pipeline.NewsDatabase", DummyDb)
    monkeypatch.setattr(
        "sync_state_db.sync_product_db",
        lambda product_key: (_ for _ in ()).throw(AssertionError("不应在非空库时调用同步")),
    )

    db = _open_news_db({"storage": {"db_path": "data/products/news/news.db"}, "product_key": "news"})

    assert isinstance(db, DummyDb)
    assert called["migration_path"] == "data/products/news/news.db"
    assert called["db_path"] == "data/products/news/news.db"


def test_get_report_publish_time_reads_schedule_publish_at():
    tz = ZoneInfo("Asia/Shanghai")
    config = {"schedule": {"timezone": "Asia/Shanghai", "publish_at": "07:45"}}
    pub_dt = _get_report_publish_time("2026-06-19", config=config)

    assert pub_dt == datetime(2026, 6, 19, 7, 45, tzinfo=tz)


def test_filter_articles_to_window_uses_fixed_bounds():
    tz = ZoneInfo("Asia/Shanghai")
    since = datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    until = datetime(2026, 6, 19, 7, 0, tzinfo=tz)
    config = {"schedule": {"timezone": "Asia/Shanghai"}}
    items = [
        ContentItem(
            id="1",
            source_type="rss",
            title="inside",
            url="https://example.com/inside",
            content="body",
            source_name="Example",
            fetched_at=datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc),
        ),
        ContentItem(
            id="2",
            source_type="rss",
            title="outside",
            url="https://example.com/outside",
            content="body",
            source_name="Example",
            fetched_at=datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc),
        ),
    ]

    filtered = _filter_articles_to_window(items, since, until, config=config)

    assert [item.title for item in filtered] == ["inside"]


def test_load_schedule_config_provides_defaults():
    cfg = _load_schedule_config({})
    assert cfg["timezone"] == "Asia/Shanghai"
    assert cfg["cutoff_hour"] == 7
    assert cfg["fetch_at"] == "07:00"
    assert cfg["publish_at"] == "07:45"


def test_filter_items_by_freshness_supports_source_override():
    now = datetime(2026, 6, 19, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    config = {
        "analysis": {"freshness_hours": 30},
        "schedule": {"timezone": "Asia/Shanghai"},
    }
    sources = [
        {"name": "Fast Feed", "max_age_hours": 12},
        {"name": "Slow Feed", "max_age_hours": 48},
    ]
    items = [
        ContentItem(
            id="1",
            source_type="rss",
            title="fresh enough",
            url="https://example.com/1",
            content="body",
            source_name="Fast Feed",
            published_at=datetime(2026, 6, 18, 22, 0, tzinfo=timezone.utc),
            column="us_politics",
        ),
        ContentItem(
            id="2",
            source_type="rss",
            title="too old for fast feed",
            url="https://example.com/2",
            content="body",
            source_name="Fast Feed",
            published_at=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
            column="us_politics",
        ),
        ContentItem(
            id="3",
            source_type="rss",
            title="allowed for slow feed",
            url="https://example.com/3",
            content="body",
            source_name="Slow Feed",
            published_at=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
            column="global_affairs",
        ),
    ]

    kept, stats = _filter_items_by_freshness(items, sources, config, now=now)

    assert [item.title for item in kept] == ["fresh enough", "allowed for slow feed"]
    assert stats["dropped"] == 1
