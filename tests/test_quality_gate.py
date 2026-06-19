"""质量门禁与 Pre-LLM 过滤测试"""

import re

from models import ContentItem, SourceType
from run_pipeline import (
    _sanitize_event_text,
    _validate_event,
    sanitize_or_validate_events,
    _pre_llm_hard_filter,
    _FORBIDDEN_LABELS,
    _FORBIDDEN_PHRASES,
)


# ── 辅助函数 ──

def _make_content_item(title: str, content: str = "", column: str = "us_politics") -> ContentItem:
    return ContentItem(
        id=f"test:{title}",
        source_type=SourceType.RSS,
        title=title,
        url=f"https://example.com/{title}",
        content=content,
        source_name="test",
        column=column,
    )


def _make_event(title: str = "测试事件", reader_body: str = "合格正文。这是第二句。") -> dict:
    return {
        "title_zh": title,
        "reader_body": reader_body,
        "core_facts": reader_body,
        "source_links": [],
        "is_followup": False,
    }


# ── 标签清理 ──


def test_sanitize_removes_forbidden_labels():
    text = "核心事实：最高法院作出裁定。这是变化。这是后果。"
    cleaned, issues = _sanitize_event_text(text)
    assert "核心事实：" not in cleaned
    assert any("标签残留" in i for i in issues)


def test_sanitize_removes_background_label():
    text = "背景与影响：此事影响深远。此前不同。现在改变了。对市场有影响。"
    cleaned, issues = _sanitize_event_text(text)
    assert "背景与影响：" not in cleaned
    assert any("标签残留" in i for i in issues)


def test_sanitize_removes_why_label():
    text = "为什么值得关注：这是一个重要事件。此前不同。现在改变了。对市场有影响。"
    cleaned, issues = _sanitize_event_text(text)
    assert "为什么值得关注：" not in cleaned


# ── 套话清理 ──


def test_sanitize_removes_forbidden_phrases():
    text = "最高法院作出裁定。此举凸显了司法趋势。此裁定意味着变化。对选民有影响。"
    cleaned, issues = _sanitize_event_text(text)
    assert "凸显了" not in cleaned
    assert any("禁用套话" in i for i in issues)


def test_sanitize_removes_according_to_reports():
    text = "据报道最高法院作出裁定。此前不同。现在改变了。对市场有影响。"
    cleaned, issues = _sanitize_event_text(text)
    assert "据报道" not in cleaned


def test_sanitize_removes_for_readers():
    text = "最高法院作出裁定。此前不同。现在改变了。对于读者来说这意味着变化。"
    cleaned, issues = _sanitize_event_text(text)
    assert "对于读者来说" not in cleaned


# ── 验证 ──


def test_validate_empty_reader_body():
    event = _make_event(reader_body="")
    issues = _validate_event(event)
    assert any("为空" in i for i in issues)


def test_validate_too_few_sentences():
    event = _make_event(reader_body="只有一句话。")
    issues = _validate_event(event)
    assert any("句数不足" in i for i in issues)


def test_validate_too_many_sentences():
    event = _make_event(reader_body="第一句。第二句。第三句。第四句。第五句。")
    issues = _validate_event(event)
    assert any("句数过多" in i for i in issues)


def test_validate_too_short():
    event = _make_event(reader_body="太短了。")
    issues = _validate_event(event)
    assert any("字数过少" in i for i in issues)


def test_validate_too_long():
    body = "这是一段很长的正文。" * 30  # > 260 字
    event = _make_event(reader_body=body)
    issues = _validate_event(event)
    assert any("字数过多" in i for i in issues)


def test_validate_good_event():
    body = "最高法院以 6 比 3 裁定，单纯使用大麻不构成剥夺持枪权的联邦法律依据。案件源于一名德克萨斯州合法大麻使用者的上诉——她因持有大麻被联邦法认定为非法药物使用者而禁止购枪。此裁定意味着 24 个已实现大麻合法化的州中，合法使用者的持枪权将获得明确联邦保护。这是最高法院继 2022 年 Bruen 案后，再次以历史传统标准收紧联邦枪权限制的信号。"
    event = _make_event(reader_body=body)
    issues = _validate_event(event)
    assert issues == []


# ── 综合清理 ──


def test_sanitize_or_validate_events_removes_empty():
    events = [
        _make_event(title="好事件", reader_body="第一句事实。第二句变化。第三句后果。"),
        _make_event(title="空事件", reader_body=""),
    ]
    cleaned, issues = sanitize_or_validate_events(events)
    assert len(cleaned) == 1
    assert cleaned[0]["title_zh"] == "好事件"
    assert any("空事件" in i for i in issues)


def test_sanitize_or_validate_events_cleans_labels():
    body = "核心事实：最高法院作出裁定。此前不同。现在改变了。对选民有影响。"
    events = [_make_event(reader_body=body)]
    cleaned, issues = sanitize_or_validate_events(events)
    assert "核心事实：" not in cleaned[0]["reader_body"]
    assert any("标签残留" in i for i in issues)


# ── Pre-LLM 过滤 ──


def test_pre_llm_filter_removes_soft_news():
    items = [
        _make_content_item("Supreme Court ruling on guns", "法院裁定"),
        _make_content_item("Celebrity wedding photos", "明星婚礼"),
        _make_content_item("Stock watchlist for Monday", "观察名单推荐"),
    ]
    filtered = _pre_llm_hard_filter(items)
    titles = [i.title for i in filtered]
    assert "Supreme Court ruling on guns" in titles
    assert "Celebrity wedding photos" not in titles
    assert "Stock watchlist for Monday" not in titles


def test_pre_llm_filter_keeps_hard_news():
    items = [
        _make_content_item("Fed rate decision", "美联储利率决定"),
        _make_content_item("AI regulation bill", "AI 监管法案"),
        _make_content_item("Tariff increase on imports", "关税上调"),
    ]
    filtered = _pre_llm_hard_filter(items)
    assert len(filtered) == 3


def test_pre_llm_filter_keeps_uncertain():
    """不确定内容保留给 LLM 判断。"""
    items = [
        _make_content_item("New study on climate change", "气候变化研究"),
    ]
    filtered = _pre_llm_hard_filter(items)
    assert len(filtered) == 1


def test_pre_llm_filter_chinese_keywords():
    items = [
        _make_content_item("最高法院裁定", "法院裁定"),
        _make_content_item("荐股推荐", "今日荐股"),
        _make_content_item("语无伦次直播", "直播中断"),
    ]
    filtered = _pre_llm_hard_filter(items)
    titles = [i.title for i in filtered]
    assert "最高法院裁定" in titles
    assert "荐股推荐" not in titles
    assert "语无伦次直播" not in titles


# ── Prompt 检查 ──


def test_prompt_contains_fact_change_consequence():
    """确认 prompt 包含'事实 → 变化 → 后果'结构要求。"""
    from ai_analyzer import COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "事实" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "变化" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "后果" in COLUMN_DIGEST_PROMPT_TEMPLATE


def test_prompt_contains_few_shot_examples():
    """确认 prompt 包含 few-shot 示例。"""
    from ai_analyzer import COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "示例 1" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "最高法院裁定大麻使用者不丧失持枪权" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "Baseten" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "NASA" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "苹果" in COLUMN_DIGEST_PROMPT_TEMPLATE


def test_prompt_forbids_labels():
    """确认 prompt 禁止输出标签。"""
    from ai_analyzer import COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "核心事实：" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "禁止" in COLUMN_DIGEST_PROMPT_TEMPLATE


def test_prompt_forbids_boilerplate():
    """确认 prompt 禁止套话。"""
    from ai_analyzer import COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "凸显了" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "反映了" in COLUMN_DIGEST_PROMPT_TEMPLATE
    assert "对于读者来说" in COLUMN_DIGEST_PROMPT_TEMPLATE
