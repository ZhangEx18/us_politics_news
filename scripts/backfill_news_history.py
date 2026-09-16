#!/usr/bin/env python3
"""按报告日期回补 news 历史文章。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from config import load_config
from database import Article, NewsDatabase, article_to_content_item
from fetchers import GDELTFetcher, GoogleNewsFetcher, merge_cross_source_duplicates
from models import ContentItem, SourceType
from ai_analyzer import _load_ai_config, score_batch
from run_pipeline import (
    _augment_ai_config_with_runtime,
    _build_scoring_entries_by_column,
    _load_sources,
    _open_news_db,
    _prefilter_items_for_scoring,
    _pre_llm_hard_filter,
)
from urls import normalize_url


DEFAULT_DATES = [
    "2026-06-01",
    "2026-06-02",
    "2026-06-03",
    "2026-06-04",
    "2026-06-05",
    "2026-06-06",
    "2026-06-07",
    "2026-06-08",
    "2026-06-09",
    "2026-06-10",
    "2026-06-11",
    "2026-06-12",
    "2026-06-13",
    "2026-06-14",
    "2026-06-15",
    "2026-06-16",
    "2026-06-17",
    "2026-06-18",
    "2026-06-19",
    "2026-06-20",
    "2026-06-21",
    "2026-06-24",
    "2026-06-25",
    "2026-06-26",
    "2026-06-28",
]


def _parse_dates(values: list[str]) -> list[date]:
    parsed: list[date] = []
    for value in values:
        raw = value.strip()
        if not raw:
            continue
        if ".." in raw:
            start_raw, end_raw = raw.split("..", 1)
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
            current = start
            while current <= end:
                parsed.append(current)
                current += timedelta(days=1)
            continue
        parsed.append(date.fromisoformat(raw))
    return sorted(set(parsed))


def _daily_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _google_history_sources(sources: list[dict], day: date) -> list[dict]:
    before = day + timedelta(days=1)
    result = []
    for source in sources:
        if not source.get("enabled", True) or source.get("fetch_mode") != "google_news":
            continue
        parsed = urlparse(source["url"])
        params = parse_qs(parsed.query)
        base_query = params.get("q", [""])[0].strip()
        if not base_query:
            continue
        params["q"] = [f"{base_query} after:{day.isoformat()} before:{before.isoformat()}"]
        params["ceid"] = [params.get("ceid", ["US:en"])[0]]
        params["hl"] = [params.get("hl", ["en"])[0]]
        params["gl"] = [params.get("gl", ["US"])[0]]
        query = urlencode(params, doseq=True)
        result.append({**source, "url": urlunparse(parsed._replace(query=query))})
    return result


def _split_google_publisher(title: str) -> tuple[str, str]:
    if " - " not in title:
        return title, "Google News"
    headline, publisher = title.rsplit(" - ", 1)
    headline = headline.strip()
    publisher = publisher.strip()
    return headline or title, publisher or "Google News"


def _published_from_google_title(title: str, day: date) -> datetime | None:
    text = title.lower()
    match = re.search(r"\b(\d{1,2})\s+hours?\s+ago\b", text)
    if match:
        # Google News 历史 RSS 常按查询日期返回相对时间；仅用于落入目标日报窗口。
        hours = min(int(match.group(1)), 23)
        return datetime.combine(day, time(23, 0), tzinfo=timezone.utc) - timedelta(hours=hours)
    return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)


def _retime_items(items: list[ContentItem], day: date) -> list[ContentItem]:
    fetched_at = datetime.combine(day, time(12, 0), tzinfo=timezone.utc)
    retimed = []
    for item in items:
        published_at = item.published_at
        if item.source_type == SourceType.GOOGLE_NEWS:
            published_at = _published_from_google_title(item.title, day)
        elif published_at is None:
            published_at = fetched_at
        update = {"published_at": published_at, "fetched_at": fetched_at}
        if item.source_type == SourceType.GOOGLE_NEWS:
            headline, publisher = _split_google_publisher(item.title)
            update.update({"title": headline, "source_name": publisher})
        retimed.append(item.model_copy(update=update))
    return retimed


async def _fetch_gdelt_history(session: aiohttp.ClientSession, day: date) -> list[ContentItem]:
    fetcher = GDELTFetcher()
    fetcher.session = session
    since, until = _daily_bounds(day)
    items: list[ContentItem] = []
    for query in fetcher.queries:
        try:
            data = await fetcher._get_json(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": query["query"],
                    "mode": "ArtList",
                    "format": "json",
                    "startdatetime": since.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": until.strftime("%Y%m%d%H%M%S"),
                    "maxrecords": 100,
                    "sort": "datedesc",
                },
                timeout=aiohttp.ClientTimeout(total=60),
            )
        except Exception as exc:
            print(f"  [GDELT] '{query['name']}' 失败: {type(exc).__name__}: {exc}")
            continue
        for article in data.get("articles", []):
            published_at = fetcher._parse_article_datetime(article)
            if published_at and not (since <= published_at < until):
                continue
            url = article.get("url", "")
            if not url:
                continue
            url_hash = fetcher._hash_id(url)
            items.append(ContentItem(
                id=fetcher._generate_id("gdelt", query["name"], url_hash),
                source_type=SourceType.GDELT,
                title=article.get("title", ""),
                url=url,
                content="",
                source_name=article.get("domain", "GDELT"),
                published_at=published_at,
                column=query["column"],
                source_tier=4,
                source_url_normalized=normalize_url(url),
                fetched_at=since + timedelta(hours=12),
            ))
    return items


async def _fetch_history_items(sources: list[dict], day: date, *, include_gdelt: bool = False) -> list[ContentItem]:
    google_sources = _google_history_sources(sources, day)
    since, _ = _daily_bounds(day)
    google_fetcher = GoogleNewsFetcher(google_sources)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; USPoliticsNews/2.0)"}
    async with aiohttp.ClientSession(headers=headers, trust_env=True) as session:
        google_fetcher.session = session
        tasks = [_fetch_one("Google News", google_fetcher, since)]
        if include_gdelt:
            tasks.append(_fetch_gdelt_history(session, day))
        results = await asyncio.gather(*tasks)
    items = [item for batch in results for item in batch]
    return _retime_items(merge_cross_source_duplicates(items), day)


async def _fetch_one(name: str, fetcher, since: datetime) -> list[ContentItem]:
    try:
        items = await asyncio.wait_for(fetcher.fetch(since), timeout=120)
        print(f"  {name}: {len(items)} 条")
        return items
    except Exception as exc:
        print(f"  {name}: 失败 {type(exc).__name__}: {exc}")
        return []


def _insert_items(items: list[ContentItem], db: NewsDatabase) -> int:
    articles = []
    for item in items:
        articles.append(Article(
            url=str(item.url),
            title=item.title,
            summary=item.content or "",
            source=item.source_name,
            source_type=str(item.source_type),
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
        ))
    return db.insert_many(articles)


def _day_stats(db: NewsDatabase, day: date) -> dict[str, object]:
    since, until = _daily_bounds(day)
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT "column", COUNT(*) AS total, COUNT(DISTINCT source) AS sources,
                      SUM(CASE WHEN llm_score IS NOT NULL THEN 1 ELSE 0 END) AS scored
               FROM articles
               WHERE fetched_at >= ? AND fetched_at < ?
               GROUP BY "column"
               ORDER BY "column" """,
            (since.isoformat(), until.isoformat()),
        ).fetchall()
    by_column = {
        row["column"] or "unknown": {
            "total": int(row["total"] or 0),
            "sources": int(row["sources"] or 0),
            "scored": int(row["scored"] or 0),
        }
        for row in rows
    }
    return {
        "total": sum(col["total"] for col in by_column.values()),
        "scored": sum(col["scored"] for col in by_column.values()),
        "columns": by_column,
    }


def _load_day_items(db: NewsDatabase, day: date) -> list[ContentItem]:
    since, until = _daily_bounds(day)
    articles = [
        article
        for article in db.fetch_since(since)
        if article.fetched_at and article.fetched_at < until
    ]
    return [article_to_content_item(article, url_hash_fn=db.url_hash) for article in articles]


def _score_day(
    db: NewsDatabase,
    config: dict,
    day: date,
    *,
    wall_timeout_seconds: int,
    max_prompt_chars: int,
    timeout_seconds: int,
    max_concurrent: int,
) -> dict[str, int]:
    items = _load_day_items(db, day)
    if not items:
        print(f"  评分跳过: {day.isoformat()} 无文章")
        return {"input": 0, "candidate": 0, "scored": 0, "updated": 0}

    columns_cfg = config.get("digest", {}).get("columns", {})
    prefiltered = _prefilter_items_for_scoring(items, columns_cfg)
    prefiltered = {
        column: _pre_llm_hard_filter(column_items, config)
        for column, column_items in prefiltered.items()
    }
    entries, _ = _build_scoring_entries_by_column(
        prefiltered,
        report_date=day.isoformat(),
        config=config,
    )
    if not entries:
        print(f"  评分跳过: {day.isoformat()} 无候选")
        return {"input": len(items), "candidate": 0, "scored": 0, "updated": 0}

    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)
    ai_config["score_wall_timeout_seconds"] = wall_timeout_seconds
    ai_config["score_max_prompt_chars"] = max_prompt_chars
    ai_config["score_timeout_seconds"] = timeout_seconds
    ai_config["score_max_concurrent"] = max_concurrent
    scored, errors = asyncio.run(score_batch(entries, ai_config))
    updated = db.update_llm_scores(scored)
    valid = sum(1 for item in scored if item.get("score") is not None)
    print(
        f"  评分完成: input={len(items)} candidate={len(entries)} "
        f"scored={valid} updated={updated} errors={len(errors)}"
    )
    for error in errors[:3]:
        print(f"    - {error}")
    return {"input": len(items), "candidate": len(entries), "scored": valid, "updated": updated}


def _print_stats(day: date, stats: dict[str, object]) -> None:
    print(f"  入库后 {day.isoformat()}: total={stats['total']} scored={stats['scored']}")
    for column, values in sorted(stats["columns"].items()):
        print(
            f"    {column}: total={values['total']} "
            f"scored={values['scored']} sources={values['sources']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="按日期回补 news 历史文章")
    parser.add_argument("--date", action="append", dest="dates", help="YYYY-MM-DD 或 YYYY-MM-DD..YYYY-MM-DD，可重复")
    parser.add_argument("--mode", choices=["fetch", "score", "both"], default="fetch", help="fetch=只补文章，score=只评分，both=补文章后评分")
    parser.add_argument("--include-gdelt", action="store_true", help="同时查询 GDELT；默认关闭以避免限流拖慢历史回补")
    parser.add_argument("--score-wall-timeout", type=int, default=1200, help="单日评分总超时秒数")
    parser.add_argument("--score-max-prompt-chars", type=int, default=6000, help="评分单批 prompt 字符上限，越小越稳")
    parser.add_argument("--score-timeout", type=int, default=240, help="评分单批请求超时秒数")
    parser.add_argument("--score-max-concurrent", type=int, default=2, help="评分并发批次数")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和统计，不写入数据库")
    args = parser.parse_args()

    config = load_config()
    sources = _load_sources(config)
    db = _open_news_db(config)
    target_dates = _parse_dates(args.dates or DEFAULT_DATES)
    print(f"目标日期: {', '.join(day.isoformat() for day in target_dates)}")

    total_inserted = 0
    for day in target_dates:
        print(f"\n[回补] {day.isoformat()}")
        if args.mode in {"fetch", "both"}:
            items = asyncio.run(_fetch_history_items(sources, day, include_gdelt=args.include_gdelt))
            by_column = Counter(item.column or "unknown" for item in items)
            by_source_type = Counter(str(item.source_type) for item in items)
            print(f"  去重后抓取: {len(items)} 条 columns={dict(by_column)} source_types={dict(by_source_type)}")
            if not args.dry_run:
                inserted = _insert_items(items, db)
                total_inserted += inserted
                print(f"  新增入库: {inserted} 条")
                _print_stats(day, _day_stats(db, day))
        if args.mode in {"score", "both"} and not args.dry_run:
            _score_day(
                db,
                config,
                day,
                wall_timeout_seconds=args.score_wall_timeout,
                max_prompt_chars=args.score_max_prompt_chars,
                timeout_seconds=args.score_timeout,
                max_concurrent=args.score_max_concurrent,
            )
            _print_stats(day, _day_stats(db, day))

    if not args.dry_run:
        print(f"\n完成: 新增 {total_inserted} 条")


if __name__ == "__main__":
    main()
