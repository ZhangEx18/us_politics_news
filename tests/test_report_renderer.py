"""日报渲染测试 — Reader 专用输出"""

from report_renderer import render_reader_content


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
    assert "<h1>2026年6月18日 新闻</h1>" not in html


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


def test_render_dual_style_daily_has_numbered_and_bullet():
    """日报双样式：编号条目带正文，无序条目仅标题"""
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
    # 编号条目（_pangu 会在中英文间加空格）
    assert "<h3>1. 重要事件 A</h3>" in html
    assert "<h3>2. 重要事件 B</h3>" in html
    assert "<p>这是正文。第二句。</p>" in html
    # 无序条目
    assert "<li>快讯 C</li>" in html
    assert "<li>快讯 D</li>" in html
    # 无序条目前没有分组标题
    assert "其他要闻" not in html
    assert "补充快讯" not in html


def test_render_dual_style_no_extra_heading_between():
    """无序条目前不插入任何分区标题"""
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [{"title_zh": "事件A", "reader_body": "正文。"}],
            "headline_only_events": [{"title_zh": "标题B"}],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }
    html = render_reader_content(meta, columns, report_type="daily")
    # 在 </h3> 和 <li> 之间不应有额外标题
    h3_pos = html.index("</h3>")
    li_pos = html.index("<li>")
    between = html[h3_pos:li_pos]
    assert "<h2>" not in between
    assert "<h3>" not in between


def test_render_headline_only_has_no_body():
    """无序条目只有标题，没有正文"""
    meta = {"title": "测试", "highlights": [], "date": "2026-06-19"}
    columns = {
        "us_politics": {
            "detailed_events": [],
            "headline_only_events": [{"title_zh": "仅标题"}],
        },
        "global_affairs": {"detailed_events": [], "headline_only_events": []},
        "technology": {"detailed_events": [], "headline_only_events": []},
        "economy": {"detailed_events": [], "headline_only_events": []},
    }
    html = render_reader_content(meta, columns, report_type="daily")
    assert "<li>仅标题</li>" in html
    assert "<p>" not in html


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
