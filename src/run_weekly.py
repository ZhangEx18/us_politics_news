#!/usr/bin/env python3
"""周报生成管线 — 从数据库读取 7 天已评分文章，按栏目聚合生成周报

流程：
1.  计算周报时间窗口（上周一 07:00 → 本周一 07:00）
2.  从数据库 fetch_since 拉取已评分文章
3.  转为 ContentItem 并过滤到窗口内
4.  事件级合并（merge_events）
5.  min_llm_score 过滤
6.  按栏目分组
7.  每栏按配额选择候选
8.  每栏单独 generate_column_digest
9.  提炼本周要点
10. 质量门禁
11. 保存到 docs/weekly/
12. 更新 feed.xml
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

from database import NewsDatabase
from ai_analyzer import generate_column_digest, merge_events, _load_ai_config
from models import ContentItem, SourceType
from report_renderer import save_daily_report, COLUMN_ORDER
from feed_builder import save_feed

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


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


def _build_reader_highlights(columns: dict[str, list[dict]], limit: int = 10) -> list[str]:
    """从最终入选事件直接提炼本周要点。"""
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


def run_weekly() -> dict:
    """生成周报"""
    start_time = datetime.now()
    config = _load_config()
    output_cfg = config.get("output", {})
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})

    db_path = storage_cfg.get("db_path", "data/news.db")
    weekly_dir = output_cfg.get("weekly_dir", "docs/weekly")
    feed_path = output_cfg.get("feed_path", "docs/feed.xml")
    base_url = output_cfg.get("base_url", "")
    history_days = analysis_cfg.get("history_context_days", 3)
    columns_cfg = digest_cfg.get("columns", {})

    # 从环境变量加载 AI 配置
    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)

    print("=" * 60)
    print("四维周报 Pipeline")
    print(f"时间: {start_time.isoformat()}")
    print("=" * 60)

    # === 1. 计算时间窗口 ===
    since, until, report_key = _get_weekly_window()
    week_num = since.isocalendar()[1]
    print(f"\n[1/12] 周报窗口: {since.strftime('%m-%d %H:%M')} → {until.strftime('%m-%d %H:%M')}")
    print(f"   报告标识: {report_key}")

    # === 2. 从数据库读取文章 ===
    db = NewsDatabase(db_path)
    articles = db.fetch_since(since.replace(tzinfo=None))
    print(f"\n[2/12] 从数据库加载 {len(articles)} 条文章")

    if not articles:
        print("\n[警告] 数据库中无最近文章，无法生成周报")
        return {"total_selected": 0}

    # === 3. 转为 ContentItem 并过滤到窗口内 ===
    from fetchers import normalize_url
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
        for a in articles
    ]

    # 过滤到周报窗口
    filtered_items: list[ContentItem] = []
    for item in merged_items:
        ref = item.published_at or item.fetched_at
        if ref is None:
            continue
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        ref_local = ref.astimezone(BEIJING_TZ)
        if since <= ref_local < until:
            filtered_items.append(item)

    print(f"   窗口过滤后保留 {len(filtered_items)} 条文章")
    if not filtered_items:
        print("\n[警告] 周报窗口内无文章，无法生成周报")
        return {"total_selected": 0}

    # === 3.5 历史上下文（用于 digest 去重） ===
    history_context = _load_history_context(db, history_days)

    # === 4. 事件级合并 ===
    print(f"\n[4/12] 事件级合并...")
    # 构建 dict 格式供 merge_events 使用
    event_dicts = [
        {
            "link": str(it.url),
            "title": it.title,
            "source": it.source_name,
            "score": it.score,
            "summary": it.content or "",
            "content": it.content or "",
            "event_key": it.event_key or "",
            "column": it.column or "",
            "source_tier": it.source_tier or 4,
        }
        for it in filtered_items
    ]
    merged_events = merge_events(event_dicts)
    print(f"   {len(event_dicts)} 条 → {len(merged_events)} 个事件")

    # === 5. min_llm_score 过滤 ===
    min_llm_score = analysis_cfg.get("min_llm_score", 65)
    print(f"\n[5/12] min_llm_score 过滤 (阈值={min_llm_score})...")
    high_score_events = [
        e for e in merged_events if (e.get("score") or 0) >= min_llm_score
    ]
    print(f"   {len(merged_events)} → {len(high_score_events)} 条")

    # === 6. 按栏目分组 ===
    print(f"\n[6/12] 按栏目分组...")
    by_column: dict[str, list[dict]] = {}
    for e in high_score_events:
        col = e.get("column", "us_politics")
        by_column.setdefault(col, []).append(e)
    for col in by_column:
        by_column[col].sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
    for col in sorted(by_column):
        print(f"   {col}: {len(by_column[col])} 条")

    # === 7. 每栏按配额选择候选 ===
    print(f"\n[7/12] 每栏按配额选择候选...")
    column_candidates: dict[str, list[dict]] = {}
    # 周报配额：比日报宽松，覆盖一周积累
    weekly_max_items = {
        "us_politics": 15,
        "global_affairs": 15,
        "technology": 12,
        "economy": 12,
    }
    for col_key in columns_cfg:
        col_items = by_column.get(col_key, [])
        max_n = weekly_max_items.get(col_key, 12)
        candidates = col_items[:min(len(col_items), max_n)]
        column_candidates[col_key] = [
            {
                "title": e.get("title", ""),
                "source": e.get("source", ""),
                "score": e.get("score", 0),
                "summary": e.get("summary", ""),
                "content": e.get("content", ""),
                "source_links": e.get("source_links", []),
            }
            for e in candidates
        ]
        print(f"   {col_key}: {len(candidates)} 条 (max={max_n})")

    # === 8. 每栏单独 generate_column_digest ===
    print(f"\n[8/12] 每栏生成 digest...")
    weekly_wc = digest_cfg.get("weekly", {})
    column_word_min = weekly_wc.get("target_word_count_min", 8000)
    column_word_max = weekly_wc.get("target_word_count_max", 16000)
    column_results = asyncio.run(_generate_all_column_digests(
        columns_cfg=columns_cfg,
        column_candidates=column_candidates,
        history_context=history_context,
        ai_config=ai_config,
        word_count_min=column_word_min,
        word_count_max=column_word_max,
    ))

    # === 9. 提炼本周要点 ===
    print(f"\n[9/12] 提炼本周要点...")
    highlights = _build_reader_highlights(column_results, limit=10)
    print(f"   本周要点: {len(highlights)} 条")

    # === 10. 质量门禁 ===
    from run_pipeline import sanitize_or_validate_events
    print(f"\n[10/12] 质量门禁检查...")
    total_issues = 0
    for col_key in list(column_results.keys()):
        events = column_results[col_key]
        if not events:
            continue
        cleaned, issues = sanitize_or_validate_events(events)
        if issues:
            for issue in issues:
                print(f"   {col_key}: {issue}")
            total_issues += len(issues)
        column_results[col_key] = cleaned
    if total_issues:
        print(f"   共 {total_issues} 个质量问题（已清理）")
    else:
        print("   全部通过")

    # === 11. 保存周报文件 ===
    print(f"\n[11/12] 保存周报文件...")
    year = since.year
    title = f"{year}年第{week_num}周周报"
    meta = {
        "title": title,
        "lead": "",
        "highlights": highlights,
        "date": report_key,
        "report_since": since.isoformat(),
        "report_until": until.isoformat(),
        "pub_date": until.isoformat(),
    }

    # 组装 columns dict（按 COLUMN_ORDER 排序）
    columns: dict[str, list[dict]] = {}
    for col_key in COLUMN_ORDER:
        columns[col_key] = column_results.get(col_key, [])

    md_path, html_path = save_daily_report(
        meta, columns, weekly_dir, report_type="weekly",
    )
    print(f"   Markdown: {md_path}")
    print(f"   HTML: {html_path}")

    # === 12. 更新 RSS Feed ===
    print(f"\n[12/12] 保存 RSS Feed...")
    save_feed(
        meta, columns, feed_path, base_url,
        report_type="weekly", report_key=report_key,
    )
    print(f"   Feed: {feed_path}")

    # 统计
    duration = (datetime.now() - start_time).total_seconds()
    total_events = sum(len(evts) for evts in column_results.values())
    col_counts = {k: len(v) for k, v in column_results.items()}

    stats = {
        "duration_seconds": round(duration, 1),
        "total_articles": len(filtered_items),
        "total_events": len(merged_events),
        "total_selected": total_events,
        "report_key": report_key,
        "column_counts": col_counts,
        "outputs": {"markdown": md_path, "html": html_path, "feed": feed_path},
    }

    print("\n" + "=" * 60)
    print("周报生成完成")
    print("=" * 60)
    print(f"  耗时: {duration:.1f}s")
    print(f"  文章: {len(filtered_items)} → 事件: {len(merged_events)} → 精选: {total_events}")
    for col, cnt in sorted(col_counts.items()):
        print(f"    {col}: {cnt}")
    print(f"  输出: {weekly_dir}/")

    return stats


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


async def _generate_all_column_digests(
    columns_cfg: dict[str, dict],
    column_candidates: dict[str, list[dict]],
    history_context: str,
    ai_config: dict,
    word_count_min: int,
    word_count_max: int,
) -> dict[str, list[dict]]:
    """并发生成四栏 digest"""
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


def main():
    stats = run_weekly()
    if stats.get("total_selected", 0) == 0:
        print("[错误] 周报未生成任何内容")
        sys.exit(1)
    print(f"\n周报生成完成: {stats}")


if __name__ == "__main__":
    main()
