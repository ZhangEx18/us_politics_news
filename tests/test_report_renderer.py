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
            "detail_level": "full",
            "core_facts": ["事实一", "事实二"],
            "background_context": "背景信息",
            "possible_impact": "影响信息",
            "why_it_matters": "原因说明",
            "source_links": [{"title": "原文", "url": "https://example.com"}],
        }],
        "global_affairs": [{
            "title_zh": "国际事件",
            "detail_level": "brief",
            "core_facts": "单段事实",
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
    assert "核心事实：" in html
    assert "背景脉络：" in html
    assert "可能影响：" in html
    assert "为什么值得关注：" in html
    assert "单段事实" in html
    assert "<!DOCTYPE html>" not in html
    assert "<html" not in html
    assert "<head>" not in html
    assert "<style>" not in html
    assert "这是一段导语" not in html
    assert "相关阅读" not in html
    assert "原文链接" not in html
    assert "来源" not in html
