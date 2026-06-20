#!/usr/bin/env python3
"""
日报渲染器 v3 — 结构化渲染

特性：
- 从结构化 dict 直接生成 HTML / Markdown，不经过 Markdown → HTML 转换
- YAML frontmatter（title / lead / highlights / date）
- 四大栏目分组：美国政局 / 国际局势 / 科技前沿 / 经济走势
- 每条事件：核心事实 + 背景与影响 + 为什么值得关注 + 来源链接
- 中英文混排自动空格（Pangu spacing）
"""

import os
import re
import html
from datetime import datetime
from typing import TYPE_CHECKING
import yaml
from report_titles import build_report_title

if TYPE_CHECKING:
    from publish_manifest import ReportManifest

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


def _html_text(text: object) -> str:
    """渲染外部文本前转义 HTML，避免源站或 LLM 输出注入页面。"""
    if text is None:
        return ""
    return html.escape(_pangu(str(text)), quote=False)


def _html_title_text(text: object) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _markdown_text(text: object) -> str:
    if text is None:
        return ""
    return html.escape(_pangu(str(text)), quote=False)


def _markdown_title_text(text: object) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _frontmatter(title: str, lead: str, highlights: list, date: str) -> str:
    payload = {
        "title": title,
        "lead": lead,
        "highlights": highlights,
        "date": date,
    }
    dumped = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n"


# ---------------------------------------------------------------------------
# 栏目配置
# ---------------------------------------------------------------------------
COLUMN_META: dict[str, dict[str, str]] = {
    "us_politics": {"heading": "美国政局", "icon": ""},
    "global_affairs": {"heading": "国际局势", "icon": ""},
    "technology": {"heading": "科技前沿", "icon": ""},
    "economy": {"heading": "经济走势", "icon": ""},
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
        title = _html_title_text(meta["title"])
    else:
        date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
        title = _html_title_text(build_report_title(report_type, date))
    lead = _html_text(meta.get("lead", ""))
    highlights = meta.get("highlights", [])
    date = _html_text(meta.get("date", datetime.now().strftime("%Y-%m-%d")))

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
            html.append(f"<li>{_html_text(h)}</li>")
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

        if not detailed:
            continue

        meta_col = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        num = _COLUMN_NUM.get(col_key, "")
        html.append(f"<h2>{meta_col['icon']} {num}、{meta_col['heading']}</h2>")

        # 编号条目
        for idx, event in enumerate(detailed, 1):
            event_title = _html_text(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""

            html.append("<div class='event'>")
            html.append(f"<h3>{idx}. {event_title}{suffix}</h3>")

            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                html.append(f"<p>{_html_text(reader_body)}</p>")

            html.append("</div>")

        # 无序条目：仅标题
        if headline_only:
            html.append("<ul>")
            for event in headline_only:
                event_title = _html_text(event.get("title_zh", ""))
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
    manifest: "ReportManifest | None" = None,
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
        manifest: 统一发布元数据；提供时优先使用其 title 和 highlights_heading
    """
    # manifest 提供时优先使用其标题
    if manifest:
        title = _html_title_text(manifest.title)
    else:
        date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
        try:
            title = _html_title_text(build_report_title(report_type, date))
        except ValueError:
            title = _html_title_text(meta.get("title", ""))
    highlights = meta.get("highlights", []) or []

    html: list[str] = ["<article>"]

    if highlights:
        heading = manifest.highlights_heading if manifest else _HIGHLIGHTS_HEADING.get(report_type, "今日要点")
        html.append(f"<h2>{heading}</h2>")
        html.append("<ul>")
        for item in highlights:
            html.append(f"<li>{_html_text(item)}</li>")
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

        if not detailed:
            continue

        meta_col = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        num = _COLUMN_NUM.get(col_key, "")
        html.append(f"<h2>{num}、{meta_col['heading']}</h2>")

        # 编号条目：带正文
        for idx, event in enumerate(detailed, 1):
            event_title = _html_text(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""
            html.append(f"<h3>{idx}. {event_title}{suffix}</h3>")
            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                html.append(f"<p>{_html_text(reader_body)}</p>")

        if headline_only:
            html.append("<ul>")
            for event in headline_only:
                event_title = _html_text(event.get("title_zh", ""))
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
        title = _markdown_title_text(meta["title"])
    else:
        date_raw = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
        title = _markdown_title_text(build_report_title(report_type, date_raw))
    lead = _markdown_text(meta.get("lead", ""))
    highlights = [_markdown_text(h) for h in meta.get("highlights", [])]
    date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))

    lines: list[str] = [_frontmatter(title, lead, highlights, date), ""]

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
            event_title = _markdown_text(event.get("title_zh", ""))
            is_followup = event.get("is_followup", False)
            suffix = " [持续跟踪]" if is_followup else ""

            lines.append(f"### {idx}. {event_title}{suffix}")
            lines.append("")

            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            if reader_body:
                lines.append(_markdown_text(reader_body))
                lines.append("")

        # 无序条目：仅标题
        if headline_only:
            for event in headline_only:
                event_title = _markdown_text(event.get("title_zh", ""))
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
    manifest: "ReportManifest | None" = None,
) -> tuple[str, str]:
    """
    保存报告到对应目录的 YYYY-MM-DD.md 和 .html

    参数：
        output_dir: 输出目录；为 None 时根据 report_type 自动选择
        report_type: 报告类型（daily / weekly / monthly）
        manifest: 统一发布元数据；提供时用于推导输出目录等
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
