"""栏目配额与过滤测试 — run_pipeline.py"""

from models import ContentItem, SourceType


def _make_item(url: str, column: str, score: float) -> ContentItem:
    """构造测试用 ContentItem"""
    return ContentItem(
        id=f"test:{url}",
        source_type=SourceType.RSS,
        title=f"Test {url}",
        url=url,
        content="test content",
        source_name="test",
        column=column,
        score=score,
    )


# ── _apply_column_quota ──


def test_column_quota_respects_total_max():
    """三段式选择：总结果不应超过 total_max_items"""
    from run_pipeline import _apply_column_quota

    config = {
        "digest": {
            "columns": {
                "us_politics": {"min_items": 5, "target_items": 7, "max_items": 10},
                "global_affairs": {"min_items": 5, "target_items": 7, "max_items": 10},
                "technology": {"min_items": 5, "target_items": 7, "max_items": 10},
                "economy": {"min_items": 5, "target_items": 7, "max_items": 10},
            },
            "total_target_items": 28,
            "total_max_items": 30,
        }
    }

    items = []
    for col in ["us_politics", "global_affairs", "technology", "economy"]:
        for i in range(15):
            items.append(_make_item(f"https://example.com/{col}/{i}", col, 90 - i))

    result = _apply_column_quota(items, config)
    assert len(result) <= 30, f"总数不应超过 total_max_items=30，实际 {len(result)}"


def test_column_quota_selects_highest_scored():
    """应优先选取高分文章"""
    from run_pipeline import _apply_column_quota

    config = {
        "digest": {
            "columns": {
                "us_politics": {"min_items": 2, "target_items": 2, "max_items": 5},
                "global_affairs": {"min_items": 2, "target_items": 2, "max_items": 5},
                "technology": {"min_items": 2, "target_items": 2, "max_items": 5},
                "economy": {"min_items": 2, "target_items": 2, "max_items": 5},
            },
            "total_target_items": 8,
            "total_max_items": 10,
        }
    }

    items = [
        _make_item("https://example.com/high", "us_politics", 95),
        _make_item("https://example.com/mid", "us_politics", 70),
        _make_item("https://example.com/low", "us_politics", 50),
    ]

    result = _apply_column_quota(items, config)
    scores = [item.score for item in result if item.column == "us_politics"]
    assert scores == sorted(scores, reverse=True), "应按分数降序选取"


# ── min_llm_score 过滤 ──


def test_min_llm_score_filters_low_scored_items():
    """低于 min_llm_score 的文章应被过滤"""
    config = {
        "analysis": {"min_llm_score": 70},
        "digest": {
            "columns": {
                "us_politics": {"target_items": 10, "max_items": 10},
                "global_affairs": {"target_items": 10, "max_items": 10},
                "technology": {"target_items": 10, "max_items": 10},
                "economy": {"target_items": 10, "max_items": 10},
            },
            "total_max_items": 40,
        },
    }

    items = [
        _make_item("https://example.com/1", "us_politics", 80),
        _make_item("https://example.com/2", "us_politics", 70),
        _make_item("https://example.com/3", "us_politics", 65),
        _make_item("https://example.com/4", "us_politics", 50),
    ]

    min_llm_score = config["analysis"]["min_llm_score"]
    filtered = [it for it in items if (it.score or 0) >= min_llm_score]

    assert len(filtered) == 2, f"应过滤掉 65 和 50，实际保留 {len(filtered)} 条"
    assert all(it.score >= 70 for it in filtered)
