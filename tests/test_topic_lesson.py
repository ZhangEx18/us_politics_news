"""算法专题 handler 测试 — topic_lesson.py"""

from __future__ import annotations

import os
import tempfile

import yaml

from topic_lesson import (
    TopicEntry,
    TopicStateDb,
    load_topics,
    select_topic,
    _render_markdown,
    _render_html,
    LessonContent,
)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
TOPICS_FILE = os.path.join(PROJECT_ROOT, "config", "products", "algorithms", "topics.yaml")


def _make_topic(slug="test-topic", title="测试主题", tags=["数组"], difficulty=1, **kwargs):
    defaults = dict(
        slug=slug,
        title=title,
        tags=tags,
        difficulty=difficulty,
        summary="测试简介",
        python_required=True,
        cpp_optional=True,
        related_topics=["测试"],
    )
    defaults.update(kwargs)
    return TopicEntry(**defaults)


def _make_lesson(**kwargs):
    defaults = dict(
        title="测试课程",
        core_question="这是一道什么题？",
        intuition="直觉理解内容",
        approach="算法思路内容",
        python_code="def solve(): pass",
        cpp_code="",
        complexity="O(n) 时间，O(1) 空间",
        pitfalls=["易错点1", "易错点2"],
        exercises=["练习1", "练习2"],
    )
    defaults.update(kwargs)
    return LessonContent(**defaults)


# ── 主题池加载 ──


def test_load_topics_from_yaml():
    topics = load_topics(TOPICS_FILE)
    assert len(topics) > 0
    for t in topics:
        assert isinstance(t, TopicEntry)
        assert t.slug
        assert t.title
        assert t.tags
        assert t.difficulty in (1, 2, 3)


def test_load_topics_required_fields():
    topics = load_topics(TOPICS_FILE)
    for t in topics:
        assert t.slug, f"slug 为空"
        assert t.title, f"title 为空"
        assert t.summary, f"summary 为空: {t.slug}"


def test_load_topics_file_not_found():
    import pytest
    with pytest.raises(FileNotFoundError):
        load_topics("/nonexistent/topics.yaml")


# ── 状态数据库 ──


def test_state_db_migrate_and_crud(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = TopicStateDb(db_path)
    try:
        # 初始无记录
        assert db.get_recent_slugs() == []

        # 记录发布
        db.record_publish("2026-06-18", "array-two-sum")
        db.record_publish("2026-06-19", "binary-search-boundary")

        recent = db.get_recent_slugs(30)
        assert "array-two-sum" in recent
        assert "binary-search-boundary" in recent

        # 重复 report_key 不报错（REPLACE）
        db.record_publish("2026-06-18", "array-two-sum")
        assert len(db.get_recent_slugs(30)) == 2

        # 运行记录
        db.start_run("2026-06-20", "test-slug")
        db.finish_run("2026-06-20", status="success")

        # 选题日志
        db.log_selection("2026-06-20", "test-slug", "测试理由")
    finally:
        db.close()


def test_state_db_recent_slugs_ordering(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = TopicStateDb(db_path)
    try:
        db.record_publish("2026-06-17", "topic-a")
        db.record_publish("2026-06-18", "topic-b")
        db.record_publish("2026-06-19", "topic-c")

        recent = db.get_recent_slugs(2)
        assert len(recent) == 2
        # 最新的在前
        assert recent[0] == "topic-c"
        assert recent[1] == "topic-b"
    finally:
        db.close()


# ── 选题策略 ──


def test_select_topic_avoids_recent(tmp_path):
    topics = [
        _make_topic(slug="a", tags=["数组"]),
        _make_topic(slug="b", tags=["树"]),
        _make_topic(slug="c", tags=["动态规划"]),
    ]
    db_path = str(tmp_path / "test.db")
    db = TopicStateDb(db_path)
    try:
        db.record_publish("2026-06-18", "a")
        selected, reason = select_topic(topics, db)
        assert selected.slug in ("b", "c")
        assert "a" not in selected.slug
    finally:
        db.close()


def test_select_topic_all_recent_uses_any(tmp_path):
    topics = [
        _make_topic(slug="a", tags=["数组"]),
        _make_topic(slug="b", tags=["树"]),
    ]
    db_path = str(tmp_path / "test.db")
    db = TopicStateDb(db_path)
    try:
        db.record_publish("2026-06-17", "a")
        db.record_publish("2026-06-18", "b")
        selected, reason = select_topic(topics, db, {"history_limit": 30})
        assert selected in topics
        assert "放宽限制" in reason
    finally:
        db.close()


def test_select_topic_empty_pool_raises(tmp_path):
    import pytest
    db_path = str(tmp_path / "test.db")
    db = TopicStateDb(db_path)
    try:
        with pytest.raises(IndexError):
            select_topic([], db)
    finally:
        db.close()


# ── 渲染 ──


def test_render_markdown_contains_sections():
    topic = _make_topic()
    lesson = _make_lesson()
    md = _render_markdown(topic, lesson, "2026-06-18", "测试标题")

    assert "---" in md  # frontmatter
    assert "# 测试标题" in md
    assert "## 直觉理解" in md
    assert "## 算法思路" in md
    assert "## Python 示例" in md
    assert "## 复杂度分析" in md
    assert "## 易错点" in md
    assert "## 延伸练习" in md
    assert "def solve()" in md


def test_render_markdown_cpp_optional():
    topic = _make_topic(cpp_optional=True)
    lesson = _make_lesson(cpp_code="int main() {}")
    md = _render_markdown(topic, lesson, "2026-06-18", "测试")

    assert "## C++ 对照" in md
    assert "int main()" in md


def test_render_markdown_no_cpp_when_empty():
    topic = _make_topic()
    lesson = _make_lesson(cpp_code="")
    md = _render_markdown(topic, lesson, "2026-06-18", "测试")

    assert "## C++ 对照" not in md


def test_render_html_contains_structure():
    topic = _make_topic()
    lesson = _make_lesson()
    html = _render_html(topic, lesson, "2026-06-18", "测试标题")

    assert "<!DOCTYPE html>" in html
    assert "测试标题" in html
    assert "直觉理解" in html
    assert "算法思路" in html
    assert "Python 示例" in html
    assert "复杂度分析" in html
    assert "易错点" in html
    assert "延伸练习" in html


def test_render_html_escapes_code():
    topic = _make_topic()
    lesson = _make_lesson(python_code='x = "<script>alert(1)</script>"')
    html = _render_html(topic, lesson, "2026-06-18", "测试")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
