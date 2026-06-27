#!/usr/bin/env python3
"""周报生成管线 — 从数据库读取 7 天已评分文章，调用 report_engine.build_report() 生成周报

流程：
1.  计算周报时间窗口（上周一 07:00 → 本周一 07:00）
2.  从数据库 fetch_since 拉取已评分文章
3.  转为 ContentItem 并过滤到窗口内
4.  构建 scored_events dict 列表
5.  构造 ReportSpec，调用 build_report()
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import NewsDatabase, article_to_content_item
from ai_analyzer import _load_ai_config
from config import load_config, augment_ai_config_with_runtime
from run_pipeline import _open_news_db
from models import ContentItem
from report_engine import ReportSpec, build_report
from report_titles import build_weekly_title

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _load_config() -> dict:
    return load_config()


def _augment_ai_config_with_runtime(ai_config: dict, config: dict) -> dict:
    return augment_ai_config_with_runtime(ai_config, config)


def _get_weekly_window() -> tuple[datetime, datetime, str]:
    """计算周报时间窗口：上周一 07:00 到本周一 07:00，返回 (since, until, report_key)"""
    now = datetime.now(BEIJING_TZ)
    # 找到本周一 07:00
    monday = now - timedelta(days=now.weekday())
    this_monday_7am = monday.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= this_monday_7am:
        until = this_monday_7am
        since = until - timedelta(days=7)
    else:
        until = this_monday_7am - timedelta(days=7)
        since = until - timedelta(days=7)
    # ISO 周号
    week_num = since.isocalendar()[1]
    report_key = f"{since.year}-W{week_num:02d}"
    return since, until, report_key


def _get_month_week_number(date: datetime) -> int:
    month_start = date.replace(day=1)
    return ((date.day + month_start.weekday() - 1) // 7) + 1


def _build_weekly_scored_events(filtered_items: list[ContentItem], articles: list) -> tuple[list[dict], int]:
    articles_by_url = {a.url: a for a in articles}
    event_dicts: list[dict] = []
    skipped = 0
    for item in filtered_items:
        article = articles_by_url.get(str(item.url))
        if article is None or article.llm_score is None:
            skipped += 1
            continue
        tags = [t.strip() for t in (article.llm_tags or "").split(",") if t.strip()]
        event_dicts.append({
            "link": str(item.url),
            "title": item.title,
            "source": item.source_name,
            "score": article.llm_score,
            "summary": article.llm_summary or item.content or "",
            "content": item.content or "",
            "tags": tags,
            "event_key": item.event_key or "",
            "column": item.column or "",
            "source_tier": item.source_tier or 4,
            "is_hard_news": True,
            "source_links": [],
        })
    return event_dicts, skipped


def _report_event_to_scored_dict(event) -> dict:
    return {
        "link": event.source_links[0].get("url", "") if event.source_links else "",
        "title": event.title_zh,
        "source": event.source_links[0].get("title", "") if event.source_links else "",
        "score": event.score,
        "summary": event.summary_zh,
        "content": event.summary_zh,
        "tags": [t.strip() for t in (event.tags or "").split(",") if t.strip()],
        "event_key": event.event_key,
        "column": event.column,
        "source_tier": 2,
        "is_hard_news": True,
        "source_links": event.source_links,
    }


def run_weekly() -> dict:
    """生成周报：计算窗口 → 读取 DB → 构建事件列表 → build_report()"""
    start_time = datetime.now()
    config = _load_config()
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    publish_cfg = config.get("publish", {})

    db = _open_news_db(config)

    # === 1. 计算时间窗口 ===
    since, until, report_key = _get_weekly_window()
    week_num = _get_month_week_number(since)
    print(f"[周报] 窗口: {since.strftime('%m-%d %H:%M')} → {until.strftime('%m-%d %H:%M')}  标识: {report_key}")

    # 优先复用日报沉淀事件，避免周报重新处理大量原始文章。
    since_key = since.strftime("%Y-%m-%d")
    until_key = until.strftime("%Y-%m-%d")
    stored_events = db.fetch_report_events(since_key, until_key, report_type="daily")
    if stored_events:
        event_dicts = [_report_event_to_scored_dict(event) for event in stored_events]
        print(f"[周报] 复用日报事件 {len(event_dicts)} 条")
        skipped = 0
        filtered_items = []
    else:
        event_dicts = []
        skipped = 0

    if not event_dicts:
        # === 2. 从数据库读取文章 ===
        articles = db.fetch_since(since.replace(tzinfo=None))
        print(f"[周报] 数据库加载 {len(articles)} 条文章")

        if not articles:
            print("[警告] 数据库中无最近文章，无法生成周报")
            return {"total_selected": 0}

        # === 3. 转为 ContentItem 并过滤到窗口内 ===
        merged_items = [
            article_to_content_item(a, url_hash_fn=db.url_hash)
            for a in articles
        ]

        # 过滤到周报窗口
        filtered_items = []
        for item in merged_items:
            ref = item.published_at or item.fetched_at
            if ref is None:
                continue
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
            ref_local = ref.astimezone(BEIJING_TZ)
            if since <= ref_local < until:
                filtered_items.append(item)

        print(f"[周报] 窗口过滤后保留 {len(filtered_items)} 条文章")
        if not filtered_items:
            print("[警告] 周报窗口内无文章，无法生成周报")
            return {"total_selected": 0}

        # === 4. 构建 scored_events dict 列表（跳过未评分） ===
        event_dicts, skipped = _build_weekly_scored_events(filtered_items, articles)
        print(f"[周报] 已评分 {len(event_dicts)} 条（跳过 {skipped} 条未评分）")

    if not event_dicts:
        print("[警告] 无已评分文章")
        return {"total_selected": 0}

    # === 5. 构造 ReportSpec，调用 build_report() ===
    spec = ReportSpec(
        product_key=config.get("product_key", "news"),
        report_type="weekly",
        report_key=report_key,
        title=build_weekly_title(since),
        since=since,
        until=until,
        site_root=publish_cfg.get("site_root", "docs/news"),
        output_dir=os.path.join(publish_cfg.get("site_root", "docs/news"), "weekly"),
        feed_path=publish_cfg.get("feed_path", "docs/feeds/news.xml"),
        base_url=publish_cfg.get("base_url", ""),
        column_quotas=digest_cfg.get("columns", {}),
        word_count_min=digest_cfg.get("weekly", {}).get("target_word_count_min", 8000),
        word_count_max=digest_cfg.get("weekly", {}).get("target_word_count_max", 16000),
        highlights_limit=10,
        allow_headline_only=False,
        pub_date=until,
        history_days=analysis_cfg.get("history_context_days", 3),
        min_llm_score=analysis_cfg.get("min_llm_score", 65),
    )

    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)
    stats = build_report(spec, event_dicts, config, ai_config, db)

    # 补充周报特有统计
    stats["duration_seconds"] = round((datetime.now() - start_time).total_seconds(), 1)
    stats["total_articles"] = len(filtered_items) if "filtered_items" in locals() else len(event_dicts)
    stats["reused_report_events"] = len(stored_events)

    return stats


def main():
    stats = run_weekly()
    if stats.get("total_selected", 0) == 0:
        print("[错误] 周报未生成任何内容")
        sys.exit(1)
    print(f"\n周报生成完成: {stats}")


if __name__ == "__main__":
    main()
