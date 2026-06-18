"""评分模块测试 — scoring.py 与 run_pipeline._assign_level"""

from scoring import ScoredArticle as LegacyScoredArticle
from models import ScoredArticle


# ── run_pipeline._assign_level ──


def test_assign_level_90_is_important():
    """评分 >= 85 应为「重点」"""
    from run_pipeline import _assign_level

    assert _assign_level(90) == "重点"


def test_assign_level_70_is_observe():
    """评分 < 85 应为「观察」"""
    from run_pipeline import _assign_level

    assert _assign_level(70) == "观察"


def test_assign_level_boundary_85():
    """恰好 85 应为「重点」"""
    from run_pipeline import _assign_level

    assert _assign_level(85) == "重点"


def test_assign_level_boundary_84():
    """84 应为「观察」"""
    from run_pipeline import _assign_level

    assert _assign_level(84) == "观察"


# ── ScoredArticle 默认值 ──


def test_scored_article_default_column():
    article = ScoredArticle(
        url="https://example.com/1",
        title="Test",
        summary="Summary",
        source="test",
        source_type="rss",
    )
    assert article.column == ""


def test_scored_article_default_source_tier():
    article = ScoredArticle(
        url="https://example.com/1",
        title="Test",
        summary="Summary",
        source="test",
        source_type="rss",
    )
    assert article.source_tier == 4


def test_scored_article_default_ai_tags():
    article = ScoredArticle(
        url="https://example.com/1",
        title="Test",
        summary="Summary",
        source="test",
        source_type="rss",
    )
    assert article.ai_tags == []


def test_scored_article_default_event_key():
    article = ScoredArticle(
        url="https://example.com/1",
        title="Test",
        summary="Summary",
        source="test",
        source_type="rss",
    )
    assert article.event_key == ""


def test_scored_article_default_is_followup():
    article = ScoredArticle(
        url="https://example.com/1",
        title="Test",
        summary="Summary",
        source="test",
        source_type="rss",
    )
    assert article.is_followup is False
