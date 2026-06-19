#!/usr/bin/env python3
"""
四维日报 Pipeline v3 — 分栏生成流程

1.  并发抓取
2.  跨源 URL 去重
3.  入库
3.5 Pre-LLM 硬过滤（移除高置信度软新闻）
4.  预筛 + AI score_batch
5.  更新数据库 LLM 评分
6.  硬新闻过滤
7.  事件级合并（merge_events）
8.  min_llm_score 过滤
9.  按四栏分桶
10. 每栏按配额选择候选
11. 每栏单独 generate_column_digest
12. _build_reader_highlights 提炼今日要点
12.5 质量门禁（标签/套话清理 + 句数/字数验证）
13. 代码模板组装 + save_daily_report
14. save_feed

支持模式：
  默认       完整流程（1-14）
  --fetch-only   只执行步骤 1-3
  --digest-only  只执行步骤 4-14（从数据库读取）
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_analyzer import (
    score_batch,
    generate_column_digest,
    has_ai_config,
    merge_events,
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
from report_renderer import save_daily_report
from feed_builder import save_feed


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
REPORT_CUTOFF_HOUR = 7
REPORT_PUBLISH_HOUR = 8


def _load_config() -> dict:
    path = os.path.join(_project_root, "config", "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def _scored_dicts_to_content_items(
    scored: list[dict],
    original_items: list[ContentItem],
) -> list[ContentItem]:
    url_index: dict[str, ContentItem] = {}
    for item in original_items:
        url_index[str(item.url)] = item

    result: list[ContentItem] = []
    for entry in scored:
        link = entry.get("link", "")
        original = url_index.get(link)

        if original:
            updated = original.model_copy(update={
                "score": entry.get("score", original.score),
                "column": entry.get("column", original.column),
                "topic": ",".join(entry.get("tags", [])) if entry.get("tags") else original.topic,
                "content": entry.get("summary", original.content),
                "event_key": entry.get("event_key", original.event_key),
            })
            result.append(updated)
        else:
            result.append(ContentItem(
                id=f"scored:{link}",
                source_type=SourceType.RSS,
                title=entry.get("title", ""),
                url=link,
                content=entry.get("summary", entry.get("content", "")),
                source_name=entry.get("source", ""),
                published_at=None,
                column=entry.get("column", ""),
                topic=",".join(entry.get("tags", [])),
                score=entry.get("score", 0),
                event_key=entry.get("event_key", ""),
                metadata={"merged_sources": entry.get("source_links", [])},
            ))

    return result


def _load_history_context(db: NewsDatabase, days: int = 3) -> str:
    """加载近 N 天已推送事件文本，用于 digest prompt 去重"""
    since = datetime.now() - timedelta(days=days)
    articles = db.fetch_since(since)
    if not articles:
        return ""

    lines = []
    for a in articles[:30]:
        tags = a.llm_tags or ""
        lines.append(
            f"[score: {a.llm_score or 0}] title:{a.title}\n"
            f"published: {a.published_at or ''}\ntags: {tags}\n"
            f"source: {a.source}\nsummary: {(a.llm_summary or a.summary or '')[:120]}"
        )
    return "\n\n".join(lines)


def _assign_level(score: float, important_threshold: float = 85) -> str:
    """根据 LLM 评分分配 level"""
    if score >= important_threshold:
        return "重点"
    return "观察"


_COLUMN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "us_politics": ("white house", "trump", "biden", "senate", "house", "supreme court", "congress", "election"),
    "global_affairs": ("china", "iran", "israel", "ukraine", "russia", "g7", "nato", "diplom"),
    "technology": ("ai", "openai", "chip", "semiconductor", "tesla", "meta", "google", "microsoft"),
    "economy": ("fed", "inflation", "tariff", "jobs", "market", "bond", "trade", "gdp"),
}


# ── 质量门禁 ──

# 禁止的标签（严重问题，触发重写）
_FORBIDDEN_LABELS: list[str] = [
    "核心事实：", "核心事实:",
    "背景脉络：", "背景脉络:",
    "背景与影响：", "背景与影响:",
    "可能影响：", "可能影响:",
    "为什么值得关注：", "为什么值得关注:",
]

# 禁止的套话（清理后记录）
_FORBIDDEN_PHRASES: list[str] = [
    "据报道", "据悉", "有消息称",
    "值得注意的是", "需要指出的是",
    "凸显了", "反映了", "意味着", "标志着",
    "引发了讨论", "增添了变数", "存在不确定性",
    "产生深远影响", "仍需观察",
    "对于读者来说", "值得关注的是",
]


def _sanitize_event_text(text: str) -> tuple[str, list[str]]:
    """清理单条事件正文，返回 (清理后文本, 问题列表)。"""
    issues: list[str] = []
    cleaned = text

    # 检查标签残留（严重问题）
    for label in _FORBIDDEN_LABELS:
        if label in cleaned:
            issues.append(f"标签残留: {label}")
            cleaned = cleaned.replace(label, "")

    # 检查套话（清理并记录）
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in cleaned:
            issues.append(f"禁用套话: {phrase}")
            cleaned = cleaned.replace(phrase, "")

    # 清理多余空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned, issues


def _validate_event(event: dict) -> list[str]:
    """验证单条事件的质量，返回问题列表（空 = 合格）。"""
    issues: list[str] = []
    body = str(event.get("reader_body", "")).strip()

    if not body:
        issues.append("reader_body 为空")
        return issues

    # 句数检查（按中文句号、问号、感叹号分句）
    sentences = re.split(r"[。！？!?]", body)
    sentences = [s for s in sentences if s.strip()]
    if len(sentences) < 2:
        issues.append(f"句数不足: {len(sentences)} 句（要求 2-4 句）")
    elif len(sentences) > 4:
        issues.append(f"句数过多: {len(sentences)} 句（要求 2-4 句）")

    # 字数检查
    char_count = len(body)
    if char_count < 80:
        issues.append(f"字数过少: {char_count} 字（要求 80-260 字）")
    elif char_count > 260:
        issues.append(f"字数过多: {char_count} 字（要求 80-260 字）")

    return issues


def sanitize_or_validate_events(events: list[dict]) -> tuple[list[dict], list[str]]:
    """清理并验证事件列表，返回 (清理后事件, 全部问题)。"""
    all_issues: list[str] = []
    cleaned_events: list[dict] = []

    for i, event in enumerate(events):
        title = event.get("title_zh", f"事件{i+1}")

        # 清理正文
        body = str(event.get("reader_body", "")).strip()
        cleaned_body, sanitize_issues = _sanitize_event_text(body)
        if sanitize_issues:
            for issue in sanitize_issues:
                all_issues.append(f"[{title}] {issue}")
            event = {**event, "reader_body": cleaned_body}
            # 同步 core_facts
            if event.get("core_facts") == body:
                event["core_facts"] = cleaned_body

        # 验证
        validate_issues = _validate_event(event)
        for issue in validate_issues:
            all_issues.append(f"[{title}] {issue}")

        # 空 reader_body 是严重问题，跳过该事件
        if not cleaned_body.strip():
            all_issues.append(f"[{title}] 严重: reader_body 清理后为空，已移除")
            continue

        cleaned_events.append(event)

    return cleaned_events, all_issues


# ── Pre-LLM 硬过滤 ──

_SOFT_NEWS_KEYWORDS: list[str] = [
    # 英文
    "celebrity", "sports", "entertainment", "movie", "music", "lifestyle",
    "watchlist", "stock to watch", "buy rating", "price target",
    "opinion", "editorial", "column", "reaction", "mocked", "blasted",
    "viral", "awkward moment", "red carpet", "box office",
    # 中文
    "观察名单", "荐股", "买入评级", "目标价", "娱乐", "体育",
    "明星", "网友热议", "尴尬瞬间", "语无伦次", "直播中断",
]

_HARD_NEWS_KEYWORDS: list[str] = [
    "court", "supreme court", "ruling", "lawsuit", "congress", "senate",
    "white house", "executive order", "regulation", "sanctions", "tariff",
    "fed", "federal reserve", "inflation", "jobs", "earnings", "revenue",
    "chip", "ai ", "artificial intelligence", "semiconductor",
    "military", "ceasefire", "nato", "g7", "diplomacy", "treaty",
    "法院", "最高法院", "国会", "白宫", "行政命令", "制裁",
    "美联储", "通胀", "就业", "财报", "芯片", "半导体",
]


def _pre_llm_hard_filter(items: list[ContentItem]) -> list[ContentItem]:
    """Pre-LLM 硬过滤：移除高置信度软新闻，保留不确定内容交给 LLM。"""
    filtered: list[ContentItem] = []
    for item in items:
        text = f"{item.title} {item.content or ''}".lower()
        # 硬新闻关键词命中 → 直接保留
        if any(kw in text for kw in _HARD_NEWS_KEYWORDS):
            filtered.append(item)
            continue
        # 软新闻关键词命中 → 过滤
        if any(kw in text for kw in _SOFT_NEWS_KEYWORDS):
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


async def _generate_all_column_digests(
    columns_cfg: dict[str, dict],
    column_candidates: dict[str, list[dict]],
    history_context: str,
    ai_config: dict,
    word_count_min: int,
    word_count_max: int,
) -> dict[str, list[dict]]:
    """并发生成四栏 digest，缩短总发布耗时。"""
    semaphore = asyncio.Semaphore(4)

    async def _generate(col_key: str, col_cfg: dict) -> tuple[str, list[dict]]:
        candidates = column_candidates.get(col_key, [])
        if not candidates:
            print(f"   {col_key}: 无候选，跳过")
            return col_key, []

        print(f"   {col_key}: 生成中 ({len(candidates)} 条候选)...")
        async with semaphore:
            events = await generate_column_digest(
                column_key=col_key,
                column_label=col_cfg.get("label", col_key),
                events=candidates,
                history_context=history_context,
                ai_config=ai_config,
                word_count_min=word_count_min,
                word_count_max=word_count_max,
            )
        print(f"   {col_key}: 生成 {len(events)} 条事件卡片")
        return col_key, events

    results = await asyncio.gather(*[
        _generate(col_key, col_cfg) for col_key, col_cfg in columns_cfg.items()
    ])
    return {col_key: events for col_key, events in results if events}


def _build_reader_highlights(columns: dict[str, list[dict]], limit: int = 8) -> list[str]:
    """从最终入选事件直接提炼 Reader 顶部今日要点，避免额外 AI 调用。"""
    highlights: list[str] = []
    for events in columns.values():
        for event in events:
            title = str(event.get("title_zh", "")).strip()
            core = event.get("core_facts", "")
            if isinstance(core, list):
                core = " ".join(str(part).strip() for part in core if str(part).strip())
            core = str(core).strip()

            if title:
                text = title
            else:
                text = core[:45]

            text = re.sub(r"\s+", " ", text).strip("：:，,。. ")
            if not text:
                continue
            if len(text) > 45:
                text = text[:45].rstrip() + "…"
            if text not in highlights:
                highlights.append(text)
            if len(highlights) >= limit:
                return highlights
    return highlights


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
    print("四维日报 Pipeline v3")
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
    all_items = asyncio.run(fetch_all_sources(since))
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
    print("四维日报 Pipeline — 抓取模式")
    print(f"时间窗口: 最近 {hours} 小时")
    print("=" * 60)

    db = NewsDatabase(db_path)

    # 1. 并发抓取
    print("\n[1/3] 并发抓取所有数据源...")
    all_items = asyncio.run(fetch_all_sources(since))
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
    print("四维日报 Pipeline — Digest 模式")
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
            id=f"db:{db.url_hash(db.normalize_url(a.url))}",
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
    # 保存预筛计数（Pre-LLM 过滤前），用于阶段统计
    prefilter_counts = {col: len(items) for col, items in prefiltered_by_column.items()}

    # Pre-LLM 硬过滤：在预筛后、送评前移除高置信度软新闻
    pre_llm_before = sum(len(v) for v in prefiltered_by_column.values())
    prefiltered_by_column = {
        col: _pre_llm_hard_filter(items)
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

    # === 7. 事件级合并 ===
    print("\n[7/13] 事件级合并（按 event_key）...")
    merged_events = merge_events(hard_news_scored)
    # 按栏目统计
    _merged_by_col: dict[str, int] = {}
    for entry in merged_events:
        c = entry.get("column", "unknown")
        _merged_by_col[c] = _merged_by_col.get(c, 0) + 1
    print(f"   {len(hard_news_scored)} 条 -> {len(merged_events)} 个事件")
    for c in sorted(_merged_by_col):
        print(f"     {c}: {_merged_by_col[c]} 个事件")

    # === 8. min_llm_score 过滤 ===
    min_llm_score = analysis_cfg.get("min_llm_score", 65)
    print(f"\n[8/13] min_llm_score 过滤 (阈值={min_llm_score})...")
    content_items = _scored_dicts_to_content_items(merged_events, merged_items)
    content_items.sort(key=lambda x: x.score or 0, reverse=True)
    filtered_items = [it for it in content_items if (it.score or 0) >= min_llm_score]
    # 按栏目统计
    _filtered_by_col: dict[str, int] = {}
    for item in filtered_items:
        c = item.column or "unknown"
        _filtered_by_col[c] = _filtered_by_col.get(c, 0) + 1
    print(f"   {len(content_items)} -> {len(filtered_items)} 条")
    for c in sorted(_filtered_by_col):
        print(f"     {c}: {_filtered_by_col[c]} 条")

    total_min_items = digest_cfg.get("total_min_items", 20)
    if len(filtered_items) < total_min_items:
        print(f"   警告: 过滤后 {len(filtered_items)} 条 < 最低要求 {total_min_items}，继续运行")

    # === 9. 按四栏分桶 ===
    print("\n[9/13] 按栏目分桶...")
    by_column: dict[str, list[ContentItem]] = {}
    for item in filtered_items:
        col = item.column or "us_politics"
        by_column.setdefault(col, []).append(item)
    for col in by_column:
        by_column[col].sort(key=lambda x: x.score or 0, reverse=True)
    for col, items in sorted(by_column.items()):
        print(f"   {col}: {len(items)} 条")

    # === 10. 每栏按配额选择候选（双样式：编号条目 + 无序条目）===
    print("\n[10/13] 每栏按配额选择候选...")
    column_candidates: dict[str, list[dict]] = {}
    column_headline_only: dict[str, list[dict]] = {}
    for col_key, col_cfg in columns_cfg.items():
        col_items = by_column.get(col_key, [])
        detailed_n = col_cfg.get("target_items", 5)
        max_n = col_cfg.get("max_items", detailed_n)
        headline_n = col_cfg.get("headline_items", 0)

        # 编号条目：取 top N
        detailed_items = col_items[:min(len(col_items), max_n)]
        # 无序条目：跳过编号条目，取接下来的 M 条
        remaining_items = col_items[len(detailed_items):]
        headline_items = remaining_items[:headline_n]

        column_candidates[col_key] = [
            {
                "title": it.title, "source": it.source_name,
                "score": it.score, "summary": it.content or "",
                "content": it.content or "",
                "source_links": (it.metadata or {}).get("merged_sources", []),
            }
            for it in detailed_items
        ]
        column_headline_only[col_key] = [
            {"title": it.title, "score": it.score}
            for it in headline_items
        ]

        if len(detailed_items) < col_cfg.get("min_items", 0):
            print(f"   警告: {col_key} 仅 {len(detailed_items)} 条编号 < 最低要求 {col_cfg.get('min_items', 0)}")
        print(f"   {col_key}: 编号 {len(detailed_items)} 条 + 无序 {len(headline_items)} 条")

    # === 11. 每栏单独 generate_column_digest ===
    print("\n[11/13] 每栏生成 digest...")
    history_context = _load_history_context(db, history_days)
    column_word_min = digest_cfg.get("column", {}).get("target_word_count_min", 5000)
    column_word_max = digest_cfg.get("column", {}).get("target_word_count_max", 7000)
    column_results = asyncio.run(_generate_all_column_digests(
        columns_cfg=columns_cfg,
        column_candidates=column_candidates,
        history_context=history_context,
        ai_config=ai_config,
        word_count_min=column_word_min,
        word_count_max=column_word_max,
    ))

    # === 12. 提炼今日要点 ===
    print("\n[12/13] 提炼今日要点...")
    highlights = _build_reader_highlights(column_results, limit=8)
    print(f"   今日要点: {len(highlights)} 条")

    # === 12.5 质量门禁 ===
    print("\n[12.5/13] 质量门禁检查...")
    total_issues = 0
    for col_key in list(column_results.keys()):
        events = column_results[col_key]
        if not events:
            continue
        cleaned, issues = sanitize_or_validate_events(events)
        if issues:
            for issue in issues:
                print(f"   ⚠ {col_key}: {issue}")
            total_issues += len(issues)
        column_results[col_key] = cleaned
    if total_issues:
        print(f"   共 {total_issues} 个质量问题（已清理）")
    else:
        print("   全部通过")

    # === 13. 代码模板组装 + save_daily_report ===
    print("\n[13/13] 保存日报文件...")
    dt = datetime.strptime(report_date, "%Y-%m-%d")
    meta = {
        "title": f"{dt.year}年{dt.month}月{dt.day}日日报" if report_type == "daily" else f"{dt.year}年{dt.month}月{dt.day}日 新闻",
        "lead": "",
        "highlights": highlights,
        "date": report_date,
        "report_since": window_since.isoformat(),
        "report_until": window_until.isoformat(),
        "pub_date": _get_report_publish_time(report_date).isoformat(),
    }

    # 组装 columns dict（按 COLUMN_ORDER 排序，无数据的栏位填空列表）
    # 日报使用双样式结构：detailed_events + headline_only_events
    # 周报/月报只有 detailed_events
    from report_renderer import COLUMN_ORDER
    columns: dict[str, dict[str, list[dict]]] = {}
    for col_key in COLUMN_ORDER:
        columns[col_key] = {
            "detailed_events": column_results.get(col_key, []),
            "headline_only_events": column_headline_only.get(col_key, []) if report_type == "daily" else [],
        }

    md_path, html_path = save_daily_report(meta, columns, daily_dir, report_type=report_type)
    print(f"   Markdown: {md_path}")
    print(f"   HTML: {html_path}")

    # === 14. save_feed ===
    print("\n[14/13] 保存 RSS Feed...")
    save_feed(meta, columns, feed_path, base_url, report_type=report_type)
    print(f"   Feed: {feed_path}")

    # 统计
    duration = (datetime.now() - start_time).total_seconds()
    total_events = sum(len(evts) for evts in column_results.values())
    col_counts = {k: len(v) for k, v in column_results.items()}

    # 阶段统计：每栏从预筛到最终生成的数量分解
    stage_stats: dict[str, dict[str, int]] = {}
    for col_key in columns_cfg:
        prefilter_n = prefilter_counts.get(col_key, 0)
        pre_llm_n = len(prefiltered_by_column.get(col_key, []))
        score_input_n = sum(1 for e in entries_for_scoring if e.get("column_hint") == col_key)
        score_valid_n = sum(1 for e in scored_dicts if e.get("column") == col_key and e.get("event_key"))
        hard_n = sum(1 for e in hard_news_scored if e.get("column") == col_key)
        merged_n = sum(1 for e in merged_events if e.get("column") == col_key)
        filtered_n = sum(1 for it in filtered_items if (it.column or "") == col_key)
        final_n = col_counts.get(col_key, 0)
        stage_stats[col_key] = {
            "prefilter": prefilter_n,
            "pre_llm": pre_llm_n,
            "score_input": score_input_n,
            "score_valid": score_valid_n,
            "hard_news": hard_n,
            "merged": merged_n,
            "min_score_kept": filtered_n,
            "final": final_n,
        }

    stats = {
        "duration_seconds": round(duration, 1),
        "total_fetched": len(merged_items),
        "total_events": len(merged_events),
        "total_selected": total_events,
        "column_counts": col_counts,
        "stage_stats": stage_stats,
        "outputs": {"markdown": md_path, "html": html_path, "feed": feed_path},
    }

    print("\n" + "=" * 60)
    print("Pipeline 完成")
    print("=" * 60)
    print(f"  耗时: {duration:.1f}s")
    print(f"  事件: {len(merged_events)} -> 精选: {total_events}")
    for col, cnt in sorted(col_counts.items()):
        print(f"    {col}: {cnt}")
    print(f"  输出: {daily_dir}/")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="四维日报 Pipeline v3")
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
