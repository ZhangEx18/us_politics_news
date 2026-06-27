"""RSS Feed 生成测试 — feed_builder.py (v3)"""

import os
import re
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from feed_builder import (
    _build_item_xml,
    _merge_items,
    _extract_item_date,
    _extract_item_guid,
    _build_short_description,
    _generate_title,
    build_feed,
    save_feed,
    RSS_NS,
)


def test_build_short_description():
    meta = {"title": "测试", "lead": "这是导语", "highlights": ["重点1", "重点2", "重点3"]}
    desc = _build_short_description(meta)
    assert desc == "这是导语"
    assert "重点1" not in desc
    assert len(desc) <= 220


def test_build_feed_contains_content_namespace():
    feed_xml = build_feed([], base_url="https://example.com")
    assert f'xmlns:content="{RSS_NS}"' in feed_xml


def test_build_item_contains_content_encoded():
    item = _build_item_xml("2026-06-18", "Test Title", "Short desc", "<h1>Hello</h1>", "https://example.com")
    assert "<content:encoded>" in item
    assert "</content:encoded>" in item


def test_build_item_splits_cdata_end_markers():
    item = _build_item_xml("2026-06-18", "Test Title", "bad ]]> desc", "<p>bad ]]> body</p>", "")
    assert "bad ]]> desc" not in item
    assert "bad ]]> body" not in item
    assert "]]]]><![CDATA[>" in item


def test_build_item_uses_fixed_pub_date_when_provided():
    item = _build_item_xml(
        "2026-06-19",
        "Title",
        "Desc",
        "<p>Body</p>",
        "",
        datetime(2026, 6, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert "<pubDate>Fri, 19 Jun 2026 08:00:00 +0800</pubDate>" in item


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


def test_save_feed_uses_reader_friendly_fragment():
    meta = {
        "title": "测试标题",
        "lead": "这是一段导语",
        "highlights": ["重点1", "重点2", "重点3", "重点4"],
        "date": "2026-06-18",
    }
    columns = {
        "us_politics": [{
            "title_zh": "测试事件",
            "reader_body": "测试事件的单段概述正文。",
            "core_facts": "测试事件的单段概述正文。",
            "source_links": [{"title": "原文", "url": "https://example.com"}],
        }],
        "global_affairs": [{
            "title_zh": "简要事件",
            "reader_body": "简要事件的一段概述。",
        }],
        "technology": [],
        "economy": [],
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="https://example.com")
        with open(path, encoding="utf-8") as f:
            content = f.read()

        assert "<article>" in content
        assert "<p>这是一段导语</p>" not in content
        assert "<h2>今日要点</h2>" in content
        assert "<li>重点 1</li>" in content
        assert "<h2>一、美国政局</h2>" in content
        assert "<h2>二、国际局势</h2>" in content
        assert "<h3>1. 测试事件</h3>" in content
        assert "测试事件的单段概述正文" in content
        assert "<h3>1. 简要事件</h3>" in content
        assert "简要事件的一段概述" in content
        assert "核心事实：" not in content
        assert "背景脉络：" not in content
        assert "可能影响：" not in content
        assert "为什么值得关注：" not in content
        assert "<!DOCTYPE html>" not in content
        assert "<html" not in content
        assert "<head>" not in content
        assert "<style>" not in content
        assert "原文链接" not in content
        assert "相关阅读" not in content
        assert "来源" not in content
        assert "<h1>2026年6月18日 日报</h1>" not in content
        assert "<a href=" not in content


def test_save_feed_headline_only_prefers_reader_body():
    meta = {"title": "测试标题", "lead": "", "highlights": [], "date": "2026-06-18"}
    columns = {
        "us_politics": {
            "detailed_events": [{"title_zh": "测试事件", "reader_body": "测试事件的单段概述正文。"}],
            "headline_only_events": [{"title_zh": "英文标题", "reader_body": "白宫要求国会重启谈判。"}],
        },
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="https://example.com")
        content = open(path, encoding="utf-8").read()
    assert "英文标题" not in content
    assert "白宫要求国会重启谈判。" in content


def test_save_feed_uses_meta_pub_date_for_reeder_timestamp():
    meta = {
        "title": "2026年6月19日 日报",
        "lead": "",
        "highlights": [],
        "date": "2026-06-19",
        "pub_date": "2026-06-19T08:00:00+08:00",
    }
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="https://example.com")
        content = open(path, encoding="utf-8").read()

    pub_date = re.search(r"<pubDate>(.*?)</pubDate>", content)
    assert pub_date
    assert pub_date.group(1) == "Fri, 19 Jun 2026 08:00:00 +0800"


def test_save_feed_changes_guid_revision_when_same_day_content_changes():
    meta = {"title": "2026年6月19日 日报", "lead": "", "highlights": [], "date": "2026-06-19"}
    columns_v1 = {
        "us_politics": [{"title_zh": "事件一", "reader_body": "6 月 19 日，第一版正文。"}],
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }
    columns_v2 = {
        "us_politics": [{"title_zh": "事件一", "reader_body": "6 月 19 日，第二版正文。"}],
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns_v1, output_path=path, base_url="https://example.com")
        first = open(path, encoding="utf-8").read()
        first_guid = re.search(r"<guid[^>]*>([^<]+)</guid>", first).group(1)

        save_feed(meta=meta, columns=columns_v2, output_path=path, base_url="https://example.com")
        second = open(path, encoding="utf-8").read()
        second_guid = re.search(r"<guid[^>]*>([^<]+)</guid>", second).group(1)
        second_link = re.findall(r"<link>([^<]+)</link>", second)[-1]

    assert first_guid.startswith("daily/2026-06-19?v=")
    assert second_guid.startswith("daily/2026-06-19?v=")
    assert first_guid != second_guid
    assert second.count("<item>") == 1
    assert "第二版正文" in second
    assert "第一版正文" not in second
    assert "daily/2026-06-19.html?v=" in second_link


def test_save_feed_syncs_legacy_news_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    meta = {"title": "2026年6月27日 日报", "lead": "", "highlights": [], "date": "2026-06-27"}
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}

    path = os.path.join("docs", "feeds", "news.xml")
    save_feed(meta=meta, columns=columns, output_path=path, base_url="https://example.com")

    news_feed = (tmp_path / "docs" / "feeds" / "news.xml").read_text(encoding="utf-8")
    legacy_feed = (tmp_path / "docs" / "feed.xml").read_text(encoding="utf-8")
    assert news_feed == legacy_feed


def test_guid_based_on_date():
    item = _build_item_xml("2026-06-18", "Title", "Desc", "<p>Body</p>", "")
    assert "news/daily/2026-06-18" in item


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


# === report_type 相关测试 ===


def test_build_item_weekly_type():
    """weekly 类型的 guid 和 link 使用 report_key"""
    item = _build_item_xml(
        "2026-06-19", "Weekly Report", "Desc", "<p>Body</p>",
        "https://example.com", report_type="weekly", report_key="2026-W25",
    )
    guid = re.search(r"<guid[^>]*>([^<]+)</guid>", item)
    assert guid and guid.group(1) == "news/weekly/2026-W25"
    assert "news/weekly/2026-W25.html" in item


def test_build_item_monthly_type():
    """monthly 类型的 guid 和 link 使用 report_key"""
    item = _build_item_xml(
        "2026-06-01", "Monthly Report", "Desc", "<p>Body</p>",
        "", report_type="monthly", report_key="2026-06",
    )
    guid = re.search(r"<guid[^>]*>([^<]+)</guid>", item)
    assert guid and guid.group(1) == "news/monthly/2026-06"
    assert "news/monthly/2026-06.html" in item


def test_extract_item_guid_new_format():
    """_extract_item_guid 提取新格式 guid"""
    assert _extract_item_guid('<item><guid>weekly/2026-W25</guid></item>') == "news/weekly/2026-W25"
    assert _extract_item_guid('<item><guid>monthly/2026-06</guid></item>') == "news/monthly/2026-06"
    assert _extract_item_guid('<item><guid>daily/2026-06-18</guid></item>') == "news/daily/2026-06-18"


def test_extract_item_guid_old_format_compat():
    """_extract_item_guid 向后兼容旧格式（纯日期）"""
    assert _extract_item_guid('<item><guid>2026-06-18</guid></item>') == "news/daily/2026-06-18"


def test_extract_item_guid_returns_none_for_missing():
    """_extract_item_guid 无 guid 时返回 None"""
    assert _extract_item_guid("<item><title>no guid</title></item>") is None


def test_merge_items_different_report_types_coexist():
    """不同 report_type 的 item 按 guid 共存，不互相替换"""
    daily = _build_item_xml("2026-06-19", "Daily", "D", "<p>D</p>", "", report_type="daily", report_key="2026-06-19")
    weekly = _build_item_xml("2026-06-19", "Weekly", "W", "<p>W</p>", "", report_type="weekly", report_key="2026-W25")
    result = _merge_items(daily, [weekly])
    assert len(result) == 2  # 不同 report_type 共存


def test_merge_items_same_report_type_replaces():
    """同 report_type + report_key 的 item 被替换"""
    old = _build_item_xml(
        "2026-06-18", "Old Weekly", "O", "<p>O</p>", "",
        report_type="weekly", report_key="2026-W25",
    )
    new = _build_item_xml(
        "2026-06-19", "New Weekly", "N", "<p>N</p>", "",
        report_type="weekly", report_key="2026-W25",
    )
    result = _merge_items(new, [old])
    assert len(result) == 1
    assert "New Weekly" in result[0]


def test_generate_title_daily():
    """daily 类型标题格式"""
    assert _generate_title("daily", None, "2026-06-18") == "2026年6月18日 日报"


def test_generate_title_weekly():
    """weekly 类型标题格式"""
    assert _generate_title("weekly", "2026-W25", "2026-06-18") == "2026年6月第3周 周报"


def test_generate_title_monthly():
    """monthly 类型标题格式"""
    assert _generate_title("monthly", "2026-06", "2026-06-01") == "2026年6月 月报"


def test_save_feed_weekly_type():
    """save_feed 支持 weekly 类型，生成对应 guid 和标题"""
    meta = {"title": "", "lead": "", "highlights": [], "date": "2026-06-19"}
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(
            meta=meta, columns=columns, output_path=path,
            base_url="https://example.com",
            report_type="weekly", report_key="2026-W25",
        )
        content = open(path, encoding="utf-8").read()
        assert "weekly/2026-W25" in content
        assert "2026年6月第3周 周报" in content


def test_guid_format_daily():
    """daily 类型 guid 包含 daily/ 前缀"""
    item = _build_item_xml("2026-06-19", "Title", "Desc", "<p>Body</p>", "", report_type="daily", report_key="2026-06-19")
    guid = re.search(r"<guid[^>]*>([^<]+)</guid>", item)
    assert guid and "daily/" in guid.group(1)


def test_guid_format_weekly():
    """weekly 类型 guid 包含 weekly/ 前缀"""
    item = _build_item_xml("2026-06-19", "Title", "Desc", "<p>Body</p>", "", report_type="weekly", report_key="2026-W25")
    guid = re.search(r"<guid[^>]*>([^<]+)</guid>", item)
    assert guid and "weekly/" in guid.group(1)


def test_guid_format_monthly():
    """monthly 类型 guid 包含 monthly/ 前缀"""
    item = _build_item_xml("2026-06-19", "Title", "Desc", "<p>Body</p>", "", report_type="monthly", report_key="2026-06")
    guid = re.search(r"<guid[^>]*>([^<]+)</guid>", item)
    assert guid and "monthly/" in guid.group(1)


def test_save_feed_weekly_uses_periodical_overview():
    """save_feed report_type=weekly 时 content:encoded 使用周报总览块。"""
    meta = {
        "title": "2026年6月第3周 周报",
        "lead": "",
        "highlights": ["要点一", "要点二"],
        "date": "2026-06-19",
        "overview": {
            "summary": "本周综述段落。",
            "themes": ["主题甲"],
            "watchlist": ["观察点一"],
        },
    }
    columns = {
        "us_politics": [{"title_zh": "事件", "reader_body": "正文。"}],
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="",
                  report_type="weekly", report_key="2026-W25")
        content = open(path, encoding="utf-8").read()
        assert "本周综述" in content
        assert "本周核心主题" in content
        assert "下周观察点" in content
        assert "今日要点" not in content


def test_save_feed_monthly_uses_periodical_overview():
    """save_feed report_type=monthly 时 content:encoded 使用月报总览块。"""
    meta = {
        "title": "2026年6月 月报",
        "lead": "",
        "highlights": ["要点一"],
        "date": "2026-06-19",
        "overview": {
            "summary": "本月综述段落。",
            "themes": ["主题甲"],
            "watchlist": ["观察点一"],
        },
    }
    columns = {
        "us_politics": [],
        "global_affairs": [{"title_zh": "事件", "reader_body": "正文。"}],
        "technology": [],
        "economy": [],
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "feed.xml")
        save_feed(meta=meta, columns=columns, output_path=path, base_url="",
                  report_type="monthly", report_key="2026-06")
        content = open(path, encoding="utf-8").read()
        assert "本月综述" in content
        assert "本月核心主题" in content
        assert "下月观察点" in content
        assert "今日要点" not in content
