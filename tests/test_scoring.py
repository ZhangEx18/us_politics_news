"""评分模块测试 — ai_analyzer._score_batch_with_retry"""

import asyncio

from models import ScoredArticle
from ai_analyzer import _build_digest_evidence, _score_batch_with_retry, merge_events, score_batch


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


def test_score_batch_with_retry_splits_on_high_risk_rejection(monkeypatch):
    entries = [
        {"link": "https://example.com/1", "title": "A"},
        {"link": "https://example.com/2", "title": "B"},
    ]

    async def fake_score_single_batch(batch, config, batch_index=0):
        if len(batch) == 2:
            return [], ["批次1 评分失败: ValueError: high risk"]
        item = batch[0]
        return (
            [{"link": item["link"], "score": 90, "column": "us_politics", "summary": "ok", "event_key": "x_20260627"}],
            [],
        )

    monkeypatch.setattr("ai_analyzer._score_single_batch", fake_score_single_batch)

    scores, errors = asyncio.run(
        _score_batch_with_retry(
            entries,
            {"score_retry_split_depth": 2},
            batch_index=0,
        )
    )

    assert len(scores) == 2
    assert errors == []


def test_score_batch_wall_timeout_keeps_finished_batches(monkeypatch):
    entries = [
        {
            "link": "https://example.com/1",
            "title": "A",
            "content": "a" * 50,
            "source": "test",
        },
        {
            "link": "https://example.com/2",
            "title": "B",
            "content": "b" * 50,
            "source": "test",
        },
    ]

    async def fake_score_batch_with_retry(batch, config, batch_index=0, depth=0):
        if batch_index == 0:
            return (
                [{"link": batch[0]["link"], "score": 90, "column": "us_politics", "summary": "ok", "event_key": "a_20260627"}],
                [],
            )
        await asyncio.sleep(1)
        return [], []

    monkeypatch.setattr("ai_analyzer._score_batch_with_retry", fake_score_batch_with_retry)

    scores, errors = asyncio.run(
        score_batch(
            entries,
            {
                "score_max_prompt_chars": 40,
                "score_max_concurrent": 2,
                "score_wall_timeout_seconds": 0.05,
            },
        )
    )

    scored = [item for item in scores if item.get("score")]
    assert len(scored) == 1
    assert any("评分总耗时超过" in err for err in errors)


def test_merge_events_preserves_source_evidence_for_writer():
    items = [
        {
            "link": "https://example.com/a",
            "title": "A",
            "source": "source-a",
            "score": 90,
            "summary": "官方宣布新政策。",
            "content": "官方文件说明政策适用对象和执行时间。",
            "event_key": "policy_change_20260619",
            "tags": ["政策"],
        },
        {
            "link": "https://example.com/b",
            "title": "B",
            "source": "source-b",
            "score": 88,
            "summary": "监管机构给出执行细节。",
            "content": "监管机构列出申报流程和企业合规要求。",
            "event_key": "policy_change_20260619",
            "tags": ["监管"],
        },
    ]

    merged = merge_events(items)

    assert len(merged) == 1
    content = merged[0]["content"]
    assert "摘要：官方宣布新政策。" in content
    assert "原文片段：官方文件说明政策适用对象和执行时间。" in content
    assert "摘要：监管机构给出执行细节。" in content
    assert "原文片段：监管机构列出申报流程和企业合规要求。" in content


def test_build_digest_evidence_excludes_raw_article_content():
    event = {
        "summary": "监管机构宣布新规。",
        "content": "这是一段不应直接进入写作模型的原文长片段。",
        "language": "en",
        "source": "Example Source",
        "source_tier": 3,
        "source_links": [
            {"title": "Agency announces rule", "url": "https://example.com/a"},
            {"title": "Agency announces rule", "url": "https://example.com/b"},
            {"title": "Companies face compliance deadline", "url": "https://example.com/c"},
        ],
    }

    evidence = _build_digest_evidence(event)

    assert "摘要：监管机构宣布新规。" in evidence
    assert "语言：en" in evidence
    assert "主来源：Example Source" in evidence
    assert "来源层级：3" in evidence
    assert "来源标题：Agency announces rule；Companies face compliance deadline" in evidence
    assert "原文长片段" not in evidence
