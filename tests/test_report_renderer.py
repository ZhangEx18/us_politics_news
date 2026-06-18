"""日报渲染测试 — Reader 专用输出"""

from report_renderer import render_reader_content


def test_render_reader_content_is_plain_article_fragment():
    meta = {
        "title": "测试标题",
        "lead": "这是一段导语",
        "highlights": ["不应出现"],
        "date": "2026-06-18",
    }
    columns = {
        "us_politics": [{
            "title_zh": "美国事件",
            "core_facts": ["事实一", "事实二"],
            "background_impact": "背景信息",
            "why_it_matters": "原因说明",
            "source_links": [{"title": "原文", "url": "https://example.com"}],
        }],
        "global_affairs": [{
            "title_zh": "国际事件",
            "core_facts": "单段事实",
            "why_it_matters": "国际原因",
        }],
        "technology": [],
        "economy": [],
    }

    html = render_reader_content(meta, columns)

    assert html.startswith("<article>")
    assert html.endswith("</article>")
    assert "<h1>测试标题</h1>" in html
    assert "<p>这是一段导语</p>" in html
    assert "<h2>一、美国政情</h2>" in html
    assert "<h2>二、国际风云</h2>" in html
    assert "<h3>1. 美国事件</h3>" in html
    assert "<h3>1. 国际事件</h3>" in html
    assert "核心事实：" in html
    assert "背景与影响：" in html
    assert "为什么值得关注：" in html
    assert "<!DOCTYPE html>" not in html
    assert "<html" not in html
    assert "<head>" not in html
    assert "<style>" not in html
    assert "不应出现" not in html
    assert "相关阅读" not in html
    assert "原文链接" not in html
