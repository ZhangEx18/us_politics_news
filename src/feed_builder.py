#!/usr/bin/env python3
"""RSS Feed 生成器 v3：短摘要 + 全文分层

特性：
- description 只放短摘要（highlights 拼接，< 300 字）
- content:encoded 放完整 HTML（render_structured_html）
- 保留最近 30 天历史 item
- guid 基于 report_type + report_key，同一天重复运行不会产生重复 item
- 支持 daily / weekly / monthly 三种报告类型
- atom:link 仅在 base_url 非空时输出
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from report_renderer import render_reader_content

RSS_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _escape_xml(s: str) -> str:
    """转义 XML 特殊字符"""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
        .replace('"', "&quot;")
    )


def _rfc2822(dt: datetime) -> str:
    """格式化为 RFC 2822 日期"""
    dt_local = dt.astimezone(BEIJING_TZ) if dt.tzinfo else dt.replace(tzinfo=BEIJING_TZ)
    return dt_local.strftime("%a, %d %b %Y %H:%M:%S +0800")


def _build_short_description(meta: dict) -> str:
    """只从导语生成短摘要，避免 Reader 列表页碎片化。"""
    lead = re.sub(r"\s+", " ", str(meta.get("lead", "") or "")).strip()
    if not lead:
        return ""
    return lead[:220]


def _build_item_xml(
    date: str,
    title: str,
    short_description: str,
    html_body: str,
    base_url: str,
    pub_date: datetime | None = None,
    report_type: str = "daily",
    report_key: str | None = None,
) -> str:
    """
    生成单个 RSS <item> XML 片段

    Args:
        date: YYYY-MM-DD 格式日期
        title: item 标题
        short_description: 短摘要（<description>）
        html_body: 完整 HTML 正文（<content:encoded>）
        base_url: 日报基础 URL
        pub_date: 发布时间
        report_type: 报告类型（daily / weekly / monthly）
        report_key: 报告标识，如 "2026-W25"、"2026-06"；默认用 date
    """
    guid_key = report_key or date
    guid = f"{report_type}/{guid_key}"
    link_path = f"{report_type}/{guid_key}.html"
    link = f"{base_url}/{link_path}" if base_url else link_path
    pub_date_text = _rfc2822(pub_date or datetime.now(BEIJING_TZ))

    return f"""    <item>
      <title>{_escape_xml(title)}</title>
      <link>{_escape_xml(link)}</link>
      <description><![CDATA[{short_description}]]></description>
      <content:encoded><![CDATA[{html_body}]]></content:encoded>
      <pubDate>{pub_date_text}</pubDate>
      <guid isPermaLink="false">{_escape_xml(guid)}</guid>
    </item>"""


def _parse_existing_items(feed_xml: str) -> list[str]:
    """
    从已有 feed.xml 中提取 <item> 片段列表

    Returns:
        item XML 片段列表（原始字符串）
    """
    return re.findall(r"<item>.*?</item>", feed_xml, re.DOTALL)


def _extract_item_guid(item_xml: str) -> str | None:
    """从 item 片段中提取完整 guid（如 "daily/2026-06-19" 或 "weekly/2026-W25"）"""
    m = re.search(r"<guid[^>]*>([^<]+)</guid>", item_xml)
    if not m:
        return None
    guid = m.group(1)
    # 向后兼容：旧格式只有日期，补全为 daily/ 前缀
    if re.match(r"^\d{4}-\d{2}-\d{2}$", guid):
        return f"daily/{guid}"
    return guid


def _extract_item_date(item_xml: str) -> str | None:
    """向后兼容：从 item 片段中提取日期部分（YYYY-MM-DD）"""
    guid = _extract_item_guid(item_xml)
    if not guid:
        return None
    # 尝试从 guid 中提取日期（daily/YYYY-MM-DD 或纯日期）
    m = re.search(r"(\d{4}-\d{2}-\d{2})", guid)
    return m.group(1) if m else None


def _merge_items(new_item: str, existing_items: list[str], max_days: int = 30) -> list[str]:
    """
    合并新 item 与已有 items，按 guid 去重

    同 guid 替换，不同 guid 共存。超出 max_days 的旧 daily item 被裁剪。
    """
    cutoff = (datetime.now() - timedelta(days=max_days)).strftime("%Y-%m-%d")
    new_guid = _extract_item_guid(new_item)

    merged: list[str] = [new_item]
    for item in existing_items:
        item_guid = _extract_item_guid(item)
        if item_guid is None:
            continue
        # 同 guid 的旧 item 跳过（已被新 item 替换）
        if new_guid and item_guid == new_guid:
            continue
        # 裁剪超出保留天数的 daily 类型 item
        item_date = _extract_item_date(item)
        if item_date and item_date < cutoff:
            continue
        merged.append(item)

    def _sort_key(item: str) -> str:
        guid = _extract_item_guid(item)
        return guid or "0000-00-00"

    merged.sort(key=_sort_key, reverse=True)
    return merged


def build_feed(items: list[str], base_url: str = "") -> str:
    """
    拼装完整 RSS 2.0 feed XML

    Args:
        items: item XML 片段列表
        base_url: 日报基础 URL
    """
    now = _rfc2822(datetime.now(BEIJING_TZ))
    items_xml = "\n".join(items)

    atom_link = ""
    if base_url:
        atom_link = f'\n    <atom:link href="{_escape_xml(base_url)}/feed.xml" rel="self" type="application/rss+xml"/>'

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="{ATOM_NS}" xmlns:content="{RSS_NS}">
  <channel>
    <title>四维日报</title>
    <link>{_escape_xml(base_url) if base_url else "."}</link>
    <description>每日国际新闻精选：美国政情 · 国际风云 · 科技前沿 · 财经脉动</description>
    <language>zh-cn</language>
    <lastBuildDate>{now}</lastBuildDate>{atom_link}
{items_xml}
  </channel>
</rss>"""


def _generate_title(report_type: str, report_key: str | None, date: str) -> str:
    """根据报告类型生成标题"""
    if report_type == "weekly" and report_key:
        # report_key 格式如 "2026-W25"
        m = re.match(r"(\d{4})-W(\d+)", report_key)
        if m:
            return f"{m.group(1)}年第{int(m.group(2))}周周报"
    elif report_type == "monthly" and report_key:
        # report_key 格式如 "2026-06"
        m = re.match(r"(\d{4})-(\d{2})", report_key)
        if m:
            return f"{m.group(1)}年{int(m.group(2))}月月报"
    # daily 或 fallback：用 date 生成
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        return f"{dt.year}年{dt.month}月{dt.day}日日报"
    except ValueError:
        return f"{date} 四维日报"


def save_feed(
    meta: dict,
    columns: dict[str, list[dict]],
    output_path: str = "docs/feed.xml",
    base_url: str = "",
    report_type: str = "daily",
    report_key: str | None = None,
) -> str:
    """
    保存 RSS feed 到文件（v3 短摘要 + 全文分层）

    - description: 短摘要（< 300 字）
    - content:encoded: 完整 HTML 日报正文
    - 保留最近 30 天历史 item
    - 按 guid 去重，同 report_type + report_key 重复运行时替换而非追加

    Args:
        meta: {"title", "lead", "highlights", "date"}
        columns: {"us_politics": [events...], ...}
        output_path: 输出文件路径
        base_url: 日报基础 URL
        report_type: 报告类型（daily / weekly / monthly）
        report_key: 报告标识，如 "2026-W25"、"2026-06"

    Returns:
        保存的文件路径
    """
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
    title = meta.get("title") or _generate_title(report_type, report_key, date)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 短摘要（description）
    short_description = _build_short_description(meta)

    # Reader 专用正文片段（content:encoded）
    html_body = render_reader_content(meta, columns, report_type=report_type)

    # 构建今天的 item
    pub_date_raw = meta.get("pub_date")
    if isinstance(pub_date_raw, str) and pub_date_raw:
        pub_date = datetime.fromisoformat(pub_date_raw)
    elif isinstance(pub_date_raw, datetime):
        pub_date = pub_date_raw
    else:
        pub_date = datetime.now(BEIJING_TZ)

    new_item = _build_item_xml(date, title, short_description, html_body, base_url, pub_date, report_type, report_key)

    # 读取已有 feed，合并历史 items
    existing_items: list[str] = []
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing_xml = f.read()
            existing_items = _parse_existing_items(existing_xml)
        except (OSError, ET.ParseError):
            existing_items = []

    merged_items = _merge_items(new_item, existing_items, max_days=30)
    feed_xml = build_feed(merged_items, base_url)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(feed_xml)

    return output_path
