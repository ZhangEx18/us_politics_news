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

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import NewsDatabase
from ai_analyzer import _load_ai_config
from models import ContentItem, SourceType
from report_engine import ReportSpec, build_report

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
    """生成周报：计算窗口 → 读取 DB → 构建事件列表 → build_report()"""
    start_time = datetime.now()
    config = _load_config()
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    output_cfg = config.get("output", {})

    db_path = storage_cfg.get("db_path", "data/news.db")

    # === 1. 计算时间窗口 ===
    since, until, report_key = _get_weekly_window()
    week_num = since.isocalendar()[1]
    print(f"[周报] 窗口: {since.strftime('%m-%d %H:%M')} → {until.strftime('%m-%d %H:%M')}  标识: {report_key}")

    # === 2. 从数据库读取文章 ===
    db = NewsDatabase(db_path)
    articles = db.fetch_since(since.replace(tzinfo=None))
    print(f"[周报] 数据库加载 {len(articles)} 条文章")

    if not articles:
        print("[警告] 数据库中无最近文章，无法生成周报")
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

    print(f"[周报] 窗口过滤后保留 {len(filtered_items)} 条文章")
    if not filtered_items:
        print("[警告] 周报窗口内无文章，无法生成周报")
        return {"total_selected": 0}

    # === 4. 构建 scored_events dict 列表 ===
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

    # === 5. 构造 ReportSpec，调用 build_report() ===
    spec = ReportSpec(
        report_type="weekly",
        report_key=report_key,
        title=f"{since.year}年第{week_num}周周报",
        since=since,
        until=until,
        output_dir=output_cfg.get("weekly_dir", "docs/weekly"),
        feed_path=output_cfg.get("feed_path", "docs/feed.xml"),
        base_url=output_cfg.get("base_url", ""),
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
    stats["total_articles"] = len(filtered_items)

    return stats


def main():
    stats = run_weekly()
    if stats.get("total_selected", 0) == 0:
        print("[错误] 周报未生成任何内容")
        sys.exit(1)
    print(f"\n周报生成完成: {stats}")


if __name__ == "__main__":
    main()
