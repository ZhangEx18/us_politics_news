"""报告编排器测试 — ReportSpec、质量门禁、要点提炼、栏级降级"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from report_engine import (
    ReportSpec,
    _generate_all_column_digests,
    build_reader_highlights,
    build_report,
    sanitize_or_validate_events,
)


# ── ReportSpec 默认值 ──


def test_report_spec_defaults():
    """ReportSpec 应有合理的默认值"""
    spec = ReportSpec(
        report_type="daily",
        report_key="2026-06-19",
        title="测试",
        since=datetime(2026, 6, 18),
        until=datetime(2026, 6, 19),
        output_dir="docs/daily",
        feed_path="docs/feed.xml",
        base_url="",
        column_quotas={},
    )
    assert spec.allow_headline_only is True
    assert spec.min_llm_score == 65


# ── sanitize_or_validate_events ──


def test_sanitize_removes_labels():
    """清理应移除 reader_body 中的标签前缀"""
    events = [
        {
            "title_zh": "测试",
            "reader_body": "核心事实：这是正文内容。第二句描述变化。第三句说明后果影响。",
        }
    ]
    cleaned, issues = sanitize_or_validate_events(events)
    assert len(cleaned) == 1
    assert "核心事实：" not in cleaned[0]["reader_body"]


def test_sanitize_removes_boilerplate():
    """清理应移除 reader_body 中的禁用套话"""
    events = [
        {
            "title_zh": "测试",
            "reader_body": "最高法院作出裁定。此举凸显了趋势变化。此裁定意味着未来方向。对选民有深远影响。",
        }
    ]
    cleaned, issues = sanitize_or_validate_events(events)
    assert len(cleaned) == 1
    assert "凸显了" not in cleaned[0]["reader_body"]


def test_validate_empty_body():
    """空 reader_body 的事件应被过滤移除"""
    events = [{"title_zh": "测试", "reader_body": ""}]
    cleaned, issues = sanitize_or_validate_events(events)
    assert len(cleaned) == 0


# ── build_reader_highlights ──


def test_build_reader_highlights_limit():
    """要点数量不应超过指定 limit"""
    columns = {"us_politics": [{"title_zh": f"事件{i}"} for i in range(20)]}
    highlights = build_reader_highlights(columns, limit=5)
    assert len(highlights) <= 5


def test_build_reader_highlights_empty():
    """空栏目应返回空列表"""
    columns = {"us_politics": [], "global_affairs": []}
    highlights = build_reader_highlights(columns)
    assert highlights == []


def test_generate_all_column_digests_falls_back_per_column():
    columns_cfg = {
        "us_politics": {"label": "美国政局"},
        "global_affairs": {"label": "国际局势"},
    }
    candidates = {
        "us_politics": [{"title": "重要事件", "summary": "摘要一。摘要二。"}],
        "global_affairs": [{"title": "国际事件", "summary": "国际摘要。"}],
    }

    async def _fake_digest(**kwargs):
        if kwargs["column_key"] == "global_affairs":
            raise RuntimeError("llm timeout")
        return [{"title_zh": "重要事件", "reader_body": "生成正文。"}]

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)):
        results, failures = __import__("asyncio").run(_generate_all_column_digests(
            columns_cfg=columns_cfg,
            column_candidates=candidates,
            history_context="",
            ai_config={},
            word_count_min=100,
            word_count_max=200,
        ))

    assert results["us_politics"][0]["reader_body"] == "生成正文。"
    assert results["global_affairs"][0]["title_zh"] == "国际事件"
    assert "摘要" in results["global_affairs"][0]["reader_body"]
    assert failures == {"global_affairs": "llm timeout"}


def test_build_report_tracks_cn_source_metrics(tmp_path):
    spec = ReportSpec(
        report_type="daily",
        report_key="2026-06-19",
        title="测试日报",
        since=datetime(2026, 6, 18, tzinfo=timezone.utc),
        until=datetime(2026, 6, 19, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "daily"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 3, "max_items": 3, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 3, "max_items": 3, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 3, "max_items": 3, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 3, "max_items": 3, "headline_items": 0},
        },
    )
    scored_events = [
        {
            "title": "中文国际事件",
            "source": "联合早报 - 国际",
            "score": 88,
            "summary": "摘要",
            "content": "正文",
            "column": "global_affairs",
            "event_key": "cn_event_20260619",
            "language": "zh",
            "tags": ["cn_source", "geopolitics"],
            "source_links": [{"title": "原文", "url": "https://example.com/a"}],
        },
        {
            "title": "英文经济事件",
            "source": "Supply Chain Dive",
            "score": 86,
            "summary": "摘要",
            "content": "正文",
            "column": "economy",
            "event_key": "en_event_20260619",
            "language": "en",
            "tags": ["supply_chain"],
            "source_links": [{"title": "原文", "url": "https://example.com/b"}],
        },
    ]
    config = {"rules": {"quality_gate": {"min_chars": 2, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [{
            "title_zh": kwargs["events"][0]["title"],
            "reader_body": "已生成正文。",
            "core_facts": "已生成正文。",
            "source_links": kwargs["events"][0].get("source_links", []),
        }]

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)):
        stats = build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    metrics = stats["metrics"]
    assert metrics["cn_source_selected"] == 1
    assert metrics["cn_source_selected_by_column"]["global_affairs"] == 1
