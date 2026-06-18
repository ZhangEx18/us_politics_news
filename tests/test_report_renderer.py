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
    assert "<h1>2026年6月18日 新闻</h1>" in html
    assert "<h2>今日要点</h2>" in html
    assert "<li>要点一</li>" in html
    assert "<li>要点二</li>" in html
    assert "<h2>一、美国政情</h2>" in html
    assert "<h2>二、国际风云</h2>" in html
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
