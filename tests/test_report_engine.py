"""报告编排器测试 — ReportSpec、质量门禁、要点提炼"""

from datetime import datetime, timezone

from report_engine import (
    ReportSpec,
    build_reader_highlights,
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
