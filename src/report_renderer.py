#!/usr/bin/env python3
"""
日报渲染器 v3 — 结构化渲染

特性：
- 从结构化 dict 直接生成 HTML / Markdown，不经过 Markdown → HTML 转换
- YAML frontmatter（title / lead / highlights / date）
- 四大栏目分组：美国政情 / 国际风云 / 科技前沿 / 财经脉动
- 每条事件：核心事实 + 背景与影响 + 为什么值得关注 + 来源链接
- 中英文混排自动空格（Pangu spacing）
"""

import os
import re
from datetime import datetime

# ---------------------------------------------------------------------------
# Pangu spacing：中英文之间自动加空格
# ---------------------------------------------------------------------------
_CJK = r"[一-鿿㐀-䶿]"
_ASCII = r"[A-Za-z0-9]"


def _pangu(text: str) -> str:
    """在中英文之间插入空格"""
    text = re.sub(rf"({_CJK})({_ASCII})", r"\1 \2", text)
    text = re.sub(rf"({_ASCII})({_CJK})", r"\1 \2", text)
    return text


# ---------------------------------------------------------------------------
# 栏目配置
# ---------------------------------------------------------------------------
COLUMN_META: dict[str, dict[str, str]] = {
    "us_politics": {"heading": "美国政情", "icon": ""},
    "global_affairs": {"heading": "国际风云", "icon": ""},
    "technology": {"heading": "科技前沿", "icon": ""},
    "economy": {"heading": "财经脉动", "icon": ""},
}

# 栏目输出顺序
COLUMN_ORDER: list[str] = [
    "us_politics",
    "global_affairs",
    "technology",
    "economy",
]


# 栏目序号映射（中文）
_COLUMN_NUM: dict[str, str] = {
    "us_politics": "一",
    "global_affairs": "二",
    "technology": "三",
    "economy": "四",
}


# ---------------------------------------------------------------------------
# 核心渲染：结构化 → HTML
# ---------------------------------------------------------------------------

# 报告类型 → 默认标题文案
_REPORT_TYPE_TITLE: dict[str, str] = {
    "daily": "每日新闻",
    "weekly": "每周新闻",
    "monthly": "每月新闻",
}


def render_structured_html(
    meta: dict,
    columns: dict[str, list[dict]],
    report_type: str = "daily",
) -> str:
    """
    从结构化数据直接生成干净 HTML

    参数：
        meta: {"title", "lead", "highlights", "date"}
        columns: {"us_politics": [{"title_zh", "core_facts", "background_impact",
                   "why_it_matters", "source_links", "is_followup"}, ...], ...}
        report_type: 报告类型（daily / weekly / monthly），影响默认标题
    """
    # meta.title 优先；否则根据 report_type 生成默认标题
    if meta.get("title"):
        title = _pangu(meta["title"])
    else:
        date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
        type_label = _REPORT_TYPE_TITLE.get(report_type, "每日新闻")
        title = f"{date} {type_label}"
    lead = _pangu(meta.get("lead", ""))
    highlights = meta.get("highlights", [])
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))

    css = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
                 'Microsoft YaHei', 'Noto Sans SC', sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 20px;
    line-height: 1.8;
    color: #1a1a1a;
    background: #fafafa;
}
header {
    border-bottom: 2px solid #1a1a1a;
    padding-bottom: 16px;
    margin-bottom: 32px;
}
header h1 {
    font-size: 1.6em;
    margin: 0 0 8px 0;
    font-weight: 700;
    letter-spacing: -0.02em;
}
header .date {
    color: #666;
    font-size: 0.9em;
}
header .lead {
    color: #444;
    font-size: 0.95em;
    margin-top: 12px;
    line-height: 1.6;
}
.highlights {
    background: #f5f5f5;
    border-radius: 6px;
    padding: 12px 16px;
    margin: 16px 0;
}
.highlights ul {
    margin: 0;
    padding-left: 20px;
}
.highlights li {
    font-size: 0.9em;
    color: #333;
    margin: 4px 0;
}
h2 {
    font-size: 1.3em;
    font-weight: 700;
    margin-top: 40px;
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid #e0e0e0;
}
.event {
    margin-bottom: 24px;
    padding-bottom: 20px;
    border-bottom: 1px solid #f0f0f0;
}
.event:last-child {
    border-bottom: none;
}
.event h3 {
    font-size: 1.05em;
    font-weight: 600;
    margin-top: 28px;
    margin-bottom: 8px;
    color: #1a1a1a;
}
.facts {
    margin: 8px 0;
}
.impact {
    color: #555;
    margin: 8px 0;
}
.why {
    background: #fafafa;
    border-left: 3px solid #999;
    padding: 8px 12px;
    margin: 8px 0;
}
.links {
    font-size: 0.85em;
    color: #666;
}
.links a {
    color: #1a6b3c;
    text-decoration: none;
}
.links a:hover {
    text-decoration: underline;
}
footer {
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid #e0e0e0;
    color: #999;
    font-size: 0.8em;
    text-align: center;
}
"""

    html = [
        "<!DOCTYPE html>",
        "<html lang='zh'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>{title} — {date}</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        # header
        "<header>",
        f"<h1>{title}</h1>",
        f"<div class='date'>{date}</div>",
        f"<div class='lead'>{lead}</div>",
        "</header>",
    ]

    # highlights
    if highlights:
        html.append("<div class='highlights'>")
        html.append("<ul>")
        for h in highlights:
            html.append(f"<li>{_pangu(h)}</li>")
        html.append("</ul>")
        html.append("</div>")

    # 四大栏目
    for col_key in COLUMN_ORDER:
        col_data = columns.get(col_key, {})
        if isinstance(col_data, list):
            detailed = col_data
            headline_only = []
        else:
            detailed = col_data.get("detailed_events", [])
            headline_only = col_data.get("headline_only_events", [])

        if not detailed and not headline_only:
            continue

        meta_col = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        num = _COLUMN_NUM.get(col_key, "")
        html.append(f"<h2>{meta_col['icon']} {num}、{meta_col['heading']}</h2>")

        # 编号条目
        for idx, event in enumerate(detailed, 1):
            event_title = _pangu(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""

            html.append("<div class='event'>")
            html.append(f"<h3>{idx}. {event_title}{suffix}</h3>")

            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                html.append(f"<p>{_pangu(reader_body)}</p>")

            source_links = event.get("source_links", [])
            if source_links:
                html.append("<div class='links'>")
                for link in source_links:
                    link_title = _pangu(str(link.get("title", "原文")))
                    link_url = str(link.get("url", ""))
                    if link_url:
                        html.append(f"<a href='{link_url}'>{link_title}</a> ")
                html.append("</div>")

            html.append("</div>")

        # 无序条目：仅标题
        if headline_only:
            html.append("<ul>")
            for event in headline_only:
                event_title = _pangu(event.get("title_zh", ""))
                if event_title:
                    html.append(f"<li>{event_title}</li>")
            html.append("</ul>")

    html.append("</body>")
    html.append("</html>")

    return "\n".join(html)


# 报告类型 → 要点标题
_HIGHLIGHTS_HEADING: dict[str, str] = {
    "daily": "今日要点",
    "weekly": "本周要点",
    "monthly": "本月要点",
}


def render_reader_content(
    meta: dict,
    columns: dict[str, list[dict]],
    report_type: str = "daily",
) -> str:
    """
    为 RSS Reader 生成纯正文 HTML 片段。

    约束：
    - 只保留标题 + 要点 + 四大栏目正文
    - 不输出完整 HTML 文档壳
    - 不输出来源、原文链接、标签式小标题
    - 所有事件统一为标题 + 单段概述

    参数：
        report_type: 报告类型（daily / weekly / monthly），影响要点标题
    """
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        title = f"{dt.year}年{dt.month}月{dt.day}日 新闻"
    except ValueError:
        title = _pangu(meta.get("title", ""))
    highlights = meta.get("highlights", []) or []

    html: list[str] = ["<article>"]

    if highlights:
        heading = _HIGHLIGHTS_HEADING.get(report_type, "今日要点")
        html.append(f"<h2>{heading}</h2>")
        html.append("<ul>")
        for item in highlights:
            html.append(f"<li>{_pangu(str(item))}</li>")
        html.append("</ul>")

    for col_key in COLUMN_ORDER:
        col_data = columns.get(col_key, {})
        # 兼容旧格式（纯列表）和新格式（dict with detailed_events + headline_only_events）
        if isinstance(col_data, list):
            detailed = col_data
            headline_only = []
        else:
            detailed = col_data.get("detailed_events", [])
            headline_only = col_data.get("headline_only_events", [])

        if not detailed and not headline_only:
            continue

        meta_col = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        num = _COLUMN_NUM.get(col_key, "")
        html.append(f"<h2>{num}、{meta_col['heading']}</h2>")

        # 编号条目：带正文
        for idx, event in enumerate(detailed, 1):
            event_title = _pangu(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""
            html.append(f"<h3>{idx}. {event_title}{suffix}</h3>")
            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                html.append(f"<p>{_pangu(reader_body)}</p>")

        # 无序条目：仅标题，无分组标题
        if headline_only:
            html.append("<ul>")
            for event in headline_only:
                event_title = _pangu(event.get("title_zh", ""))
                if event_title:
                    html.append(f"<li>{event_title}</li>")
            html.append("</ul>")

    html.append("</article>")
    return "\n".join(html)


# ---------------------------------------------------------------------------
# 核心渲染：结构化 → Markdown
# ---------------------------------------------------------------------------

def render_structured_markdown(
    meta: dict,
    columns: dict[str, list[dict]],
    report_type: str = "daily",
) -> str:
    """
    从结构化数据生成 Markdown

    格式严格按内容协议：YAML frontmatter + 四大栏目 + 每条事件详情

    参数：
        report_type: 报告类型（daily / weekly / monthly），影响默认标题
    """
    # meta.title 优先；否则根据 report_type 生成默认标题
    if meta.get("title"):
        title = meta["title"]
    else:
        date_raw = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
        type_label = _REPORT_TYPE_TITLE.get(report_type, "每日新闻")
        title = f"{date_raw} {type_label}"
    lead = meta.get("lead", "")
    highlights = meta.get("highlights", [])
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))

    lines: list[str] = []

    # frontmatter
    lines.append("---")
    lines.append(f'title: "{title}"')
    lines.append(f'lead: "{lead}"')
    lines.append("highlights:")
    for h in highlights:
        lines.append(f'  - "{h}"')
    lines.append(f'date: "{date}"')
    lines.append("---")
    lines.append("")

    # 四大栏目
    for col_key in COLUMN_ORDER:
        col_data = columns.get(col_key, {})
        if isinstance(col_data, list):
            detailed = col_data
            headline_only = []
        else:
            detailed = col_data.get("detailed_events", [])
            headline_only = col_data.get("headline_only_events", [])

        if not detailed and not headline_only:
            continue

        meta_col = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        num = _COLUMN_NUM.get(col_key, "")
        lines.append(f"## {meta_col['icon']} {num}、{meta_col['heading']}")
        lines.append("")

        # 编号条目
        for idx, event in enumerate(detailed, 1):
            event_title = _pangu(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""

            lines.append(f"### {idx}. {event_title}{suffix}")
            lines.append("")

            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                lines.append(_pangu(reader_body))
                lines.append("")

            source_links = event.get("source_links", [])
            if source_links:
                for link in source_links:
                    link_title = str(link.get("title", "原文"))
                    link_url = str(link.get("url", ""))
                    if link_url:
                        lines.append(f"- [{link_title}]({link_url})")
                lines.append("")

        # 无序条目：仅标题
        if headline_only:
            for event in headline_only:
                event_title = _pangu(event.get("title_zh", ""))
                if event_title:
                    lines.append(f"- {event_title}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

# 报告类型 → 输出子目录
_REPORT_TYPE_DIR: dict[str, str] = {
    "daily": "docs/daily",
    "weekly": "docs/weekly",
    "monthly": "docs/monthly",
}


def save_daily_report(
    meta: dict,
    columns: dict[str, list[dict]],
    output_dir: str | None = None,
    report_type: str = "daily",
) -> tuple[str, str]:
    """
    保存报告到对应目录的 YYYY-MM-DD.md 和 .html

    参数：
        output_dir: 输出目录；为 None 时根据 report_type 自动选择
        report_type: 报告类型（daily / weekly / monthly）
    """
    if output_dir is None:
        output_dir = _REPORT_TYPE_DIR.get(report_type, "docs/daily")
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(output_dir, exist_ok=True)

    md_content = render_structured_markdown(meta, columns, report_type=report_type)
    html_content = render_structured_html(meta, columns, report_type=report_type)

    md_path = os.path.join(output_dir, f"{date}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    html_path = os.path.join(output_dir, f"{date}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return md_path, html_path
