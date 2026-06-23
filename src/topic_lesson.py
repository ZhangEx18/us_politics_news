#!/usr/bin/env python3
"""
专题型内容 handler — topic_lesson 产品线

职责：
- 从本地主题池选题（避开最近已发布、优先补标签覆盖、控制难度波动）
- 调用 LLM 生成结构化课程内容
- 持久化状态到独立 SQLite
- 输出 Markdown / HTML / RSS Feed
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from ai_analyzer import _call_llm, _load_ai_config
from config import load_product_config, augment_ai_config_with_runtime
from feed_builder import save_feed
from publish_manifest import build_manifest
from report_titles import build_report_title

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# ── 数据结构 ──


@dataclass
class TopicEntry:
    """主题池中的单个主题。"""
    slug: str
    title: str
    tags: list[str]
    difficulty: int
    summary: str
    python_required: bool
    cpp_optional: bool
    related_topics: list[str]


@dataclass
class LessonContent:
    """LLM 生成的课程内容。"""
    title: str
    core_question: str
    intuition: str
    approach: str
    python_code: str
    cpp_code: str
    complexity: str
    pitfalls: list[str]
    exercises: list[str]


# ── 主题池加载 ──


def load_topics(topics_file: str) -> list[TopicEntry]:
    """从 YAML 加载主题池。"""
    path = Path(topics_file)
    if not path.exists():
        raise FileNotFoundError(f"主题池文件不存在: {topics_file}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    entries = []
    for item in data:
        entries.append(TopicEntry(
            slug=item["slug"],
            title=item["title"],
            tags=list(item.get("tags", [])),
            difficulty=int(item.get("difficulty", 1)),
            summary=item.get("summary", ""),
            python_required=bool(item.get("python_required", True)),
            cpp_optional=bool(item.get("cpp_optional", True)),
            related_topics=list(item.get("related_topics", [])),
        ))
    return entries


# ── 状态数据库 ──


class TopicStateDb:
    """算法专题状态持久化。"""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS published_topic_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_slug TEXT NOT NULL,
                published_at TEXT NOT NULL,
                report_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS report_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_key TEXT NOT NULL UNIQUE,
                topic_slug TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT DEFAULT 'running',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS selection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_key TEXT NOT NULL,
                topic_slug TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_published_slug
                ON published_topic_history(topic_slug);
            CREATE INDEX IF NOT EXISTS idx_published_at
                ON published_topic_history(published_at);
        """)
        self._conn.commit()

    def get_recent_slugs(self, limit: int = 30) -> list[str]:
        """获取最近已发布的主题 slug 列表。"""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT topic_slug FROM published_topic_history "
            "ORDER BY published_at DESC LIMIT ?",
            (limit,),
        )
        return [row["topic_slug"] for row in cur.fetchall()]

    def get_recent_tag_counts(self, limit: int = 30) -> Counter:
        """获取最近已发布主题的标签分布。"""
        recent_slugs = self.get_recent_slugs(limit)
        # 需要从主题池反查标签，这里只返回 slug 列表
        return Counter(recent_slugs)

    def record_publish(self, report_key: str, topic_slug: str) -> None:
        """记录发布。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO published_topic_history "
            "(topic_slug, published_at, report_key) VALUES (?, ?, ?)",
            (topic_slug, now, report_key),
        )
        self._conn.commit()

    def start_run(self, report_key: str, topic_slug: str) -> int:
        """记录运行开始。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO report_runs "
            "(report_key, topic_slug, started_at, status) VALUES (?, ?, ?, 'running')",
            (report_key, topic_slug, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, report_key: str, status: str = "success", error: str | None = None) -> None:
        """记录运行结束。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE report_runs SET finished_at=?, status=?, error=? WHERE report_key=?",
            (now, status, error, report_key),
        )
        self._conn.commit()

    def log_selection(self, report_key: str, topic_slug: str, reason: str) -> None:
        """记录选题决策。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO selection_log (report_key, topic_slug, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (report_key, topic_slug, reason, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── 选题策略 ──


def select_topic(
    topics: list[TopicEntry],
    state_db: TopicStateDb,
    selection_cfg: dict | None = None,
) -> tuple[TopicEntry, str]:
    """
    选题策略：
    1. 避开最近已发布主题
    2. 优先补标签覆盖缺口
    3. 控制难度连续波动
    4. 主题池不足时显式失败

    返回 (选中主题, 决策理由)。
    """
    cfg = selection_cfg or {}
    history_limit = cfg.get("history_limit", 30)
    max_same_tag_gap = cfg.get("max_same_tag_gap", 7)
    preferred_difficulty_span = cfg.get("preferred_difficulty_span", 1)

    recent_slugs = set(state_db.get_recent_slugs(history_limit))

    # 第一轮：过滤掉最近已发布的
    candidates = [t for t in topics if t.slug not in recent_slugs]
    if not candidates:
        # 所有主题都最近发布过，放宽限制
        candidates = topics
        reason = "所有主题均在最近发布过，放宽限制"
    else:
        reason = "正常选题"

    # 第二轮：计算标签覆盖缺口
    # 统计最近发布中各标签出现次数
    recent_tag_counter: Counter = Counter()
    for slug in recent_slugs:
        for t in topics:
            if t.slug == slug:
                for tag in t.tags:
                    recent_tag_counter[tag] += 1

    # 给每个候选打分：标签出现越少分越高，难度适中加分
    def _score(t: TopicEntry) -> float:
        tag_score = sum(1.0 / (recent_tag_counter.get(tag, 0) + 1) for tag in t.tags)
        # 难度 2 最优，1 和 3 次之
        diff_score = 1.0 - abs(t.difficulty - 2) * 0.3
        return tag_score + diff_score

    candidates.sort(key=_score, reverse=True)
    selected = candidates[0]

    # 记录详细理由
    tag_gaps = [tag for tag in selected.tags if recent_tag_counter.get(tag, 0) < max_same_tag_gap]
    reason = f"{reason}；标签覆盖: {tag_gaps}；难度: {selected.difficulty}"

    return selected, reason


# ── LLM 课程生成 ──

LESSON_PROMPT_TEMPLATE = """你是一位资深算法竞赛教练和计算机科学教授。请根据以下主题信息，生成一篇高质量的每日算法专题教程。

## 主题信息

- 标题：{title}
- 标签：{tags}
- 难度：{difficulty}/3
- 简介：{summary}
- 相关知识点：{related_topics}
- 需要 Python 示例：{python_required}
- 可选 C++ 对照：{cpp_optional}

## 输出要求

请严格按以下 JSON 格式输出，不要添加任何额外文字：

```json
{{
  "title": "教程标题（简洁有力）",
  "core_question": "这篇文章要解决的核心问题（一句话）",
  "intuition": "直觉理解：用最通俗的语言解释这个算法/数据结构的本质（200-300字）",
  "approach": "算法思路：分步骤讲解解题框架（300-500字）",
  "python_code": "Python 示例代码（含详细注释，完整可运行）",
  "cpp_code": "C++ 对照代码（如 cpp_optional=false 则留空字符串）",
  "complexity": "时间复杂度与空间复杂度分析（100-200字）",
  "pitfalls": ["易错点1", "易错点2", "易错点3"],
  "exercises": ["延伸练习1", "延伸练习2"]
}}
```

## 质量要求

1. **直觉优先**：先让人"感觉懂了"，再给形式化描述
2. **代码可运行**：Python 代码必须完整，包含测试用例
3. **中文撰写**：所有文字内容用中文，代码注释也用中文
4. **避免八股**：不要用"值得注意的是""需要指出的是"等套话
5. **pitfalls 必须具体**：每个易错点要指出具体的错误模式和修复方法
6. **exercises 有梯度**：第一题巩固基础，第二题适度拓展"""


async def generate_lesson(
    topic: TopicEntry,
    ai_config: dict,
    cpp_optional: bool = True,
) -> LessonContent:
    """调用 LLM 生成课程内容。"""
    prompt = LESSON_PROMPT_TEMPLATE.format(
        title=topic.title,
        tags=", ".join(topic.tags),
        difficulty=topic.difficulty,
        summary=topic.summary,
        related_topics=", ".join(topic.related_topics),
        python_required="是" if topic.python_required else "否",
        cpp_optional="是" if cpp_optional and topic.cpp_optional else "否",
    )

    timeout = int(ai_config.get("meta_timeout_seconds", 120))
    response = await _call_llm(prompt, ai_config, timeout=timeout)

    # 解析 JSON 响应
    text = response.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # 尝试提取 JSON
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        for pattern in (r"\{.*\}",):
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    break
                except json.JSONDecodeError:
                    continue

    if parsed is None:
        raise ValueError(f"无法从 LLM 响应中解析 JSON: {response[:200]}")

    return LessonContent(
        title=str(parsed.get("title", topic.title)),
        core_question=str(parsed.get("core_question", "")),
        intuition=str(parsed.get("intuition", "")),
        approach=str(parsed.get("approach", "")),
        python_code=str(parsed.get("python_code", "")),
        cpp_code=str(parsed.get("cpp_code", "")),
        complexity=str(parsed.get("complexity", "")),
        pitfalls=list(parsed.get("pitfalls", [])),
        exercises=list(parsed.get("exercises", [])),
    )


# ── 渲染 ──


def _escape_html(text: str) -> str:
    import html as _html
    return _html.escape(text, quote=False)


def _render_markdown(
    topic: TopicEntry,
    lesson: LessonContent,
    report_key: str,
    title: str,
) -> str:
    """渲染为 Markdown。"""
    lines = [
        "---",
        f"title: \"{title}\"",
        f"date: {report_key}",
        f"topic: {topic.slug}",
        f"difficulty: {topic.difficulty}",
        f"tags: {json.dumps(topic.tags, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        f"> {lesson.core_question}",
        "",
        "## 直觉理解",
        "",
        lesson.intuition,
        "",
        "## 算法思路",
        "",
        lesson.approach,
        "",
        "## Python 示例",
        "",
        "```python",
        lesson.python_code,
        "```",
    ]

    if lesson.cpp_code:
        lines.extend([
            "",
            "## C++ 对照",
            "",
            "```cpp",
            lesson.cpp_code,
            "```",
        ])

    lines.extend([
        "",
        "## 复杂度分析",
        "",
        lesson.complexity,
        "",
        "## 易错点",
        "",
    ])
    for i, pitfall in enumerate(lesson.pitfalls, 1):
        lines.append(f"{i}. {pitfall}")
    lines.append("")
    lines.append("## 延伸练习")
    lines.append("")
    for i, exercise in enumerate(lesson.exercises, 1):
        lines.append(f"{i}. {exercise}")
    lines.append("")

    return "\n".join(lines)


def _render_html(
    topic: TopicEntry,
    lesson: LessonContent,
    report_key: str,
    title: str,
) -> str:
    """渲染为完整 HTML 页面。"""
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
}
header .meta {
    color: #666;
    font-size: 0.9em;
}
header .question {
    color: #444;
    font-size: 1em;
    margin-top: 12px;
    padding: 8px 12px;
    background: #f5f5f5;
    border-radius: 4px;
    border-left: 3px solid #0f5e3a;
}
h2 {
    font-size: 1.3em;
    font-weight: 700;
    margin-top: 36px;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid #e0e0e0;
}
pre {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 16px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 0.9em;
    line-height: 1.5;
}
code {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
}
p code {
    background: #f0f0f0;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 0.9em;
}
ol, ul {
    padding-left: 24px;
}
li {
    margin: 6px 0;
}
.tags {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 8px;
}
.tag {
    background: #e8f5e9;
    color: #2e7d32;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.85em;
}
.difficulty {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.85em;
    font-weight: 600;
}
.diff-1 { background: #e8f5e9; color: #2e7d32; }
.diff-2 { background: #fff3e0; color: #e65100; }
.diff-3 { background: #fce4ec; color: #c62828; }
footer {
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid #e0e0e0;
    color: #999;
    font-size: 0.8em;
    text-align: center;
}
"""

    difficulty_labels = {1: "入门", 2: "进阶", 3: "挑战"}
    diff_label = difficulty_labels.get(topic.difficulty, "未知")
    tags_html = "".join(f'<span class="tag">{_escape_html(tag)}</span>' for tag in topic.tags)

    parts = [
        "<!DOCTYPE html>",
        "<html lang='zh'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>{_escape_html(title)}</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{_escape_html(lesson.title)}</h1>",
        f'<div class="meta">',
        f'  <span class="difficulty diff-{topic.difficulty}">{diff_label}</span>',
        f'  <div class="tags">{tags_html}</div>',
        f"</div>",
        f'<div class="question">{_escape_html(lesson.core_question)}</div>',
        "</header>",
        "<main>",
        "<h2>直觉理解</h2>",
        f"<p>{_escape_html(lesson.intuition)}</p>",
        "<h2>算法思路</h2>",
        f"<p>{_escape_html(lesson.approach)}</p>",
        "<h2>Python 示例</h2>",
        f"<pre><code>{_escape_html(lesson.python_code)}</code></pre>",
    ]

    if lesson.cpp_code:
        parts.extend([
            "<h2>C++ 对照</h2>",
            f"<pre><code>{_escape_html(lesson.cpp_code)}</code></pre>",
        ])

    parts.extend([
        "<h2>复杂度分析</h2>",
        f"<p>{_escape_html(lesson.complexity)}</p>",
        "<h2>易错点</h2>",
        "<ol>",
    ])
    for pitfall in lesson.pitfalls:
        parts.append(f"<li>{_escape_html(pitfall)}</li>")
    parts.append("</ol>")

    parts.append("<h2>延伸练习</h2>")
    parts.append("<ol>")
    for exercise in lesson.exercises:
        parts.append(f"<li>{_escape_html(exercise)}</li>")
    parts.append("</ol>")

    parts.extend([
        "</main>",
        "<footer>",
        f"<p>每日算法专题 · {report_key}</p>",
        "</footer>",
        "</body>",
        "</html>",
    ])

    return "\n".join(parts)


def _render_reader_html(
    topic: TopicEntry,
    lesson: LessonContent,
    title: str,
) -> str:
    """为 RSS Reader 生成纯正文 HTML 片段。"""
    parts = [
        "<article>",
        f"<h2>{_escape_html(lesson.core_question)}</h2>",
        "<h3>直觉理解</h3>",
        f"<p>{_escape_html(lesson.intuition)}</p>",
        "<h3>算法思路</h3>",
        f"<p>{_escape_html(lesson.approach)}</p>",
        "<h3>Python 示例</h3>",
        f"<pre><code>{_escape_html(lesson.python_code)}</code></pre>",
    ]

    if lesson.cpp_code:
        parts.extend([
            "<h3>C++ 对照</h3>",
            f"<pre><code>{_escape_html(lesson.cpp_code)}</code></pre>",
        ])

    parts.extend([
        "<h3>复杂度分析</h3>",
        f"<p>{_escape_html(lesson.complexity)}</p>",
        "<h3>易错点</h3>",
        "<ol>",
    ])
    for pitfall in lesson.pitfalls:
        parts.append(f"<li>{_escape_html(pitfall)}</li>")
    parts.append("</ol>")
    parts.append("</article>")

    return "\n".join(parts)


# ── Feed 生成 ──


def save_lesson_feed(
    topic: TopicEntry,
    lesson: LessonContent,
    report_key: str,
    title: str,
    feed_path: str,
    base_url: str,
    pub_date: datetime,
    product_key: str = "algorithms",
) -> str:
    """保存 RSS Feed item。"""
    manifest = build_manifest(
        product_key=product_key,
        report_type="daily",
        report_key=report_key,
        title=title,
        pub_date=pub_date,
        base_url=base_url,
    )

    # 短摘要
    short_desc = lesson.core_question[:200] if lesson.core_question else lesson.intuition[:200]

    # Reader 正文
    html_body = _render_reader_html(topic, lesson, title)

    # 直接操作 feed XML，复用 feed_builder 的合并逻辑
    from feed_builder import (
        _parse_existing_items,
        _merge_items,
        _build_item_xml_from_manifest,
        build_feed,
    )
    import xml.etree.ElementTree as ET

    link = f"{base_url}/{manifest.link_path}" if base_url else manifest.link_path
    new_item = _build_item_xml_from_manifest(
        title=manifest.title,
        short_description=short_desc,
        html_body=html_body,
        link=link,
        pub_date=manifest.pub_date,
        guid=manifest.guid,
    )

    existing_items: list[str] = []
    if os.path.exists(feed_path):
        try:
            with open(feed_path, "r", encoding="utf-8") as f:
                existing_xml = f.read()
            existing_items = _parse_existing_items(existing_xml)
        except (OSError, ET.ParseError):
            existing_items = []

    merged_items = _merge_items(new_item, existing_items, max_days=30)
    feed_xml = build_feed(merged_items, base_url)

    os.makedirs(os.path.dirname(feed_path) or ".", exist_ok=True)
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_xml)

    return feed_path


# ── 主流程 ──


def run_topic_lesson_daily(product_key: str = "algorithms") -> dict:
    """
    算法专题 daily 完整流程：
    1. 加载配置和主题池
    2. 选题
    3. 生成课程
    4. 渲染输出
    5. 更新 Feed
    6. 持久化状态
    """
    start_time = datetime.now(BEIJING_TZ)
    config = load_product_config(product_key)
    ai_config = augment_ai_config_with_runtime(_load_ai_config(), config)

    # 解析配置
    publish_cfg = config.get("publish", {})
    storage_cfg = config.get("storage", {})
    selection_cfg = config.get("topic_selection", {})
    digest_cfg = config.get("digest", {})

    topics_file = config.get("topics_file", f"config/products/{product_key}/topics.yaml")
    db_path = storage_cfg.get("db_path", f"data/products/{product_key}/state.db")
    site_root = publish_cfg.get("site_root", f"docs/{product_key}")
    feed_path = publish_cfg.get("feed_path", f"docs/feeds/{product_key}.xml")
    base_url = publish_cfg.get("base_url", "")

    # 计算 report_key（北京时间当天）
    report_key = start_time.strftime("%Y-%m-%d")
    title = f"{report_key} 每日算法专题"

    print("=" * 60)
    print(f"算法专题 Pipeline — {title}")
    print(f"时间: {start_time.isoformat()}")
    print("=" * 60)

    # 加载主题池
    topics = load_topics(topics_file)
    print(f"\n[1/5] 加载主题池: {len(topics)} 个主题")

    if not topics:
        raise ValueError("主题池为空，无法生成内容")

    # 选题
    state_db = TopicStateDb(db_path)
    try:
        topic, reason = select_topic(topics, state_db, selection_cfg)
        print(f"\n[2/5] 选题: {topic.title} ({topic.slug})")
        print(f"   理由: {reason}")

        state_db.start_run(report_key, topic.slug)
        state_db.log_selection(report_key, topic.slug, reason)

        # 生成课程
        print(f"\n[3/5] 生成课程内容...")
        lesson = asyncio.run(generate_lesson(
            topic=topic,
            ai_config=ai_config,
            cpp_optional=topic.cpp_optional,
        ))
        print(f"   标题: {lesson.title}")
        print(f"   易错点: {len(lesson.pitfalls)} 个")
        print(f"   练习: {len(lesson.exercises)} 个")

        # 渲染输出
        print(f"\n[4/5] 渲染输出...")
        output_dir = os.path.join(site_root, "daily")
        os.makedirs(output_dir, exist_ok=True)

        md_path = os.path.join(output_dir, f"{report_key}.md")
        html_path = os.path.join(output_dir, f"{report_key}.html")

        md_content = _render_markdown(topic, lesson, report_key, title)
        html_content = _render_html(topic, lesson, report_key, title)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        print(f"   Markdown: {md_path}")
        print(f"   HTML: {html_path}")

        # 更新 Feed
        print(f"\n[5/5] 更新 RSS Feed...")
        pub_date = start_time
        save_lesson_feed(topic, lesson, report_key, title, feed_path, base_url, pub_date)
        print(f"   Feed: {feed_path}")

        # 记录发布
        state_db.record_publish(report_key, topic.slug)
        state_db.finish_run(report_key, status="success")

    except Exception as e:
        state_db.finish_run(report_key, status="error", error=str(e))
        raise
    finally:
        state_db.close()

    duration = (datetime.now(BEIJING_TZ) - start_time).total_seconds()
    stats = {
        "product_key": product_key,
        "report_type": "daily",
        "report_key": report_key,
        "topic_slug": topic.slug,
        "topic_title": topic.title,
        "duration_seconds": round(duration, 1),
        "outputs": {
            "markdown": md_path,
            "html": html_path,
            "feed": feed_path,
        },
    }

    print(f"\n{'=' * 60}")
    print(f"完成: {title}")
    print(f"{'=' * 60}")
    print(f"  耗时: {duration:.1f}s")
    print(f"  主题: {topic.title}")

    return stats
