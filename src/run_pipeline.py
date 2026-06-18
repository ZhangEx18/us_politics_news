#!/usr/bin/env python3
"""
四维日报 Pipeline — 12 步流程

1. 并发抓取所有数据源
2. 跨源 URL 去重
3. 入库
4. AI score_batch 批量评分（含 event_key）
5. 更新数据库 LLM 评分
6. 事件级合并（按 event_key 合并多源报道）
7. 加载近 3 天历史上文
8. 均衡选择（按栏目配额）
9. AI digest 生成日报正文（5000-10000 字）
10. 保存日报文件（直接使用 AI 正文）
11. 保存 RSS Feed
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_analyzer import score_batch, generate_digest, has_ai_config, merge_events, _load_ai_config
from database import NewsDatabase
from fetchers import (
    fetch_all_sources,
    merge_cross_source_duplicates,
    merge_topic_duplicates,
    save_to_db,
    normalize_url,
)
from models import ContentItem, SourceType, ScoredArticle, DailyReport
from report_renderer import save_daily_report
from feed_builder import save_feed


def _load_config() -> dict:
    path = os.path.join(_project_root, "config", "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _ScoredAdapter:
    """将 score_batch 返回的 dict 适配为 update_llm_scores 期望的属性访问"""
    __slots__ = ("url", "llm_score", "llm_summary", "llm_tags", "column", "event_key", "source_tier")

    def __init__(self, entry: dict):
        self.url = entry.get("link", "")
        self.llm_score = entry.get("score")
        self.llm_summary = entry.get("summary", "")
        self.llm_tags = entry.get("tags", [])
        self.column = entry.get("column", "")
        self.event_key = entry.get("event_key", "")
        self.source_tier = 4


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
            ))

    return result


def _apply_column_quota(items: list[ContentItem], config: dict) -> list[ContentItem]:
    """三段式栏目配额选择：min -> target -> 全局补到 total_target -> 截断 total_max"""
    digest_cfg = config.get("digest", {})
    columns_cfg = digest_cfg.get("columns", {})
    total_target = digest_cfg.get("total_target_items", 28)
    total_max = digest_cfg.get("total_max_items", 40)

    # 按栏目分组，每组按分数降序
    by_column: dict[str, list[ContentItem]] = {}
    for item in items:
        col = item.column or "us_politics"
        by_column.setdefault(col, []).append(item)
    for col in by_column:
        by_column[col].sort(key=lambda x: x.score or 0, reverse=True)

    selected: list[ContentItem] = []
    selected_urls: set[str] = set()

    # 第一段：每个栏目先满足 min_items
    for col_key, col_cfg in columns_cfg.items():
        col_items = by_column.get(col_key, [])
        min_n = col_cfg.get("min_items", 5)
        taken = [it for it in col_items[:min_n] if str(it.url) not in selected_urls]
        for it in taken:
            selected_urls.add(str(it.url))
        selected.extend(taken)

    # 第二段：每个栏目补到 target_items
    for col_key, col_cfg in columns_cfg.items():
        col_items = by_column.get(col_key, [])
        target_n = col_cfg.get("target_items", 7)
        taken = []
        for it in col_items:
            if len(taken) >= target_n:
                break
            if str(it.url) not in selected_urls:
                selected_urls.add(str(it.url))
                taken.append(it)
        selected.extend(taken)

    # 第三段：全局按分数补到 total_target_items
    if len(selected) < total_target:
        remaining = [it for it in items if str(it.url) not in selected_urls]
        remaining.sort(key=lambda x: x.score or 0, reverse=True)
        need = total_target - len(selected)
        for it in remaining[:need]:
            selected_urls.add(str(it.url))
            selected.append(it)

    # 截断到 total_max_items
    selected.sort(key=lambda x: x.score or 0, reverse=True)
    return selected[:total_max]


def _load_history_context(db: NewsDatabase, days: int = 3) -> str:
    """加载近 N 天已推送事件文本，用于 digest prompt 去重"""
    since = datetime.now() - timedelta(days=days)
    articles = db.fetch_since(since)
    if not articles:
        return ""

    lines = []
    for a in articles[:100]:
        tags = a.llm_tags or ""
        lines.append(
            f"[score: {a.llm_score or 0}] title:{a.title}\n"
            f"published: {a.published_at or ''}\ntags: {tags}\n"
            f"source: {a.source}\nsummary: {a.llm_summary or a.summary or ''}"
        )
    return "\n\n".join(lines)


def _assign_level(score: float, important_threshold: float = 85) -> str:
    """根据 LLM 评分分配 level"""
    if score >= important_threshold:
        return "重点"
    return "观察"


def run_pipeline(hours: int = 24) -> dict:
    start_time = datetime.now()
    since = start_time - timedelta(hours=hours)

    config = _load_config()
    output_cfg = config.get("output", {})
    storage_cfg = config.get("storage", {})
    digest_cfg = config.get("digest", {})
    runtime_cfg = config.get("runtime", {})
    analysis_cfg = config.get("analysis", {})

    db_path = storage_cfg.get("db_path", "data/news.db")
    daily_dir = output_cfg.get("daily_dir", "docs/daily")
    feed_path = output_cfg.get("feed_path", "docs/feed.xml")
    base_url = output_cfg.get("base_url", "")
    history_days = analysis_cfg.get("history_context_days", 3)
    important_score = analysis_cfg.get("important_score", 85)

    # 构建 AI 配置（从 config.yaml 的 llm 段 + 环境变量）
    llm_cfg = config.get("llm", {})
    ai_config = {
        "api_key": os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
        "base_url": llm_cfg.get("base_url", os.getenv("AI_BASE_URL", "https://api.openai.com/v1")),
        "model": llm_cfg.get("model", os.getenv("AI_MODEL", "gpt-4o-mini")),
    }

    print("=" * 60)
    print("四维日报 Pipeline")
    print(f"时间: {start_time.isoformat()}")
    print(f"时间窗口: 最近 {hours} 小时")
    print("=" * 60)

    # 前置检查
    if runtime_cfg.get("require_ai", True) and not ai_config["api_key"]:
        print("\n[错误] 未配置 AI_API_KEY，无法运行 pipeline")
        sys.exit(1)

    db = NewsDatabase(db_path)

    # === 1. 并发抓取 ===
    print("\n[1/11] 并发抓取所有数据源...")
    all_items = asyncio.run(fetch_all_sources(since))
    print(f"   共抓取 {len(all_items)} 条")

    if not all_items:
        print("\n[警告] 未抓取到任何内容，pipeline 终止")
        return {"total_fetched": 0, "total_selected": 0}

    # === 2. 跨源 URL 去重 ===
    print("\n[2/11] 跨源 URL 去重...")
    merged_items = merge_cross_source_duplicates(all_items)
    print(f"   合并 {len(all_items) - len(merged_items)} 条 -> {len(merged_items)} 条唯一")

    # === 3. 入库 ===
    print("\n[3/11] 入库...")
    fetch_stats = save_to_db(merged_items, db)
    print(f"   新增 {sum(fetch_stats.values())} 条")

    # === 4. AI score_batch ===
    print("\n[4/11] AI 批量评分...")
    today_articles = db.fetch_since(since)
    entries_for_scoring = [
        {
            "link": a.url, "title": a.title, "source": a.source,
            "published": a.published_at.isoformat() if a.published_at else "",
            "content": a.summary or "",
        }
        for a in today_articles
    ]
    scored_dicts, score_errors = asyncio.run(score_batch(entries_for_scoring, ai_config))
    print(f"   完成 {len(scored_dicts)} 条评分")

    # require_ai 时，评分失败必须退出
    require_ai = runtime_cfg.get("require_ai", True)
    if require_ai:
        candidate_count = len(entries_for_scoring)
        valid_count = len(scored_dicts)
        if score_errors or valid_count == 0 or valid_count < candidate_count * 0.5:
            print(f"\n[错误] AI 评分失败: errors={len(score_errors)}, "
                  f"valid={valid_count}/{candidate_count}")
            sys.exit(1)

    # === 5. 更新数据库 LLM 评分 ===
    print("\n[5/11] 更新数据库 LLM 评分...")
    adapted_for_db = [_ScoredAdapter(e) for e in scored_dicts]
    updated_count = db.update_llm_scores(adapted_for_db)
    print(f"   更新 {updated_count} 条")

    # === 6. 事件级合并 ===
    print("\n[6/11] 事件级合并（按 event_key）...")
    merged_events = merge_events(scored_dicts)
    print(f"   {len(scored_dicts)} 条 -> {len(merged_events)} 个事件")

    # === 7. 加载历史上文 ===
    print(f"\n[7/11] 加载近 {history_days} 天历史上文...")
    history_context = _load_history_context(db, history_days)
    print(f"   {len(history_context)} 字上下文")

    # === 8. 均衡选择 ===
    print("\n[8/11] 按栏目配额选择...")
    content_items = _scored_dicts_to_content_items(merged_events, merged_items)
    content_items.sort(key=lambda x: x.score or 0, reverse=True)

    # 按 min_llm_score 过滤低分候选
    min_llm_score = analysis_cfg.get("min_llm_score", 65)
    total_min_items = digest_cfg.get("total_min_items", 20)
    filtered_items = [it for it in content_items if (it.score or 0) >= min_llm_score]
    print(f"   min_llm_score={min_llm_score}: {len(content_items)} -> {len(filtered_items)} 条")
    if len(filtered_items) < total_min_items:
        print(f"   警告: 过滤后 {len(filtered_items)} 条 < 最低要求 {total_min_items}，继续运行")

    balanced_items = _apply_column_quota(filtered_items, config)
    print(f"   选出 {len(balanced_items)} 个事件")

    col_counts: dict[str, int] = {}
    for item in balanced_items:
        col = item.column or "other"
        col_counts[col] = col_counts.get(col, 0) + 1
    for col, cnt in sorted(col_counts.items()):
        print(f"     {col}: {cnt}")

    # === 9. AI digest 生成日报正文 ===
    print("\n[9/11] AI 生成日报正文...")
    digest_entries = [
        {
            "link": str(item.url), "title": item.title, "source": item.source_name,
            "score": item.score, "column": item.column,
            "tags": item.topic.split(",") if item.topic else [],
            "summary": item.content or "", "content": item.content or "",
            "source_links": item.metadata.get("merged_sources", []),
        }
        for item in balanced_items
    ]
    digest_text = asyncio.run(generate_digest(
        entries=digest_entries,
        recent_context=[],  # list[dict] — 空，因为历史上下文通过 recent_push_context 传入
        config=ai_config,
        recent_push_context=history_context,
        digest_config=digest_cfg,
    ))
    print(f"   生成 {len(digest_text)} 字")

    # === 10. 保存日报（直接使用 AI 生成的正文）===
    print("\n[10/11] 保存日报文件...")
    md_path, html_path = save_daily_report(
        articles=None,  # 不从 articles 渲染，直接用 digest_text
        output_dir=daily_dir,
        digest_text=digest_text,
    )
    print(f"   Markdown: {md_path}")
    print(f"   HTML: {html_path}")

    # === 11. 保存 RSS Feed ===
    print("\n[11/11] 保存 RSS Feed...")
    # 从 digest_text 解析 frontmatter 用于 RSS 标题
    title = f"{datetime.now().strftime('%Y-%m-%d')} 四维日报"
    if digest_text.startswith("---"):
        parts = digest_text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
                title = fm.get("title", title)
            except yaml.YAMLError:
                pass

    save_feed(
        articles=None,
        output_path=feed_path,
        base_url=base_url,
        digest_text=digest_text,
        title=title,
    )
    print(f"   Feed: {feed_path}")

    # 统计
    duration = (datetime.now() - start_time).total_seconds()
    stats = {
        "duration_seconds": round(duration, 1),
        "total_fetched": len(all_items),
        "total_merged": len(merged_items),
        "total_events": len(merged_events),
        "total_selected": len(balanced_items),
        "digest_chars": len(digest_text),
        "column_counts": col_counts,
        "outputs": {"markdown": md_path, "html": html_path, "feed": feed_path},
    }

    print("\n" + "=" * 60)
    print("Pipeline 完成")
    print("=" * 60)
    print(f"  耗时: {duration:.1f}s")
    print(f"  抓取: {len(all_items)} -> 事件: {len(merged_events)} -> 精选: {len(balanced_items)}")
    print(f"  日报: {len(digest_text)} 字")
    print(f"  输出: {daily_dir}/")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="四维日报 Pipeline")
    parser.add_argument("--hours", type=int, default=24, help="时间窗口（小时）")
    args = parser.parse_args()

    try:
        stats = run_pipeline(hours=args.hours)
        if stats.get("total_fetched", 0) == 0:
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
