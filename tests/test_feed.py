"""RSS Feed 生成测试 — feed_builder.py"""

import os
import tempfile

from feed_builder import (
    _build_item_xml,
    _merge_items,
    _extract_item_date,
    build_feed,
    save_feed,
    RSS_NS,
)


# ── XML 命名空间 ──


def test_build_feed_contains_content_namespace():
    """生成的 XML 应包含 xmlns:content 命名空间"""
    feed_xml = build_feed([], base_url="https://example.com")
    assert f'xmlns:content="{RSS_NS}"' in feed_xml


def test_build_item_contains_content_encoded():
    """item 片段应包含 content:encoded 标签"""
    item = _build_item_xml(
        date="2026-06-18",
        html_body="<h1>Hello</h1>",
        title="Test Title",
        base_url="https://example.com",
    )
    assert "<content:encoded>" in item
    assert "</content:encoded>" in item


def test_save_feed_generates_valid_xml_with_namespaces():
    """save_feed 生成的文件应包含 xmlns:content 和 content:encoded"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(output_path=path, base_url="https://example.com", digest_text="# test")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert f'xmlns:content="{RSS_NS}"' in content
        assert "content:encoded" in content


# ── guid 稳定性 ──


def test_guid_based_on_date():
    """同一天的 item guid 应基于日期"""
    item = _build_item_xml(
        date="2026-06-18",
        html_body="<p>Body</p>",
        title="Title",
        base_url="",
    )
    assert "daily/2026-06-18" in item


def test_same_day_repeated_run_guid_unchanged():
    """两次 _build_item_xml 同一天，guid 应相同"""
    item1 = _build_item_xml("2026-06-18", "<p>A</p>", "Title A", "")
    item2 = _build_item_xml("2026-06-18", "<p>B</p>", "Title B", "")
    # 提取 guid 值
    import re
    g1 = re.search(r"<guid[^>]*>([^<]+)</guid>", item1)
    g2 = re.search(r"<guid[^>]*>([^<]+)</guid>", item2)
    assert g1 and g2
    assert g1.group(1) == g2.group(1)


# ── _merge_items 去重 ──


def test_merge_items_replaces_same_date():
    """新 item 应替换同日期的旧 item"""
    old_item = _build_item_xml("2026-06-18", "<p>Old</p>", "Old Title", "")
    new_item = _build_item_xml("2026-06-18", "<p>New</p>", "New Title", "")
    result = _merge_items(new_item, [old_item])
    assert len(result) == 1
    assert "New" in result[0]


def test_merge_items_keeps_different_dates():
    """不同日期的 item 应保留"""
    item_today = _build_item_xml("2026-06-18", "<p>Today</p>", "Today", "")
    item_yesterday = _build_item_xml("2026-06-17", "<p>Yesterday</p>", "Yesterday", "")
    result = _merge_items(item_today, [item_yesterday])
    assert len(result) == 2


def test_merge_items_dedup_mixed_list():
    """混合列表中同日期应只保留新 item"""
    new = _build_item_xml("2026-06-18", "<p>New</p>", "New", "")
    old_same = _build_item_xml("2026-06-18", "<p>Old Same</p>", "Old Same", "")
    old_other = _build_item_xml("2026-06-17", "<p>Old Other</p>", "Old Other", "")
    result = _merge_items(new, [old_same, old_other])
    dates = [_extract_item_date(r) for r in result]
    assert dates.count("2026-06-18") == 1, "同日期应去重，只保留新 item"
    assert len(result) == 2
