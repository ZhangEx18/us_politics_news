#!/usr/bin/env python3
"""RSS Feed 生成器：生成符合 RSS 2.0 规范的 feed.xml

特性：
- item 内嵌完整日报 HTML 正文（content:encoded）
- 保留最近 30 天历史 item
- guid 基于日期，同一天重复运行不会产生重复 item
- atom:link 仅在 base_url 非空时输出
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from report_renderer import render_html
from scoring import ScoredArticle

RSS_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"


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
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


def _build_item_xml(date: str, html_body: str, title: str, base_url: str) -> str:
    """
    生成单个 RSS <item> XML 片段

    Args:
        date: YYYY-MM-DD 格式日期
        html_body: 完整日报 HTML 正文
        title: item 标题（来自 frontmatter）
        base_url: 日报基础 URL
    """
    link = f"{base_url}/daily/{date}.html" if base_url else f"daily/{date}.html"
    pub_date = _rfc2822(datetime.now())
    # guid 基于日期，同一天重复运行保持不变
    guid = f"daily/{date}"

    return f"""    <item>
      <title>{_escape_xml(title)}</title>
      <link>{_escape_xml(link)}</link>
      <description>{_escape_xml(title)}</description>
      <content:encoded><![CDATA[{html_body}]]></content:encoded>
      <pubDate>{pub_date}</pubDate>
      <guid isPermaLink="false">{_escape_xml(guid)}</guid>
    </item>"""


def _parse_existing_items(feed_xml: str) -> list[str]:
    """
    从已有 feed.xml 中提取 <item> 片段列表

    Returns:
        item XML 片段列表（原始字符串）
    """
    # 用正则提取 <item>...</item> 块，避免命名空间解析问题
    return re.findall(r"<item>.*?</item>", feed_xml, re.DOTALL)


def _extract_item_date(item_xml: str) -> str | None:
    """从 item 片段中提取 guid 中的日期（YYYY-MM-DD）"""
    m = re.search(r"<guid[^>]*>daily/(\d{4}-\d{2}-\d{2})</guid>", item_xml)
    return m.group(1) if m else None


def _merge_items(new_item: str, existing_items: list[str], max_days: int = 30) -> list[str]:
    """
    合并新 item 与已有 items，裁剪到最近 max_days 天

    新 item 替换同日期的已有 item（guid 去重）
    """
    cutoff = (datetime.now() - timedelta(days=max_days)).strftime("%Y-%m-%d")
    new_date = _extract_item_date(new_item)

    merged: list[str] = [new_item]
    for item in existing_items:
        item_date = _extract_item_date(item)
        if item_date is None:
            continue
        # 跳过过期条目
        if item_date < cutoff:
            continue
        # 跳过与新 item 同日期的旧条目（由新条目替代）
        if new_date and item_date == new_date:
            continue
        merged.append(item)

    # 按日期降序排列
    def _sort_key(item: str) -> str:
        d = _extract_item_date(item)
        return d or "0000-00-00"

    merged.sort(key=_sort_key, reverse=True)
    return merged


def build_feed(items: list[str], base_url: str = "") -> str:
    """
    拼装完整 RSS 2.0 feed XML

    Args:
        items: item XML 片段列表
        base_url: 日报基础 URL
    """
    now = _rfc2822(datetime.now())
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


def save_feed(articles: list[ScoredArticle], output_path: str = "docs/feed.xml", base_url: str = "") -> str:
    """
    保存 RSS feed 到文件

    - item description/content:encoded 内嵌完整日报 HTML
    - 保留最近 30 天历史 item
    - 同一天重复运行时替换而非追加
    - atom:link 仅在 base_url 非空时输出

    Returns:
        保存的文件路径
    """
    date = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 生成完整 HTML 日报正文
    html_body = render_html(articles, date)

    # frontmatter title（第一条重点新闻标题，或默认标题）
    key_articles = [a for a in articles if a.level == "重点"]
    if key_articles:
        top_title = key_articles[0].title_zh or key_articles[0].title
        title = f"{date} {top_title}"
    else:
        title = f"{date} 四维日报"

    # 构建今天的 item
    new_item = _build_item_xml(date, html_body, title, base_url)

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
