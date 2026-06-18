#!/usr/bin/env python3
"""
日报渲染器 — Yeekal 风格

特性：
- YAML frontmatter（title / lead / highlights / date）
- 四大栏目分组：美国政情 / 国际风云 / 科技前沿 / 财经脉动
- 每条新闻：中文标题 + 核心亮点 + 核心事实 + 要点列表 + 背景/影响 + 为什么值得关注 + 原文链接
- 中英文混排自动空格（Pangu spacing）
- 无文章时生成"今日重点较少"说明页
"""

import os
import re
from collections import defaultdict
from datetime import datetime

from scoring import ScoredArticle

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
    "us_politics": {"heading": "美国政情", "icon": "🏛️"},
    "global_affairs": {"heading": "国际风云", "icon": "🌍"},
    "technology": {"heading": "科技前沿", "icon": "🔬"},
    "economy": {"heading": "财经脉动", "icon": "📊"},
}

# 栏目输出顺序
COLUMN_ORDER: list[str] = [
    "us_politics",
    "global_affairs",
    "technology",
    "economy",
]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _get_column(article: ScoredArticle) -> str:
    """获取文章栏目，兼容旧字段 topic"""
    return getattr(article, "column", "") or getattr(article, "topic", "") or "us_politics"


def _get_key_points(article: ScoredArticle) -> list[str]:
    """获取要点列表，兼容旧字段"""
    points = getattr(article, "key_points", None)
    if points:
        return list(points)
    # 从 summary_zh 或 summary 拆分
    text = getattr(article, "summary_zh", "") or article.summary or ""
    if not text:
        return []
    # 按句号/分号拆分，保留有实质内容的句子
    sentences = re.split(r"[。；;]", text)
    return [s.strip() for s in sentences if len(s.strip()) > 8][:4]


def _get_core_facts(article: ScoredArticle) -> list[str]:
    """获取核心事实列表，无则从 key_points 拼接"""
    facts = getattr(article, "core_facts", None)
    if facts:
        return list(facts)
    # 回退：从 key_points 中取前 3 条作为核心事实
    return _get_key_points(article)[:3]


def _get_background_impact(article: ScoredArticle) -> str:
    """获取背景/影响，无则从 analysis 获取"""
    text = getattr(article, "background_impact", None)
    if text:
        return str(text)
    return getattr(article, "analysis", "") or ""


def _get_why_it_matters(article: ScoredArticle) -> str:
    """获取"为什么值得关注"，兼容旧字段"""
    text = getattr(article, "why_it_matters", None)
    if text:
        return str(text)
    return getattr(article, "analysis", "") or ""


def _get_source_links(article: ScoredArticle) -> list[dict[str, str]]:
    """获取来源链接列表，兼容旧字段"""
    links = getattr(article, "source_links", None)
    if links:
        return list(links)
    # 单链接回退
    return [{"title": article.source, "url": str(article.url)}]


def _get_hook(article: ScoredArticle) -> str:
    """获取核心亮点（一句话）"""
    hook = getattr(article, "one_line_hook", None)
    if hook:
        return str(hook)
    return getattr(article, "title_zh", "") or _pangu(article.title)


def _build_lead(articles: list[ScoredArticle], config: dict | None = None) -> str:
    """从前几条重点文章生成导读（150-300 字）"""
    # 从配置读取字数范围，或使用默认值
    digest_cfg = (config or {}).get("digest", {})
    min_len = digest_cfg.get("lead_min_chars", 150)
    max_len = digest_cfg.get("lead_max_chars", 300)

    top = sorted(articles, key=lambda a: a.score, reverse=True)[:5]
    hooks = [_get_hook(a) for a in top]
    if not hooks:
        return "今日重点新闻较少，以下是值得关注的动态汇总。"
    lead = "，".join(hooks)
    # 控制长度
    if len(lead) > max_len:
        lead = lead[:max_len - 3] + "..."
    elif len(lead) < min_len:
        # 不够长时追加更多文章的 hook
        for a in top[len(hooks):]:
            extra = _get_hook(a)
            candidate = f"{lead}，{extra}"
            if len(candidate) > max_len:
                remaining = max_len - len(lead) - 1
                if remaining > 10:
                    lead = f"{lead}，{extra[:remaining - 3]}..."
                break
            lead = candidate
            if len(lead) >= min_len:
                break
    return lead


def _build_highlights(articles: list[ScoredArticle]) -> list[str]:
    """生成 highlights 列表（2-3 条，每条 15-30 字）"""
    top = sorted(articles, key=lambda a: a.score, reverse=True)[:3]
    result = []
    for a in top:
        hook = _get_hook(a)
        if len(hook) > 30:
            hook = hook[:27] + "..."
        result.append(hook)
    return result


def _build_title(articles: list[ScoredArticle]) -> str:
    """生成日报标题（核心事件 1-2 个）"""
    top = sorted(articles, key=lambda a: a.score, reverse=True)[:2]
    if not top:
        return "今日重点较少"
    titles = []
    for a in top:
        t = getattr(a, "title_zh", "") or _pangu(a.title)
        # 截断到合理长度
        if len(t) > 20:
            t = t[:17] + "..."
        titles.append(t)
    return "，".join(titles)


def _render_article(article: ScoredArticle, index: int) -> list[str]:
    """渲染单条新闻（事件详情结构）"""
    lines: list[str] = []

    # 标题
    title = _pangu(getattr(article, "title_zh", "") or article.title)
    title = title.replace("[", "(").replace("]", ")")
    lines.append(f"### {index}. {title}")
    lines.append("")

    # 核心亮点
    hook = _get_hook(article)
    if hook and hook != title:
        lines.append(f"**核心亮点**：{_pangu(hook)}")
        lines.append("")

    # 核心事实
    core_facts = _get_core_facts(article)
    if core_facts:
        lines.append("**核心事实**：")
        for fact in core_facts:
            lines.append(f"- {_pangu(fact)}")
        lines.append("")

    # 要点（避免与核心事实重复）
    key_points = _get_key_points(article)
    if key_points and (not core_facts or set(map(str.strip, core_facts)) != set(map(str.strip, key_points))):
        for point in key_points:
            lines.append(f"- {_pangu(point)}")
        lines.append("")

    # 背景/影响
    background_impact = _get_background_impact(article)
    if background_impact:
        lines.append(f"**背景/影响**：{_pangu(background_impact)}")
        lines.append("")

    # 为什么值得关注
    why = _get_why_it_matters(article)
    if why:
        lines.append(f"**为什么值得关注**：{_pangu(why)}")
        lines.append("")

    # 原文链接
    source_links = _get_source_links(article)
    if source_links:
        link_parts = []
        for sl in source_links:
            link_title = _pangu(sl.get("title", article.source))
            link_url = sl.get("url", str(article.url))
            link_parts.append(f"[{link_title}]({link_url})")
        lines.append(" ".join(link_parts))
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# 核心渲染
# ---------------------------------------------------------------------------

def render_markdown(articles: list[ScoredArticle], date: str | None = None, config: dict | None = None) -> str:
    """
    生成 Yeekal 风格的完整日报 Markdown

    格式：YAML frontmatter + 四大栏目分组 + 每条新闻详情
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # 按栏目分组
    by_column: dict[str, list[ScoredArticle]] = defaultdict(list)
    for a in articles:
        col = _get_column(a)
        # 只保留重点和观察级别
        if a.level in ("重点", "观察"):
            by_column[col].append(a)

    has_content = any(by_column.values())

    # --- 无文章时的说明页 ---
    if not has_content:
        front = [
            "---",
            f'title: "今日重点较少"',
            f'lead: "今日暂无重点新闻，以下是值得关注的动态汇总。"',
            "highlights:",
            '  - "今日暂无重点新闻"',
            f'date: "{date}"',
            "---",
            "",
            "### 今日重点较少",
            "",
            "今日暂无重点新闻值得深度报道。",
            "",
            f"*数据来源：{len(articles)} 条原始内容，经 AI 筛选后未达到重点标准。*",
        ]
        return "\n".join(front)

    # --- frontmatter ---
    all_filtered = [a for group in by_column.values() for a in group]
    title = _build_title(all_filtered)
    lead = _build_lead(all_filtered, config)
    highlights = _build_highlights(all_filtered)

    fm_lines = [
        "---",
        f'title: "{title}"',
        f'lead: "{lead}"',
        "highlights:",
    ]
    for h in highlights:
        fm_lines.append(f'  - "{h}"')
    fm_lines.append(f'date: "{date}"')
    fm_lines.append("---")
    fm_lines.append("")

    # --- 正文：按四大栏目分组 ---
    body_lines: list[str] = []
    global_index = 0

    for col_key in COLUMN_ORDER:
        col_articles = by_column.get(col_key, [])
        if not col_articles:
            continue

        # 按分数降序
        col_articles.sort(key=lambda a: a.score, reverse=True)

        meta = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        heading = meta["heading"]
        icon = meta["icon"]

        body_lines.append(f"## {icon} {heading}")
        body_lines.append("")

        for article in col_articles:
            global_index += 1
            body_lines.extend(_render_article(article, global_index))

    return "\n".join(fm_lines + body_lines)


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------

def render_html(articles: list[ScoredArticle], date: str | None = None, config: dict | None = None) -> str:
    """基于 Markdown 数据生成简洁阅读样式的 HTML"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # 按栏目分组
    by_column: dict[str, list[ScoredArticle]] = defaultdict(list)
    for a in articles:
        if a.level in ("重点", "观察"):
            col = _get_column(a)
            by_column[col].append(a)

    has_content = any(by_column.values())

    # 构建 highlights 用于 meta
    all_articles = [a for group in by_column.values() for a in group]
    highlights = _build_highlights(all_articles)
    lead = _build_lead(all_articles, config)
    page_title = _build_title(all_articles) if has_content else "今日重点较少"

    # CSS
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
h3 {
    font-size: 1.05em;
    font-weight: 600;
    margin-top: 28px;
    margin-bottom: 8px;
    color: #1a1a1a;
}
.article {
    margin-bottom: 28px;
    padding-bottom: 20px;
    border-bottom: 1px solid #f0f0f0;
}
.article:last-child {
    border-bottom: none;
}
.hook {
    color: #555;
    font-size: 0.92em;
    margin: 4px 0 8px;
}
.points {
    margin: 8px 0;
    padding-left: 20px;
    font-size: 0.92em;
}
.points li {
    margin: 4px 0;
    color: #333;
}
.facts {
    margin: 8px 0;
    padding-left: 20px;
    font-size: 0.92em;
}
.facts li {
    margin: 4px 0;
    color: #333;
}
.background {
    background: #f0f7f0;
    border-left: 3px solid #4a9;
    padding: 8px 12px;
    margin: 8px 0;
    font-size: 0.9em;
    color: #333;
}
.why {
    background: #fafafa;
    border-left: 3px solid #999;
    padding: 8px 12px;
    margin: 8px 0;
    font-size: 0.9em;
    color: #444;
}
.links {
    font-size: 0.85em;
    color: #666;
    margin-top: 8px;
}
.links a {
    color: #1a6b3c;
    text-decoration: none;
}
.links a:hover {
    text-decoration: underline;
}
.empty {
    text-align: center;
    color: #999;
    margin-top: 80px;
    font-size: 1.1em;
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
        f"<title>{page_title} — {date}</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{page_title}</h1>",
        f"<div class='date'>{date}</div>",
        f"<div class='lead'>{_pangu(lead)}</div>",
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

    if not has_content:
        html.append("<div class='empty'>今日暂无重点新闻</div>")
        html.append(f"<footer>基于 {len(articles)} 条原始内容筛选</footer>")
        html.extend(["</body>", "</html>"])
        return "\n".join(html)

    # 按栏目输出
    global_index = 0
    for col_key in COLUMN_ORDER:
        col_articles = by_column.get(col_key, [])
        if not col_articles:
            continue

        col_articles.sort(key=lambda a: a.score, reverse=True)
        meta = COLUMN_META.get(col_key, {"heading": col_key, "icon": ""})
        heading = meta["heading"]
        icon = meta["icon"]

        html.append(f"<h2>{icon} {heading}</h2>")

        for article in col_articles:
            global_index += 1
            title = _pangu(getattr(article, "title_zh", "") or article.title)
            title = title.replace("[", "(").replace("]", ")")
            hook = _get_hook(article)
            core_facts = _get_core_facts(article)
            key_points = _get_key_points(article)
            background_impact = _get_background_impact(article)
            why = _get_why_it_matters(article)
            source_links = _get_source_links(article)

            html.append("<div class='article'>")
            html.append(f"<h3>{global_index}. {title}</h3>")

            if hook and hook != title:
                html.append(f"<div class='hook'>{_pangu(hook)}</div>")

            if core_facts:
                html.append("<ul class='facts'>")
                for fact in core_facts:
                    html.append(f"<li>{_pangu(fact)}</li>")
                html.append("</ul>")

            if key_points and (not core_facts or set(map(str.strip, core_facts)) != set(map(str.strip, key_points))):
                html.append("<ul class='points'>")
                for point in key_points:
                    html.append(f"<li>{_pangu(point)}</li>")
                html.append("</ul>")

            if background_impact:
                html.append(f"<div class='background'><strong>背景/影响</strong>：{_pangu(background_impact)}</div>")

            if why:
                html.append(f"<div class='why'><strong>为什么值得关注</strong>：{_pangu(why)}</div>")

            if source_links:
                link_parts = []
                for sl in source_links:
                    lt = _pangu(sl.get("title", article.source))
                    lu = sl.get("url", str(article.url))
                    link_parts.append(f"<a href='{lu}'>{lt}</a>")
                html.append(f"<div class='links'>{' | '.join(link_parts)}</div>")

            html.append("</div>")

    # footer
    total_count = len(articles)
    selected_count = sum(len(v) for v in by_column.values())
    html.append(f"<footer>从 {total_count} 条原始内容中筛选出 {selected_count} 条</footer>")
    html.extend(["</body>", "</html>"])

    return "\n".join(html)


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

def save_daily_report(
    articles: list[ScoredArticle],
    output_dir: str = "docs/daily",
    config: dict | None = None,
) -> tuple[str, str]:
    """保存日报到 docs/daily/YYYY-MM-DD.md 和 .html"""
    date = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(output_dir, exist_ok=True)

    md_path = os.path.join(output_dir, f"{date}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(articles, date, config))

    html_path = os.path.join(output_dir, f"{date}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(articles, date, config))

    return md_path, html_path
