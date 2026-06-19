#!/usr/bin/env python3
"""月报生成管线 — 从数据库读取过去一个月已评分文章，调用 report_engine.build_report() 生成月报。

流程：
1.  计算月报时间窗口（上月 1 日 07:00 → 本月 1 日 07:00）
2.  从数据库读取窗口内已评分文章
3.  转为 scored_events dict 列表
4.  构造 ReportSpec，调用 build_report()
"""

import os
import sys
from datetime import datetime, timezone
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


def _get_monthly_window() -> tuple[datetime, datetime, str]:
    """计算月报时间窗口：上月 1 日 07:00 到本月 1 日 07:00"""
    now = datetime.now(BEIJING_TZ)
    # 本月 1 日 07:00
    this_month_1st = now.replace(day=1, hour=7, minute=0, second=0, microsecond=0)
    if now >= this_month_1st:
        until = this_month_1st
    else:
        until = this_month_1st
    # 上月 1 日
    if until.month == 1:
        since = until.replace(year=until.year - 1, month=12)
    else:
        since = until.replace(month=until.month - 1)
    report_key = f"{since.year}-{since.month:02d}"
    return since, until, report_key


def run_monthly() -> dict:
    """月报生成管线：计算窗口 → 读取 DB → 构建事件列表 → build_report()"""
    start_time = datetime.now()
    config = _load_config()
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    analysis_cfg = config.get("analysis", {})
    output_cfg = config.get("output", {})

    db_path = storage_cfg.get("db_path", "data/news.db")

    # === 1. 计算时间窗口 ===
    since, until, report_key = _get_monthly_window()
    year, month = since.year, since.month
    print(f"[月报] 窗口: {since.strftime('%Y-%m-%d %H:%M')} → {until.strftime('%Y-%m-%d %H:%M')}  标识: {report_key}")

    # === 2. 从数据库读取文章 ===
    db = NewsDatabase(db_path)
    since_utc = since.astimezone(tz=None).replace(tzinfo=None)
    all_articles = db.fetch_since(since_utc)
    print(f"[月报] 数据库返回 {len(all_articles)} 条")

    if not all_articles:
        print("[警告] 数据库中无文章，无法生成月报")
        return {"total_selected": 0}

    # === 3. 过滤到月报窗口 ===
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
    print(f"[月报] 窗口内 {len(windowed)} 条")

    if not windowed:
        print("[警告] 月报窗口内无文章")
        return {"total_selected": 0}

    # === 4. 构建 scored_events dict 列表（跳过未评分） ===
    event_dicts: list[dict] = []
    skipped = 0
    for a in windowed:
        if a.llm_score is None:
            skipped += 1
            continue
        tags = [t.strip() for t in (a.llm_tags or "").split(",") if t.strip()]
        event_dicts.append({
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
            "is_hard_news": True,
            "source_links": [],
        })
    print(f"[月报] 已评分 {len(event_dicts)} 条（跳过 {skipped} 条未评分）")

    if not event_dicts:
        print("[警告] 无已评分文章")
        return {"total_selected": 0}

    # === 5. 构造 ReportSpec，调用 build_report() ===
    monthly_digest_cfg = digest_cfg.get("monthly", {})

    spec = ReportSpec(
        report_type="monthly",
        report_key=report_key,
        title=f"{year}年{month}月月报",
        since=since,
        until=until,
        output_dir=output_cfg.get("monthly_dir", "docs/monthly"),
        feed_path=output_cfg.get("feed_path", "docs/feed.xml"),
        base_url=output_cfg.get("base_url", ""),
        column_quotas=digest_cfg.get("columns", {}),
        word_count_min=monthly_digest_cfg.get("target_word_count_min", 15000),
        word_count_max=monthly_digest_cfg.get("target_word_count_max", 30000),
        highlights_limit=10,
        allow_headline_only=False,
        pub_date=until,
        history_days=analysis_cfg.get("history_context_days", 3),
        min_llm_score=analysis_cfg.get("min_llm_score", 65),
    )

    ai_config = _augment_ai_config_with_runtime(_load_ai_config(), config)
    stats = build_report(spec, event_dicts, config, ai_config, db)

    # 补充月报特有统计
    stats["duration_seconds"] = round((datetime.now() - start_time).total_seconds(), 1)
    stats["total_articles_in_window"] = len(windowed)
    stats["total_scored"] = len(event_dicts)

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
