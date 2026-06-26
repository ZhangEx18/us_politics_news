"""抓取器路由与自定义源测试"""

import asyncio
from datetime import datetime, timezone

from fetchers import CustomFeedFetcher, GoogleNewsFetcher, RSSFetcher, fetch_all_sources, _build_contextual_snippet


def test_rss_fetcher_accepts_rss_and_rsshub_modes():
    sources = [
        {"name": "Official RSS", "url": "https://example.com/feed.xml", "fetch_mode": "rss", "enabled": True},
        {"name": "RSSHub Feed", "url": "https://rsshub.app/36kr/newsflashes", "fetch_mode": "rsshub", "enabled": True},
        {"name": "Google News", "url": "https://news.google.com/rss/search?q=test", "fetch_mode": "google_news", "enabled": True},
    ]
    fetcher = RSSFetcher(sources)
    assert [item["name"] for item in fetcher.feeds] == ["Official RSS", "RSSHub Feed"]


def test_rss_fetcher_compares_naive_since_with_aware_feed_dates(monkeypatch):
    source = {
        "name": "Official RSS",
        "url": "https://example.com/feed.xml",
        "fetch_mode": "rss",
        "column": "us_politics",
        "enabled": True,
    }
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <title>White House announces policy update</title>
        <link>https://example.com/policy</link>
        <description>Policy summary</description>
        <pubDate>Sat, 20 Jun 2026 00:30:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    fetcher = RSSFetcher([source])

    async def _fake_get(url: str, **kwargs) -> str:
        return rss_xml

    fetcher._get = _fake_get  # type: ignore[method-assign]
    items = asyncio.run(fetcher.fetch(datetime(2026, 6, 19, 23, 0)))

    assert [item.title for item in items] == ["White House announces policy update"]


def test_google_news_fetcher_only_accepts_google_news_mode():
    sources = [
        {"name": "Google", "url": "https://news.google.com/rss/search?q=test", "fetch_mode": "google_news", "enabled": True},
        {"name": "RSS", "url": "https://example.com/feed.xml", "fetch_mode": "rss", "enabled": True},
    ]
    fetcher = GoogleNewsFetcher(sources)
    assert [item["name"] for item in fetcher.feeds] == ["Google"]


def test_custom_fetcher_can_build_content_item_from_cn_media_page():
    source = {
        "name": "联合早报 - 国际",
        "url": "https://www.zaobao.com.sg/realtime/world",
        "fetch_mode": "custom",
        "fetcher_key": "china_media_article_list",
        "column": "global_affairs",
        "source_tier": 2,
        "language": "zh",
        "tags": ["cn_source", "geopolitics"],
        "custom": {
            "item_patterns": [r'<a[^>]+href="(?P<href>/realtime/china/story123)"[^>]*>(?P<title>[^<]+)</a>'],
            "summary_chars": 80,
        },
        "enabled": True,
    }
    html = '<html><body><a href="/realtime/china/story123">中国与美国恢复高级别贸易会谈</a></body></html>'
    fetcher = CustomFeedFetcher([source])

    async def _fake_get(url: str, **kwargs) -> str:
        return html

    fetcher._get = _fake_get  # type: ignore[method-assign]
    items = asyncio.run(fetcher.fetch(datetime(2026, 6, 19, tzinfo=timezone.utc)))

    assert len(items) == 1
    item = items[0]
    assert item.title == "中国与美国恢复高级别贸易会谈"
    assert item.column == "global_affairs"
    assert item.metadata["language"] == "zh"
    assert "cn_source" in item.metadata["tags"]


def test_build_contextual_snippet_prefers_title_neighborhood():
    page_text = "前言无关内容。美国国会提出《大街竞争法案》，旨在支持中小企业发展，并调整市场准入规则。后文继续解释实施范围。"

    snippet = _build_contextual_snippet(page_text, "《大街竞争法案》", 60)

    assert "大街竞争法案" in snippet
    assert "旨在支持中小企业发展" in snippet


def test_custom_fetcher_legislative_records_prefers_detail_page_context():
    source = {
        "name": "GovTrack",
        "url": "https://www.govtrack.us/congress/bills",
        "fetch_mode": "custom",
        "fetcher_key": "legislative_or_public_records",
        "column": "us_politics",
        "source_tier": 1,
        "language": "en",
        "custom": {
            "item_patterns": [r'<a[^>]+href="(?P<href>/congress/bills/119/hr999)"[^>]*>(?P<title>[^<]+)</a>'],
            "summary_chars": 90,
        },
        "enabled": True,
    }
    list_html = '<html><body><a href="/congress/bills/119/hr999">Main Street Competition Act</a> 列表页只有名称</body></html>'
    detail_html = "<html><body>Main Street Competition Act This bill would support small businesses and revise market access requirements.</body></html>"
    fetcher = CustomFeedFetcher([source])

    async def _fake_get(url: str, **kwargs) -> str:
        if url.endswith("/congress/bills"):
            return list_html
        return detail_html

    fetcher._get = _fake_get  # type: ignore[method-assign]
    items = asyncio.run(fetcher.fetch(datetime(2026, 6, 19, tzinfo=timezone.utc)))

    assert len(items) == 1
    assert "support small businesses" in (items[0].content or "")


def test_custom_fetcher_can_build_official_press_release_item():
    source = {
        "name": "USTR Press Releases",
        "url": "https://ustr.gov/about-us/policy-offices/press-office/press-releases",
        "fetch_mode": "custom",
        "fetcher_key": "intl_org_feed",
        "column": "economy",
        "source_tier": 1,
        "language": "en",
        "tags": ["official", "trade", "policy"],
        "custom": {
            "item_patterns": [
                r'<a[^>]+href="(?P<href>/about-us/policy-offices/press-office/press-releases/2025/march/statement-section-301)"[^>]*>(?P<title>[^<]+)</a>'
            ],
            "summary_chars": 100,
        },
        "enabled": True,
    }
    html = """
    <html><body>
      <a href="/about-us/policy-offices/press-office/press-releases/2025/march/statement-section-301">
        Ambassador Issues Statement on Section 301 Tariff Action
      </a>
    </body></html>
    """
    fetcher = CustomFeedFetcher([source])

    async def _fake_get(url: str, **kwargs) -> str:
        return html

    fetcher._get = _fake_get  # type: ignore[method-assign]
    items = asyncio.run(fetcher.fetch(datetime(2026, 6, 19, tzinfo=timezone.utc)))

    assert len(items) == 1
    item = items[0]
    assert item.title.strip() == "Ambassador Issues Statement on Section 301 Tariff Action"
    assert item.column == "economy"
    assert item.metadata["fetch_mode"] == "custom"
    assert "trade" in item.metadata["tags"]


def test_intl_org_feed_prefers_detail_page_context():
    source = {
        "name": "FTC Press Releases",
        "url": "https://www.ftc.gov/news-events/news/press-releases",
        "fetch_mode": "custom",
        "fetcher_key": "intl_org_feed",
        "column": "technology",
        "source_tier": 1,
        "language": "en",
        "tags": ["official", "regulation", "antitrust"],
        "custom": {
            "item_patterns": [
                r'<a[^>]+href="(?P<href>/news-events/news/press-releases/2026/06/sample-antitrust-release)"[^>]*>(?P<title>[^<]+)</a>'
            ],
            "summary_chars": 100,
        },
        "enabled": True,
    }
    list_html = """
    <html><body>
      <a href="/news-events/news/press-releases/2026/06/sample-antitrust-release">
        FTC Announces Antitrust Action
      </a>
    </body></html>
    """
    detail_html = """
    <html><body>
      FTC Announces Antitrust Action The commission filed a complaint to block anti-competitive
      conduct in the AI infrastructure market.
    </body></html>
    """
    fetcher = CustomFeedFetcher([source])

    async def _fake_get(url: str, **kwargs) -> str:
        if url == source["url"]:
            return list_html
        return detail_html

    fetcher._get = _fake_get  # type: ignore[method-assign]
    items = asyncio.run(fetcher.fetch(datetime(2026, 6, 19, tzinfo=timezone.utc)))

    assert len(items) == 1
    assert "The commission filed a complaint" in (items[0].content or "")


def test_fetch_all_sources_routes_custom_and_rsshub(monkeypatch):
    since = datetime(2026, 6, 19, tzinfo=timezone.utc)
    sources = [
        {
            "name": "36氪 - 科技",
            "url": "https://rsshub.app/36kr/newsflashes",
            "fetch_mode": "rsshub",
            "column": "technology",
            "source_tier": 2,
            "language": "zh",
            "tags": ["cn_source", "china_tech"],
            "enabled": True,
        },
        {
            "name": "联合早报 - 国际",
            "url": "https://www.zaobao.com.sg/realtime/world",
            "fetch_mode": "custom",
            "fetcher_key": "china_media_article_list",
            "column": "global_affairs",
            "source_tier": 2,
            "language": "zh",
            "tags": ["cn_source", "geopolitics"],
            "custom": {
                "item_patterns": [r'<a[^>]+href="(?P<href>/realtime/china/story123)"[^>]*>(?P<title>[^<]+)</a>'],
            },
            "enabled": True,
        },
    ]
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item><title>36氪快讯：国产大模型公司完成新一轮融资</title><link>https://example.com/36kr-1</link><description>摘要</description></item></channel></rss>"""
    custom_html = '<html><body><a href="/realtime/china/story123">中国与美国恢复高级别贸易会谈</a></body></html>'

    async def _fake_get(self, url: str, **kwargs) -> str:
        if "rsshub.app" in url:
            return rss_xml
        return custom_html

    monkeypatch.setattr("fetchers.BaseFetcher._get", _fake_get)
    items = asyncio.run(fetch_all_sources(since, sources))

    assert len(items) >= 2
    by_source = {item.source_name: item for item in items}
    assert by_source["36氪 - 科技"].metadata["fetch_mode"] == "rsshub"
    assert by_source["联合早报 - 国际"].metadata["language"] == "zh"
