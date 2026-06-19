"""评分模块测试 — ai_analyzer._score_batch_with_retry"""

import asyncio

from models import ScoredArticle
from ai_analyzer import _score_batch_with_retry


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


def test_score_batch_with_retry_recovers_missing_entries(monkeypatch):
    entries = [
        {"link": "https://example.com/1", "title": "A"},
        {"link": "https://example.com/2", "title": "B"},
    ]
    calls = {"count": 0}

    async def fake_score_single_batch(batch, config, batch_index=0):
        calls["count"] += 1
        if calls["count"] == 1:
            return (
                [{"link": "https://example.com/1", "score": 90, "column": "us_politics", "summary": "a", "event_key": "a_20260618"}],
                ["批次1 结果不完整: 输入2, 匹配1, 缺失['https://example.com/2']"],
            )
        item = batch[0]
        return (
            [{"link": item["link"], "score": 88, "column": "us_politics", "summary": "b", "event_key": "b_20260618"}],
            [],
        )

    monkeypatch.setattr("ai_analyzer._score_single_batch", fake_score_single_batch)

    scores, errors = asyncio.run(
        _score_batch_with_retry(
            entries,
            {"score_retry_split_depth": 3},
            batch_index=0,
        )
    )

    assert len(scores) == 2
    assert errors == []


def test_score_batch_with_retry_keeps_unresolved_errors(monkeypatch):
    entries = [
        {"link": "https://example.com/1", "title": "A"},
        {"link": "https://example.com/2", "title": "B"},
    ]

    async def fake_score_single_batch(batch, config, batch_index=0):
        if len(batch) == 2:
            return [], ["批次1 评分失败: TimeoutError: TimeoutError()"]
        item = batch[0]
        if item["link"].endswith("/1"):
            return (
                [{"link": item["link"], "score": 90, "column": "us_politics", "summary": "a", "event_key": "a_20260618"}],
                [],
            )
        return [], ["批次1 评分失败: TimeoutError: TimeoutError()"]

    monkeypatch.setattr("ai_analyzer._score_single_batch", fake_score_single_batch)

    scores, errors = asyncio.run(
        _score_batch_with_retry(
            entries,
            {"score_retry_split_depth": 2},
            batch_index=0,
        )
    )

    assert len(scores) == 1
    assert any("拆分后仍缺失" in err for err in errors)
