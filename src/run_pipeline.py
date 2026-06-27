#!/usr/bin/env python3
"""
观察日报 Pipeline v4 — fetch + score 与 report 分离

1.  并发抓取
2.  跨源 URL 去重
3.  入库
3.5 Pre-LLM 硬过滤（移除高置信度软新闻）
4.  预筛 + AI score_batch
5.  更新数据库 LLM 评分
6.  硬新闻过滤
7+. 构造 ReportSpec，委托 report_engine.build_report() 完成后续阶段

支持模式：
  默认       完整流程（fetch + score + report）
  --fetch-only   只执行抓取入库（步骤 1-3）
  --digest-only  只执行 score + report（从数据库读取）
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_project_root, "scripts"))

from ai_analyzer import score_batch, _load_ai_config
from config import (
    load_config,
    load_product_config,
    augment_ai_config_with_runtime as _augment_real,
)
from database import (
    ArticleCandidate,
    NewsDatabase,
    ReportEvent,
    article_to_content_item,
    migrate_legacy_news_db,
    db_health_check,
    format_health_report,
)
from fetchers import (
    fetch_all_sources,
    merge_cross_source_duplicates,
    save_to_db,
    normalize_url,
)
from models import ContentItem
from report_engine import ReportSpec, build_report
from report_titles import build_daily_title


DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_CUTOFF_HOUR = 7
DEFAULT_FETCH_AT = "07:00"
DEFAULT_PUBLISH_AT = "07:45"


def _load_config() -> dict:
    return load_config()


def _load_sources(config: dict | None = None) -> list[dict]:
    cfg = config or _load_config()
    path = os.path.join(_project_root, cfg.get("sources_file", "config/products/news/sources.yaml"))
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _load_schedule_config(config: dict | None = None) -> dict:
    """加载日报调度配置，并提供稳定默认值。"""
    cfg = config or _load_config()
    schedule_cfg = cfg.get("schedule", {})
    return {
        "timezone": schedule_cfg.get("timezone", DEFAULT_TZ),
        "cutoff_hour": int(schedule_cfg.get("cutoff_hour", DEFAULT_CUTOFF_HOUR)),
        "fetch_at": str(schedule_cfg.get("fetch_at", DEFAULT_FETCH_AT)),
        "publish_at": str(schedule_cfg.get("publish_at", DEFAULT_PUBLISH_AT)),
    }


def _get_schedule_timezone(config: dict | None = None) -> ZoneInfo:
    schedule_cfg = _load_schedule_config(config)
    return ZoneInfo(schedule_cfg["timezone"])


def _parse_schedule_time(value: str, fallback: str) -> tuple[int, int]:
    raw = (value or fallback).strip() or fallback
    try:
        hour_str, minute_str = raw.split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        fallback_hour, fallback_minute = fallback.split(":", 1)
        return int(fallback_hour), int(fallback_minute)


def _augment_ai_config_with_runtime(ai_config: dict, config: dict) -> dict:
    return _augment_real(ai_config, config)


def _open_news_db(config: dict) -> NewsDatabase:
    """打开正式数据库，自动执行：旧库迁移 → 远端状态同步（空库时）→ 返回实例。"""
    from sync_state_db import sync_product_db

    storage_cfg = config.get("storage", {})
    db_path = storage_cfg.get("db_path", "data/products/news/news.db")
    product_key = config.get("product_key", "news")

    # 1. 旧库迁移
    migration = migrate_legacy_news_db(db_path)
    if migration["legacy_exists"] and (migration["migrated_articles"] or migration["migrated_fetch_logs"]):
        print(
            f"[数据库] 已从旧库迁移到正式库: "
            f"{migration['migrated_articles']} 篇文章, {migration['migrated_fetch_logs']} 条抓取日志"
        )

    # 2. 空库或不存在时，自动从远端 state branch 同步
    db = NewsDatabase(db_path)
    if db.article_count() == 0:
        print(f"[数据库] 本地库为空，尝试从远端 {product_key} 状态分支同步...")
        result = sync_product_db(product_key)
        if result["restored"]:
            print(f"[数据库] 已从 {result['source_ref']} 恢复: {result['article_count']} 条文章")
            if result.get("latest_fetched_at"):
                print(f"[数据库] 最晚抓取时间: {result['latest_fetched_at']}")
            db = NewsDatabase(db_path)
        elif result.get("error"):
            print(f"[数据库] 远端同步未成功: {result['error']}")

    return db


def _get_source_max_age_hours(source_name: str, sources: list[dict], default_hours: int) -> int:
    for source in sources:
        if source.get("name") == source_name:
            return int(source.get("max_age_hours", default_hours))
    return default_hours




def _count_scored_entries(entries: list[dict]) -> int:
    """统计真正拿到 AI 评分结果的条目数。"""
    valid = 0
    for entry in entries:
        if (
            str(entry.get("event_key", "")).strip()
            and str(entry.get("column", "")).strip()
            and str(entry.get("summary", "")).strip()
        ):
            valid += 1
    return valid


def _is_cn_source_item(item: ContentItem) -> bool:
    language = str(item.metadata.get("language", "")).lower()
    tags = {str(tag).lower() for tag in item.metadata.get("tags", [])}
    return language.startswith("zh") or "cn_source" in tags


def _is_cn_source_entry(entry: dict) -> bool:
    language = str(entry.get("language", "")).lower()
    tags = {str(tag).lower() for tag in entry.get("tags", [])}
    return language.startswith("zh") or "cn_source" in tags


def _is_hard_news_entry(entry: dict) -> bool:
    """只保留硬新闻进入正文链路。"""
    return bool(entry.get("is_hard_news", False))


_COLUMN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "us_politics": ("white house", "trump", "biden", "senate", "house", "supreme court", "congress", "election"),
    "global_affairs": ("china", "iran", "israel", "ukraine", "russia", "g7", "nato", "diplom"),
    "technology": ("ai", "openai", "chip", "semiconductor", "tesla", "meta", "google", "microsoft"),
    "economy": ("fed", "inflation", "tariff", "jobs", "market", "bond", "trade", "gdp"),
}


# ── Pre-LLM 硬过滤 ──

# 默认软/硬新闻关键词（config 未配置时的 fallback）
_DEFAULT_SOFT_NEWS_KEYWORDS: list[str] = [
    "celebrity", "sports", "entertainment", "movie", "music", "lifestyle",
    "watchlist", "stock to watch", "buy rating", "price target",
    "opinion", "editorial", "column", "reaction", "mocked", "blasted",
    "viral", "awkward moment", "red carpet", "box office",
    "观察名单", "荐股", "买入评级", "目标价", "娱乐", "体育",
    "明星", "网友热议", "尴尬瞬间", "语无伦次", "直播中断",
]

_DEFAULT_HARD_NEWS_KEYWORDS: list[str] = [
    "court", "supreme court", "ruling", "lawsuit", "congress", "senate",
    "white house", "executive order", "regulation", "sanctions", "tariff",
    "fed", "federal reserve", "inflation", "jobs", "earnings", "revenue",
    "chip", "ai ", "artificial intelligence", "semiconductor",
    "military", "ceasefire", "nato", "g7", "diplomacy", "treaty",
    "法院", "最高法院", "国会", "白宫", "行政命令", "制裁",
    "美联储", "通胀", "就业", "财报", "芯片", "半导体",
]


def _pre_llm_hard_filter(items: list[ContentItem], config: dict | None = None) -> list[ContentItem]:
    """Pre-LLM 硬过滤：移除高置信度软新闻，保留不确定内容交给 LLM。"""
    rules_cfg = (config or {}).get("rules", {})
    soft_keywords = rules_cfg.get("soft_news_keywords", _DEFAULT_SOFT_NEWS_KEYWORDS)
    hard_keywords = rules_cfg.get("hard_news_keywords", _DEFAULT_HARD_NEWS_KEYWORDS)

    filtered: list[ContentItem] = []
    for item in items:
        text = f"{item.title} {item.content or ''}".lower()
        # 硬新闻关键词命中 → 直接保留
        if any(kw in text for kw in hard_keywords):
            filtered.append(item)
            continue
        # 软新闻关键词命中 → 过滤
        if any(kw in text for kw in soft_keywords):
            continue
        # 不确定 → 保留交给 LLM
        filtered.append(item)
    return filtered


def _item_recency_hours(item: ContentItem, now: datetime) -> float:
    """估算条目距当前的小时数，优先用发布时间。"""
    ref = item.published_at or item.fetched_at
    if ref is None:
        return 9999.0
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return max((now_utc - ref).total_seconds() / 3600, 0.0)


def _keyword_bonus(item: ContentItem) -> int:
    """按栏目关键词给预筛选打额外权重。"""
    keywords = _COLUMN_KEYWORDS.get(item.column or "us_politics", ())
    haystack = f"{item.title} {item.content or ''}".lower()
    hits = sum(1 for kw in keywords if kw in haystack)
    return min(hits * 4, 12)


def _prefilter_signal(item: ContentItem, now: datetime) -> float:
    """预筛选综合信号：规则分 + 来源等级 + 时效 + 信息密度 + 关键词。"""
    tier_bonus = {1: 30, 2: 20, 3: 10, 4: 0}.get(item.source_tier or 4, 0)
    hours_old = _item_recency_hours(item, now)
    if hours_old <= 6:
        recency_bonus = 15
    elif hours_old <= 12:
        recency_bonus = 10
    elif hours_old <= 24:
        recency_bonus = 5
    else:
        recency_bonus = 0

    content_len = len((item.content or "").strip())
    if content_len >= 600:
        content_bonus = 8
    elif content_len >= 240:
        content_bonus = 4
    elif content_len >= 80:
        content_bonus = 1
    else:
        content_bonus = 0

    return (item.score or 0) + tier_bonus + recency_bonus + content_bonus + _keyword_bonus(item)


def _candidate_reason(item: ContentItem, now: datetime) -> str:
    parts = [
        f"tier={item.source_tier or 4}",
        f"recency_hours={_item_recency_hours(item, now):.1f}",
    ]
    bonus = _keyword_bonus(item)
    if bonus:
        parts.append(f"keyword_bonus={bonus}")
    return ",".join(parts)


def _content_items_to_candidates(
    items_by_column: dict[str, list[ContentItem]],
    report_key: str,
    report_type: str,
    now: datetime | None = None,
) -> list[ArticleCandidate]:
    now = now or datetime.now(timezone.utc)
    candidates: list[ArticleCandidate] = []
    for col_key, items in items_by_column.items():
        for item in items:
            candidates.append(ArticleCandidate(
                report_key=report_key,
                report_type=report_type,
                url=str(item.url),
                title=item.title,
                source=item.source_name,
                column=item.column or col_key,
                candidate_score=_prefilter_signal(item, now),
                source_tier=item.source_tier or 4,
                reason=_candidate_reason(item, now),
                status="candidate",
                event_key=item.event_key or "",
                published_at=item.published_at,
                fetched_at=item.fetched_at,
                source_url_normalized=item.source_url_normalized or normalize_url(str(item.url)),
            ))
    return candidates


def _prefilter_items_for_scoring(
    items: list[ContentItem],
    columns_cfg: dict[str, dict],
    now: datetime | None = None,
) -> dict[str, list[ContentItem]]:
    """
    规则预筛：按栏目压缩候选池，优先保留来源等级高、更新近、信息更完整的条目。
    """
    now = now or datetime.now(timezone.utc)
    deduped: dict[str, ContentItem] = {}
    for item in items:
        col = item.column or "us_politics"
        normalized = item.source_url_normalized or normalize_url(str(item.url))
        key = f"{col}:{normalized}"
        current = deduped.get(key)
        if current is None or _prefilter_signal(item, now) > _prefilter_signal(current, now):
            deduped[key] = item

    by_column: dict[str, list[ContentItem]] = {col_key: [] for col_key in columns_cfg}
    for item in deduped.values():
        col = item.column or "us_politics"
        by_column.setdefault(col, []).append(item)

    selected: dict[str, list[ContentItem]] = {}
    for col_key, col_cfg in columns_cfg.items():
        ranked = sorted(
            by_column.get(col_key, []),
            key=lambda item: (
                _prefilter_signal(item, now),
                -(item.source_tier or 4),
                -len((item.content or "").strip()),
            ),
            reverse=True,
        )
        limit = col_cfg.get("prefilter_items", 18)
        selected[col_key] = ranked[:limit]
    return selected


def _build_scoring_entries_by_column(
    column_items: dict[str, list[ContentItem]],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """将预筛后的 ContentItem 转成评分输入，同时保留按栏目映射。"""
    all_entries: list[dict] = []
    by_column_entries: dict[str, list[dict]] = {}
    for col_key, items in column_items.items():
        entries = [
            {
                "link": str(item.url),
                "title": item.title,
                "source": item.source_name,
                "published": item.published_at.isoformat() if item.published_at else "",
                "content": (item.content or "")[:600],
                "column_hint": item.column or col_key,
                "source_tier": item.source_tier or 4,
                "language": item.metadata.get("language", ""),
                "tags": list(item.metadata.get("tags", [])),
            }
            for item in items
        ]
        by_column_entries[col_key] = entries
        all_entries.extend(entries)
    return all_entries, by_column_entries


def _content_item_to_report_candidate(item: ContentItem, score: float = 0) -> dict:
    return {
        "title": item.title,
        "source": item.source_name,
        "score": score,
        "summary": item.content or "",
        "content": item.content or "",
        "source_links": [{"title": item.source_name, "url": str(item.url)}] if item.url else [],
        "language": item.metadata.get("language", ""),
        "tags": list(item.metadata.get("tags", [])),
        "event_key": "",
        "is_hard_news": False,
        "column": item.column or "us_politics",
    }


def _scored_entries_to_report_events(
    scored_entries: list[dict],
    report_key: str,
    report_type: str,
) -> list[ReportEvent]:
    events: list[ReportEvent] = []
    for entry in scored_entries:
        event_key = str(entry.get("event_key") or entry.get("link") or "").strip()
        title_zh = str(entry.get("title_zh") or entry.get("title") or "").strip()
        summary_zh = str(entry.get("summary") or entry.get("content") or "").strip()
        if not event_key or not title_zh:
            continue
        source_links = entry.get("source_links")
        if not isinstance(source_links, list) or not source_links:
            source_links = [{
                "title": entry.get("source", ""),
                "url": entry.get("link", ""),
            }]
        tags = entry.get("tags", [])
        tags_str = ",".join(tags) if isinstance(tags, list) else str(tags or "")
        events.append(ReportEvent(
            report_key=report_key,
            report_type=report_type,
            event_key=event_key,
            column=str(entry.get("column") or ""),
            title_zh=title_zh,
            summary_zh=summary_zh,
            score=float(entry.get("score") or 0),
            source_links=source_links,
            quality_status="ok" if _is_hard_news_entry(entry) else "candidate",
            tags=tags_str,
        ))
    return events


def _report_event_to_scored_dict(event: ReportEvent) -> dict:
    primary_link = event.source_links[0] if event.source_links else {}
    return {
        "link": primary_link.get("url", ""),
        "title": event.title_zh,
        "title_zh": event.title_zh,
        "source": primary_link.get("title", ""),
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


def _has_events_for_required_columns(events: list[ReportEvent], columns_cfg: dict) -> bool:
    expected = set(columns_cfg.keys()) or {"us_politics", "global_affairs", "technology", "economy"}
    covered = {event.column for event in events if event.column}
    return expected.issubset(covered)


def _log_digest_run(
    db: NewsDatabase,
    *,
    report_key: str,
    report_type: str,
    status: str,
    window_since: datetime,
    window_until: datetime,
    input_count: int,
    candidate_count: int,
    selected_count: int = 0,
    ai_duration_seconds: float = 0.0,
    error_count: int = 0,
    output_md_path: str = "",
    output_html_path: str = "",
    metrics: dict | None = None,
) -> None:
    db.log_report_run(
        report_key=report_key,
        report_type=report_type,
        status=status,
        window_since=window_since,
        window_until=window_until,
        input_count=input_count,
        candidate_count=candidate_count,
        selected_count=selected_count,
        ai_duration_seconds=ai_duration_seconds,
        error_count=error_count,
        output_md_path=output_md_path,
        output_html_path=output_html_path,
        metrics=metrics or {},
    )


def _get_report_window(now: datetime | None = None, config: dict | None = None) -> tuple[datetime, datetime, str]:
    """固定晨报窗口：配置时区下前一天 cutoff 到当天 cutoff。"""
    schedule_cfg = _load_schedule_config(config)
    local_tz = ZoneInfo(schedule_cfg["timezone"])
    cutoff_hour = schedule_cfg["cutoff_hour"]
    now_local = now.astimezone(local_tz) if now and now.tzinfo else datetime.now(local_tz)
    today_cutoff = now_local.replace(
        hour=cutoff_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if now_local >= today_cutoff:
        since_local = today_cutoff - timedelta(days=1)
        until_local = today_cutoff
        report_date = today_cutoff.date().isoformat()
    else:
        since_local = today_cutoff - timedelta(days=2)
        until_local = today_cutoff - timedelta(days=1)
        report_date = (today_cutoff - timedelta(days=1)).date().isoformat()
    return since_local, until_local, report_date


def _get_report_publish_time(report_date: str, config: dict | None = None) -> datetime:
    """晨报 RSS 发布时间由配置驱动。"""
    schedule_cfg = _load_schedule_config(config)
    local_tz = ZoneInfo(schedule_cfg["timezone"])
    publish_hour, publish_minute = _parse_schedule_time(schedule_cfg["publish_at"], DEFAULT_PUBLISH_AT)
    report_day = datetime.strptime(report_date, "%Y-%m-%d").date()
    return datetime(
        report_day.year,
        report_day.month,
        report_day.day,
        publish_hour,
        publish_minute,
        0,
        tzinfo=local_tz,
    )


def _filter_articles_to_window(
    items: list[ContentItem],
    since: datetime,
    until: datetime,
    config: dict | None = None,
) -> list[ContentItem]:
    """只保留日报固定窗口内抓取到的新闻。"""
    local_tz = _get_schedule_timezone(config)
    filtered: list[ContentItem] = []
    for item in items:
        ref = item.published_at or item.fetched_at
        if ref is None:
            continue
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        ref_local = ref.astimezone(local_tz)
        if since <= ref_local < until:
            filtered.append(item)
    return filtered


def _filter_items_by_freshness(
    items: list[ContentItem],
    sources: list[dict],
    config: dict,
    now: datetime | None = None,
) -> tuple[list[ContentItem], dict[str, int]]:
    """抓取后按全局/来源时效过滤，避免旧文反复挤占当天候选池。"""
    analysis_cfg = config.get("analysis", {})
    schedule_tz = _get_schedule_timezone(config)
    current = now.astimezone(schedule_tz) if now and now.tzinfo else datetime.now(schedule_tz)
    default_hours = int(analysis_cfg.get("freshness_hours", analysis_cfg.get("window_hours", 24)))

    kept: list[ContentItem] = []
    dropped = 0
    dropped_by_column: dict[str, int] = {}
    for item in items:
        max_age_hours = _get_source_max_age_hours(item.source_name, sources, default_hours)
        age_hours = _item_recency_hours(item, current.astimezone(timezone.utc))
        if age_hours <= max_age_hours:
            kept.append(item)
            continue
        dropped += 1
        col = item.column or "unknown"
        dropped_by_column[col] = dropped_by_column.get(col, 0) + 1

    return kept, {
        "freshness_hours": default_hours,
        "dropped": dropped,
        "kept": len(kept),
        **{f"dropped_{col}": count for col, count in dropped_by_column.items()},
    }


def _write_metrics_file(output_root: str, metrics: dict) -> str:
    """写出最新一次发布观测数据，供运营排查栏目不足原因。"""
    metrics_dir = os.path.join(output_root, "metrics")
    os.makedirs(metrics_dir, exist_ok=True)
    path = os.path.join(metrics_dir, "latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    return path


def run_pipeline(hours: int = 24, report_type: str = "daily") -> dict:
    """完整流程：抓取 + 评分 + 分栏 digest"""
    start_time = datetime.now()
    config = _load_config()
    since, until, report_date = _get_report_window(config=config)
    print(f"日报窗口: {since.strftime('%m-%d %H:%M')} → {until.strftime('%m-%d %H:%M')}")
    publish_cfg = config.get("publish", {})
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    runtime_cfg = config.get("runtime", {})

    site_root = publish_cfg.get("site_root", "docs/news")
    feed_path = publish_cfg.get("feed_path", "docs/feeds/news.xml")
    base_url = publish_cfg.get("base_url", "")
    history_days = analysis_cfg.get("history_context_days", 3)

    # 从环境变量加载 AI 配置
    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)

    print("=" * 60)
    print("观察日报 Pipeline v3")
    print(f"时间: {start_time.isoformat()}")
    print(f"时间窗口: 最近 {hours} 小时")
    print("=" * 60)

    # 前置检查
    if runtime_cfg.get("require_ai", True) and not ai_config.get("api_key"):
        print("\n[错误] 未配置 AI_API_KEY，无法运行 pipeline")
        sys.exit(1)

    db = _open_news_db(config)

    # 前置健康检查（可观测性）
    health = db_health_check(
        storage_cfg.get("db_path", "data/products/news/news.db"),
        window_since=since.astimezone(timezone.utc).replace(tzinfo=None),
    )
    print(f"\n[数据库状态]\n{format_health_report(health)}")

    # === 1. 并发抓取 ===
    print("\n[1/13] 并发抓取所有数据源...")
    sources = _load_sources(config)
    all_items = asyncio.run(fetch_all_sources(since, sources))
    print(f"   共抓取 {len(all_items)} 条")

    if not all_items:
        print("\n[警告] 未抓取到任何内容，pipeline 终止")
        return {"total_fetched": 0, "total_selected": 0}

    # === 2. 跨源 URL 去重 ===
    print("\n[2/13] 跨源 URL 去重...")
    merged_items = merge_cross_source_duplicates(all_items)
    print(f"   合并 {len(all_items) - len(merged_items)} 条 -> {len(merged_items)} 条唯一")

    print("\n[2.5/13] 新鲜度过滤...")
    merged_items, freshness_stats = _filter_items_by_freshness(merged_items, sources, config)
    print(f"   保留 {freshness_stats['kept']} 条，过滤陈旧内容 {freshness_stats['dropped']} 条")

    # === 3. 入库 ===
    print("\n[3/13] 入库...")
    fetch_stats = save_to_db(merged_items, db)
    new_count = sum(fetch_stats.values())
    print(f"   新增 {new_count} 条")

    # 抓取可观测性日志
    post_fetch_health = db_health_check(
        storage_cfg.get("db_path", "data/products/news/news.db"),
    )
    print(f"   [可观测] 入库后文章总数: {post_fetch_health['article_count']}")
    print(f"   [可观测] 最晚抓取时间: {post_fetch_health.get('latest_fetched_at', '未知')}")
    print(f"   [可观测] 目标数据库: {post_fetch_health['db_path']}")

    # === 4-13: digest 流程 ===
    return _run_digest_phase(
        config,
        db,
        merged_items,
        ai_config,
        start_time,
        since,
        until,
        report_date,
        report_type=report_type,
        product_key=config.get("product_key", "news"),
        site_root=site_root,
        pipeline_context={
            "sources": sources,
            "freshness": freshness_stats,
            "raw_fetched": len(all_items),
            "raw_merged": len(merged_items),
        },
    )


def run_fetch_only(hours: int = 24) -> dict:
    """只执行抓取入库（步骤 1-3）"""
    start_time = datetime.now()
    since = start_time - timedelta(hours=hours)

    config = _load_config()
    storage_cfg = config.get("storage", {})
    print("=" * 60)
    print("观察日报 Pipeline — 抓取模式")
    print(f"时间窗口: 最近 {hours} 小时")
    print("=" * 60)

    db = _open_news_db(config)

    # 前置健康检查（可观测性）
    health = db_health_check(storage_cfg.get("db_path", "data/products/news/news.db"))
    print(f"\n[数据库状态]\n{format_health_report(health)}")

    # 1. 并发抓取
    print("\n[1/3] 并发抓取所有数据源...")
    sources = _load_sources(config)
    all_items = asyncio.run(fetch_all_sources(since, sources))
    print(f"   共抓取 {len(all_items)} 条")

    if not all_items:
        print("\n[警告] 未抓取到任何内容")
        return {"total_fetched": 0}

    # 2. 跨源 URL 去重
    print("\n[2/3] 跨源 URL 去重...")
    merged_items = merge_cross_source_duplicates(all_items)
    print(f"   合并 {len(all_items) - len(merged_items)} 条 -> {len(merged_items)} 条唯一")

    # 3. 入库
    print("\n[3/3] 入库...")
    fetch_stats = save_to_db(merged_items, db)
    total_new = sum(fetch_stats.values())
    print(f"   新增 {total_new} 条")

    duration = (datetime.now() - start_time).total_seconds()
    print(f"\n抓取完成，耗时 {duration:.1f}s")

    return {
        "total_fetched": len(all_items),
        "total_merged": len(merged_items),
        "total_new": total_new,
        "duration_seconds": round(duration, 1),
    }


def run_digest_only(hours: int = 24, report_type: str = "daily") -> dict:
    """只执行 digest 流程（步骤 4-13），从数据库读取已有数据"""
    start_time = datetime.now()
    config = _load_config()
    since, until, report_date = _get_report_window(config=config)
    storage_cfg = config.get("storage", {})
    publish_cfg = config.get("publish", {})
    # 从环境变量加载 AI 配置
    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)

    print("=" * 60)
    print("观察日报 Pipeline — Digest 模式")
    print("=" * 60)

    db = _open_news_db(config)

    # 健康检查：在查询前打印数据库状态
    window_since_utc = since.astimezone(timezone.utc).replace(tzinfo=None)
    health = db_health_check(
        storage_cfg.get("db_path", "data/products/news/news.db"),
        window_since=window_since_utc,
    )
    print(f"\n[数据库状态]\n{format_health_report(health)}")

    # 从数据库读取固定晨报窗口文章，补跑不改变窗口
    today_articles = db.fetch_since(window_since_utc)
    if not today_articles:
        if not health["db_exists"]:
            print("\n[错误] 正式库不存在，请先执行: python3 scripts/sync_state_db.py")
        elif health["article_count"] == 0:
            print("\n[错误] 正式库为空，请先执行抓取或同步远端状态: python3 scripts/sync_state_db.py")
        elif health["window_count"] == 0:
            print(f"\n[错误] 有历史数据 ({health['article_count']} 条) 但当前窗口内无文章")
            print(f"  最晚抓取时间: {health.get('latest_fetched_at', '未知')}")
            print(f"  窗口起始: {health.get('window_since', '未知')}")
            print("  可能原因: 本地数据库已停更，需要先同步远端状态或执行抓取")
            print("  同步命令: python3 scripts/sync_state_db.py")
        else:
            print("\n[警告] 数据库中无最近文章，无法生成 digest")
        return {"total_selected": 0}

    print(f"\n从数据库加载 {len(today_articles)} 条文章")

    # 转为 ContentItem 供后续 pipeline 使用
    merged_items = [
        article_to_content_item(a, url_hash_fn=db.url_hash)
        for a in today_articles
    ]

    sources = _load_sources(config)
    merged_items = _filter_articles_to_window(merged_items, since, until, config=config)
    merged_items, freshness_stats = _filter_items_by_freshness(merged_items, sources, config)
    print(f"固定窗口过滤后保留 {len(merged_items)} 条文章")
    if not merged_items:
        print("\n[警告] 固定日报窗口内无文章，无法生成 digest")
        return {"total_selected": 0}

    return _run_digest_phase(
        config,
        db,
        merged_items,
        ai_config,
        start_time,
        since,
        until,
        report_date,
        report_type=report_type,
        product_key=config.get("product_key", "news"),
        site_root=publish_cfg.get("site_root", "docs/news"),
        pipeline_context={
            "sources": sources,
            "freshness": freshness_stats,
            "raw_fetched": len(today_articles),
            "raw_merged": len(merged_items),
        },
    )


def _build_and_log_digest_report(
    *,
    config: dict,
    db: NewsDatabase,
    hard_news_scored: list[dict],
    fallback_candidates_by_column: dict[str, list[dict]],
    phase_metrics: dict,
    start_time: datetime,
    phase_start: datetime,
    window_since: datetime,
    window_until: datetime,
    report_date: str,
    report_type: str,
    product_key: str,
    site_root: str,
    daily_dir: str,
    feed_path: str,
    base_url: str,
    history_days: int,
    columns_cfg: dict,
    digest_cfg: dict,
    analysis_cfg: dict,
    ai_config: dict,
    merged_count: int,
    candidate_count: int,
    ai_duration: float,
    score_errors: list[str],
) -> dict:
    """构造并保存日报，供新评分路径和事件库复用路径共享。"""
    spec = ReportSpec(
        product_key=product_key,
        report_type=report_type,
        report_key=report_date,
        title=build_daily_title(report_date),
        since=window_since,
        until=window_until,
        site_root=site_root,
        output_dir=daily_dir,
        feed_path=feed_path,
        base_url=base_url,
        column_quotas=columns_cfg,
        word_count_min=digest_cfg.get("column", {}).get("target_word_count_min", 2500),
        word_count_max=digest_cfg.get("column", {}).get("target_word_count_max", 5000),
        highlights_limit=8,
        allow_headline_only=True,
        pub_date=_get_report_publish_time(report_date, config=config),
        history_days=history_days,
        min_llm_score=analysis_cfg.get("min_llm_score", 65),
        fallback_candidates_by_column=fallback_candidates_by_column,
    )

    try:
        stats = build_report(spec, hard_news_scored, config, ai_config, db, phase_metrics=phase_metrics)
    except Exception as exc:
        _log_digest_run(
            db,
            report_key=report_date,
            report_type=report_type,
            status="render_failed",
            window_since=window_since,
            window_until=window_until,
            input_count=merged_count,
            candidate_count=candidate_count,
            selected_count=len(hard_news_scored),
            ai_duration_seconds=ai_duration,
            error_count=len(score_errors) + 1,
            metrics={**phase_metrics, "render_error": str(exc)},
        )
        raise

    stats["total_fetched"] = merged_count
    metrics_payload = stats.get("metrics") or {
        **phase_metrics,
        "report": {
            "title": spec.title,
            "published_at": spec.pub_date.isoformat() if spec.pub_date else "",
            "duration_seconds": round((datetime.now() - start_time).total_seconds(), 1),
        },
    }
    metrics_path = _write_metrics_file(site_root, metrics_payload)
    stats.setdefault("outputs", {})["metrics"] = metrics_path
    stats["metrics"] = metrics_payload
    outputs = stats.get("outputs", {})
    _log_digest_run(
        db,
        report_key=report_date,
        report_type=report_type,
        status="ok" if stats.get("total_selected", 0) else "empty",
        window_since=window_since,
        window_until=window_until,
        input_count=merged_count,
        candidate_count=candidate_count,
        selected_count=stats.get("total_selected", 0),
        ai_duration_seconds=ai_duration,
        error_count=len(score_errors),
        output_md_path=str(outputs.get("markdown", "")),
        output_html_path=str(outputs.get("html", "")),
        metrics={
            **metrics_payload,
            "duration_seconds": round((datetime.now() - phase_start).total_seconds(), 1),
        },
    )
    return stats


def _run_digest_phase(
    config: dict,
    db: NewsDatabase,
    merged_items: list[ContentItem],
    ai_config: dict,
    start_time: datetime,
    window_since: datetime,
    window_until: datetime,
    report_date: str,
    report_type: str = "daily",
    product_key: str = "news",
    site_root: str = "docs/news",
    pipeline_context: dict | None = None,
) -> dict:
    """步骤 4-13：评分 + 分栏 digest + 输出"""
    phase_start = datetime.now()
    publish_cfg = config.get("publish", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    runtime_cfg = config.get("runtime", {})

    daily_dir = os.path.join(site_root, report_type)
    feed_path = publish_cfg.get("feed_path", "docs/feeds/news.xml")
    base_url = publish_cfg.get("base_url", "")
    history_days = analysis_cfg.get("history_context_days", 3)
    columns_cfg = digest_cfg.get("columns", {})
    schedule_cfg = _load_schedule_config(config)
    phase_metrics: dict[str, object] = {
        "schedule": schedule_cfg,
        "window": {
            "since": window_since.isoformat(),
            "until": window_until.isoformat(),
            "report_key": report_date,
        },
        "pipeline": pipeline_context or {},
        "cn_source_fetched": 0,
        "cn_source_selected": 0,
        "cn_source_selected_by_column": {},
        "columns": {},
        "ai": {"score_errors": len([]), "digest_failures": {}},
    }
    phase_metrics["cn_source_fetched"] = sum(1 for item in merged_items if _is_cn_source_item(item))

    # === 4. 预筛 + Pre-LLM 硬过滤 + AI score_batch ===
    print("\n[4/13] 预筛 + AI 批量评分...")
    prefiltered_by_column = _prefilter_items_for_scoring(merged_items, columns_cfg)
    for col_key, items in sorted(prefiltered_by_column.items()):
        print(f"   预筛 {col_key}: {len(items)} 条")
        phase_metrics["columns"].setdefault(col_key, {})["prefiltered"] = len(items)
    # Pre-LLM 硬过滤：在预筛后、送评前移除高置信度软新闻
    pre_llm_before = sum(len(v) for v in prefiltered_by_column.values())
    prefiltered_by_column = {
        col: _pre_llm_hard_filter(items, config)
        for col, items in prefiltered_by_column.items()
    }
    pre_llm_after = sum(len(v) for v in prefiltered_by_column.values())
    print(f"   Pre-LLM 过滤: {pre_llm_before} -> {pre_llm_after} 条")
    for col_key, items in sorted(prefiltered_by_column.items()):
        print(f"   送评 {col_key}: {len(items)} 条")
        phase_metrics["columns"].setdefault(col_key, {})["hard_news_candidates"] = len(items)

    candidates = _content_items_to_candidates(
        prefiltered_by_column,
        report_key=report_date,
        report_type=report_type,
    )
    candidate_upserts = db.upsert_article_candidates(candidates)
    phase_metrics["candidate_pool"] = {
        "upserted": candidate_upserts,
        "total": len(candidates),
    }
    print(f"   候选池记录: {len(candidates)} 条")

    reuse_events_cfg = runtime_cfg.get("reuse_report_events", True)
    next_report_date = (datetime.strptime(report_date, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
    stored_events = (
        db.fetch_report_events(report_date, next_report_date, report_type=report_type)
        if reuse_events_cfg and report_type == "daily"
        else []
    )
    if stored_events and _has_events_for_required_columns(stored_events, columns_cfg):
        print(f"   复用事件库: {len(stored_events)} 条，跳过 AI 评分")
        hard_news_scored = [_report_event_to_scored_dict(event) for event in stored_events]
        phase_metrics["report_events"] = {
            "reused": len(stored_events),
            "source": "report_events",
        }
        phase_metrics["ai"]["score_errors"] = 0
        phase_metrics["ai"]["score_duration_seconds"] = 0
        fallback_candidates_by_column = {
            col_key: [_content_item_to_report_candidate(item) for item in items]
            for col_key, items in prefiltered_by_column.items()
        }
        return _build_and_log_digest_report(
            config=config,
            db=db,
            hard_news_scored=hard_news_scored,
            fallback_candidates_by_column=fallback_candidates_by_column,
            phase_metrics=phase_metrics,
            start_time=start_time,
            phase_start=phase_start,
            window_since=window_since,
            window_until=window_until,
            report_date=report_date,
            report_type=report_type,
            product_key=product_key,
            site_root=site_root,
            daily_dir=daily_dir,
            feed_path=feed_path,
            base_url=base_url,
            history_days=history_days,
            columns_cfg=columns_cfg,
            digest_cfg=digest_cfg,
            analysis_cfg=analysis_cfg,
            ai_config=ai_config,
            merged_count=len(merged_items),
            candidate_count=len(candidates),
            ai_duration=0.0,
            score_errors=[],
        )

    entries_for_scoring, _ = _build_scoring_entries_by_column(prefiltered_by_column)
    print(f"   送评总数: {len(entries_for_scoring)} 条")
    score_start = datetime.now()
    scored_dicts, score_errors = asyncio.run(score_batch(entries_for_scoring, ai_config))
    ai_duration = (datetime.now() - score_start).total_seconds()
    print(f"   完成 {len(scored_dicts)} 条评分")
    phase_metrics["ai"]["score_errors"] = len(score_errors)
    phase_metrics["ai"]["score_duration_seconds"] = round(ai_duration, 1)

    # require_ai 时，评分失败必须退出
    require_ai = runtime_cfg.get("require_ai", True)
    if require_ai:
        candidate_count = len(entries_for_scoring)
        valid_count = _count_scored_entries(scored_dicts)
        min_coverage = float(runtime_cfg.get("min_score_coverage", 0.7))
        coverage = (valid_count / candidate_count) if candidate_count else 0.0

        if valid_count == 0 or coverage < min_coverage:
            print(f"\n[错误] AI 评分失败: errors={len(score_errors)}, "
                  f"valid={valid_count}/{candidate_count}, "
                  f"coverage={coverage:.0%}, required>={min_coverage:.0%}")
            for err in score_errors[:3]:
                print(f"   - {err}")
            _log_digest_run(
                db,
                report_key=report_date,
                report_type=report_type,
                status="ai_failed",
                window_since=window_since,
                window_until=window_until,
                input_count=len(merged_items),
                candidate_count=len(candidates),
                ai_duration_seconds=ai_duration,
                error_count=len(score_errors),
                metrics={**phase_metrics, "ai_coverage": coverage},
            )
            sys.exit(1)
        if score_errors:
            print(f"\n[警告] AI 评分存在部分批次异常，但覆盖率达标: "
                  f"errors={len(score_errors)}, valid={valid_count}/{candidate_count}, "
                  f"coverage={coverage:.0%}")
            for err in score_errors[:3]:
                print(f"   - {err}")

    # === 5. 更新数据库 LLM 评分 ===
    print("\n[5/13] 更新数据库 LLM 评分...")
    updated_count = db.update_llm_scores(scored_dicts)
    print(f"   更新 {updated_count} 条")

    # === 6. 硬新闻过滤 ===
    print("\n[6/13] 硬新闻过滤...")
    hard_news_scored = [entry for entry in scored_dicts if _is_hard_news_entry(entry)]
    event_upserts = db.upsert_report_events(
        _scored_entries_to_report_events(hard_news_scored, report_date, report_type)
    )
    phase_metrics["report_events"] = {
        "upserted": event_upserts,
        "hard_news": len(hard_news_scored),
    }
    fallback_candidates_by_column: dict[str, list[dict]] = {}
    for col_key, items in prefiltered_by_column.items():
        non_hard: list[dict] = []
        for item in items:
            non_hard.append(_content_item_to_report_candidate(item))
        fallback_candidates_by_column[col_key] = non_hard
    # 按栏目统计
    _hard_by_col: dict[str, int] = {}
    for entry in hard_news_scored:
        c = entry.get("column", "unknown")
        _hard_by_col[c] = _hard_by_col.get(c, 0) + 1
    print(f"   {len(scored_dicts)} -> {len(hard_news_scored)} 条")
    for c in sorted(_hard_by_col):
        print(f"     {c}: {_hard_by_col[c]} 条硬新闻")
        phase_metrics["columns"].setdefault(c, {})["hard_news_scored"] = _hard_by_col[c]

    cn_selected_by_column: dict[str, int] = {}
    for entry in hard_news_scored:
        if not _is_cn_source_entry(entry):
            continue
        col = entry.get("column", "unknown")
        cn_selected_by_column[col] = cn_selected_by_column.get(col, 0) + 1
    phase_metrics["cn_source_selected"] = sum(cn_selected_by_column.values())
    phase_metrics["cn_source_selected_by_column"] = cn_selected_by_column

    # === 7+. 构造 ReportSpec，委托 report_engine 完成后续阶段 ===
    return _build_and_log_digest_report(
        config=config,
        db=db,
        hard_news_scored=hard_news_scored,
        fallback_candidates_by_column=fallback_candidates_by_column,
        phase_metrics=phase_metrics,
        start_time=start_time,
        phase_start=phase_start,
        window_since=window_since,
        window_until=window_until,
        report_date=report_date,
        report_type=report_type,
        product_key=product_key,
        site_root=site_root,
        daily_dir=daily_dir,
        feed_path=feed_path,
        base_url=base_url,
        history_days=history_days,
        columns_cfg=columns_cfg,
        digest_cfg=digest_cfg,
        analysis_cfg=analysis_cfg,
        ai_config=ai_config,
        merged_count=len(merged_items),
        candidate_count=len(candidates),
        ai_duration=ai_duration,
        score_errors=score_errors,
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="观察日报 Pipeline v3")
    parser.add_argument("--hours", type=int, default=24, help="时间窗口（小时）")
    parser.add_argument("--fetch-only", action="store_true", help="只执行抓取入库（步骤 1-3）")
    parser.add_argument("--digest-only", action="store_true", help="只执行 digest 流程（步骤 4-13）")
    parser.add_argument("--report-type", default="daily", choices=["daily", "weekly", "monthly"], help="报告类型（默认 daily）")
    args = parser.parse_args()

    if args.fetch_only and args.digest_only:
        print("[错误] --fetch-only 和 --digest-only 不能同时使用")
        sys.exit(1)

    try:
        if args.fetch_only:
            stats = run_fetch_only(hours=args.hours)
        elif args.digest_only:
            stats = run_digest_only(hours=args.hours, report_type=args.report_type)
        else:
            stats = run_pipeline(hours=args.hours, report_type=args.report_type)

        if args.digest_only and stats.get("total_selected", 0) == 0:
            print("[错误] Digest 未生成任何内容")
            sys.exit(1)

        if stats.get("total_fetched", 0) == 0 and not args.digest_only:
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[错误] Pipeline 失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
