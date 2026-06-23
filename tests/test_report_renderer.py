"""日报渲染测试 — Reader 专用输出"""

from report_renderer import render_reader_content, render_structured_html, render_structured_markdown


def test_render_reader_content_is_plain_article_fragment():
    meta = {
        "title": "测试标题",
        "lead": "这是一段导语",
        "highlights": ["要点一", "要点二"],
        "date": "2026-06-18",
    }
    columns = {
        "us_politics": [{
            "title_zh": "美国事件",
            "reader_body": "美国事件概述正文",
            "source_links": [{"title": "原文", "url": "https://example.com"}],
        }],
        "global_affairs": [{
            "title_zh": "国际事件",
            "reader_body": "国际事件单段概述",
        }],
        "technology": [],
        "economy": [],
    }

    html = render_reader_content(meta, columns)

    assert html.startswith("<article>")
    assert html.endswith("</article>")
    assert "<h2>今日要点</h2>" in html
    assert "<li>要点一</li>" in html
    assert "<li>要点二</li>" in html
    assert "<h2>一、美国政局</h2>" in html
    assert "<h2>二、国际局势</h2>" in html
    assert "<h3>1. 美国事件</h3>" in html
    assert "<h3>1. 国际事件</h3>" in html
    assert "<p>美国事件概述正文</p>" in html
    assert "<p>国际事件单段概述</p>" in html
    assert "核心事实：" not in html
    assert "背景脉络：" not in html
    assert "可能影响：" not in html
    assert "为什么值得关注：" not in html
    assert "<!DOCTYPE html>" not in html
    assert "<html" not in html
    assert "<head>" not in html
    assert "<style>" not in html
    assert "这是一段导语" not in html
    assert "相关阅读" not in html
    assert "原文链接" not in html
    assert "来源" not in html
    assert "<h1>2026年6月18日 日报</h1>" not in html
    assert "<a href=" not in html


def test_render_reader_content_weekly_highlights():
    """weekly 类型显示"本周要点"标题"""
    meta = {"title": "测试周报", "highlights": ["要点一"], "date": "2026-06-19"}
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}
    html = render_reader_content(meta, columns, report_type="weekly")
    assert "本周要点" in html


def test_render_reader_content_monthly_highlights():
    """monthly 类型显示"本月要点"标题"""
    meta = {"title": "测试月报", "highlights": ["要点一"], "date": "2026-06-19"}
    columns = {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}
    html = render_reader_content(meta, columns, report_type="monthly")
    assert "本月要点" in html


def test_render_reader_has_numbered_events_and_bullet_titles():
    """Reader 栏目内先显示编号正文，再显示次要 bullet 标题。"""
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [
                {"title_zh": "重要事件A", "reader_body": "这是正文。第二句。"},
                {"title_zh": "重要事件B", "reader_body": "另一条正文。"},
            ],
            "headline_only_events": [
                {"title_zh": "快讯C"},
                {"title_zh": "快讯D"},
            ],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }
    html = render_reader_content(meta, columns, report_type="daily")
    assert "<h3>1. 重要事件 A</h3>" in html
    assert "<h3>2. 重要事件 B</h3>" in html
    assert "<p>这是正文。第二句。</p>" in html
    assert "<li>快讯 C</li>" in html
    assert "<li>快讯 D</li>" in html
    assert "其他要闻" not in html
    assert "补充快讯" not in html


def test_render_reader_headline_only_requires_chinese_title():
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [
                {"title_zh": "重要事件A", "reader_body": "这是正文。第二句。"},
            ],
            "headline_only_events": [
                {"title": "快讯C"},
                {"title": "快讯D"},
            ],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }

    html = render_reader_content(meta, columns, report_type="daily")
    structured_html = render_structured_html(meta, columns, report_type="daily")
    markdown = render_structured_markdown(meta, columns, report_type="daily")

    for output in (html, structured_html, markdown):
        assert "快讯 C" not in output
        assert "快讯 D" not in output


def test_render_reader_skips_headline_only_column():
    """只有 headline_only_events 的栏目在 Reader 中不显示。"""
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [],
            "headline_only_events": [{"title_zh": "标题B"}],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }
    html = render_reader_content(meta, columns, report_type="daily")
    assert "标题 B" not in html
    assert "<h2>一、美国政局</h2>" not in html


def test_render_weekly_no_headline_only():
    """周报 pipeline 不传 headline_only_events（renderer 不过滤，由 pipeline 控制）"""
    # 模拟 pipeline 行为：周报时 headline_only_events 为空
    meta = {"title": "测试周报", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [{"title_zh": "事件", "reader_body": "正文。"}],
            "headline_only_events": [],  # pipeline 在周报时传空
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }
    html = render_reader_content(meta, columns, report_type="weekly")
    assert "<h3>1. 事件</h3>" in html
    assert "<ul>" not in html  # 无序条目为空时不渲染 <ul>


def test_render_old_list_format_still_works():
    """兼容旧格式（纯列表），不崩溃"""
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": [{"title_zh": "旧格式事件", "reader_body": "正文。"}],
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }
    html = render_reader_content(meta, columns, report_type="daily")
    assert "<h3>1. 旧格式事件</h3>" in html
    assert "<p>正文。</p>" in html


def test_renderers_escape_external_text_and_drop_unsafe_links():
    meta = {
        "title": "标题<script>alert(1)</script>",
        "lead": "导语<img src=x onerror=alert(2)>",
        "highlights": ["要点<script>x</script>"],
        "date": "2026-06-19",
    }
    columns = {
        "us_politics": {
            "detailed_events": [{
                "title_zh": "事件<script>alert(3)</script>",
                "reader_body": "正文<img src=x onerror=alert(4)>",
                "source_links": [{"title": "原文", "url": "javascript:alert(5)"}],
            }],
            "headline_only_events": [],
        },
        "global_affairs": [],
        "technology": [],
        "economy": [],
    }

    html = render_structured_html(meta, columns)
    reader = render_reader_content(meta, columns)
    markdown = render_structured_markdown(meta, columns)

    for output in (html, reader, markdown):
        assert "<script>" not in output
        assert "javascript:alert" not in output
        assert "&lt;script&gt;" in output
        assert "<img src=x" not in output
        assert "&lt;img src=x onerror=alert" in output
    assert "<a href=" not in html
    assert "](" not in markdown


def test_render_reader_content_weekly_overview_precedes_columns():
    meta = {
        "title": "测试周报",
        "highlights": ["要点一", "要点二"],
        "date": "2026-W25",
        "overview": {
            "summary": "本周综述段落。",
            "themes": ["主题甲", "主题乙"],
            "watchlist": ["观察点一", "观察点二"],
        },
    }
    columns = {
        "us_politics": {
            "analysis": "美国政局本周主线。",
            "detailed_events": [{"title_zh": "重点事件", "reader_body": "正文。"}],
            "headline_only_events": [],
        },
        "global_affairs": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "technology": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "economy": {"analysis": "", "detailed_events": [], "headline_only_events": []},
    }

    html = render_reader_content(meta, columns, report_type="weekly")

    assert "<h2>本周综述</h2>" in html
    assert "<p>本周综述段落。</p>" in html
    assert "<h2>本周核心主题</h2>" in html
    assert "<li>主题甲</li>" in html
    assert "<h2>下周观察点</h2>" in html
    assert "<li>观察点一</li>" in html
    assert html.index("<h2>本周综述</h2>") < html.index("<h2>一、美国政局</h2>")
    assert html.index("<p>美国政局本周主线。</p>") < html.index("<h3>1. 重点事件</h3>")


def test_render_structured_markdown_monthly_overview_precedes_columns():
    meta = {
        "title": "测试月报",
        "highlights": ["要点一"],
        "date": "2026-06",
        "overview": {
            "summary": "本月综述段落。",
            "themes": ["主题甲"],
            "watchlist": ["观察点一"],
        },
    }
    columns = {
        "us_politics": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "global_affairs": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "technology": {"analysis": "", "detailed_events": [], "headline_only_events": []},
        "economy": {
            "analysis": "经济走势本月主线。",
            "detailed_events": [{"title_zh": "月度事件", "reader_body": "月度正文。"}],
            "headline_only_events": [],
        },
    }

    markdown = render_structured_markdown(meta, columns, report_type="monthly")

    assert "## 本月综述" in markdown
    assert "本月综述段落。" in markdown
    assert "## 本月核心主题" in markdown
    assert "- 主题甲" in markdown
    assert "## 下月观察点" in markdown
    assert "- 观察点一" in markdown
    assert markdown.index("## 本月综述") < markdown.index("##  四、经济走势")
    assert markdown.index("经济走势本月主线。") < markdown.index("### 1. 月度事件")


def test_render_structured_markdown_escapes_overview_markdown_control_text():
    meta = {
        "title": "测试月报",
        "highlights": [],
        "date": "2026-06",
        "overview": {
            "summary": "## 伪标题\n[危险链接](javascript:alert(1))",
            "themes": ["![图片](https://example.com/x.png)"],
            "watchlist": ["> 引用"],
        },
    }

    markdown = render_structured_markdown(meta, {"us_politics": [], "global_affairs": [], "technology": [], "economy": []}, report_type="monthly")

    assert "[危险链接](javascript:alert(1))" not in markdown
    assert "![图片](https://example.com/x.png)" not in markdown
    assert "\n## 伪标题\n" not in markdown
    assert "&gt; 引用" in markdown
