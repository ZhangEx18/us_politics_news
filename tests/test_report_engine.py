"""报告编排器测试 — ReportSpec、质量门禁、要点提炼、栏级降级"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from report_engine import (
    ReportSpec,
    _generate_all_column_digests,
    _translate_headline_only_by_column,
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


def test_translate_headline_only_by_column_drops_untranslated():
    async def _fake_translate(titles, ai_config):
        assert titles == ["English one", "English two"]
        return ["中文一", ""]

    with patch("report_engine.translate_headline_titles", new=AsyncMock(side_effect=_fake_translate)):
        translated, metrics = __import__("asyncio").run(_translate_headline_only_by_column(
            {"global_affairs": [{"title": "English one"}, {"title": "English two"}]},
            {"api_key": "x", "base_url": "https://example.com", "model": "test"},
        ))

    assert translated["global_affairs"] == [{"title": "English one", "title_zh": "中文一"}]
    assert metrics["global_affairs"]["headline_translated"] == 1
    assert metrics["global_affairs"]["headline_translation_failed"] == 1


def test_build_report_prefers_quantity_for_daily_fill(tmp_path):
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
            "us_politics": {"label": "美国政局", "target_items": 2, "max_items": 2, "headline_items": 2},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 0, "max_items": 0, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 0, "max_items": 0, "headline_items": 0},
        },
        fallback_candidates_by_column={
            "us_politics": [
                {"title": "Fallback C", "summary": "摘要 C", "content": "正文 C", "column": "us_politics"},
                {"title": "Fallback D", "summary": "摘要 D", "content": "正文 D", "column": "us_politics"},
            ]
        },
        min_llm_score=65,
    )
    scored_events = [
        {
            "title": "High A",
            "source": "A",
            "score": 90,
            "summary": "摘要A",
            "content": "正文A",
            "column": "us_politics",
            "event_key": "a",
            "language": "en",
            "tags": [],
            "source_links": [{"title": "A", "url": "https://example.com/a"}],
            "is_hard_news": True,
        },
        {
            "title": "Low B",
            "source": "B",
            "score": 50,
            "summary": "摘要B",
            "content": "正文B",
            "column": "us_politics",
            "event_key": "b",
            "language": "en",
            "tags": [],
            "source_links": [{"title": "B", "url": "https://example.com/b"}],
            "is_hard_news": True,
        },
    ]
    config = {"rules": {"quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [
            {
                "title_zh": event["title"],
                "reader_body": f'{event["title"]} 正文。',
                "core_facts": f'{event["title"]} 正文。',
            }
            for event in kwargs["events"]
        ]

    async def _fake_translate(titles, ai_config):
        return [f"中文 {title}" for title in titles]

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.translate_headline_titles", new=AsyncMock(side_effect=_fake_translate)):
        stats = build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    metrics = stats["metrics"]["columns"]["us_politics"]
    assert metrics["detailed_filled"] == 2
    assert metrics["headline_filled"] == 2
    assert metrics["detailed_filled_from_low_score"] == 1
    assert metrics["headline_filled_from_non_hard_news"] == 2
    assert metrics["headline_translated"] == 2


def test_build_report_injects_weekly_overview_into_meta_and_columns(tmp_path):
    spec = ReportSpec(
        report_type="weekly",
        report_key="2026-W25",
        title="测试周报",
        since=datetime(2026, 6, 15, tzinfo=timezone.utc),
        until=datetime(2026, 6, 22, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "weekly"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 2, "max_items": 2, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 0, "max_items": 0, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 0, "max_items": 0, "headline_items": 0},
        },
        allow_headline_only=False,
    )
    scored_events = [{
        "title": "美国事件",
        "source": "Example",
        "score": 90,
        "summary": "摘要",
        "content": "正文",
        "column": "us_politics",
        "event_key": "weekly-a",
        "language": "zh",
        "tags": [],
        "source_links": [],
        "is_hard_news": True,
    }]
    config = {"rules": {"quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [{
            "title_zh": "美国事件",
            "reader_body": "美国事件正文。",
            "core_facts": "美国事件正文。",
        }]

    overview = {
        "summary": "本周综述。",
        "themes": ["主题甲", "主题乙"],
        "watchlist": ["观察点一"],
        "column_analyses": {"us_politics": "美国政局本周主线。"},
    }

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.generate_periodical_overview", new=AsyncMock(return_value=overview)), \
         patch("report_engine.save_daily_report", return_value=("weekly.md", "weekly.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml") as save_feed:
        build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    meta = save_report.call_args.args[0]
    columns = save_report.call_args.args[1]
    feed_meta = save_feed.call_args.args[0]

    assert meta["lead"] == "本周综述。"
    assert meta["overview"] == {
        "summary": "本周综述。",
        "themes": ["主题甲", "主题乙"],
        "watchlist": ["观察点一"],
    }
    assert columns["us_politics"]["analysis"] == "美国政局本周主线。"
    assert feed_meta["overview"]["themes"] == ["主题甲", "主题乙"]


def test_build_report_injects_monthly_overview_into_meta_and_columns(tmp_path):
    spec = ReportSpec(
        report_type="monthly",
        report_key="2026-06",
        title="测试月报",
        since=datetime(2026, 6, 1, tzinfo=timezone.utc),
        until=datetime(2026, 7, 1, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "monthly"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 0, "max_items": 0, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 0, "max_items": 0, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 2, "max_items": 2, "headline_items": 0},
        },
        allow_headline_only=False,
    )
    scored_events = [{
        "title": "经济事件",
        "source": "Example",
        "score": 91,
        "summary": "摘要",
        "content": "正文",
        "column": "economy",
        "event_key": "monthly-a",
        "language": "zh",
        "tags": [],
        "source_links": [],
        "is_hard_news": True,
    }]
    config = {"rules": {"quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [{
            "title_zh": "经济事件",
            "reader_body": "经济事件正文。",
            "core_facts": "经济事件正文。",
        }]

    overview = {
        "summary": "本月综述。",
        "themes": ["主题甲"],
        "watchlist": ["观察点一"],
        "column_analyses": {"economy": "经济走势本月主线。"},
    }

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.generate_periodical_overview", new=AsyncMock(return_value=overview)), \
         patch("report_engine.save_daily_report", return_value=("monthly.md", "monthly.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml") as save_feed:
        build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    meta = save_report.call_args.args[0]
    columns = save_report.call_args.args[1]
    feed_meta = save_feed.call_args.args[0]

    assert meta["lead"] == "本月综述。"
    assert meta["overview"] == {
        "summary": "本月综述。",
        "themes": ["主题甲"],
        "watchlist": ["观察点一"],
    }
    assert columns["economy"]["analysis"] == "经济走势本月主线。"
    assert feed_meta["overview"]["watchlist"] == ["观察点一"]


def test_build_report_daily_skips_periodical_overview_generation(tmp_path):
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
            "us_politics": {"label": "美国政局", "target_items": 1, "max_items": 1, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 0, "max_items": 0, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 0, "max_items": 0, "headline_items": 0},
        },
    )
    scored_events = [{
        "title": "美国事件",
        "source": "Example",
        "score": 90,
        "summary": "摘要",
        "content": "正文",
        "column": "us_politics",
        "event_key": "daily-a",
        "language": "zh",
        "tags": [],
        "source_links": [],
        "is_hard_news": True,
    }]
    config = {"rules": {"quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [{
            "title_zh": "美国事件",
            "reader_body": "美国事件正文。",
            "core_facts": "美国事件正文。",
        }]

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.generate_periodical_overview", new=AsyncMock(return_value={})) as overview, \
         patch("report_engine.save_daily_report", return_value=("daily.md", "daily.html")), \
         patch("report_engine.save_feed", return_value="feed.xml"):
        build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    overview.assert_not_called()


def test_build_report_periodical_overview_failure_falls_back_to_empty_payload(tmp_path):
    spec = ReportSpec(
        report_type="weekly",
        report_key="2026-W25",
        title="测试周报",
        since=datetime(2026, 6, 15, tzinfo=timezone.utc),
        until=datetime(2026, 6, 22, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "weekly"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 1, "max_items": 1, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 0, "max_items": 0, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 0, "max_items": 0, "headline_items": 0},
        },
        allow_headline_only=False,
    )
    scored_events = [{
        "title": "美国事件",
        "source": "Example",
        "score": 90,
        "summary": "摘要",
        "content": "正文",
        "column": "us_politics",
        "event_key": "weekly-fallback",
        "language": "zh",
        "tags": [],
        "source_links": [],
        "is_hard_news": True,
    }]
    config = {"rules": {"quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4}}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        return [{
            "title_zh": "美国事件",
            "reader_body": "美国事件正文。",
            "core_facts": "美国事件正文。",
        }]

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.generate_periodical_overview", new=AsyncMock(side_effect=RuntimeError("overview timeout"))), \
         patch("report_engine.save_daily_report", return_value=("weekly.md", "weekly.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml") as save_feed:
        stats = build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    meta = save_report.call_args.args[0]
    columns = save_report.call_args.args[1]
    feed_meta = save_feed.call_args.args[0]

    assert meta["lead"] == ""
    assert meta["overview"] == {}
    assert columns["us_politics"]["analysis"] == ""
    assert feed_meta["overview"] == {}
    assert stats["metrics"]["ai"]["overview_failure"] == "overview timeout"
