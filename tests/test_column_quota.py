"""栏目配额与过滤测试 — run_pipeline.py (v3)"""

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


def test_assign_level_important():
    from run_pipeline import _assign_level
    assert _assign_level(90) == "重点"
    assert _assign_level(85) == "重点"


def test_assign_level_observe():
    from run_pipeline import _assign_level
    assert _assign_level(84) == "观察"
    assert _assign_level(50) == "观察"
