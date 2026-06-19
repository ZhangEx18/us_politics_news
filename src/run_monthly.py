#!/usr/bin/env python3
"""
月报生成管线 — 从数据库读取过去一个月已评分文章，按栏目聚合，生成月报。

流程：
1.  计算月报时间窗口（上月 1 日 07:00 → 本月 1 日 07:00）
2.  从数据库读取窗口内已评分文章
3.  转为 pipeline 内部格式
4.  事件级合并（merge_events）
5.  min_llm_score 过滤
6.  按栏目分桶
7.  每栏按配额选择候选
8.  每栏 generate_column_digest
9.  提炼本月要点
10. 质量门禁
11. 保存月报文件
"""

import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_analyzer import (
    generate_column_digest,
    merge_events,
    _load_ai_config,
)
from database import NewsDatabase
from models import ContentItem, SourceType
from report_renderer import save_daily_report, COLUMN_ORDER

# 从 run_pipeline 复用的工具函数
from run_pipeline import (
    _augment_ai_config_with_runtime,
    _build_reader_highlights,
    _generate_all_column_digests,
    _load_config,
    _load_history_context,
    _scored_dicts_to_content_items,
    sanitize_or_validate_events,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _get_monthly_window() -> tuple[datetime, datetime, str]:
    """计算月报时间窗口：上月 1 日 07:00 到本月 1 日 07:00"""
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    # 本月 1 日 07:00
    this_month_1st = now.replace(day=1, hour=7, minute=0, second=0, microsecond=0)
    if now >= this_month_1st:
        until = this_month_1st
        # 上月 1 日
        if until.month == 1:
            since = until.replace(year=until.year - 1, month=12)
        else:
            since = until.replace(month=until.month - 1)
    else:
        until = this_month_1st
        if until.month == 1:
            since = until.replace(year=until.year - 1, month=12)
        else:
            since = until.replace(month=until.month - 1)
    report_key = f"{since.year}-{since.month:02d}"
    return since, until, report_key


def _articles_to_scored_dicts(articles: list) -> list[dict]:
    """将数据库已评分文章转为 score_batch 输出格式，跳过未评分条目。"""
    scored: list[dict] = []
    for a in articles:
        if a.llm_score is None:
            continue
        tags = [t.strip() for t in (a.llm_tags or "").split(",") if t.strip()]
        scored.append({
            "link": a.url,
            "title": a.title,
            "source": a.source,
            "score": a.llm_score,
            "summary": a.llm_summary or a.summary or "",
            "content": a.summary or "",
            "tags": tags,
            "column": a.column or "",
            "event_key": a.event_key or "",
            "source_tier": a.source_tier or 4,
            "is_hard_news": True,  # 数据库中已评分的文章均通过过硬新闻过滤
            "source_links": [],
        })
    return scored


def run_monthly() -> dict:
    """月报生成管线：从数据库读取已评分文章，按栏目聚合生成月报。"""
    start_time = datetime.now()
    since, until, report_key = _get_monthly_window()
    year, month = since.year, since.month
    print(f"月报窗口: {since.strftime('%Y-%m-%d %H:%M')} → {until.strftime('%Y-%m-%d %H:%M')}")

    config = _load_config()
    output_cfg = config.get("output", {})
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    history_days = analysis_cfg.get("history_context_days", 3)

    monthly_dir = output_cfg.get("monthly_dir", "docs/monthly")
    db_path = storage_cfg.get("db_path", "data/news.db")
    columns_cfg = digest_cfg.get("columns", {})

    # 月报字数配置
    monthly_digest_cfg = digest_cfg.get("monthly", {})
    monthly_word_min = monthly_digest_cfg.get("target_word_count_min", 15000)
    monthly_word_max = monthly_digest_cfg.get("target_word_count_max", 30000)

    # 加载 AI 配置（digest 生成需要）
    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)

    print("=" * 60)
    print(f"{year}年{month}月 月报生成管线")
    print(f"时间: {start_time.isoformat()}")
    print("=" * 60)

    db = NewsDatabase(db_path)

    # === 1. 从数据库读取窗口内文章 ===
    print("\n[1/11] 从数据库读取文章...")
    # fetch_since 需要 naive datetime（UTC）
    since_utc = since.astimezone(tz=None).replace(tzinfo=None)
    all_articles = db.fetch_since(since_utc)
    print(f"   数据库返回 {len(all_articles)} 条")

    if not all_articles:
        print("\n[警告] 数据库中无文章，无法生成月报")
        return {"total_selected": 0}

    # === 2. 过滤到月报窗口 ===
    print("\n[2/11] 过滤到月报窗口...")
    windowed: list = []
    for a in all_articles:
        ref = a.published_at or a.fetched_at
        if ref is None:
            continue
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=BEIJING_TZ)
        ref_local = ref.astimezone(BEIJING_TZ)
        if since <= ref_local < until:
            windowed.append(a)
    print(f"   窗口内 {len(windowed)} 条")

    if not windowed:
        print("\n[警告] 月报窗口内无文章")
        return {"total_selected": 0}

    # === 3. 转为 scored_dicts 格式（跳过未评分） ===
    print("\n[3/11] 转换已评分文章...")
    scored_dicts = _articles_to_scored_dicts(windowed)
    print(f"   已评分 {len(scored_dicts)} 条（跳过 {len(windowed) - len(scored_dicts)} 条未评分）")

    if not scored_dicts:
        print("\n[警告] 无已评分文章")
        return {"total_selected": 0}

    # === 4. 事件级合并 ===
    print("\n[4/11] 事件级合并（按 event_key）...")
    merged_events = merge_events(scored_dicts)
    _merged_by_col: dict[str, int] = {}
    for entry in merged_events:
        c = entry.get("column", "unknown")
        _merged_by_col[c] = _merged_by_col.get(c, 0) + 1
    print(f"   {len(scored_dicts)} 条 → {len(merged_events)} 个事件")
    for c in sorted(_merged_by_col):
        print(f"     {c}: {_merged_by_col[c]} 个事件")

    # === 5. min_llm_score 过滤 ===
    min_llm_score = analysis_cfg.get("min_llm_score", 65)
    print(f"\n[5/11] min_llm_score 过滤（阈值={min_llm_score}）...")
    # 用原始 windowed 构建 ContentItem 索引，供 _scored_dicts_to_content_items 使用
    original_items = [
        ContentItem(
            id=f"db:{a.url}",
            source_type=SourceType(a.source_type) if a.source_type else SourceType.RSS,
            title=a.title,
            url=a.url,
            content=a.summary or "",
            source_name=a.source,
            published_at=a.published_at,
            column=a.column or "",
            source_tier=a.source_tier or 4,
            event_key=a.event_key or "",
            source_url_normalized=a.source_url_normalized or "",
            topic=a.topic or "",
            score=a.llm_score or a.score or 0.0,
            reason=a.reason or "",
            level=a.level or "",
        )
        for a in windowed
    ]
    content_items = _scored_dicts_to_content_items(merged_events, original_items)
    content_items.sort(key=lambda x: x.score or 0, reverse=True)
    filtered_items = [it for it in content_items if (it.score or 0) >= min_llm_score]
    print(f"   {len(content_items)} → {len(filtered_items)} 条")

    if not filtered_items:
        print("\n[警告] 过滤后无文章")
        return {"total_selected": 0}

    # === 6. 按栏目分桶 ===
    print("\n[6/11] 按栏目分桶...")
    by_column: dict[str, list[ContentItem]] = {}
    for item in filtered_items:
        col = item.column or "us_politics"
        by_column.setdefault(col, []).append(item)
    for col in by_column:
        by_column[col].sort(key=lambda x: x.score or 0, reverse=True)
    for col, items in sorted(by_column.items()):
        print(f"   {col}: {len(items)} 条")

    # === 7. 每栏按配额选择候选 ===
    print("\n[7/11] 每栏按配额选择候选...")
    column_candidates: dict[str, list[dict]] = {}
    for col_key, col_cfg in columns_cfg.items():
        col_items = by_column.get(col_key, [])
        min_n = col_cfg.get("min_items", 0)
        max_n = col_cfg.get("max_items", col_cfg.get("target_items", 6))
        candidates = col_items[: min(len(col_items), max_n)]
        column_candidates[col_key] = [
            {
                "title": it.title,
                "source": it.source_name,
                "score": it.score,
                "summary": it.content or "",
                "content": it.content or "",
                "source_links": (it.metadata or {}).get("merged_sources", []),
            }
            for it in candidates
        ]
        if len(candidates) < min_n:
            print(f"   警告: {col_key} 仅 {len(candidates)} 条 < 最低要求 {min_n}")
        print(f"   {col_key}: {len(candidates)} 条 (max={max_n})")

    # === 8. 每栏生成 digest ===
    print("\n[8/11] 每栏生成 digest...")
    history_context = _load_history_context(db, history_days)
    column_results = asyncio.run(_generate_all_column_digests(
        columns_cfg=columns_cfg,
        column_candidates=column_candidates,
        history_context=history_context,
        ai_config=ai_config,
        word_count_min=monthly_word_min,
        word_count_max=monthly_word_max,
    ))

    # === 9. 提炼本月要点 ===
    print("\n[9/11] 提炼本月要点...")
    highlights = _build_reader_highlights(column_results, limit=10)
    print(f"   本月要点: {len(highlights)} 条")

    # === 10. 质量门禁 ===
    print("\n[10/11] 质量门禁检查...")
    total_issues = 0
    for col_key in list(column_results.keys()):
        events = column_results[col_key]
        if not events:
            continue
        cleaned, issues = sanitize_or_validate_events(events)
        if issues:
            for issue in issues:
                print(f"   [{col_key}] {issue}")
            total_issues += len(issues)
        column_results[col_key] = cleaned
    if total_issues:
        print(f"   共 {total_issues} 个质量问题（已清理）")
    else:
        print("   全部通过")

    # === 11. 保存月报 ===
    print("\n[11/11] 保存月报文件...")
    meta = {
        "title": f"{year}年{month}月月报",
        "lead": "",
        "highlights": highlights,
        "date": report_key,
        "report_since": since.isoformat(),
        "report_until": until.isoformat(),
    }

    # 按 COLUMN_ORDER 排序
    columns: dict[str, list[dict]] = {}
    for col_key in COLUMN_ORDER:
        columns[col_key] = column_results.get(col_key, [])

    md_path, html_path = save_daily_report(
        meta, columns, output_dir=monthly_dir, report_type="monthly",
    )
    print(f"   Markdown: {md_path}")
    print(f"   HTML: {html_path}")

    # 统计
    duration = (datetime.now() - start_time).total_seconds()
    total_events = sum(len(evts) for evts in column_results.values())
    col_counts = {k: len(v) for k, v in column_results.items()}

    stats = {
        "duration_seconds": round(duration, 1),
        "report_key": report_key,
        "total_articles_in_window": len(windowed),
        "total_scored": len(scored_dicts),
        "total_events": len(merged_events),
        "total_selected": total_events,
        "column_counts": col_counts,
        "outputs": {"markdown": md_path, "html": html_path},
    }

    print("\n" + "=" * 60)
    print(f"{year}年{month}月 月报生成完成")
    print("=" * 60)
    print(f"  耗时: {duration:.1f}s")
    print(f"  窗口文章: {len(windowed)} → 已评分: {len(scored_dicts)} → 事件: {len(merged_events)} → 精选: {total_events}")
    for col, cnt in sorted(col_counts.items()):
        print(f"    {col}: {cnt}")
    print(f"  输出: {monthly_dir}/")

    return stats


def main():
    try:
        stats = run_monthly()
        if stats.get("total_selected", 0) == 0:
            print("[错误] 月报未生成任何内容")
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[错误] 月报生成失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
