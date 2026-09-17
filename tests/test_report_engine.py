"""报告编排器测试 — ReportSpec、质量门禁、要点提炼、栏级降级"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from report_engine import (
    PeriodicalOverview,
    ReportPreparation,
    ReportSpec,
    _audit_daily_content,
    _build_fallback_detailed_event,
    _build_periodical_gate_result,
    _build_periodical_overview_fallback,
    _dedupe_daily_column_events,
    _normalize_detailed_events_to_chinese,
    _normalize_headline_only_by_column,
    _build_periodical_overview_payload,
    _generate_all_column_digests,
    _prepare_report_inputs,
    _translate_headline_only_by_column,
    build_reader_highlights,
    build_report,
    sanitize_or_validate_events,
)

from ai_analyzer import generate_daily_overview, generate_periodical_overview


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


def test_generate_all_column_digests_serializes_bigmodel(monkeypatch):
    active = 0
    max_active = 0

    async def fake_generate_column_digest(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [{"title_zh": kwargs["column_label"], "reader_body": "正文"}]

    monkeypatch.setattr("report_engine.generate_column_digest", fake_generate_column_digest)

    asyncio.run(_generate_all_column_digests(
        {"us_politics": {"label": "美国政局"}, "economy": {"label": "经济走势"}},
        {"us_politics": [{"title": "A"}], "economy": [{"title": "B"}]},
        "",
        {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
        10,
        20,
    ))

    assert max_active == 1


def test_generate_all_column_digests_serializes_baicai(monkeypatch):
    active = 0
    max_active = 0

    async def fake_generate_column_digest(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [{"title_zh": kwargs["column_label"], "reader_body": "正文"}]

    monkeypatch.setattr("report_engine.generate_column_digest", fake_generate_column_digest)

    asyncio.run(_generate_all_column_digests(
        {"us_politics": {"label": "美国政局"}, "economy": {"label": "经济走势"}},
        {"us_politics": [{"title": "A"}], "economy": [{"title": "B"}]},
        "",
        {"base_url": "https://api.baicai798.cn/v1"},
        10,
        20,
    ))

    assert max_active == 1


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


def test_sanitize_or_validate_events_drops_body_date_outside_daily_window():
    events = [{
        "title_zh": "旧事件",
        "reader_body": "6 月 3 日，USTR 公布一项旧程序安排。该安排已不属于本期日报窗口。",
    }]

    cleaned, issues = sanitize_or_validate_events(events, {
        "require_date_in_body": True,
        "allowed_body_dates": ["2026-07-02", "2026-07-03"],
        "body_date_year": 2026,
    })

    assert cleaned == []
    assert any("正文日期不在日报窗口: 2026-06-03" in issue for issue in issues)


def test_sanitize_or_validate_events_rewrites_body_date_when_event_date_in_window():
    events = [{
        "title_zh": "旧事件",
        "reader_body": "6 月 3 日，USTR 公布一项旧程序安排。该安排已不属于本期日报窗口。",
        "event_date": "2026-07-03",
        "freshness_date": "2026-07-03",
    }]

    cleaned, issues = sanitize_or_validate_events(events, {
        "require_date_in_body": True,
        "allowed_body_dates": ["2026-07-02", "2026-07-03"],
        "body_date_year": 2026,
    })

    assert len(cleaned) == 1
    assert cleaned[0]["reader_body"].startswith("7 月 3 日")
    assert any("正文日期已重写" in issue for issue in issues)


def test_dedupe_daily_column_events_removes_similar_titles_and_event_keys():
    results, metrics = _dedupe_daily_column_events({
        "us_politics": [
            {"event_key": "a", "title_zh": "最高法院裁定协同党派竞选支出限制违宪"},
            {"event_key": "a", "title_zh": "重复 event key"},
            {"title_zh": "最高法院在 NRSC v. FEC 中认定相关竞选支出限制违宪"},
            {"title_zh": "FTC 就药企垄断问题提交法庭意见书"},
        ],
    })

    assert [event["title_zh"] for event in results["us_politics"]] == [
        "最高法院裁定协同党派竞选支出限制违宪",
        "FTC 就药企垄断问题提交法庭意见书",
    ]
    assert metrics["us_politics"]["deduped_detailed"] == 2


def test_audit_daily_content_counts_common_content_problems():
    metrics = _audit_daily_content(
        {
            "us_politics": {
                "detailed_events": [
                    {
                        "title_zh": "最高法院裁定协同党派竞选支出限制违宪",
                        "reader_body": "7 月 3 日，最高法院裁定相关限制违宪。政党支出安排将受到影响。",
                    },
                    {
                        "title_zh": "最高法院在 NRSC v. FEC 中认定相关竞选支出限制违宪",
                        "reader_body": "6 月 3 日，最高法院裁定相关限制违宪。现有材料未提供更多可核验细节。",
                    },
                    {
                        "title_zh": "FTC 就 Aurobindo 和 Lannett 的交易采取行动，以防止美国人承",
                        "reader_body": "7 月 3 日，FTC 采取行动。相关交易仍待后续审查。",
                    },
                ],
            },
        },
        ["2026-07-02", "2026-07-03"],
        2026,
    )

    assert metrics == {
        "duplicate_titles": 1,
        "old_body_dates": 1,
        "fallback_boilerplate": 1,
        "truncated_titles": 1,
        "meta_commentary": 0,
        "pipeline_leak": 0,
        "untranslated_terms": 0,
        "long_titles": 2,
    }


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


def test_build_reader_highlights_round_robin_across_columns():
    columns = {
        "us_politics": [{"title_zh": "美国一"}, {"title_zh": "美国二"}],
        "global_affairs": [{"title_zh": "国际一"}, {"title_zh": "国际二"}],
        "technology": [{"title_zh": "科技一"}],
    }

    highlights = build_reader_highlights(columns, limit=4)

    assert highlights == ["美国一", "国际一", "科技一", "美国二"]


def test_build_periodical_overview_payload_from_dataclass():
    overview = PeriodicalOverview(
        summary="本周综述。",
        themes=["主题甲", "", "主题乙"],
        watchlist=["观察点一", ""],
        column_analyses={"us_politics": "主线。"},
    )

    payload = _build_periodical_overview_payload(overview)

    assert payload == {
        "summary": "本周综述。",
        "themes": ["主题甲", "主题乙"],
        "watchlist": ["观察点一"],
    }


def test_generate_periodical_overview_cleans_and_limits_output():
    columns = {
        "us_politics": {
            "analysis": "美国政局本周主线。",
            "detailed_events": [
                {"title_zh": "长标题一长标题一长标题一", "reader_body": "正文一。"},
                {"title_zh": "事件二", "reader_body": "正文二。"},
            ],
            "headline_only_events": [],
        },
        "global_affairs": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "technology": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "economy": {"analysis": "", "detailed_events": [], "headline_only_events": []},
    }
    ai_config = {"base_url": "https://example.com", "api_key": "x", "model": "test"}
    raw = {
        "summary": "  本周综述。\n\n可以看出 形势复杂，总体来看 经济与政治联动。  " + "长" * 260,
        "themes": ["主题甲", "主题甲", "", "主题乙" * 20, "主题丙"],
        "watchlist": ["观察点一", "", "观察点二" * 20, "观察点三"],
        "column_analyses": {
            "us_politics": "  美国政局本周主线。\n可以看出。  " + "长" * 150,
            "global_affairs": "",
            "technology": "技术栏主线。",
            "economy": "经济栏主线。",
        },
    }

    class _DummyCall:
        async def __call__(self, *args, **kwargs):
            return json.dumps(raw, ensure_ascii=False)

    with patch("ai_analyzer._call_llm", new=_DummyCall()):
        overview = __import__("asyncio").run(generate_periodical_overview(
            report_type="weekly",
            title="测试周报",
            highlights=["要点一", "要点二"],
            columns=columns,
            ai_config=ai_config,
        ))

    assert overview["summary"].startswith("本周综述，经济与政治联动。")
    assert len(overview["summary"]) == 220
    assert overview["themes"] == ["主题甲", "主题乙主题乙主题乙主题乙主题乙主题乙主题乙主题乙", "主题丙"]
    assert overview["watchlist"] == ["观察点一", "观察点二观察点二观察点二观察点二观察点二观察点二观察点二观察点二", "观察点三"]
    assert overview["column_analyses"]["us_politics"].startswith("美国政局本周主线。")
    assert len(overview["column_analyses"]["us_politics"]) == 110
    assert overview["column_analyses"]["global_affairs"] == ""


def test_generate_daily_overview_cleans_summary():
    columns = {
        "us_politics": {
            "analysis": "",
            "detailed_events": [
                {"title_zh": "白宫推动新措施", "reader_body": "白宫推动新措施并与国会协调推进。"},
            ],
            "headline_only_events": [],
        },
        "global_affairs": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "technology": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "economy": {"analysis": "", "detailed_events": [], "headline_only_events": []},
    }
    ai_config = {"base_url": "https://example.com", "api_key": "x", "model": "test"}

    class _DummyCall:
        async def __call__(self, *args, **kwargs):
            return json.dumps({
                "summary": "  可以看出 白宫与国会围绕政策推进重新拉开拉锯，整体来看 市场与政治议程重新联动。  "
            }, ensure_ascii=False)

    with patch("ai_analyzer._call_llm", new=_DummyCall()):
        summary = __import__("asyncio").run(generate_daily_overview(
            title="测试日报",
            columns=columns,
            ai_config=ai_config,
        ))

    assert summary == "白宫与国会围绕政策推进重新拉开拉锯，市场与政治议程重新联动"


def test_generate_all_column_digests_falls_back_per_column():
    columns_cfg = {
        "us_politics": {"label": "美国政局"},
        "global_affairs": {"label": "国际局势"},
    }
    candidates = {
        "us_politics": [{"title": "重要事件", "summary": "摘要一。摘要二。"}],
        "global_affairs": [{
            "title": "国际事件",
            "summary": "国际谈判代表就安全安排继续磋商，并把停火监督机制列入下一轮议程。相关安排将影响边境管控、人道援助通道、监督机制执行和后续外交协调。各方还将继续提交书面意见。",
            "freshness_date": "2026-07-03",
            "event_date": "2026-07-03",
            "freshness_status": "today",
        }],
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
    assert "国际谈判" in results["global_affairs"][0]["reader_body"]
    assert failures == {"global_affairs": "llm timeout"}


def test_generate_all_column_digests_fallback_keeps_more_context():
    columns_cfg = {"us_politics": {"label": "美国政局"}}
    long_summary = "政策团队围绕预算、监管和外交议程展开密集谈判。相关安排将影响委员会审议、部门执行、行业合规和后续外交协调。各方还将继续提交书面意见，并说明谈判结果如何影响后续政策时间表。"
    candidates = {
        "us_politics": [
            {
                "title": f"事件{i}",
                "summary": long_summary,
                "freshness_date": "2026-07-03",
                "event_date": "2026-07-03",
                "freshness_status": "today",
            }
            for i in range(1, 7)
        ],
    }

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=RuntimeError("rate limited"))):
        results, failures = asyncio.run(_generate_all_column_digests(
            columns_cfg=columns_cfg,
            column_candidates=candidates,
            history_context="",
            ai_config={},
            word_count_min=100,
            word_count_max=200,
        ))

    assert [event["title_zh"] for event in results["us_politics"]] == ["事件1", "事件2", "事件3", "事件4", "事件5"]
    assert len(results["us_politics"][0]["reader_body"]) >= 80
    assert "现有材料未提供" not in results["us_politics"][0]["reader_body"]
    assert failures == {"us_politics": "rate limited"}


def test_prepare_report_inputs_extracts_shared_context():
    spec = ReportSpec(
        report_type="daily",
        report_key="2026-06-19",
        title="测试日报",
        since=datetime(2026, 6, 18, tzinfo=timezone.utc),
        until=datetime(2026, 6, 19, tzinfo=timezone.utc),
        output_dir="docs/daily",
        feed_path="docs/feed.xml",
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 1, "max_items": 1, "headline_items": 1},
            "global_affairs": {"label": "国际局势", "target_items": 0, "max_items": 0, "headline_items": 0},
        },
        fallback_candidates_by_column={
            "us_politics": [{"title": "备用条目", "summary": "备用摘要", "content": "备用正文", "column": "us_politics"}],
        },
        min_llm_score=65,
    )
    scored_events = [
        {
            "title": "高分事件",
            "source": "A",
            "score": 90,
            "summary": "摘要 A",
            "content": "正文 A",
            "column": "us_politics",
            "event_key": "a",
            "language": "zh",
            "tags": ["cn_source"],
            "source_links": [],
            "is_hard_news": True,
        },
        {
            "title": "低分事件",
            "source": "B",
            "score": 50,
            "summary": "摘要 B",
            "content": "正文 B",
            "column": "us_politics",
            "event_key": "b",
            "language": "en",
            "tags": [],
            "source_links": [],
            "is_hard_news": True,
        },
    ]
    metrics = {"columns": {}, "ai": {}}

    class _DummyDb:
        def fetch_since(self, since):
            return []

    preparation = _prepare_report_inputs(spec, scored_events, _DummyDb(), metrics)

    assert isinstance(preparation, ReportPreparation)
    assert len(preparation.merged_events) == 2
    assert preparation.by_column["us_politics"][0]["title"] == "高分事件"
    assert preparation.column_candidates["us_politics"][0]["title"] == "高分事件"
    assert preparation.column_headline_only["us_politics"][0]["title"] == "低分事件"
    assert preparation.history_context == ""
    assert preparation.metrics["cn_source_selected"] == 1
    assert preparation.metrics["columns"]["us_politics"]["post_merge_scored"] == 2


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


def test_normalize_headline_only_by_column_filters_cryptic_titles():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "法案 4238 号", "summary": "众议院推进法案。"},
            {"title_zh": "白宫要求国会加快表决", "summary": "白宫要求国会尽快推进相关表决。第二句。"},
        ]
    })

    assert normalized["us_politics"] == [{
        "title_zh": "白宫要求国会加快表决",
        "summary": "白宫要求国会尽快推进相关表决。第二句。",
        "reader_body": "白宫要求国会尽快推进相关表决。",
    }]
    assert metrics["us_politics"]["headline_cryptic_dropped"] == 1
    assert metrics["us_politics"]["headline_reader_body_missing"] == 0


def test_normalize_headline_only_by_column_drops_english_fragments():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "President Trump will welcome a group of American farmers", "summary": "President Trump will welcome a group of American farmers from across the U.S. to"},
            {"title_zh": "白宫要求国会尽快表决", "summary": "白宫要求国会尽快推进相关表决。"},
        ]
    })

    assert normalized["us_politics"] == [{
        "title_zh": "白宫要求国会尽快表决",
        "summary": "白宫要求国会尽快推进相关表决。",
        "reader_body": "白宫要求国会尽快推进相关表决。",
    }]
    assert metrics["us_politics"]["headline_reader_body_missing"] == 1


def test_normalize_headline_only_by_column_drops_uninformative_bill_items():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "《2025 年防止金融剥削法案》", "summary": "《2025 年防止金融剥削法案》（H.R. 2478）被提交至国会审议。"},
        ]
    })

    assert normalized["us_politics"] == []
    assert metrics["us_politics"]["headline_reader_body_missing"] == 1


def test_normalize_detailed_events_to_chinese_drops_english_items():
    normalized, metrics = _normalize_detailed_events_to_chinese({
        "technology": [
            {
                "title_zh": "FTC 对 AI 基础设施市场提起反垄断诉讼",
                "reader_body": "FTC 对 AI 基础设施市场提起反垄断诉讼，并指控相关企业限制竞争。",
                "core_facts": "FTC 对 AI 基础设施市场提起反垄断诉讼，并指控相关企业限制竞争。",
            },
            {
                "title_zh": "FTC files antitrust complaint",
                "reader_body": "The commission filed a complaint in federal court.",
                "core_facts": "The commission filed a complaint in federal court.",
            },
        ]
    })

    assert normalized["technology"] == [{
        "title_zh": "FTC 对 AI 基础设施市场提起反垄断诉讼",
        "reader_body": "FTC 对 AI 基础设施市场提起反垄断诉讼，并指控相关企业限制竞争。",
        "core_facts": "FTC 对 AI 基础设施市场提起反垄断诉讼，并指控相关企业限制竞争。",
    }]
    assert metrics["technology"]["detailed_translation_failed"] == 1


def test_build_report_daily_falls_back_when_digest_outputs_empty_columns(tmp_path):
    """2026-06-28 发布回归：AI 栏目写作被过滤为空时，候选摘要应补成重点解析。"""
    spec = ReportSpec(
        report_type="daily",
        report_key="2026-06-28",
        title="测试日报",
        since=datetime(2026, 6, 27, tzinfo=timezone.utc),
        until=datetime(2026, 6, 28, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "daily"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="https://example.com",
        column_quotas={
            "us_politics": {"label": "美国政局", "target_items": 1, "max_items": 1, "headline_items": 0},
            "global_affairs": {"label": "国际局势", "target_items": 1, "max_items": 1, "headline_items": 0},
            "technology": {"label": "科技前沿", "target_items": 1, "max_items": 1, "headline_items": 0},
            "economy": {"label": "经济走势", "target_items": 1, "max_items": 1, "headline_items": 0},
        },
    )
    scored_events = [
        {
            "title": "White House meeting",
            "source": "Example",
            "score": 90,
            "summary": "白宫与国会领导人举行会议，讨论预算安排和后续表决节奏。双方还把委员会审议、拨款期限、政府部门执行准备和后续协调范围列入议程。相关办公室将继续提交书面方案。",
            "content": "白宫与国会领导人举行会议，讨论预算安排和后续表决节奏。双方还把委员会审议、拨款期限、政府部门执行准备和后续协调范围列入议程。相关办公室将继续提交书面方案。",
            "column": "us_politics",
            "event_key": "us_budget_20260628",
            "event_date": "2026-06-28",
            "freshness_date": "2026-06-28",
            "freshness_status": "today",
            "language": "en",
            "tags": [],
            "source_links": [{"title": "Example", "url": "https://example.com/us"}],
            "is_hard_news": True,
        },
        {
            "title": "国际谈判继续推进",
            "source": "Example",
            "score": 89,
            "summary": "多国代表继续推进安全谈判，并把后续文本审议列入下一轮议程。谈判安排还涉及监督机制、执行时间表、各方后续书面反馈和现场协调安排。秘书处将汇总各方文本。",
            "content": "多国代表继续推进安全谈判，并把后续文本审议列入下一轮议程。谈判安排还涉及监督机制、执行时间表、各方后续书面反馈和现场协调安排。秘书处将汇总各方文本。",
            "column": "global_affairs",
            "event_key": "security_talks_20260628",
            "event_date": "2026-06-28",
            "freshness_date": "2026-06-28",
            "freshness_status": "today",
            "language": "zh",
            "tags": [],
            "source_links": [{"title": "Example", "url": "https://example.com/global"}],
            "is_hard_news": True,
        },
        {
            "title": "AI regulation update",
            "source": "Example",
            "score": 88,
            "summary": "监管机构发布人工智能合规指引，要求平台补充风险披露和审计材料。相关要求还覆盖模型评估、用户告知、企业内部责任链条和后续整改流程。企业需要准备补充说明。",
            "content": "监管机构发布人工智能合规指引，要求平台补充风险披露和审计材料。相关要求还覆盖模型评估、用户告知、企业内部责任链条和后续整改流程。企业需要准备补充说明。",
            "column": "technology",
            "event_key": "ai_regulation_20260628",
            "event_date": "2026-06-28",
            "freshness_date": "2026-06-28",
            "freshness_status": "today",
            "language": "en",
            "tags": [],
            "source_links": [{"title": "Example", "url": "https://example.com/tech"}],
            "is_hard_news": True,
        },
        {
            "title": "经济数据更新",
            "source": "Example",
            "score": 87,
            "summary": "政府发布新的经济数据，显示就业和价格指标继续影响政策预期。市场参与者将据此调整利率路径、财政判断、企业成本假设和资产配置安排。后续数据将影响政策定价。",
            "content": "政府发布新的经济数据，显示就业和价格指标继续影响政策预期。市场参与者将据此调整利率路径、财政判断、企业成本假设和资产配置安排。后续数据将影响政策定价。",
            "column": "economy",
            "event_key": "economic_data_20260628",
            "event_date": "2026-06-28",
            "freshness_date": "2026-06-28",
            "freshness_status": "today",
            "language": "zh",
            "tags": [],
            "source_links": [{"title": "Example", "url": "https://example.com/economy"}],
            "is_hard_news": True,
        },
    ]
    config = {
        "rules": {"quality_gate": {"min_chars": 40, "max_chars": 260, "min_sentences": 2, "max_sentences": 4}},
        "format_contract": {
            "require_non_empty_columns": True,
            "require_detailed_events": True,
            "require_date_in_body": True,
        },
    }

    class _DummyDb:
        def fetch_since(self, since):
            return []

    async def _fake_digest(**kwargs):
        if kwargs["column_key"] == "global_affairs":
            return [{
                "title_zh": "国际谈判继续推进",
                "reader_body": "6 月 28 日，多国代表继续推进安全谈判。下一轮议程将审议后续文本。",
                "core_facts": "6 月 28 日，多国代表继续推进安全谈判。下一轮议程将审议后续文本。",
            }]
        if kwargs["column_key"] == "technology":
            return [{
                "title_zh": "AI regulation update",
                "reader_body": "The agency released guidance.",
                "core_facts": "The agency released guidance.",
            }]
        return []

    with patch("report_engine.generate_column_digest", new=AsyncMock(side_effect=_fake_digest)), \
         patch("report_engine.generate_daily_overview", new=AsyncMock(return_value="")), \
         patch("report_engine.save_daily_report", return_value=("daily.md", "daily.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml"):
        stats = build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    columns = save_report.call_args.args[1]
    assert all(
        columns[col_key]["detailed_events"] or columns[col_key]["headline_only_events"]
        for col_key in spec.column_quotas
    )
    assert columns["us_politics"]["detailed_events"][0]["title_zh"].startswith("白宫与国会")
    assert columns["technology"]["detailed_events"][0]["title_zh"].startswith("监管机构")
    assert stats["metrics"]["columns"]["us_politics"]["detailed_fallback_added"] == 1
    assert stats["metrics"]["columns"]["technology"]["detailed_translation_failed"] == 1
    assert stats["metrics"]["columns"]["technology"]["detailed_fallback_added"] == 1
    assert stats["total_selected"] == 3


def test_normalize_headline_only_by_column_keeps_bill_items_with_clear_summary():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "《大街竞争法案》", "summary": "美国国会提出《大街竞争法案》，旨在支持中小企业发展。第二句。"},
        ]
    })

    assert normalized["us_politics"] == [{
        "title_zh": "《大街竞争法案》",
        "summary": "美国国会提出《大街竞争法案》，旨在支持中小企业发展。第二句。",
        "reader_body": "美国国会提出《大街竞争法案》，旨在支持中小企业发展。",
    }]
    assert metrics["us_politics"]["headline_reader_body_missing"] == 0


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
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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


def test_build_fallback_detailed_event_rejects_old_background_date():
    candidate = {
        "title_zh": "美国贸易代表与中国副总理举行会谈，讨论双边贸易关系",
        "summary": "双方讨论双边贸易关系，并就后续沟通安排交换意见。",
        "freshness_date": "2026-06-30",
        "event_date": "2026-03-01",
        "freshness_status": "today",
    }

    assert _build_fallback_detailed_event(candidate) is None


def test_build_fallback_detailed_event_meets_daily_quality_length():
    candidate = {
        "title_zh": "FTC 就 AI 准确性政策声明征求公众意见",
        "summary": "FTC 就 AI 准确性政策声明征求公众意见，要求企业说明自动化系统如何降低错误输出。该声明将影响平台、开发者、消费者保护流程和使用 AI 决策工具的企业。",
        "freshness_date": "2026-07-03",
        "event_date": "2026-07-03",
        "freshness_status": "today",
    }

    event = _build_fallback_detailed_event(candidate)

    assert event is not None
    body = event["reader_body"]
    assert 40 <= len(body) <= 260
    assert body.count("。") >= 2


def test_build_fallback_detailed_event_rejects_short_summary():
    candidate = {
        "title_zh": "FTC 就 AI 准确性政策声明征求公众意见",
        "summary": "FTC 就 AI 准确性政策声明征求公众意见。",
        "freshness_date": "2026-07-03",
        "event_date": "2026-07-03",
        "freshness_status": "today",
    }

    assert _build_fallback_detailed_event(candidate) is None


def test_build_fallback_detailed_event_rejects_truncated_title():
    candidate = {
        "title_zh": "FTC 就 Aurobindo 和 Lannett 的交易采取行动，以防止美国人承",
        "summary": "FTC 就 Aurobindo 和 Lannett 的交易采取行动，以防止美国人承担更高的药品成本。",
        "freshness_date": "2026-06-30",
        "event_date": "2026-06-30",
        "freshness_status": "today",
    }

    assert _build_fallback_detailed_event(candidate) is None


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
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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


def test_build_periodical_gate_result_blocks_single_source_sparse_window():
    spec = ReportSpec(
        report_type="weekly",
        report_key="2026-W25",
        title="测试周报",
        since=datetime(2026, 6, 15, tzinfo=timezone.utc),
        until=datetime(2026, 6, 22, tzinfo=timezone.utc),
        output_dir="docs/news/weekly",
        feed_path="docs/feeds/news.xml",
        base_url="",
        column_quotas={
            "us_politics": {"label": "美国政局"},
            "global_affairs": {"label": "国际局势"},
            "technology": {"label": "科技前沿"},
            "economy": {"label": "经济走势"},
        },
    )

    scored_events = [
        {
            "title": "美国事件",
            "source": "Source A",
            "score": 90,
            "summary": "摘要",
            "content": "正文",
            "column": "us_politics",
            "event_key": "a",
            "source_tier": 1,
            "is_hard_news": True,
        }
    ]

    class _DummyDb:
        def source_health_rows(self, **kwargs):
            return [{
                "source": "Source A",
                "source_tier": 1,
                "column": "us_politics",
                "article_count": 1,
                "scored_count": 1,
                "strong_scored_count": 1,
                "active_days": 1,
                "latest_seen_at": "2026-06-21T00:00:00+00:00",
                "fetch_mode": "rss",
                "configured_column": "us_politics",
                "configured_source_tier": 1,
                "source_enabled": True,
            }]

        def fetch_log_rows(self, **kwargs):
            return []

    config = {"rules": {"periodical_gate": {"min_total_events": 2, "min_columns_with_events": 2, "min_hard_news": 2}}}
    result = _build_periodical_gate_result(spec, scored_events, _DummyDb(), config)

    assert result["gate_failed"] is True
    assert any("总事件量不足" in item for item in result["reasons"])
    assert any("覆盖栏目不足" in item for item in result["reasons"])


def test_build_periodical_overview_fallback_uses_existing_column_text():
    columns = {
        "us_politics": {
            "detailed_events": [{"title_zh": "白宫推动新措施", "reader_body": "6 月 28 日，白宫推动新措施并协调国会。"}],
            "headline_only_events": [],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
    }
    overview = _build_periodical_overview_fallback("weekly", "测试周报", ["要点一"], columns)

    assert "测试周报" in overview.summary
    assert overview.themes[0] == "白宫推动新措施"
    assert "us_politics" in overview.column_analyses


def test_build_report_daily_uses_daily_overview_generation(tmp_path):
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
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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
         patch("report_engine.generate_daily_overview", new=AsyncMock(return_value="日报总览导语")) as daily_overview, \
         patch("report_engine.generate_periodical_overview", new=AsyncMock(return_value={})) as overview, \
         patch("report_engine.save_daily_report", return_value=("daily.md", "daily.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml"):
        build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    meta = save_report.call_args.args[0]
    assert meta["lead"] == ""
    daily_overview.assert_called_once()
    overview.assert_not_called()


def test_build_report_daily_overview_failure_falls_back_to_empty_lead(tmp_path):
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
        "event_key": "daily-overview-fallback",
        "language": "zh",
        "tags": [],
        "source_links": [],
        "is_hard_news": True,
    }]
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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
         patch("report_engine.generate_daily_overview", new=AsyncMock(side_effect=RuntimeError("daily overview timeout"))), \
         patch("report_engine.save_daily_report", return_value=("daily.md", "daily.html")) as save_report, \
         patch("report_engine.save_feed", return_value="feed.xml"):
        stats = build_report(spec, scored_events, config, {}, _DummyDb(), phase_metrics={"columns": {}, "ai": {}})

    meta = save_report.call_args.args[0]
    assert meta["lead"] == ""
    assert stats["metrics"]["ai"]["daily_overview_failure"] == "daily overview timeout"


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
    config = {"rules": {
        "quality_gate": {"min_chars": 1, "max_chars": 260, "min_sentences": 1, "max_sentences": 4},
        "periodical_gate": {"min_total_events": 1, "min_columns_with_events": 1, "min_hard_news": 1, "min_source_tiers": 1},
    }}

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

    assert "测试周报" in meta["lead"]
    assert meta["overview"]["summary"] == meta["lead"]
    assert columns["us_politics"]["analysis"] == "美国事件正文"
    assert feed_meta["overview"]["themes"] == ["美国事件"]
    assert stats["metrics"]["ai"]["overview_failure"] == "overview timeout"


def test_normalize_headline_falls_back_to_title_when_body_untranslated():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [{
            "title": "Senate passes budget bill",
            "title_zh": "参议院通过预算案",
            "summary": "The Senate passed the budget bill on Tuesday.",
            "content": "The Senate passed the budget bill on Tuesday.",
        }],
    })

    assert len(normalized["us_politics"]) == 1
    assert normalized["us_politics"][0]["reader_body"] == "参议院通过预算案"
    assert metrics["us_politics"]["headline_body_from_title"] == 1
    assert metrics["us_politics"]["headline_reader_body_missing"] == 0


def test_normalize_headline_prefers_chinese_body():
    normalized, _ = _normalize_headline_only_by_column({
        "us_politics": [{
            "title": "Senate passes budget bill",
            "title_zh": "参议院通过预算案",
            "summary": "参议院通过预算案，程序性表决过关。",
            "content": "参议院通过预算案，程序性表决过关。",
        }],
    })

    assert normalized["us_politics"][0]["reader_body"] == "参议院通过预算案，程序性表决过关。"


# ── AI 兜底扩写（重点解析过短/英文候选） ──


def test_ai_expand_fallback_events_fills_short_columns():
    import report_engine

    column_results = {"us_politics": []}
    column_candidates = {
        "us_politics": [
            {
                "title": "Iran war live: Republicans rebel",
                "link": "https://example.com/a",
                "summary": "众议院再次就伊朗战争权力决议投票。",
                "freshness_status": "today",
                "event_date": "2026-09-16",
            }
        ]
    }
    columns_cfg = {"us_politics": {"min_items": 3}}

    async def fake_bodies(entries, ai_config):
        return {entries[0]["link"]: "9 月 16 日，众议院再次就伊朗战争权力决议投票。表决结果与后续程序仍待确认，相关条款将影响总统动武权限。"}

    with patch.object(report_engine, "generate_fallback_bodies", fake_bodies):
        expanded, metrics = report_engine._ai_expand_fallback_events(
            column_results, column_candidates, columns_cfg, {},
        )

    events = expanded["us_politics"]
    assert len(events) == 1
    assert events[0]["reader_body"].startswith("9 月 16 日")
    assert metrics["us_politics"]["ai_fallback_added"] == 1


def test_ai_expand_fallback_events_skips_full_columns():
    import report_engine

    column_results = {"us_politics": [{"title_zh": "已有解析", "reader_body": "正文"}]}
    columns_cfg = {"us_politics": {"min_items": 1}}

    async def fake_bodies(entries, ai_config):  # pragma: no cover - 不应被调用
        raise AssertionError("columns 已满足 min_items 时不应调用 AI")

    with patch.object(report_engine, "generate_fallback_bodies", fake_bodies):
        expanded, metrics = report_engine._ai_expand_fallback_events(
            column_results, {}, columns_cfg, {},
        )

    assert metrics == {}
    assert expanded["us_politics"][0]["title_zh"] == "已有解析"


def test_ai_expand_fallback_events_requires_recent_candidates():
    import report_engine

    column_results = {"technology": []}
    column_candidates = {
        "technology": [
            {"title": "Old news", "link": "https://example.com/old", "freshness_status": "old_background"},
        ]
    }
    columns_cfg = {"technology": {"min_items": 3}}

    async def fake_bodies(entries, ai_config):  # pragma: no cover - 不应被调用
        raise AssertionError("无可选候选时不应调用 AI")

    with patch.object(report_engine, "generate_fallback_bodies", fake_bodies):
        expanded, metrics = report_engine._ai_expand_fallback_events(
            column_results, column_candidates, columns_cfg, {},
        )

    assert expanded["technology"] == []
    assert metrics == {}


# ── WP1: 元评论/管道泄漏审计 + 观点稿过滤 ──


def test_audit_daily_content_counts_meta_commentary_and_pipeline_leak():
    columns = {
        "us_politics": {
            "detailed_events": [
                {"title_zh": "测试事件", "reader_body": "9 月 16 日，事实一。报道把这一动作定位为重要进展。"},
                {"title_zh": "另一事件", "reader_body": "9 月 16 日，事实二。同一日 Google News 聚合条目也收录了这条 BBC 稿件。"},
            ]
        }
    }

    metrics = _audit_daily_content(columns, ["2026-09-15", "2026-09-16"], 2026)

    assert metrics["meta_commentary"] == 1
    assert metrics["pipeline_leak"] == 1


def test_normalize_headline_drops_opinion_and_promo_titles():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "尽管有警告，特朗普为何仍全力押注人工智能", "reader_body": "分析稿。"},
            {"title_zh": "Google 发布新工具，提供 AI 新洞察", "reader_body": "公关稿。"},
            {"title_zh": "众议院通过决议", "summary": "众议院通过决议。", "content": "众议院通过决议。"},
        ]
    })

    kept_titles = [item["title_zh"] for item in normalized["us_politics"]]
    assert kept_titles == ["众议院通过决议"]
    assert metrics["us_politics"]["headline_opinion_dropped"] == 1
    assert metrics["us_politics"]["headline_promo_dropped"] == 1


# ── 要点过滤补强（观点/公关/法案编号/跨层重复） ──


def test_normalize_headline_drops_analysis_and_promo_variants():
    normalized, metrics = _normalize_headline_only_by_column({
        "global_affairs": [
            {"title_zh": "分析最高法院关于邮寄投票的裁决及特朗普的反应", "summary": "分析稿。"},
            {"title_zh": "世界齐聚纽约：联合国大会高级别周的关键所在", "summary": "预告稿。"},
            {"title_zh": "以 AI 重新构想广告", "summary": "公关稿。"},
            {"title_zh": "众议院第 22 号法案：SAVE 法案", "summary": "法案条目。"},
            {"title_zh": "波兰在无人机袭击后重申支持乌克兰", "summary": "波兰重申对乌克兰的支持。"},
        ]
    })

    kept_titles = [item["title_zh"] for item in normalized["global_affairs"]]
    assert kept_titles == ["波兰在无人机袭击后重申支持乌克兰"]
    assert metrics["global_affairs"]["headline_opinion_dropped"] == 2
    assert metrics["global_affairs"]["headline_promo_dropped"] == 1
    assert metrics["global_affairs"]["headline_cryptic_dropped"] == 1


def test_normalize_headline_drops_cross_level_duplicate():
    normalized, metrics = _normalize_headline_only_by_column(
        {
            "global_affairs": [
                {"title_zh": "救援人员：加沙一栋战损建筑倒塌致 21 死，含 8 名儿童", "summary": "救援人员称，加沙一栋战损建筑倒塌。"},
                {"title_zh": "欧盟邀请加拿大成为首个联系国成员", "summary": "欧盟邀请加拿大成为联系国成员。"},
            ]
        },
        detailed_titles={
            "global_affairs": ["加沙城一栋建筑倒塌，至少 20 人死亡"],
        },
    )

    kept_titles = [item["title_zh"] for item in normalized["global_affairs"]]
    assert kept_titles == ["欧盟邀请加拿大成为首个联系国成员"]
    assert metrics["global_affairs"]["headline_duplicate_dropped"] == 1


def test_same_event_titles_not_fooled_by_shared_glossary_names():
    from report_engine import _same_event_titles

    # 同主体不同事件：不得判定为重复（特朗普在术语表中，其双字片段应被排除）
    assert not _same_event_titles(
        "特朗普与习近平讨论关税",
        "特朗普与普京讨论乌克兰",
    )
    # 同事件不同表述：应判定为重复
    assert _same_event_titles(
        "加沙城一栋建筑倒塌，至少 20 人死亡",
        "救援人员：加沙一栋战损建筑倒塌致 21 死，含 8 名儿童",
    )


def test_merge_headline_metrics_accumulates():
    from report_engine import _merge_headline_metrics

    metrics = {"us_politics": {"headline_opinion_dropped": 2}}
    _merge_headline_metrics(metrics, "us_politics", {"headline_opinion_dropped": 1, "headline_promo_dropped": 3})

    assert metrics["us_politics"]["headline_opinion_dropped"] == 3
    assert metrics["us_politics"]["headline_promo_dropped"] == 3


def test_normalize_headline_keeps_empty_text_items_using_title():
    normalized, metrics = _normalize_headline_only_by_column({
        "technology": [
            {"title_zh": "谷歌称部分 Pixel 手机用户遭零日攻击", "summary": "", "content": ""},
        ]
    })

    assert normalized["technology"][0]["reader_body"] == "谷歌称部分 Pixel 手机用户遭零日攻击"
    assert metrics["technology"]["headline_body_from_title"] == 1
    assert metrics["technology"]["headline_reader_body_missing"] == 0


def test_normalize_headline_dedupes_across_columns():
    """要点与其它栏目的明细重复时也应被丢弃（跨栏目去重）。"""
    normalized, metrics = _normalize_headline_only_by_column(
        {
            "us_politics": [
                {"title_zh": "欧盟邀请加拿大成为史上首个“准成员”", "summary": "欧盟邀请加拿大成为准成员。"},
                {"title_zh": "美国众议院通过拨款法案", "summary": "众议院通过拨款法案。"},
            ]
        },
        detailed_titles={
            "global_affairs": ["欧盟委员会主席冯德莱恩邀请加拿大成为欧盟史上首个“准成员”"],
        },
    )

    kept_titles = [item["title_zh"] for item in normalized["us_politics"]]
    assert kept_titles == ["美国众议院通过拨款法案"]
    assert metrics["us_politics"]["headline_duplicate_dropped"] == 1


def test_normalize_headline_drops_soft_news():
    normalized, metrics = _normalize_headline_only_by_column({
        "us_politics": [
            {"title_zh": "奥巴马夫妇爱犬桑尼去世：“热情又漂亮”", "summary": "奥巴马夫妇的爱犬去世。"},
            {"title_zh": "众议院通过拨款法案", "summary": "众议院通过拨款法案。"},
        ]
    })

    kept_titles = [item["title_zh"] for item in normalized["us_politics"]]
    assert kept_titles == ["众议院通过拨款法案"]
    assert metrics["us_politics"]["headline_soft_dropped"] == 1


def test_normalize_headline_drops_opinion_variants_added():
    normalized, metrics = _normalize_headline_only_by_column({
        "global_affairs": [
            {"title_zh": "世界齐聚纽约：联合国大会高级别周有何利害关系", "summary": "预告稿。"},
            {"title_zh": "俄非关系：莫斯科在非洲影响力日益增强", "summary": "综述稿。"},
        ]
    })

    kept_titles = [item["title_zh"] for item in normalized["global_affairs"]]
    # “有何利害关系”命中观点规则被丢弃；“俄非关系…”为综述类，暂未覆盖（记录为残留）
    assert kept_titles == ["俄非关系：莫斯科在非洲影响力日益增强"]
    assert metrics["global_affairs"]["headline_opinion_dropped"] == 1


# ── P0-2: 候选归档 ──


def test_write_candidates_archive_creates_score_and_selection(tmp_path):
    from pathlib import Path

    from report_engine import _write_candidates_archive

    spec = ReportSpec(
        report_type="daily",
        report_key="2026-09-17",
        title="测试日报",
        since=datetime(2026, 9, 16, 7, tzinfo=timezone.utc),
        until=datetime(2026, 9, 17, 7, tzinfo=timezone.utc),
        output_dir=str(tmp_path / "daily"),
        feed_path=str(tmp_path / "feed.xml"),
        base_url="",
        column_quotas={},
    )
    scored = [{
        "link": "https://example.com/a", "title": "A", "source": "S",
        "column": "us_politics", "score": 88, "is_hard_news": True,
        "newsworthiness": 0.9, "routine": 0.1, "event_key": "a_20260917",
    }]
    columns = {
        "us_politics": {
            "detailed_events": [{**scored[0], "title_zh": "标题", "source_links": []}],
            "headline_only_events": [],
        }
    }

    archive_dir = _write_candidates_archive(spec, scored, columns)

    score_payload = json.loads((Path(archive_dir) / "score.json").read_text(encoding="utf-8"))
    selection_payload = json.loads((Path(archive_dir) / "selection.json").read_text(encoding="utf-8"))

    assert score_payload["date"] == "2026-09-17"
    assert score_payload["coverage"]["start"].startswith("2026-09-16")
    assert score_payload["items"][0]["score"] == 88
    assert selection_payload["items"][0]["slot"] == "detailed"
    assert "score=88" in selection_payload["items"][0]["selection_reason"]


# ── P1-4: lead 头条层 ──


def test_select_lead_event_picks_top_newsworthiness():
    from report_engine import _select_lead_event

    columns = {
        "us_politics": {
            "detailed_events": [
                {"title_zh": "普通事件", "reader_body": "正文", "score": 80, "newsworthiness": 0.6},
            ],
        },
        "economy": {
            "detailed_events": [
                {"title_zh": "重大事件", "reader_body": "正文", "score": 88, "newsworthiness": 0.92},
            ],
        },
    }

    scored = [
        {"title_zh": "普通事件", "score": 80, "newsworthiness": 0.6},
        {"title_zh": "重大事件", "score": 88, "newsworthiness": 0.92},
    ]

    lead = _select_lead_event(columns, scored)

    assert lead is not None
    assert lead["title"] == "重大事件"
    assert lead["column"] == "economy"


def test_daily_markdown_renders_lead_block():
    from report_renderer import render_structured_markdown

    meta = {
        "title": "测试日报",
        "highlights": ["要点一"],
        "date": "2026-09-17",
        "lead_event": {"title": "今日头条事件", "body": "9 月 17 日，头条正文。", "column": "us_politics"},
    }
    columns = {
        "us_politics": {
            "detailed_events": [{"title_zh": "今日头条事件", "reader_body": "9 月 17 日，头条正文。"}],
            "headline_only_events": [],
        }
    }

    markdown = render_structured_markdown(meta, columns, report_type="daily")

    assert "## 今日头条" in markdown
    assert "**今日头条事件**" in markdown


# ── P1-5: 拒绝原因汇总 ──


def test_summarize_rejections_aggregates_across_levels():
    from report_engine import _summarize_rejections

    metrics = {
        "routine_notice_dropped": 2,
        "low_newsworthiness_dropped": 3,
        "events_merged_duplicates": 5,
        "columns": {
            "us_politics": {
                "headline_soft_dropped": 1,
                "headline_opinion_dropped": 2,
                "source_quota_dropped": 1,
            },
            "economy": {
                "headline_cryptic_dropped": 1,
            },
        },
    }

    summary = _summarize_rejections(metrics)

    assert summary["routine_notice"] == 2
    assert summary["low_newsworthiness"] == 3
    assert summary["duplicate_event"] == 5
    assert summary["soft_news"] == 1
    assert summary["opinion_piece"] == 2
    assert summary["source_quota"] == 1
    assert summary["cryptic_title"] == 1


def test_translate_headline_compacts_body_fallback_title():
    """翻译缺失时用正文压缩成短标题，不得整句正文当标题。"""
    import asyncio
    from unittest.mock import patch

    long_body = "澳工党政府在兰比及退伍军人强烈反对后，放弃对退伍军人联合医疗服务设 5000 澳元上限的计划。"

    with patch(
        "report_engine.translate_headline_titles",
        new=AsyncMock(return_value=[""]),
    ):
        normalized, metrics = asyncio.run(_translate_headline_only_by_column(
            {"technology": [{"title": "Australia drops plan", "summary": long_body, "content": long_body}]},
            {},
        ))

    items = normalized["technology"]
    assert len(items) == 1
    title = items[0]["title_zh"]
    assert len(title) <= 23
    assert title.endswith("…")


def test_normalize_compacts_long_headline_titles():
    long_title = "美联储时隔两年重启加息，但一次 25 个基点的举措本身已不是焦点——市场真正在押注的，是后续紧缩路径究竟延伸多远。"
    normalized, _ = _normalize_headline_only_by_column({
        "economy": [{"title_zh": long_title, "summary": "美联储重启加息。"}]
    })

    title = normalized["economy"][0]["title_zh"]
    assert len(title) <= 23
    assert title.endswith("…")
