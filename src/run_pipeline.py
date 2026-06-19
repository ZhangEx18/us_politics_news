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
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_analyzer import (
    score_batch,
    _load_ai_config,
)
from database import NewsDatabase
from fetchers import (
    fetch_all_sources,
    merge_cross_source_duplicates,
    save_to_db,
    normalize_url,
)
from models import ContentItem, SourceType
from report_engine import ReportSpec, build_report


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
REPORT_CUTOFF_HOUR = 7
REPORT_PUBLISH_HOUR = 8


def _load_config() -> dict:
    path = os.path.join(_project_root, "config", "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_sources() -> list[dict]:
    path = os.path.join(_project_root, "config", "sources.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _augment_ai_config_with_runtime(ai_config: dict, config: dict) -> dict:
    """把 config.yaml 里的 LLM 运行参数注入 AI 配置。"""
    llm_cfg = config.get("llm", {})
    ai_config.update({
        "score_max_prompt_chars": llm_cfg.get("score_max_prompt_chars", llm_cfg.get("max_prompt_chars", 12000)),
        "score_max_concurrent": llm_cfg.get("score_max_concurrent", max(1, min(llm_cfg.get("max_concurrent", 3), 2))),
        "score_timeout_seconds": llm_cfg.get("score_timeout_seconds", llm_cfg.get("timeout_seconds", 180)),
        "score_content_chars": llm_cfg.get("score_content_chars", 400),
        "score_retry_split_depth": llm_cfg.get("score_retry_split_depth", 3),
        "digest_timeout_seconds": llm_cfg.get("digest_timeout_seconds", llm_cfg.get("timeout_seconds", 180)),
        "digest_content_chars": llm_cfg.get("digest_content_chars", 1000),
        "meta_timeout_seconds": llm_cfg.get("meta_timeout_seconds", 120),
    })
    return ai_config




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
            }
            for item in items
        ]
        by_column_entries[col_key] = entries
        all_entries.extend(entries)
    return all_entries, by_column_entries


def _get_report_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """固定晨报窗口：北京时间前一天 07:00 到当天 07:00。"""
    now_local = now.astimezone(BEIJING_TZ) if now and now.tzinfo else datetime.now(BEIJING_TZ)
    today_cutoff = now_local.replace(
        hour=REPORT_CUTOFF_HOUR,
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


def _get_report_publish_time(report_date: str) -> datetime:
    """晨报 RSS 发布时间固定为当天北京时间 08:00。"""
    report_day = datetime.strptime(report_date, "%Y-%m-%d").date()
    return datetime(
        report_day.year,
        report_day.month,
        report_day.day,
        REPORT_PUBLISH_HOUR,
        0,
        0,
        tzinfo=BEIJING_TZ,
    )


def _filter_articles_to_window(items: list[ContentItem], since: datetime, until: datetime) -> list[ContentItem]:
    """只保留日报固定窗口内抓取到的新闻。"""
    filtered: list[ContentItem] = []
    for item in items:
        ref = item.published_at or item.fetched_at
        if ref is None:
            continue
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        ref_local = ref.astimezone(BEIJING_TZ)
        if since <= ref_local < until:
            filtered.append(item)
    return filtered


def run_pipeline(hours: int = 24, report_type: str = "daily") -> dict:
    """完整流程：抓取 + 评分 + 分栏 digest"""
    start_time = datetime.now()
    since, until, report_date = _get_report_window()
    print(f"日报窗口: {since.strftime('%m-%d %H:%M')} → {until.strftime('%m-%d %H:%M')}")

    config = _load_config()
    output_cfg = config.get("output", {})
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    runtime_cfg = config.get("runtime", {})

    db_path = storage_cfg.get("db_path", "data/news.db")
    daily_dir = output_cfg.get("daily_dir", "docs/daily")
    feed_path = output_cfg.get("feed_path", "docs/feed.xml")
    base_url = output_cfg.get("base_url", "")
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

    db = NewsDatabase(db_path)

    # === 1. 并发抓取 ===
    print("\n[1/13] 并发抓取所有数据源...")
    sources = _load_sources()
    all_items = asyncio.run(fetch_all_sources(since, sources))
    print(f"   共抓取 {len(all_items)} 条")

    if not all_items:
        print("\n[警告] 未抓取到任何内容，pipeline 终止")
        return {"total_fetched": 0, "total_selected": 0}

    # === 2. 跨源 URL 去重 ===
    print("\n[2/13] 跨源 URL 去重...")
    merged_items = merge_cross_source_duplicates(all_items)
    print(f"   合并 {len(all_items) - len(merged_items)} 条 -> {len(merged_items)} 条唯一")

    # === 3. 入库 ===
    print("\n[3/13] 入库...")
    fetch_stats = save_to_db(merged_items, db)
    print(f"   新增 {sum(fetch_stats.values())} 条")

    # === 4-13: digest 流程 ===
    return _run_digest_phase(config, db, merged_items, ai_config, start_time, since, until, report_date, report_type=report_type)


def run_fetch_only(hours: int = 24) -> dict:
    """只执行抓取入库（步骤 1-3）"""
    start_time = datetime.now()
    since = start_time - timedelta(hours=hours)

    config = _load_config()
    storage_cfg = config.get("storage", {})
    db_path = storage_cfg.get("db_path", "data/news.db")

    print("=" * 60)
    print("观察日报 Pipeline — 抓取模式")
    print(f"时间窗口: 最近 {hours} 小时")
    print("=" * 60)

    db = NewsDatabase(db_path)

    # 1. 并发抓取
    print("\n[1/3] 并发抓取所有数据源...")
    sources = _load_sources()
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
    since, until, report_date = _get_report_window(start_time.replace(tzinfo=BEIJING_TZ))

    config = _load_config()
    storage_cfg = config.get("storage", {})
    db_path = storage_cfg.get("db_path", "data/news.db")

    # 从环境变量加载 AI 配置
    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)

    print("=" * 60)
    print("观察日报 Pipeline — Digest 模式")
    print("=" * 60)

    db = NewsDatabase(db_path)

    # 从数据库读取固定晨报窗口文章，补跑不改变窗口
    today_articles = db.fetch_since(since.astimezone(timezone.utc).replace(tzinfo=None))
    if not today_articles:
        print("\n[警告] 数据库中无最近文章，无法生成 digest")
        return {"total_selected": 0}

    print(f"\n从数据库加载 {len(today_articles)} 条文章")

    # 转为 dict 格式供 score_batch 使用
    merged_items = [
        ContentItem(
            id=f"db:{db.url_hash(normalize_url(a.url))}",
            source_type=SourceType(a.source_type) if a.source_type else SourceType.RSS,
            title=a.title,
            url=a.url,
            content=a.summary or "",
            source_name=a.source,
            published_at=a.published_at,
            column=a.column or "",
            source_tier=a.source_tier or 4,
            event_key=a.event_key or "",
            source_url_normalized=a.source_url_normalized or normalize_url(a.url),
            topic=a.topic or "",
            score=a.score or 0.0,
            reason=a.reason or "",
            level=a.level or "",
        )
        for a in today_articles
    ]

    merged_items = _filter_articles_to_window(merged_items, since, until)
    print(f"固定窗口过滤后保留 {len(merged_items)} 条文章")
    if not merged_items:
        print("\n[警告] 固定日报窗口内无文章，无法生成 digest")
        return {"total_selected": 0}

    return _run_digest_phase(config, db, merged_items, ai_config, start_time, since, until, report_date, report_type=report_type)


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
) -> dict:
    """步骤 4-13：评分 + 分栏 digest + 输出"""
    output_cfg = config.get("output", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    runtime_cfg = config.get("runtime", {})

    daily_dir = output_cfg.get("daily_dir", "docs/daily")
    feed_path = output_cfg.get("feed_path", "docs/feed.xml")
    base_url = output_cfg.get("base_url", "")
    history_days = analysis_cfg.get("history_context_days", 3)
    columns_cfg = digest_cfg.get("columns", {})

    # === 4. 预筛 + Pre-LLM 硬过滤 + AI score_batch ===
    print("\n[4/13] 预筛 + AI 批量评分...")
    prefiltered_by_column = _prefilter_items_for_scoring(merged_items, columns_cfg)
    for col_key, items in sorted(prefiltered_by_column.items()):
        print(f"   预筛 {col_key}: {len(items)} 条")
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

    entries_for_scoring, _ = _build_scoring_entries_by_column(prefiltered_by_column)
    print(f"   送评总数: {len(entries_for_scoring)} 条")
    scored_dicts, score_errors = asyncio.run(score_batch(entries_for_scoring, ai_config))
    print(f"   完成 {len(scored_dicts)} 条评分")

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
    # 按栏目统计
    _hard_by_col: dict[str, int] = {}
    for entry in hard_news_scored:
        c = entry.get("column", "unknown")
        _hard_by_col[c] = _hard_by_col.get(c, 0) + 1
    print(f"   {len(scored_dicts)} -> {len(hard_news_scored)} 条")
    for c in sorted(_hard_by_col):
        print(f"     {c}: {_hard_by_col[c]} 条硬新闻")

    # === 7+. 构造 ReportSpec，委托 report_engine 完成后续阶段 ===
    dt_val = datetime.strptime(report_date, "%Y-%m-%d")
    spec = ReportSpec(
        report_type=report_type,
        report_key=report_date,
        title=f"{dt_val.year}年{dt_val.month}月{dt_val.day}日日报",
        since=window_since,
        until=window_until,
        output_dir=daily_dir,
        feed_path=feed_path,
        base_url=base_url,
        column_quotas=columns_cfg,
        word_count_min=digest_cfg.get("column", {}).get("target_word_count_min", 2500),
        word_count_max=digest_cfg.get("column", {}).get("target_word_count_max", 5000),
        highlights_limit=8,
        allow_headline_only=True,
        pub_date=_get_report_publish_time(report_date),
        history_days=history_days,
        min_llm_score=analysis_cfg.get("min_llm_score", 65),
    )

    stats = build_report(spec, hard_news_scored, config, ai_config, db)

    # 补充 pipeline 阶段统计
    stats["total_fetched"] = len(merged_items)
    return stats


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
