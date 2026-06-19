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
from typing import List, Optional
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


# ── RSS 抓取器（从 sources.yaml 读取）──

class RSSFetcher(BaseFetcher):
    """RSS/Atom 抓取器 — 读取 sources 中所有非 Google News/GDELT/HN 的源"""

    def __init__(self, sources: list[dict]):
        super().__init__()
        self.feeds = [
            s for s in sources
            if s.get("enabled", True)
            and not s["url"].startswith("https://news.google.com/rss")
            and "gdelt" not in s.get("name", "").lower()
            and "hacker news" not in s.get("name", "").lower()
            and "hnrss" not in s.get("url", "")
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
        for feed_cfg in self.feeds:
            try:
                feed_url = re.sub(
                    r"\$\{(\w+)\}",
                    lambda m: os.environ.get(m.group(1), m.group(0)).strip(),
                    feed_cfg["url"],
                )
                text = await self._get(feed_url, timeout=aiohttp.ClientTimeout(total=30))
                data = feedparser.parse(text)

                for entry in data.entries:
                    published = self._parse_date(entry)
                    if published and published < since:
                        continue

                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    content = self._extract_content(entry)
                    content = re.sub(r"<[^>]+>", "", content)

                    entry_id = entry.get("id", entry.get("link", ""))
                    entry_hash = self._hash_id(str(entry_id))

                    items.append(ContentItem(
                        id=self._generate_id("rss", feed_cfg["name"].replace(" ", "_"), entry_hash),
                        source_type=SourceType.RSS,
                        title=title,
                        url=link,
                        content=content[:500],
                        source_name=feed_cfg["name"],
                        published_at=published,
                        column=feed_cfg.get("column", ""),
                        source_tier=feed_cfg.get("source_tier", 4),
                        source_url_normalized=normalize_url(link),
                        metadata={"tags": [tag.term for tag in entry.get("tags", [])]},
                    ))
            except Exception as e:
                print(f"  [RSS] {feed_cfg['name']} 失败: {e}")
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
                    items.append(ContentItem(
                        id=self._generate_id("gdelt", q["name"], url_hash),
                        source_type=SourceType.GDELT,
                        title=a.get("title", ""),
                        url=a.get("url", ""),
                        content="",
                        source_name=a.get("domain", "GDELT"),
                        published_at=None,
                        column=q["column"],
                        source_tier=4,
                        source_url_normalized=normalize_url(a.get("url", "")),
                    ))
            except Exception as e:
                print(f"  [GDELT] '{q['name']}' 失败: {e}")
        return items


# ── Hacker News 抓取器 ──

class HackerNewsFetcher(BaseFetcher):
    def __init__(self, sources: list[dict]):
        super().__init__()
        self.hn_sources = [s for s in sources if "hacker news" in s.get("name", "").lower() and s.get("enabled", True)]

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
                        metadata={"score": story.get("score", 0)},
                    ))
                except Exception:
                    continue
        except Exception as e:
            print(f"  [HN] 失败: {e}")
        return items


# ── Google News 抓取器（从 sources.yaml 读取）──

class GoogleNewsFetcher(BaseFetcher):
    def __init__(self, sources: list[dict]):
        super().__init__()
        self.feeds = [
            s for s in sources
            if s.get("enabled", True) and s["url"].startswith("https://news.google.com/rss")
        ]

    async def fetch(self, since: datetime) -> List[ContentItem]:
        items = []
        for feed_cfg in self.feeds:
            try:
                text = await self._get(feed_cfg["url"], timeout=aiohttp.ClientTimeout(total=30))
                data = feedparser.parse(text)
                for entry in data.entries:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    content = re.sub(r"<[^>]+>", "", entry.get("summary", ""))
                    entry_hash = self._hash_id(entry.get("id", link))
                    items.append(ContentItem(
                        id=self._generate_id("gnews", feed_cfg["name"].replace(" ", "_"), entry_hash),
                        source_type=SourceType.GOOGLE_NEWS,
                        title=title,
                        url=link,
                        content=content[:500],
                        source_name="Google News",
                        published_at=None,
                        column=feed_cfg.get("column", ""),
                        source_tier=4,
                        source_url_normalized=normalize_url(link),
                    ))
            except Exception as e:
                print(f"  [Google News] '{feed_cfg['name']}' 失败: {e}")
        return items


# ── 并发抓取 ──

async def fetch_all_sources(since: datetime, sources: list[dict]) -> List[ContentItem]:
    """并发抓取所有数据源，sources 从外部注入而非模块全局读取。"""
    fetchers = [
        ("RSS", RSSFetcher(sources)),
        ("GDELT", GDELTFetcher()),
        ("Hacker News", HackerNewsFetcher(sources)),
        ("Google News", GoogleNewsFetcher(sources)),
    ]

    headers = {"User-Agent": "Mozilla/5.0 (compatible; USPoliticsNews/2.0)"}
    async with aiohttp.ClientSession(headers=headers, trust_env=True) as session:
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
        items = await asyncio.wait_for(fetcher.fetch(since), timeout=90)
        print(f"  ✅ {name}: {len(items)} 条")
        return items
    except asyncio.TimeoutError:
        print(f"  ⏰ {name}: 超时跳过")
        return []
    except Exception as e:
        print(f"  ❌ {name}: {str(e)[:60]}")
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
            fetched_at=datetime.now(timezone.utc),
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
