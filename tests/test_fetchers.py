"""抓取器路由与自定义源测试"""

import asyncio
from datetime import datetime, timezone

from fetchers import CustomFeedFetcher, GoogleNewsFetcher, RSSFetcher, fetch_all_sources


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
