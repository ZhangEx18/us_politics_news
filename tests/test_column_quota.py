"""栏目配额与过滤测试 — run_pipeline.py (v3)"""

from datetime import datetime, timedelta, timezone

from models import ContentItem, SourceType


def _make_item(url: str, column: str, score: float) -> ContentItem:
    return ContentItem(
        id=f"test:{url}", source_type=SourceType.RSS, title=f"Test {url}",
        url=url, content="test", source_name="test", column=column, score=score,
    )


def test_min_llm_score_filters_low_scored_items():
    min_llm_score = 70
    items = [
        _make_item("https://a.com", "us_politics", 80),
        _make_item("https://b.com", "us_politics", 70),
        _make_item("https://c.com", "us_politics", 65),
        _make_item("https://d.com", "us_politics", 50),
    ]
    filtered = [it for it in items if (it.score or 0) >= min_llm_score]
    assert len(filtered) == 2
    assert all(it.score >= 70 for it in filtered)


def test_split_by_column():
    items = [
        _make_item("https://a.com", "us_politics", 90),
        _make_item("https://b.com", "us_politics", 80),
        _make_item("https://c.com", "technology", 85),
        _make_item("https://d.com", "economy", 75),
    ]
    by_column: dict = {}
    for item in items:
        col = item.column or "us_politics"
        by_column.setdefault(col, []).append(item)
    assert len(by_column["us_politics"]) == 2
    assert len(by_column["technology"]) == 1
    assert len(by_column["economy"]) == 1


def test_column_items_sorted_by_score():
    items = [
        _make_item("https://a.com", "us_politics", 70),
        _make_item("https://b.com", "us_politics", 90),
        _make_item("https://c.com", "us_politics", 80),
    ]
    items.sort(key=lambda x: x.score or 0, reverse=True)
    assert [it.score for it in items] == [90, 80, 70]


def test_prefilter_items_prefers_higher_signal_items():
    from run_pipeline import _prefilter_items_for_scoring

    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    items = [
        ContentItem(
            id="test:1",
            source_type=SourceType.RSS,
            title="White House tariff update",
            url="https://example.com/us-1",
            content="A" * 700,
            source_name="Official",
            column="us_politics",
            source_tier=1,
            score=82,
            published_at=now - timedelta(hours=2),
            source_url_normalized="example.com/us-1",
        ),
        ContentItem(
            id="test:2",
            source_type=SourceType.RSS,
            title="White House tariff update duplicate",
            url="https://example.com/us-1?dup=1",
            content="short",
            source_name="Aggregator",
            column="us_politics",
            source_tier=4,
            score=90,
            published_at=now - timedelta(hours=10),
            source_url_normalized="example.com/us-1",
        ),
        ContentItem(
            id="test:3",
            source_type=SourceType.RSS,
            title="Senate hearing on AI",
            url="https://example.com/us-2",
            content="B" * 500,
            source_name="Media",
            column="us_politics",
            source_tier=2,
            score=78,
            published_at=now - timedelta(hours=3),
            source_url_normalized="example.com/us-2",
        ),
    ]
    columns_cfg = {"us_politics": {"prefilter_items": 2}}

    selected = _prefilter_items_for_scoring(items, columns_cfg, now=now)

    assert len(selected["us_politics"]) == 2
    assert selected["us_politics"][0].id == "test:1"
    assert {item.id for item in selected["us_politics"]} == {"test:1", "test:3"}


def test_prefilter_signal_demotes_routine_notice():
    from run_pipeline import _prefilter_signal

    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    routine = ContentItem(
        id="test:routine", source_type=SourceType.RSS,
        title="FTC Seeks Public Comment on Proposed Policy Statement",
        url="https://example.com/1", content="A" * 300, source_name="FTC",
        column="us_politics", source_tier=1, score=50,
        published_at=now - timedelta(hours=2),
    )
    news = ContentItem(
        id="test:news", source_type=SourceType.RSS,
        title="FTC, States Sue Amazon Over Secret Ad Surcharge Scheme",
        url="https://example.com/2", content="A" * 300, source_name="FTC",
        column="us_politics", source_tier=1, score=50,
        published_at=now - timedelta(hours=2),
    )
    assert _prefilter_signal(routine, now) < _prefilter_signal(news, now)
    assert _prefilter_signal(routine, now) <= 0.31 * _prefilter_signal(news, now)


def test_prefilter_caps_candidates_per_source():
    from run_pipeline import _prefilter_items_for_scoring

    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    items = [
        ContentItem(
            id=f"test:ftc-{i}", source_type=SourceType.CUSTOM,
            title=f"FTC enforcement action {i}", url=f"https://example.com/ftc-{i}",
            content="B" * 300, source_name="FTC Press Releases",
            column="us_politics", source_tier=1, score=50,
            published_at=now - timedelta(hours=1),
        )
        for i in range(8)
    ]
    columns_cfg = {"us_politics": {"prefilter_items": 25}}

    selected = _prefilter_items_for_scoring(items, columns_cfg, now=now)

    assert len(selected["us_politics"]) == 3


def test_prefilter_respects_source_metadata_cap():
    from run_pipeline import _prefilter_items_for_scoring

    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    items = [
        ContentItem(
            id=f"test:ftc-{i}", source_type=SourceType.CUSTOM,
            title=f"FTC enforcement action {i}", url=f"https://example.com/ftc-{i}",
            content="B" * 300, source_name="FTC Press Releases",
            column="us_politics", source_tier=1, score=50,
            published_at=now - timedelta(hours=1),
            metadata={"max_candidates_per_run": 2},
        )
        for i in range(8)
    ]
    columns_cfg = {"us_politics": {"prefilter_items": 25}}

    selected = _prefilter_items_for_scoring(items, columns_cfg, now=now)

    assert len(selected["us_politics"]) == 2


def test_select_daily_column_items_limits_source_share():
    from report_engine import _select_daily_column_items

    scored = [
        {
            "title": f"FTC action {i}",
            "source": "FTC Press Releases",
            "score": 80 - i,
            "summary": f"FTC action summary {i}",
        }
        for i in range(6)
    ]
    scored.append({
        "title": "Senate passes budget bill",
        "source": "PBS NewsHour",
        "score": 75,
        "summary": "Senate passes budget bill summary",
    })

    detailed, headline, metrics = _select_daily_column_items(
        scored_items=scored,
        fallback_items=[],
        target_items=5,
        max_items=5,
        headline_items=3,
        min_score=65,
    )

    sources = [item["source"] for item in detailed + headline]
    assert sources.count("FTC Press Releases") <= 2
    assert metrics["source_quota_dropped"] > 0


def test_prefilter_reserves_cn_source_slots():
    from run_pipeline import _prefilter_items_for_scoring

    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    items = []
    for i in range(20):
        items.append(ContentItem(
            id=f"test:tier1-{i}", source_type=SourceType.RSS,
            title=f"Major wire story {i}", url=f"https://example.com/wire-{i}",
            content="A" * 300, source_name=f"Wire {i}", column="us_politics",
            source_tier=1, score=50, published_at=now - timedelta(hours=1),
        ))
    for i in range(3):
        items.append(ContentItem(
            id=f"test:cn-{i}", source_type=SourceType.RSS,
            title=f"中文源新闻 {i}", url=f"https://example.com/cn-{i}",
            content="B" * 300, source_name=f"财新测试 {i}", column="us_politics",
            source_tier=2, score=40, published_at=now - timedelta(hours=2),
            metadata={"language": "zh", "tags": ["cn_source"]},
        ))
    columns_cfg = {"us_politics": {"prefilter_items": 5}}

    selected = _prefilter_items_for_scoring(items, columns_cfg, now=now)

    cn_items = [item for item in selected["us_politics"] if "cn_source" in (item.metadata.get("tags") or [])]
    assert len(cn_items) >= 2
