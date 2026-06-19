"""run_pipeline 回归测试"""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import Article
from models import ContentItem
from run_pipeline import (
    main,
    _augment_ai_config_with_runtime,
    _build_reader_highlights,
    _count_scored_entries,
    _filter_articles_to_window,
    _get_report_publish_time,
    _get_report_window,
    _is_hard_news_entry,
)


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

    highlights = _build_reader_highlights(columns, limit=8)

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


def test_augment_ai_config_with_runtime_applies_llm_limits():
    config = {
        "llm": {
            "max_concurrent": 5,
            "max_prompt_chars": 120000,
            "timeout_seconds": 180,
            "score_max_concurrent": 2,
            "score_max_prompt_chars": 9000,
            "score_timeout_seconds": 120,
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
    assert ai_config["score_content_chars"] == 400
    assert ai_config["score_retry_split_depth"] == 3
    assert ai_config["digest_timeout_seconds"] == 240
    assert ai_config["digest_content_chars"] == 1000
    assert ai_config["meta_timeout_seconds"] == 120


def test_get_report_window_locks_to_beijing_7am_cutoff():
    tz = ZoneInfo("Asia/Shanghai")

    since, until, report_date = _get_report_window(datetime(2026, 6, 19, 8, 30, tzinfo=tz))
    assert since == datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    assert until == datetime(2026, 6, 19, 7, 0, tzinfo=tz)
    assert report_date == "2026-06-19"

    since, until, report_date = _get_report_window(datetime(2026, 6, 19, 6, 30, tzinfo=tz))
    assert since == datetime(2026, 6, 17, 7, 0, tzinfo=tz)
    assert until == datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    assert report_date == "2026-06-18"


def test_get_report_publish_time_fixed_to_8am_beijing():
    tz = ZoneInfo("Asia/Shanghai")
    pub_dt = _get_report_publish_time("2026-06-19")

    assert pub_dt == datetime(2026, 6, 19, 8, 0, tzinfo=tz)


def test_filter_articles_to_window_uses_fixed_bounds():
    tz = ZoneInfo("Asia/Shanghai")
    since = datetime(2026, 6, 18, 7, 0, tzinfo=tz)
    until = datetime(2026, 6, 19, 7, 0, tzinfo=tz)
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

    filtered = _filter_articles_to_window(items, since, until)

    assert [item.title for item in filtered] == ["inside"]
