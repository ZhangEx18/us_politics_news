"""周报 pipeline 回归测试"""

from datetime import datetime, timezone

from database import Article, article_to_content_item
from models import ContentItem, SourceType
from run_weekly import _build_weekly_scored_events, _get_month_week_number
from report_titles import build_weekly_title


def test_weekly_scored_events_use_llm_score_and_skip_unscored_articles():
    article = Article(
        url="https://example.com/a",
        title="测试文章",
        summary="原始摘要",
        source="Example",
        source_type="rss",
        published_at=datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc),
        column="us_politics",
        source_tier=2,
        event_key="test_event_20260610",
        score=0,
        llm_score=92,
        llm_summary="LLM 摘要",
        llm_tags="法院,政策",
    )
    unscored_article = Article(
        url="https://example.com/b",
        title="未评分文章",
        summary="摘要",
        source="Example",
        source_type="rss",
        published_at=datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc),
        column="us_politics",
        source_tier=2,
        event_key="unscored_event_20260610",
        score=99,
        llm_score=None,
    )
    items = [
        ContentItem(
            id="a",
            source_type=SourceType.RSS,
            title="测试文章",
            url="https://example.com/a",
            content="原始摘要",
            source_name="Example",
            column="us_politics",
            source_tier=2,
            event_key="test_event_20260610",
        ),
        ContentItem(
            id="b",
            source_type=SourceType.RSS,
            title="未评分文章",
            url="https://example.com/b",
            content="摘要",
            source_name="Example",
            column="us_politics",
            source_tier=2,
            event_key="unscored_event_20260610",
        ),
    ]

    events, skipped = _build_weekly_scored_events(items, [article, unscored_article])

    assert skipped == 1
    assert len(events) == 1
    assert events[0]["score"] == 92
    assert events[0]["summary"] == "LLM 摘要"
    assert events[0]["tags"] == ["法院", "政策"]


def test_month_week_number_counts_weeks_within_month():
    assert _get_month_week_number(datetime(2026, 6, 1, 7, 0)) == 1
    assert _get_month_week_number(datetime(2026, 6, 8, 7, 0)) == 2
    assert _get_month_week_number(datetime(2026, 6, 18, 7, 0)) == 3


def test_build_weekly_title_uses_month_week_number():
    assert build_weekly_title("2026-06-01") == "2026年6月第1周 周报"
    assert build_weekly_title("2026-06-08") == "2026年6月第2周 周报"
    assert build_weekly_title("2026-06-18") == "2026年6月第3周 周报"


def test_article_to_content_item_preserves_fetched_at_for_weekly_window_filter():
    fetched_at = datetime(2026, 6, 21, 6, 41, tzinfo=timezone.utc)
    article = Article(
        url="https://example.com/fetched",
        title="仅抓取时间存在",
        summary="摘要",
        source="Example",
        source_type="rss",
        published_at=None,
        fetched_at=fetched_at,
    )

    item = article_to_content_item(article)

    assert item.published_at is None
    assert item.fetched_at == fetched_at
