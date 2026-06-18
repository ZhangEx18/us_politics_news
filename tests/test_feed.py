"""RSS Feed 生成测试 — feed_builder.py (v3)"""

import os
import tempfile

from feed_builder import (
    _build_item_xml,
    _merge_items,
    _extract_item_date,
    _build_short_description,
    build_feed,
    save_feed,
    RSS_NS,
)


def test_build_short_description():
    meta = {"title": "测试", "lead": "这是导语", "highlights": ["重点1", "重点2", "重点3"]}
    desc = _build_short_description(meta)
    assert "重点1" in desc
    assert "重点2" in desc
    assert len(desc) <= 300


def test_build_feed_contains_content_namespace():
    feed_xml = build_feed([], base_url="https://example.com")
    assert f'xmlns:content="{RSS_NS}"' in feed_xml


def test_build_item_contains_content_encoded():
    item = _build_item_xml("2026-06-18", "Test Title", "Short desc", "<h1>Hello</h1>", "https://example.com")
    assert "<content:encoded>" in item
    assert "</content:encoded>" in item


def test_save_feed_generates_valid_xml_with_namespaces():
    meta = {"title": "测试", "lead": "", "highlights": [], "date": "2026-06-18"}
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="https://example.com")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert f'xmlns:content="{RSS_NS}"' in content
        assert "content:encoded" in content


def test_guid_based_on_date():
    item = _build_item_xml("2026-06-18", "Title", "Desc", "<p>Body</p>", "")
    assert "daily/2026-06-18" in item


def test_same_day_repeated_run_guid_unchanged():
    item1 = _build_item_xml("2026-06-18", "Title A", "Desc A", "<p>A</p>", "")
    item2 = _build_item_xml("2026-06-18", "Title B", "Desc B", "<p>B</p>", "")
    import re
    g1 = re.search(r"<guid[^>]*>([^<]+)</guid>", item1)
    g2 = re.search(r"<guid[^>]*>([^<]+)</guid>", item2)
    assert g1 and g2
    assert g1.group(1) == g2.group(1)


def test_merge_items_replaces_same_date():
    old_item = _build_item_xml("2026-06-18", "Old", "Old", "<p>Old</p>", "")
    new_item = _build_item_xml("2026-06-18", "New", "New", "<p>New</p>", "")
    result = _merge_items(new_item, [old_item])
    assert len(result) == 1
    assert "New" in result[0]


def test_merge_items_keeps_different_dates():
    item_today = _build_item_xml("2026-06-18", "Today", "Today", "<p>Today</p>", "")
    item_yesterday = _build_item_xml("2026-06-17", "Yesterday", "Yesterday", "<p>Yesterday</p>", "")
    result = _merge_items(item_today, [item_yesterday])
    assert len(result) == 2


def test_merge_items_dedup_mixed_list():
    new = _build_item_xml("2026-06-18", "New", "New", "<p>New</p>", "")
    old_same = _build_item_xml("2026-06-18", "Old Same", "Old Same", "<p>Old Same</p>", "")
    old_other = _build_item_xml("2026-06-17", "Old Other", "Old Other", "<p>Old Other</p>", "")
    result = _merge_items(new, [old_same, old_other])
    dates = [_extract_item_date(r) for r in result]
    assert dates.count("2026-06-18") == 1
    assert len(result) == 2
