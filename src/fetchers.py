#!/usr/bin/env python3
"""
多源新闻抓取模块 — 从 sources.yaml 读取 100+ 源

所有抓取器统一返回 ContentItem，自动分配 column 和 source_tier。
"""

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import aiohttp
import feedparser

from database import Article, NewsDatabase
from models import ContentItem, SourceType
from urls import normalize_url


# ── 抓取器基类 ──

class BaseFetcher:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def fetch(self, since: datetime) -> List[ContentItem]:
        raise NotImplementedError

    def _generate_id(self, source_type: str, subtype: str, native_id: str) -> str:
        return f"{source_type}:{subtype}:{native_id}"

    def _hash_id(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    async def _get(self, url: str, **kwargs) -> str:
        async with self.session.get(url, **kwargs) as resp:
            return await resp.text()

    async def _get_json(self, url: str, **kwargs) -> dict:
        async with self.session.get(url, **kwargs) as resp:
            return await resp.json()


def _format_fetch_error(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _resolve_fetch_mode(source: dict) -> str:
    """向后兼容旧配置，同时允许新配置显式指定抓取模式。"""
    explicit = str(source.get("fetch_mode", "")).strip().lower()
    if explicit:
        return explicit

    url = str(source.get("url", "")).lower()
    name = str(source.get("name", "")).lower()
    if url.startswith("https://news.google.com/rss"):
        return "google_news"
    if "rsshub.app" in url:
        return "rsshub"
    if "gdelt" in name:
        return "gdelt"
    if "hacker news" in name or "hnrss" in url:
        return "hacker_news"
    return "rss"


_VIDEO_LIVE_TITLE_RE = re.compile(
    r"^(?:watch|live|video|full video|live updates?|photo|photos)\s*[：:]",
    re.IGNORECASE,
)
_VIDEO_LIVE_TITLE_CN_RE = re.compile(r"^(?:视频|直播|回放|图集)\s*[：:]")

def _is_video_or_live_entry(title: str) -> bool:
    """视频/直播/图集类条目不适合作为日报事件。"""
    text = str(title or "").strip()
    return bool(_VIDEO_LIVE_TITLE_RE.match(text) or _VIDEO_LIVE_TITLE_CN_RE.match(text))


def _build_item_metadata(source_cfg: dict, entry_tags: list[str] | None = None) -> dict:
    metadata = {
        "language": source_cfg.get("language", "en"),
        "tags": list(source_cfg.get("tags", [])),
        "fetch_mode": _resolve_fetch_mode(source_cfg),
    }
    cap = source_cfg.get("max_candidates_per_run")
    if cap:
        try:
            metadata["max_candidates_per_run"] = int(cap)
        except (TypeError, ValueError):
            pass
    if entry_tags:
        merged_tags = []
        seen = set()
        for tag in [*metadata["tags"], *entry_tags]:
            if tag and tag not in seen:
                seen.add(tag)
                merged_tags.append(tag)
        metadata["tags"] = merged_tags
    return metadata


_CN_TZ = ZoneInfo("Asia/Shanghai")
RSSHUB_DEFAULT_BASE = "https://rsshub.app"


def _substitute_env_vars(url: str) -> str:
    """替换 URL 中的 ${VAR}；RSSHUB_BASE_URL 未配置时回退公共实例。"""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "RSSHUB_BASE_URL":
            return (os.environ.get(name) or RSSHUB_DEFAULT_BASE).rstrip("/")
        return os.environ.get(name, match.group(0)).strip()

    return re.sub(r"\$\{(\w+)\}", _replace, str(url or ""))


_URL_DATE_PATTERNS = (
    r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)",
    r"/(20\d{2})(\d{2})(\d{2})(?:[/-]|$)",
)

_PUBLISHED_AT_PATTERNS = (
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|published_time|publishdate|pubdate|weibo:article:create_at)["\'][^>]*content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:article:published_time|publishdate|pubdate)["\']',
    r"<time[^>]+datetime=[\"']([^\"']+)[\"']",
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'"pubDate"\s*:\s*"([^"]+)"',
    r'"published_at"\s*:\s*"([^"]+)"',
)


def _parse_datetime_text(raw: str) -> Optional[datetime]:
    """解析常见日期格式（ISO / RFC2822 / 中文日期 / 时间戳）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10}|\d{13}", text):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(seconds, tz=_CN_TZ)
    normalized = text.replace("年", "-").replace("月", "-").replace("日", " ")
    normalized = normalized.replace("/", "-").strip()
    try:
        dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        match = re.search(
            r"(20\d{2})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2}))?", normalized
        )
        if not match:
            return None
        try:
            dt = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4) or 0), int(match.group(5) or 0),
            )
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CN_TZ)
    return dt


def _extract_date_from_url(url: str) -> Optional[datetime]:
    """从 URL 路径提取发布日期（/2026/09/16/、/20260916/ 等）。"""
    for pattern in _URL_DATE_PATTERNS:
        match = re.search(pattern, str(url or ""))
        if not match:
            continue
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=_CN_TZ
            )
        except ValueError:
            continue
    return None


def _extract_published_at_from_html(raw_html: str) -> Optional[datetime]:
    """从详情页 meta / time / JSON-LD 提取发布时间。"""
    for pattern in _PUBLISHED_AT_PATTERNS:
        match = re.search(pattern, str(raw_html or ""), flags=re.IGNORECASE)
        if not match:
            continue
        parsed = _parse_datetime_text(match.group(1))
        if parsed:
            return parsed
    return None


def _extract_html_text(raw_html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_article_with_trafilatura(raw_html: str) -> tuple[str, Optional[datetime]]:
    """通用正文抽取（trafilatura）；未安装或失败时返回空。"""
    try:
        import trafilatura
    except ImportError:
        return "", None
    try:
        text = trafilatura.extract(raw_html, include_comments=False, include_tables=False) or ""
    except Exception:
        text = ""
    published = None
    try:
        metadata = trafilatura.extract_metadata(raw_html)
        if metadata is not None and getattr(metadata, "date", None):
            published = _parse_datetime_text(str(metadata.date))
    except Exception:
        published = None
    return str(text).strip(), published


def _extract_article_with_gne(raw_html: str) -> tuple[str, Optional[datetime]]:
    """中文正文抽取（GeneralNewsExtractor）；未安装或失败时返回空。"""
    try:
        from gne import GeneralNewsExtractor
    except ImportError:
        return "", None
    try:
        result = GeneralNewsExtractor().extract(raw_html, with_body_html=False)
    except Exception:
        return "", None
    if not isinstance(result, dict):
        return "", None
    text = str(result.get("content") or "").strip()
    published = _parse_datetime_text(str(result.get("publish_time") or ""))
    return text, published


def _extract_article_content(raw_html: str, *, prefer_chinese: bool) -> tuple[str, Optional[datetime]]:
    """按语言偏好选择抽取器，双路互相兜底。"""
    extractors = (
        (_extract_article_with_gne, _extract_article_with_trafilatura)
        if prefer_chinese
        else (_extract_article_with_trafilatura, _extract_article_with_gne)
    )
    for extractor in extractors:
        text, published = extractor(raw_html)
        if text:
            return text, published
    return "", None


def _build_contextual_snippet(page_text: str, title: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(page_text or "")).strip()
    if not text:
        return ""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    if title_text:
        pos = text.find(title_text)
        if pos >= 0:
            start = max(0, pos - 80)
            end = min(len(text), pos + len(title_text) + max(limit, 120))
            snippet = text[start:end].strip()
            if snippet:
                return snippet[:limit]
    return text[:limit]


# ── RSS 抓取器（从 sources.yaml 读取）──

class RSSFetcher(BaseFetcher):
    """RSS/Atom 抓取器 — 读取 sources 中所有 rss/rsshub 源"""

    def __init__(self, sources: list[dict]):
        super().__init__()
        self.feeds = [
            s for s in sources
            if s.get("enabled", True)
            and _resolve_fetch_mode(s) in {"rss", "rsshub"}
        ]

    def _parse_date(self, entry: dict) -> Optional[datetime]:
        for field in ["published", "updated", "created"]:
            if f"{field}_parsed" in entry and entry[f"{field}_parsed"]:
                try:
                    return datetime(*entry[f"{field}_parsed"][:6], tzinfo=timezone.utc)
                except Exception:
                    continue
        return None

    def _extract_content(self, entry: dict) -> str:
        if "summary" in entry:
            return entry.get("summary", "")
        if "description" in entry:
            return entry.get("description", "")
        if "content" in entry and entry["content"]:
            return entry["content"][0].get("value", "")
        return ""

    async def fetch(self, since: datetime) -> List[ContentItem]:
        items = []
        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since_utc = since_utc.astimezone(timezone.utc)
        for feed_cfg in self.feeds:
            try:
                feed_url = _substitute_env_vars(feed_cfg["url"])
                timeout_seconds = int(feed_cfg.get("timeout_seconds") or 60)
                text = await self._get(feed_url, timeout=aiohttp.ClientTimeout(total=timeout_seconds))
                data = feedparser.parse(text)

                for entry in data.entries:
                    published = self._parse_date(entry)
                    if published and published < since_utc:
                        continue

                    title = unescape(entry.get("title", "")).strip()
                    if _is_video_or_live_entry(title):
                        continue
                    link = entry.get("link", "").strip()
                    content = unescape(self._extract_content(entry))
                    content = re.sub(r"<[^>]+>", "", content)

                    entry_id = entry.get("id", entry.get("link", ""))
                    entry_hash = self._hash_id(str(entry_id))

                    items.append(ContentItem(
                        id=self._generate_id(_resolve_fetch_mode(feed_cfg), feed_cfg["name"].replace(" ", "_"), entry_hash),
                        source_type=SourceType.RSS,
                        title=title,
                        url=link,
                        content=content[:500],
                        source_name=feed_cfg["name"],
                        published_at=published,
                        column=feed_cfg.get("column", ""),
                        source_tier=feed_cfg.get("source_tier", 4),
                        source_url_normalized=normalize_url(link),
                        metadata=_build_item_metadata(
                            feed_cfg,
                            [tag.term for tag in entry.get("tags", [])],
                        ),
                    ))
            except Exception as e:
                print(f"  [RSS] {feed_cfg['name']} 失败: {_format_fetch_error(e)}")
        return items


# ── GDELT 抓取器 ──

class GDELTFetcher(BaseFetcher):
    def __init__(self):
        super().__init__()
        self.queries = [
            {"name": "us_politics", "query": "Trump AND (policy OR executive OR white house)", "column": "us_politics"},
            {"name": "us_congress", "query": "Congress AND (bill OR legislation OR senate)", "column": "us_politics"},
            {"name": "global_china", "query": "China AND (US OR America OR Trump)", "column": "global_affairs"},
            {"name": "global_iran", "query": "Iran AND (US OR America)", "column": "global_affairs"},
            {"name": "global_russia", "query": "Russia AND (Ukraine OR war)", "column": "global_affairs"},
            {"name": "tech_ai", "query": "artificial intelligence OR AI model", "column": "technology"},
            {"name": "tech_semiconductor", "query": "semiconductor OR chip OR NVIDIA", "column": "technology"},
            {"name": "economy_fed", "query": "Federal Reserve OR inflation OR interest rate", "column": "economy"},
            {"name": "economy_trade", "query": "tariff OR trade war OR supply chain", "column": "economy"},
        ]

    @staticmethod
    def _parse_article_datetime(article: dict) -> Optional[datetime]:
        for field in ("seendate", "socialimage_timestamp"):
            raw = article.get(field)
            if not raw or not isinstance(raw, str):
                continue
            value = raw.strip()
            try:
                if len(value) == 14 and value.isdigit():
                    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    async def fetch(self, since: datetime) -> List[ContentItem]:
        items = []
        for q in self.queries:
            try:
                params = {
                    "query": q["query"],
                    "mode": "ArtList",
                    "format": "json",
                    "startdatetime": since.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": datetime.now().strftime("%Y%m%d%H%M%S"),
                    "maxrecords": 50,
                    "sort": "datedesc",
                }
                data = await self._get_json(
                    "https://api.gdeltproject.org/api/v2/doc/doc",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                )
                for a in data.get("articles", []):
                    url_hash = self._hash_id(a.get("url", ""))
                    published_at = self._parse_article_datetime(a)
                    items.append(ContentItem(
                        id=self._generate_id("gdelt", q["name"], url_hash),
                        source_type=SourceType.GDELT,
                        title=a.get("title", ""),
                        url=a.get("url", ""),
                        content="",
                        source_name=a.get("domain", "GDELT"),
                        published_at=published_at,
                        column=q["column"],
                        source_tier=4,
                        source_url_normalized=normalize_url(a.get("url", "")),
                    ))
            except Exception as e:
                print(f"  [GDELT] '{q['name']}' 失败: {_format_fetch_error(e)}")
        return items


# ── Hacker News 抓取器 ──

class HackerNewsFetcher(BaseFetcher):
    def __init__(self, sources: list[dict]):
        super().__init__()
        self.hn_sources = [s for s in sources if _resolve_fetch_mode(s) == "hacker_news" and s.get("enabled", True)]

    async def fetch(self, since: datetime) -> List[ContentItem]:
        hn_sources = self.hn_sources
        if not hn_sources:
            return []

        items = []
        try:
            story_ids = await self._get_json(
                "https://hacker-news.firebaseio.com/v0/topstories.json",
                timeout=aiohttp.ClientTimeout(total=30),
            )
            for sid in story_ids[:50]:
                try:
                    story = await self._get_json(
                        f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if not story or story.get("type") != "story":
                        continue
                    pub = datetime.fromtimestamp(story.get("time", 0), tz=timezone.utc)
                    if pub < since:
                        continue
                    items.append(ContentItem(
                        id=self._generate_id("hn", "story", str(sid)),
                        source_type=SourceType.HACKERNEWS,
                        title=story.get("title", ""),
                        url=story.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                        content="",
                        source_name="Hacker News",
                        published_at=pub,
                        column="technology",
                        source_tier=3,
                        metadata={"score": story.get("score", 0), "fetch_mode": "hacker_news"},
                    ))
                except Exception:
                    continue
        except Exception as e:
            print(f"  [HN] 失败: {_format_fetch_error(e)}")
        return items


# ── Google News 抓取器（从 sources.yaml 读取）──

class GoogleNewsFetcher(BaseFetcher):
    def __init__(self, sources: list[dict]):
        super().__init__()
        self.feeds = [
            s for s in sources
            if s.get("enabled", True) and _resolve_fetch_mode(s) == "google_news"
        ]

    async def fetch(self, since: datetime) -> List[ContentItem]:
        items = []
        for feed_cfg in self.feeds:
            try:
                text = await self._get(feed_cfg["url"], timeout=aiohttp.ClientTimeout(total=60))
                data = feedparser.parse(text)
                for entry in data.entries:
                    title = unescape(entry.get("title", "")).strip()
                    if _is_video_or_live_entry(title):
                        continue
                    link = entry.get("link", "").strip()
                    content = unescape(re.sub(r"<[^>]+>", "", entry.get("summary", "")))
                    entry_hash = self._hash_id(entry.get("id", link))
                    # 解析 Google News 的发布时间
                    published = None
                    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                    if parsed:
                        try:
                            published = datetime(*parsed[:6], tzinfo=timezone.utc)
                        except Exception:
                            pass
                    items.append(ContentItem(
                        id=self._generate_id("gnews", feed_cfg["name"].replace(" ", "_"), entry_hash),
                        source_type=SourceType.GOOGLE_NEWS,
                        title=title,
                        url=link,
                        content=content[:500],
                        source_name=feed_cfg["name"],
                        published_at=published,
                        column=feed_cfg.get("column", ""),
                        source_tier=4,
                        source_url_normalized=normalize_url(link),
                        metadata=_build_item_metadata(feed_cfg),
                    ))
            except Exception as e:
                print(f"  [Google News] '{feed_cfg['name']}' 失败: {_format_fetch_error(e)}")
        return items


class CustomFeedFetcher(BaseFetcher):
    """自定义抓取器分派层。当前优先覆盖中文站点列表页解析。"""

    def __init__(self, sources: list[dict]):
        super().__init__()
        self.sources = [
            s for s in sources
            if s.get("enabled", True) and _resolve_fetch_mode(s) == "custom"
        ]
        self.handlers = {
            "china_media_article_list": self._fetch_china_media_article_list,
            "china_tech_site": self._fetch_china_media_article_list,
            "legislative_or_public_records": self._fetch_feed_like_page,
            "intl_org_feed": self._fetch_feed_like_page,
        }

    async def fetch(self, since: datetime) -> List[ContentItem]:
        items: list[ContentItem] = []
        for source_cfg in self.sources:
            fetcher_key = str(source_cfg.get("fetcher_key", "")).strip()
            handler = self.handlers.get(fetcher_key)
            if not handler:
                print(f"  [Custom] {source_cfg['name']} 未知 fetcher_key: {fetcher_key}")
                continue
            try:
                items.extend(await handler(source_cfg, since))
            except Exception as exc:
                print(f"  [Custom] {source_cfg['name']} 失败: {_format_fetch_error(exc)}")
        return items

    async def _fetch_feed_like_page(self, source_cfg: dict, since: datetime) -> List[ContentItem]:
        """对公告/记录类页面做保守解析，优先抓取带链接标题的块。"""
        return await self._fetch_china_media_article_list(source_cfg, since)

    async def _fetch_china_media_article_list(self, source_cfg: dict, since: datetime) -> List[ContentItem]:
        html = await self._get(source_cfg["url"], timeout=aiohttp.ClientTimeout(total=30))
        selectors = source_cfg.get("custom", {}).get("item_patterns") or [
            r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>[^<]{8,120})</a>'
        ]
        summary_limit = int(source_cfg.get("custom", {}).get("summary_chars", 240))
        max_items = int(source_cfg.get("custom", {}).get("max_items", 12))
        fetcher_key = str(source_cfg.get("fetcher_key", "")).strip()
        fetch_detail = bool(source_cfg.get("custom", {}).get("fetch_detail", True))
        article_pattern = source_cfg.get("custom", {}).get("article_url_pattern")
        article_re = re.compile(article_pattern) if article_pattern else None
        since_utc = (since if since.tzinfo else since.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        since_cn_date = since_utc.astimezone(_CN_TZ).date()

        extracted: list[ContentItem] = []
        seen_links: set[str] = set()
        page_text = _extract_html_text(html)
        for pattern in selectors:
            for match in re.finditer(pattern, html, flags=re.IGNORECASE | re.DOTALL):
                href = (match.groupdict().get("href") or "").strip()
                title = _extract_html_text(match.groupdict().get("title") or "")
                if not href or not title:
                    continue
                link = urljoin(source_cfg["url"], href)
                if article_re and not article_re.search(link):
                    continue
                normalized_link = normalize_url(link)
                if normalized_link in seen_links:
                    continue
                seen_links.add(normalized_link)
                native_id = self._hash_id(link)

                published_at = _extract_date_from_url(link)
                snippet = _build_contextual_snippet(page_text, title, summary_limit)
                if fetch_detail:
                    detail_html = None
                    try:
                        detail_html = await self._get(link, timeout=aiohttp.ClientTimeout(total=20))
                    except Exception:
                        detail_html = None
                    if detail_html:
                        if published_at is None:
                            published_at = _extract_published_at_from_html(detail_html)
                        prefer_chinese = str(source_cfg.get("language", "")).startswith("zh")
                        article_text, article_date = _extract_article_content(
                            detail_html, prefer_chinese=prefer_chinese
                        )
                        if published_at is None and article_date is not None:
                            # 抽取器偶尔会从 URL 年份捏造日期（如 /2025/ → 2025-01-01），
                            # 只接受落在抓取窗口内的日期，其余留空交给后续今日性过滤
                            if article_date.date() >= since_cn_date:
                                published_at = article_date
                        detail_text = article_text or _extract_html_text(detail_html)
                        snippet = _build_contextual_snippet(detail_text, title, summary_limit) or snippet

                if published_at is not None and published_at.date() < since_cn_date:
                    continue

                extracted.append(ContentItem(
                    id=self._generate_id("custom", source_cfg["name"].replace(" ", "_"), native_id),
                    source_type=SourceType.CUSTOM,
                    title=title[:180],
                    url=link,
                    content=snippet,
                    source_name=source_cfg["name"],
                    published_at=published_at,
                    column=source_cfg.get("column", ""),
                    source_tier=source_cfg.get("source_tier", 4),
                    source_url_normalized=normalized_link,
                    metadata=_build_item_metadata(source_cfg),
                ))
                if len(extracted) >= max_items:
                    return extracted
        return extracted


# ── 并发抓取 ──

async def fetch_all_sources(since: datetime, sources: list[dict]) -> List[ContentItem]:
    """并发抓取所有数据源，sources 从外部注入而非模块全局读取。"""
    fetchers = [
        ("RSS", RSSFetcher(sources)),
        ("Custom", CustomFeedFetcher(sources)),
        ("GDELT", GDELTFetcher()),
        ("Hacker News", HackerNewsFetcher(sources)),
        ("Google News", GoogleNewsFetcher(sources)),
    ]

    headers = {"User-Agent": "Mozilla/5.0 (compatible; USPoliticsNews/2.0)"}
    # 禁用 SSL 验证以兼容证书过期的 RSS 源；放宽头部行长度以兼容超长 CSP 响应
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(
        headers=headers,
        trust_env=True,
        connector=connector,
        max_line_size=65536,
        max_field_size=65536,
    ) as session:
        tasks = []
        for name, fetcher in fetchers:
            fetcher.session = session
            tasks.append(_fetch_with_progress(name, fetcher, since))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for result in results:
        if isinstance(result, Exception):
            print(f"  [ERROR] 抓取异常: {result}")
        elif isinstance(result, list):
            all_items.extend(result)

    return all_items


async def _fetch_with_progress(name: str, fetcher: BaseFetcher, since: datetime) -> List[ContentItem]:
    print(f"  🔍 {name}...")
    try:
        items = await asyncio.wait_for(fetcher.fetch(since), timeout=180)
        print(f"  ✅ {name}: {len(items)} 条")
        return items
    except asyncio.TimeoutError:
        print(f"  ⏰ {name}: 超时跳过")
        return []
    except Exception as e:
        print(f"  ❌ {name}: {_format_fetch_error(e)[:120]}")
        return []


# ── 去重 ──

def merge_cross_source_duplicates(items: List[ContentItem]) -> List[ContentItem]:
    url_groups: dict[str, List[ContentItem]] = {}
    for item in items:
        key = normalize_url(str(item.url))
        url_groups.setdefault(key, []).append(item)

    merged = []
    for key, group in url_groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue

        primary = max(group, key=lambda x: len(x.content or ""))
        all_sources = set()
        for item in group:
            all_sources.add(item.source_type)
            for mk, mv in item.metadata.items():
                if mk not in primary.metadata or not primary.metadata[mk]:
                    primary.metadata[mk] = mv
        primary.metadata["merged_sources"] = list(all_sources)
        primary.source_url_normalized = normalize_url(str(primary.url))
        merged.append(primary)

    return merged


def merge_topic_duplicates(items: List[ContentItem], threshold: float = 0.45) -> List[ContentItem]:
    if len(items) <= 1:
        return items

    ENTITIES = {
        "iran", "china", "trump", "biden", "russia", "ukraine", "israel", "gaza",
        "us", "hamas", "hezbollah", "nato", "eu", "congress", "senate", "house",
        "supreme", "court", "fed", "federal", "reserve", "nvidia", "openai",
        "anthropic", "taiwan", "huawei", "semiconductor",
    }
    ACTIONS = {
        "deal", "agreement", "mou", "memorandum", "sanctions", "tariff",
        "executive", "order", "legislation", "bill", "ruling", "verdict",
        "election", "vote", "ceasefire", "war", "conflict", "invasion",
        "signs", "signed", "announced", "reveals", "revealed",
    }

    def normalize(text: str) -> set[str]:
        stop = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in",
                "for", "on", "with", "by", "at", "from", "and", "or", "its", "it",
                "has", "have", "had", "be", "been", "being", "will", "would", "could"}
        return {w for w in text.lower().split() if len(w) > 1 and w not in stop}

    def title_similarity(a: str, b: str) -> float:
        wa, wb = normalize(a), normalize(b)
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def same_event(a: str, b: str) -> bool:
        wa, wb = normalize(a), normalize(b)
        shared_entities = (wa & wb) & ENTITIES
        shared_actions = (wa & wb) & ACTIONS
        if len(shared_entities) >= 2:
            return True
        if len(shared_entities) >= 1 and len(shared_actions) >= 1:
            return True
        return False

    keep = []
    dropped = set()
    for i, item in enumerate(items):
        if i in dropped:
            continue
        keep.append(item)
        for j in range(i + 1, len(items)):
            if j in dropped:
                continue
            if title_similarity(item.title, items[j].title) >= threshold:
                dropped.add(j)
            elif same_event(item.title, items[j].title):
                dropped.add(j)
            else:
                continue
            if items[j].content and item.content:
                if items[j].content not in item.content:
                    item.content += f"\n\n--- From {items[j].source_name} ---\n{items[j].content}"

    return keep


def apply_balanced_digest(items: List[ContentItem], max_items: int = 20, config: dict | None = None) -> List[ContentItem]:
    """按栏目配额平衡选取条目，config 从外部注入。"""
    if config is None:
        config = {}
    quota = config.get("digest", {}).get("column_quota", {
        "us_politics": 5, "global_affairs": 5, "technology": 5, "economy": 5,
    })

    col_counts: dict[str, int] = {}
    selected = []

    for item in items:
        col = item.column or "other"
        limit = quota.get(col, 3)
        if col_counts.get(col, 0) >= limit:
            continue
        selected.append(item)
        col_counts[col] = col_counts.get(col, 0) + 1

        if len(selected) >= max_items:
            break

    return selected


def save_to_db(items: List[ContentItem], db: NewsDatabase) -> dict:
    stats: dict[str, int] = {}
    for item in items:
        article = Article(
            url=str(item.url),
            title=item.title,
            summary=item.content or "",
            source=item.source_name,
            source_type=item.source_type,
            published_at=item.published_at,
            fetched_at=item.fetched_at,
            topic=item.topic,
            score=item.score,
            reason=item.reason,
            level=item.level,
            column=item.column,
            source_tier=item.source_tier,
            event_key=item.event_key,
            source_url_normalized=item.source_url_normalized or normalize_url(str(item.url)),
        )
        source = item.source_type
        if db.insert(article):
            stats[source] = stats.get(source, 0) + 1

    return stats


def run_all_fetchers(db: NewsDatabase, sources: list[dict]) -> dict:
    since = datetime.now() - timedelta(hours=24)
    items = asyncio.run(fetch_all_sources(since, sources))
    return save_to_db(items, db)
