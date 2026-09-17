#!/usr/bin/env python3
"""
报告编排器 — 日报/周报/月报共享的统一 pipeline。

ReportSpec 定义报告类型差异，build_report() 执行共享阶段。
"""

import asyncio
import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_analyzer import (
    _load_glossary,
    count_untranslated_terms,
    generate_column_digest,
    generate_daily_overview,
    generate_fallback_bodies,
    generate_periodical_overview,
    merge_events,
    translate_headline_titles,
)
from database import build_source_health_summary
from feed_builder import save_feed
from content_policy import REJECT_REASONS
from publish_manifest import build_manifest
from report_renderer import COLUMN_ORDER, save_daily_report

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 选择阶段配额：防止单一来源霸占栏目（单栏 ≤30%，全报 ≤4 条）
MAX_EVENTS_PER_SOURCE_TOTAL = 4
MAX_HEADLINE_PER_ORG = 2


def _source_column_cap(max_items: int, target_items: int) -> int:
    """单源单栏上限：栏目规模 30%，至少 2 条。"""
    size = max_items if max_items > 0 else target_items
    return max(2, int(round(size * 0.3)))


# ── 报告规格 ──

@dataclass
class ReportSpec:
    """报告类型差异化参数。"""
    report_type: str
    report_key: str
    title: str
    since: datetime
    until: datetime
    output_dir: str
    feed_path: str
    base_url: str
    column_quotas: dict[str, dict]
    product_key: str = "news"
    site_root: str = "docs/news"
    word_count_min: int = 2500
    word_count_max: int = 5000
    highlights_limit: int = 8
    allow_headline_only: bool = True
    pub_date: datetime | None = None
    history_days: int = 3
    min_llm_score: float = 65
    fallback_candidates_by_column: dict[str, list[dict]] = field(default_factory=dict)


@dataclass(frozen=True)
class PeriodicalOverview:
    summary: str = ""
    themes: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    column_analyses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, overview: "PeriodicalOverview | dict | None", column_keys: list[str]) -> "PeriodicalOverview":
        if isinstance(overview, cls):
            return overview
        if not isinstance(overview, dict):
            return cls()

        raw_column_analyses = overview.get("column_analyses", {})
        column_analyses: dict[str, str] = {}
        if isinstance(raw_column_analyses, dict):
            for col_key in column_keys:
                text = str(raw_column_analyses.get(col_key, "") or "").strip()
                if text:
                    column_analyses[col_key] = text

        return cls(
            summary=str(overview.get("summary", "") or "").strip(),
            themes=[str(item).strip() for item in overview.get("themes", []) if str(item).strip()],
            watchlist=[str(item).strip() for item in overview.get("watchlist", []) if str(item).strip()],
            column_analyses=column_analyses,
        )

    def is_empty(self) -> bool:
        return not self.summary and not self.themes and not self.watchlist and not self.column_analyses

    def to_payload(self) -> dict:
        if self.is_empty():
            return {}
        return {
            "summary": self.summary,
            "themes": [item for item in self.themes if str(item).strip()],
            "watchlist": [item for item in self.watchlist if str(item).strip()],
        }


@dataclass
class ReportPreparation:
    merged_events: list[dict]
    by_column: dict[str, list[dict]]
    column_candidates: dict[str, list[dict]]
    column_headline_only: dict[str, list[dict]]
    history_context: str
    metrics: dict


def _column_label_map(spec: ReportSpec) -> dict[str, str]:
    return {col_key: str(col_cfg.get("label", col_key)) for col_key, col_cfg in spec.column_quotas.items()}


def _column_source_counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, set[str]] = {}
    for event in events:
        column = str(event.get("column") or "unknown").strip() or "unknown"
        source = str(event.get("source") or "").strip()
        if not source:
            continue
        counts.setdefault(column, set()).add(source)
    return {col: len(sources) for col, sources in counts.items()}


def _build_periodical_gate_result(
    spec: ReportSpec,
    scored_events: list[dict],
    db,
    config: dict,
) -> dict:
    """周报/月报在写作前做稳定性门禁，避免低覆盖内容继续往下写。"""
    column_labels = _column_label_map(spec)
    by_column: dict[str, list[dict]] = {}
    for event in scored_events:
        column = str(event.get("column") or "").strip()
        if not column:
            continue
        by_column.setdefault(column, []).append(event)

    thresholds = config.get("rules", {}).get("periodical_gate", {})
    min_column_events = int(thresholds.get("min_column_events", 2))
    min_total_events = int(thresholds.get("min_total_events", 8))
    min_columns_with_events = int(thresholds.get("min_columns_with_events", 3))
    min_hard_news = int(thresholds.get("min_hard_news", 8))
    min_sources_per_column = int(thresholds.get("min_sources_per_column", 2))
    min_source_tiers = int(thresholds.get("min_source_tiers", 2))
    min_scored_ratio = float(thresholds.get("min_scored_ratio", 0.6))

    column_stats: dict[str, dict] = {}
    covered_columns = 0
    total_sources = 0
    hard_news_count = 0
    scored_count = 0
    tier_set: set[int] = set()
    total_events = 0

    for col_key in spec.column_quotas:
        events = list(by_column.get(col_key, []))
        total_events += len(events)
        sources = {str(event.get("source") or "").strip() for event in events if str(event.get("source") or "").strip()}
        tier_counts = {}
        for event in events:
            tier = int(event.get("source_tier") or 4)
            tier_set.add(tier)
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            if event.get("is_hard_news", True):
                hard_news_count += 1
            if event.get("score") is not None:
                scored_count += 1

        if events:
            covered_columns += 1
        total_sources += len(sources)

        column_stats[col_key] = {
            "label": column_labels.get(col_key, col_key),
            "events": len(events),
            "sources": len(sources),
            "source_names": sorted(sources),
            "source_tiers": sorted(tier_counts),
            "status": "healthy" if len(events) >= min_column_events and len(sources) >= min_sources_per_column else (
                "thin" if len(events) else "empty"
            ),
        }

    health_rows = []
    source_health_summary = {}
    try:
        if hasattr(db, "source_health_rows"):
            health_rows = db.source_health_rows(window_since=spec.since, window_days=max(1, (spec.until - spec.since).days))
        if hasattr(db, "fetch_log_rows"):
            fetch_rows = db.fetch_log_rows(window_since=spec.since, window_days=max(1, (spec.until - spec.since).days))
        else:
            fetch_rows = []
        source_health_summary = build_source_health_summary(
            health_rows,
            fetch_log_rows=fetch_rows,
            window_days=max(1, (spec.until - spec.since).days),
        )
    except Exception:
        source_health_summary = {}

    gate_failed = False
    reasons: list[str] = []
    if total_events < min_total_events:
        gate_failed = True
        reasons.append(f"总事件量不足: {total_events} < {min_total_events}")
    if covered_columns < min_columns_with_events:
        gate_failed = True
        reasons.append(f"覆盖栏目不足: {covered_columns} < {min_columns_with_events}")
    if hard_news_count < min_hard_news:
        gate_failed = True
        reasons.append(f"硬新闻量不足: {hard_news_count} < {min_hard_news}")
    if len(tier_set) < min_source_tiers:
        gate_failed = True
        reasons.append(f"来源层级不足: {len(tier_set)} < {min_source_tiers}")
    if total_events and (scored_count / total_events) < min_scored_ratio:
        gate_failed = True
        reasons.append(f"评分覆盖不足: {scored_count}/{total_events} < {min_scored_ratio:.0%}")
    if source_health_summary:
        if source_health_summary.get("column_health_state") in {"missing_column", "single_source_bias"}:
            gate_failed = True
            reasons.append(f"栏目健康异常: {source_health_summary.get('column_health_state')}")
        if source_health_summary.get("window_state") == "stale":
            gate_failed = True
            reasons.append("窗口内容偏旧")

    return {
        "gate_failed": gate_failed,
        "reasons": reasons,
        "total_events": total_events,
        "covered_columns": covered_columns,
        "hard_news_count": hard_news_count,
        "scored_count": scored_count,
        "source_tiers": sorted(tier_set),
        "column_stats": column_stats,
        "source_health_summary": source_health_summary,
    }


# ── 共享工具 ──

def _load_history_context(db, days: int = 3) -> str:
    """加载近 N 天已推送事件文本，用于 digest prompt 去重。"""
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


def build_reader_highlights(columns: dict[str, list[dict]], limit: int = 8) -> list[str]:
    """从最终入选的重点解析事件提炼要点（跳过过短的条目）。"""
    MIN_BODY_LENGTH = 80  # reader_body 低于此长度的条目不进入今日要点

    def _highlight_text(event: dict) -> str:
        title = str(event.get("title_zh", "")).strip()
        core = event.get("core_facts", "")
        if isinstance(core, list):
            core = " ".join(str(part).strip() for part in core if str(part).strip())
        core = str(core).strip()
        text = title if title else core[:45]
        text = re.sub(r"\s+", " ", text).strip("：:，,。. ")
        if not text:
            return ""
        if len(text) > 45:
            text = text[:45].rstrip() + "…"
        return text

    def _is_detailed_event(event: dict) -> bool:
        """优先要求有足够长的正文；兼容旧测试数据时允许仅凭明确标题入选。"""
        body = str(event.get("reader_body") or event.get("core_facts") or "").strip()
        title = str(event.get("title_zh") or "").strip()
        if len(body) >= MIN_BODY_LENGTH:
            return True
        if not body:
            return bool(title and len(title) >= 2)
        return bool(title and len(title) >= 8)

    highlights: list[str] = []
    column_keys = [key for key in columns if columns.get(key)]
    if not column_keys:
        return highlights

    max_len = max(len(columns.get(key, [])) for key in column_keys)
    for idx in range(max_len):
        for col_key in column_keys:
            events = columns.get(col_key, [])
            if idx >= len(events):
                continue
            if not _is_detailed_event(events[idx]):
                continue
            text = _highlight_text(events[idx])
            if not text or text in highlights:
                continue
            highlights.append(text)
            if len(highlights) >= limit:
                return highlights
    return highlights


def _build_periodical_overview_fallback(
    report_type: str,
    title: str,
    highlights: list[str],
    columns: dict[str, dict],
) -> PeriodicalOverview:
    label = "本周" if report_type == "weekly" else "本月"
    top_titles: list[str] = []
    column_analyses: dict[str, str] = {}

    for col_key, col_data in columns.items():
        detailed = list(col_data.get("detailed_events", [])) if isinstance(col_data, dict) else list(col_data)
        if detailed:
            titles = [
                str(event.get("title_zh") or event.get("title") or "").strip()
                for event in detailed[:2]
                if str(event.get("title_zh") or event.get("title") or "").strip()
            ]
            if titles:
                top_titles.extend(titles[:2])
            first_body = str(detailed[0].get("reader_body") or detailed[0].get("core_facts") or "").strip()
            if first_body:
                column_analyses[col_key] = re.sub(r"\s+", " ", first_body).strip()[:110].rstrip(" ，,。．;；:：")

    unique_titles: list[str] = []
    for item in top_titles + highlights:
        text = re.sub(r"\s+", " ", str(item or "")).strip()[:32].rstrip(" ，,。．;；:：")
        if text and text not in unique_titles:
            unique_titles.append(text)

    summary_parts = [f"{label}聚焦于{title}。"]
    if unique_titles:
        summary_parts.append(f"主线围绕{'、'.join(unique_titles[:3])}展开。")
    else:
        summary_parts.append("主线以各栏已确认事件为准。")
    summary = re.sub(r"\s+", " ", "".join(summary_parts)).strip()[:220].rstrip(" ，,。．;；:：")

    themes = unique_titles[:4]
    if not themes:
        themes = [f"{label}主线", "栏目交叉推进"]

    watchlist = []
    for col_key in columns:
        analysis = column_analyses.get(col_key, "")
        if analysis:
            watchlist.append(f"{col_key}:{analysis[:24]}")
    if not watchlist:
        watchlist = [f"继续跟踪{label}内已确认事件的后续进展"]

    return PeriodicalOverview(
        summary=summary,
        themes=themes,
        watchlist=watchlist[:4],
        column_analyses=column_analyses,
    )


def _looks_like_english_fragment(text: str) -> bool:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return False
    if re.search(r"[\u4e00-\u9fff]", compact):
        return False
    letters = re.findall(r"[A-Za-z]", compact)
    if len(letters) < 6:
        return False
    if compact.endswith((" to", " a", " an", " the", " of", " on", " in", " for", " with", " from")):
        return True
    if compact.endswith("。") and re.search(r"[A-Za-z]", compact):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9 ,.'()&/\-]{12,}", compact))


def _contains_meaningful_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", str(text or "")))


def _normalize_detailed_events_to_chinese(
    column_results: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """正文事件必须对读者呈现为中文；疑似英文残片的条目直接丢弃。"""
    normalized_columns: dict[str, list[dict]] = {}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, items in column_results.items():
        kept: list[dict] = []
        dropped_english = 0
        for item in items:
            title_zh = str(item.get("title_zh") or "").strip()
            reader_body = str(item.get("reader_body") or item.get("core_facts") or "").strip()

            title_ok = _contains_meaningful_cjk(title_zh) and not _looks_like_english_fragment(title_zh)
            body_ok = _contains_meaningful_cjk(reader_body) and not _looks_like_english_fragment(reader_body)
            if not title_ok or not body_ok:
                dropped_english += 1
                continue
            kept.append(item)

        normalized_columns[col_key] = kept
        metrics[col_key] = {
            "detailed_translation_failed": dropped_english,
        }

    return normalized_columns, metrics


def _is_uninformative_bill_sentence(text: str) -> bool:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return True
    if not re.search(r"(法案|决议|修正案|草案|bill|resolution)", compact, re.IGNORECASE):
        return False
    if re.search(r"(旨在|将|要求|用于|以|内容包括|围绕|推动|限制|扩大|改善|支持|评估)", compact):
        return False
    if re.search(r"(被提交至国会审议|处于立法进程介绍阶段|被提出)", compact):
        return True
    return bool(re.fullmatch(r".*([A-Z]\.[RSC]\.?\s*\d+|H\.R\.\s*\d+|S\.\s*\d+).*", compact))


def _build_periodical_overview_payload(overview: PeriodicalOverview | dict | None) -> dict:
    return PeriodicalOverview.from_raw(overview, []).to_payload()


def _format_event_date_for_reader(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
        if not match:
            return ""
        return f"{int(match.group(2))} 月 {int(match.group(3))} 日"
    return f"{dt.month} 月 {dt.day} 日"


def _build_fallback_detailed_event(candidate: dict) -> dict | None:
    """从已评分候选构造保守中文正文，用于 AI 写作结果被过滤为空的日报栏。"""
    summary = str(candidate.get("summary") or candidate.get("content") or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    if not summary or _looks_like_english_fragment(summary) or not _contains_meaningful_cjk(summary):
        return None

    raw_title = str(candidate.get("title_zh") or candidate.get("title") or "").strip()
    title = raw_title
    if not title or _looks_like_english_fragment(title) or not _contains_meaningful_cjk(title):
        title = summary[:36].rstrip(" ，,。；;:：")
    if title.endswith(("承", "垄")):
        return None
    if not title or _looks_like_english_fragment(title) or not _contains_meaningful_cjk(title):
        return None

    freshness_date = str(candidate.get("freshness_date") or "").strip()
    event_date = str(candidate.get("event_date") or "").strip()
    freshness_status = str(candidate.get("freshness_status") or "").strip()
    if freshness_status not in {"today", "recent_followup"}:
        return None
    if event_date and freshness_date and event_date != freshness_date:
        # ponytail: fallback 只保留日报窗口内事件，旧背景事件不在保底分支硬写。
        return None

    date_text = (
        _format_event_date_for_reader(freshness_date or event_date)
        or _format_event_date_for_reader(candidate.get("published"))
    )
    if not date_text:
        return None

    sentences = re.findall(r"[^。！？!?]+[。！？!?]?", summary)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return None
    first_sentence = sentences[0]
    if _is_uninformative_bill_sentence(first_sentence):
        return None
    body_text = "".join(sentences[:3])
    if body_text[-1] not in "。！？!?":
        body_text += "。"
    body = f"{date_text}，{body_text}"
    if len(body) < 40:
        return None
    if len(body) > 260:
        body = body[:260].rstrip(" ，,。. ") + "。"

    return {
        **candidate,
        "title_zh": title,
        "reader_body": body,
        "core_facts": body,
    }


def _ensure_daily_detailed_events(
    column_results: dict[str, list[dict]],
    column_candidates: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """日报每个有候选的栏目至少保留一条中文重点解析，避免 AI 空输出阻断发布。"""
    ensured = {col_key: list(items) for col_key, items in column_results.items()}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, candidates in column_candidates.items():
        current = ensured.get(col_key, [])
        added = 0
        failed = 0
        if not current and candidates:
            for candidate in candidates:
                fallback_event = _build_fallback_detailed_event(candidate)
                if fallback_event:
                    ensured[col_key] = [fallback_event]
                    added = 1
                    break
                failed += 1
        metrics[col_key] = {
            "detailed_fallback_added": added,
            "detailed_fallback_failed": failed,
        }

    return ensured, metrics


def _dedupe_daily_column_events(
    column_results: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """按 event_key 和标题近似度去掉同栏重复重点解析。"""
    deduped: dict[str, list[dict]] = {}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, events in column_results.items():
        kept: list[dict] = []
        seen_keys: set[str] = set()
        seen_titles: list[str] = []
        dropped = 0
        for event in events:
            event_key = str(event.get("event_key") or "").strip()
            title = str(event.get("title_zh") or event.get("title") or "").strip()
            norm_title = _normalize_event_title(title)
            if event_key and event_key in seen_keys:
                dropped += 1
                continue
            if norm_title and any(
                norm_title == old or SequenceMatcher(None, norm_title, old).ratio() >= 0.58
                for old in seen_titles
            ):
                dropped += 1
                continue
            kept.append(event)
            if event_key:
                seen_keys.add(event_key)
            if norm_title:
                seen_titles.append(norm_title)
        deduped[col_key] = kept
        metrics[col_key] = {"deduped_detailed": dropped}

    return deduped, metrics


def _normalize_event_title(title: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(title or "")).lower()


_REJECTION_METRIC_MAP: dict[str, str] = {
    "routine_notice_dropped": "routine_notice",
    "low_newsworthiness_dropped": "low_newsworthiness",
    "repeated_story_dropped": "repeated_story",
    "headline_live_blog_dropped": "live_blog",
    "headline_truncated_dropped": "cryptic_title",
    "source_quota_dropped": "source_quota",
    "headline_cryptic_dropped": "cryptic_title",
    "headline_opinion_dropped": "opinion_piece",
    "headline_promo_dropped": "promo_piece",
    "headline_soft_dropped": "soft_news",
    "headline_duplicate_dropped": "duplicate_event",
    "headline_reader_body_missing": "unreadable_body",
    "deduped_detailed": "duplicate_event",
    "events_merged_duplicates": "duplicate_event",
}


def _summarize_rejections(metrics: dict) -> dict[str, int]:
    """把散落的拒绝计数按统一原因汇总，便于观测每日被拒构成。"""
    summary: dict[str, int] = {}

    def _add(reason: str, count: object) -> None:
        try:
            value = int(count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if value > 0:
            summary[reason] = summary.get(reason, 0) + value

    for key, reason in _REJECTION_METRIC_MAP.items():
        _add(reason, metrics.get(key))
    for column_metrics in (metrics.get("columns") or {}).values():
        if not isinstance(column_metrics, dict):
            continue
        for key, reason in _REJECTION_METRIC_MAP.items():
            _add(reason, column_metrics.get(key))
    return summary


def _normalize_link(url: object) -> str:
    """URL 归一化：去 query/fragment、去尾斜杠、小写。"""
    return re.sub(r"[#?].*$", "", str(url or "").strip()).rstrip("/").lower()


def _event_links(event: dict) -> list[str]:
    """从写作产物中提取可关联的链接（明细用 source_links，要点用 sources）。"""
    links: list[str] = []
    for field in ("source_links", "sources", "links"):
        for item in event.get(field) or []:
            url = (item.get("url") or item.get("link")) if isinstance(item, dict) else item
            normalized = _normalize_link(url)
            if normalized:
                links.append(normalized)
    for field in ("link", "url", "source_url"):
        normalized = _normalize_link(event.get(field))
        if normalized:
            links.append(normalized)
    return links


def _score_lookup(scored_events: list[dict] | None) -> dict[str, dict]:
    """按 event_key/标题/链接建立评分查找表（写作产物不带评分）。"""
    score_map: dict[str, dict] = {}
    for entry in scored_events or []:
        for key in (entry.get("event_key"), entry.get("title_zh"), entry.get("title")):
            normalized = _normalize_event_title(key)
            if normalized and normalized not in score_map:
                score_map[normalized] = entry
        link = _normalize_link(entry.get("link") or entry.get("url"))
        if link:
            score_map.setdefault(f"link::{link}", entry)
    return score_map


def _lookup_scored(score_map: dict[str, dict], event: dict) -> dict:
    for key in (event.get("event_key"), event.get("title_zh"), event.get("title")):
        normalized = _normalize_event_title(key)
        if normalized and normalized in score_map:
            return score_map[normalized]
    for link in _event_links(event):
        entry = score_map.get(f"link::{link}")
        if entry:
            return entry
    return {}


def _select_lead_event(columns: dict, scored_events: list[dict] | None = None) -> dict | None:
    """跨栏目选择当日头条：newsworthiness 优先，其次 score。

    写作产物（明细事件）本身不带评分，按 event_key/标题关联评分记录后再比较。
    """
    score_map = _score_lookup(scored_events)

    best: dict | None = None
    best_key = (0.0, 0.0)
    for col_key in COLUMN_ORDER:
        for event in columns.get(col_key, {}).get("detailed_events", []) or []:
            title = str(event.get("title_zh") or event.get("title") or "").strip()
            if not title:
                continue
            scored_entry = _lookup_scored(score_map, event)
            newsworthiness = _to_float(scored_entry.get("newsworthiness"))
            score = _to_float(scored_entry.get("score"))
            key = (newsworthiness, score)
            if best is None or key > best_key:
                best_key = key
                best = {
                    "column": col_key,
                    "title": title,
                    "body": str(event.get("reader_body") or event.get("core_facts") or "").strip(),
                    "score": scored_entry.get("score"),
                    "newsworthiness": scored_entry.get("newsworthiness"),
                }
    return best if best and best.get("title") else None


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1.0


def _selection_reason(event: dict, slot: str) -> str:
    """为归档记录生成可审计的选择理由。"""
    parts: list[str] = []
    score = event.get("score")
    if score is not None:
        parts.append(f"score={score}")
    newsworthiness = event.get("newsworthiness")
    if newsworthiness not in (None, ""):
        parts.append(f"nw={newsworthiness}")
    stage = str(event.get("event_stage") or "").strip()
    if stage:
        parts.append(f"stage={stage}")
    parts.append(f"slot={slot}")
    return ", ".join(parts)


def _write_candidates_archive(spec, scored_events: list[dict], columns: dict) -> str:
    """归档评分与选择结果到 {site_root}/candidates/{report_key}/，便于复盘与回归。"""
    base_dir = Path(spec.output_dir).parent / "candidates" / spec.report_key
    base_dir.mkdir(parents=True, exist_ok=True)
    coverage = {"start": spec.since.isoformat(), "end": spec.until.isoformat()}

    score_map = _score_lookup(scored_events)
    score_items = []
    for entry in scored_events:
        score_items.append({
            "candidate_id": str(entry.get("event_key") or entry.get("link") or ""),
            "link": entry.get("link", ""),
            "title": entry.get("title", ""),
            "source": entry.get("source", ""),
            "column": entry.get("column", ""),
            "score": entry.get("score"),
            "is_hard_news": bool(entry.get("is_hard_news")),
            "newsworthiness": entry.get("newsworthiness"),
            "routine": entry.get("routine"),
            "event_key": entry.get("event_key", ""),
        })
    score_payload = {
        "schemaVersion": 1,
        "date": spec.report_key,
        "coverage": coverage,
        "items": score_items,
    }

    selection_items = []
    for col_key, payload in columns.items():
        for slot, key in (("detailed", "detailed_events"), ("headline", "headline_only_events")):
            for event in payload.get(key, []) or []:
                scored_entry = _lookup_scored(score_map, event)
                merged = {**scored_entry, **event} if scored_entry else event
                selection_items.append({
                    "candidate_id": str(event.get("event_key") or event.get("title_zh") or event.get("title") or ""),
                    "column": col_key,
                    "slot": slot,
                    "title_zh": event.get("title_zh", ""),
                    "score": merged.get("score"),
                    "selection_reason": _selection_reason(merged, slot),
                    "sources": event.get("source_links", []),
                })
    selection_payload = {
        "schemaVersion": 1,
        "date": spec.report_key,
        "coverage": coverage,
        "items": selection_items,
    }

    (base_dir / "score.json").write_text(
        json.dumps(score_payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (base_dir / "selection.json").write_text(
        json.dumps(selection_payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return str(base_dir)


def _audit_daily_content(
    columns: dict[str, dict],
    allowed_dates: list[str] | None,
    body_date_year: int | None,
) -> dict[str, int]:
    """轻量日报内容审计，只计数不阻断发布。"""
    metrics = {
        "duplicate_titles": 0,
        "old_body_dates": 0,
        "fallback_boilerplate": 0,
        "truncated_titles": 0,
        "meta_commentary": 0,
        "pipeline_leak": 0,
        "untranslated_terms": 0,
        "long_titles": 0,
        "long_headline_bodies": 0,
    }
    allowed = set(allowed_dates or [])

    for column in columns.values():
        seen_titles: list[str] = []
        for event in column.get("headline_only_events", []) or []:
            headline_title = str(event.get("title_zh") or event.get("title") or "").strip()
            if len(headline_title) > 22:
                metrics["long_titles"] += 1
            headline_body = str(event.get("reader_body") or "").strip()
            display_text = headline_body or headline_title
            if "…" in display_text or "..." in display_text:
                metrics["truncated_titles"] += 1
            if len(headline_body) > 46:
                metrics["long_headline_bodies"] = metrics.get("long_headline_bodies", 0) + 1
        for event in column.get("detailed_events", []):
            title = str(event.get("title_zh") or event.get("title") or "").strip()
            norm_title = _normalize_event_title(title)
            if norm_title and any(
                norm_title == old or SequenceMatcher(None, norm_title, old).ratio() >= 0.58
                for old in seen_titles
            ):
                metrics["duplicate_titles"] += 1
            if norm_title:
                seen_titles.append(norm_title)
            if title.endswith(("承", "垄")) or "…" in title or "..." in title:
                metrics["truncated_titles"] += 1
            if len(title) > 26:
                metrics["long_titles"] += 1

            body = str(event.get("reader_body") or event.get("core_facts") or "").strip()
            first_date = _first_body_date(body, body_date_year)
            if first_date and allowed and first_date not in allowed:
                metrics["old_body_dates"] += 1
            if "现有材料未提供更多可核验细节" in body:
                metrics["fallback_boilerplate"] += 1
            if _META_COMMENTARY_RE.search(body):
                metrics["meta_commentary"] += 1
            if _PIPELINE_LEAK_RE.search(body):
                metrics["pipeline_leak"] += 1
            if count_untranslated_terms(f"{title} {body}"):
                metrics["untranslated_terms"] += 1

    return metrics


async def _generate_all_column_digests(
    columns_cfg: dict[str, dict],
    column_candidates: dict[str, list[dict]],
    history_context: str,
    ai_config: dict,
    word_count_min: int,
    word_count_max: int,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """生成四栏 digest；慢模型串行，避免写作阶段触发限速。"""
    base_url = str((ai_config or {}).get("base_url") or "").rstrip("/")
    serial_digest = (
        base_url == "https://open.bigmodel.cn/api/paas/v4"
        or "api.baicai798.cn" in base_url
    )
    concurrency = 1 if serial_digest else 4
    semaphore = asyncio.Semaphore(concurrency)

    def _fallback_events(candidates: list[dict]) -> list[dict]:
        events: list[dict] = []
        for candidate in candidates[:5]:
            fallback_event = _build_fallback_detailed_event(candidate)
            if fallback_event:
                events.append(fallback_event)
        return events

    async def _generate(col_key: str, col_cfg: dict) -> tuple[str, list[dict], str | None]:
        candidates = column_candidates.get(col_key, [])
        if not candidates:
            return col_key, [], None
        async with semaphore:
            try:
                events = await generate_column_digest(
                    column_key=col_key,
                    column_label=col_cfg.get("label", col_key),
                    events=candidates,
                    history_context=history_context,
                    ai_config=ai_config,
                    word_count_min=word_count_min,
                    word_count_max=word_count_max,
                )
                return col_key, events, None
            except Exception as exc:
                fallback = _fallback_events(candidates)
                return col_key, fallback, str(exc)

    results = await asyncio.gather(*[
        _generate(col_key, col_cfg) for col_key, col_cfg in columns_cfg.items()
    ])
    column_results = {col_key: events for col_key, events, _ in results if events}
    failures = {col_key: err for col_key, _, err in results if err}

    # 同机构去重：每栏同一机构/主体最多保留 2 条重点解析
    MAX_SAME_ORG = 2
    for col_key, events in column_results.items():
        column_results[col_key] = _limit_same_org_events(events, MAX_SAME_ORG)

    return column_results, failures


def _fill_underrepresented_columns(
    column_results: dict[str, list[dict]],
    column_candidates: dict[str, list[dict]],
    columns_cfg: dict[str, dict],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """AI 写作后，某栏目事件数不足 min_items 时，从候选中补充。"""
    filled = {col_key: list(items) for col_key, items in column_results.items()}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, col_cfg in columns_cfg.items():
        min_items = col_cfg.get("min_items", 3)
        current = filled.get(col_key, [])
        if len(current) >= min_items:
            metrics[col_key] = {"fill_added": 0}
            continue

        candidates = column_candidates.get(col_key, [])
        existing_titles = {str(e.get("title_zh", "")).strip() for e in current}
        existing_bodies = {str(e.get("reader_body") or e.get("core_facts") or "").strip() for e in current}
        added = 0

        for candidate in candidates:
            if len(current) + added >= min_items:
                break
            title = str(candidate.get("title_zh") or candidate.get("title") or "").strip()
            if not title or title in existing_titles:
                continue
            fallback_event = _build_fallback_detailed_event(candidate)
            if not fallback_event:
                continue
            reader_body = str(fallback_event.get("reader_body") or fallback_event.get("core_facts") or "").strip()
            if not reader_body or reader_body in existing_bodies:
                continue
            filled[col_key].append(fallback_event)
            existing_titles.add(str(fallback_event.get("title_zh") or title).strip())
            existing_bodies.add(reader_body)
            added += 1

        metrics[col_key] = {"fill_added": added}

    return filled, metrics


def _ai_expand_fallback_events(
    column_results: dict[str, list[dict]],
    column_candidates: dict[str, list[dict]],
    columns_cfg: dict[str, dict],
    ai_config: dict,
    max_per_column: int = 2,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """栏目重点解析不足时，用 AI 把候选扩写成中文简讯正文。"""
    expanded: dict[str, list[dict]] = {key: list(value) for key, value in column_results.items()}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, col_cfg in columns_cfg.items():
        min_items = int(col_cfg.get("min_items", 3) or 3)
        current = expanded.get(col_key, [])
        deficit = min_items - len(current)
        if deficit <= 0:
            continue

        used_titles = {
            _normalize_event_title(event.get("title_zh") or event.get("title"))
            for event in current
        }
        picks: list[dict] = []
        for candidate in column_candidates.get(col_key, []):
            link = str(candidate.get("link") or "").strip()
            if not link:
                continue
            title = str(candidate.get("title_zh") or candidate.get("title") or "").strip()
            if not title or _normalize_event_title(title) in used_titles:
                continue
            if str(candidate.get("freshness_status") or "") not in {"today", "recent_followup"}:
                continue
            picks.append(candidate)
            if len(picks) >= min(deficit, max_per_column):
                break

        if not picks:
            continue

        entries = [
            {
                "link": candidate.get("link"),
                "title": candidate.get("title"),
                "summary": candidate.get("summary"),
                "content": str(candidate.get("content") or "")[:500],
                "source": candidate.get("source"),
                "event_date": candidate.get("event_date") or candidate.get("freshness_date"),
            }
            for candidate in picks
        ]
        bodies = asyncio.run(generate_fallback_bodies(entries, ai_config))

        added = 0
        for candidate in picks:
            body = bodies.get(str(candidate.get("link") or "").strip())
            if not body:
                continue
            title = str(candidate.get("title_zh") or "").strip()
            if not _contains_meaningful_cjk(title):
                summary = str(candidate.get("summary") or "").strip()
                title = summary[:36].rstrip(" ，,。；;:：") or body[:36]
            expanded.setdefault(col_key, []).append({
                **candidate,
                "title_zh": title,
                "reader_body": body,
                "core_facts": body,
                "summary": body,
            })
            added += 1

        if added:
            metrics.setdefault(col_key, {})["ai_fallback_added"] = added

    return expanded, metrics


def _limit_same_org_events(events: list[dict], max_per_org: int) -> list[dict]:
    """限制同一机构/主体的事件数量，超出的降级为丢弃。"""
    if not events or max_per_org <= 0:
        return events

    # 高频机构关键词 → 归一化标识
    _ORG_KEYWORDS = {
        "FTC": "ftc", "联邦贸易委员会": "ftc",
        "SEC": "sec", "证券交易委员会": "sec",
        "美联储": "fed", "Federal Reserve": "fed",
        "最高法院": "supreme_court", "Supreme Court": "supreme_court",
        "白宫": "white_house", "White House": "white_house",
        "国会": "congress", "Congress": "congress",
        "NATO": "nato", "北约": "nato",
        "欧盟": "eu", "EU": "eu",
    }

    org_counts: dict[str, int] = {}
    kept: list[dict] = []

    for event in events:
        title = str(event.get("title_zh") or "").strip()
        body = str(event.get("reader_body") or event.get("core_facts") or "").strip()
        text = f"{title} {body}"

        org_id = ""
        for keyword, normalized in _ORG_KEYWORDS.items():
            if keyword in text:
                org_id = normalized
                break

        if not org_id:
            kept.append(event)
            continue

        count = org_counts.get(org_id, 0)
        if count < max_per_org:
            org_counts[org_id] = count + 1
            kept.append(event)
        # 超出限制的丢弃

    return kept


# ── 质量门禁 ──

_FORBIDDEN_LABELS: list[str] = [
    "核心事实：", "核心事实:", "背景脉络：", "背景脉络:",
    "背景与影响：", "背景与影响:", "可能影响：", "可能影响:",
    "为什么值得关注：", "为什么值得关注:",
]

_FORBIDDEN_PHRASES: list[str] = [
    "据报道", "据悉", "有消息称", "值得注意的是", "需要指出的是",
    "凸显了", "反映了", "意味着", "标志着", "引发了讨论",
    "增添了变数", "存在不确定性", "产生深远影响", "仍需观察",
    "对于读者来说", "值得关注的是",
]

# 元评论句式（描述报道本身而非事实）
_META_COMMENTARY_RE = re.compile(
    r"(报道把|讨论焦点|此次被证实的是|此次公布的是|此次变化集中在"
    r"|这次\S{0,6}(表态|警告|召见|发布)\S{0,4}把|把\S{0,8}列为\S{0,6}对象"
    r"|报道将|报道把这一)"
)

# 管道/采集信息泄漏（系统元数据写进正文）
_PIPELINE_LEAK_RE = re.compile(
    r"(聚合条目|收录了这条|转载自|抓取|来源层级|多来源收录|Google News 聚合)"
)

# 观点/分析稿标题（不进要点列表）
_OPINION_TITLE_RE = re.compile(
    r"(为何|为什么|如何|解读|观察|盘点|展望|一文看懂|背后|意味着什么|说明了什么"
    r"|关键所在|关键在哪|何利害关系|有何|前景|影响几何|^分析|^前瞻|^复盘|^影评|^书评"
    r"|或迎|看多|看空|转机|拐点|研判|料将|料无|几无|恐将|恐难"
    r"|^帮助|^助|^指南|新闻综述|新闻速览|一周要闻|每日简报"
    r"|^helping\b|^how\s+to\b)",
    re.IGNORECASE,
)

# 公关语（标题命中时剔除）
_PROMO_WORD_RE = re.compile(r"(新洞察|赋能|重磅|颠覆|引爆|震撼|重新构想|重新定义|再想象|以 .{0,10} 重新)")


@lru_cache(maxsize=1)
def _soft_news_regex() -> re.Pattern:
    """软新闻关键词（配置 + 中文兜底），用于要点列表过滤。"""
    keywords: list[str] = []
    try:
        from config import load_config

        keywords = [
            str(item)
            for item in (load_config().get("rules", {}).get("soft_news_keywords") or [])
            if str(item).strip()
        ]
    except Exception:
        keywords = []
    terms = [re.escape(item) for item in keywords]
    if not terms:
        terms = [re.escape(item) for item in ("celebrity", "sports", "爱犬", "宠物")]
    return re.compile("|".join(terms), re.IGNORECASE)


def _glossary_name_bigrams() -> set[str]:
    """术语表中人名/机构/法案的中文双字片段（用于排除专名造成的误判）。"""
    try:
        groups = _load_glossary()
    except NameError:  # pragma: no cover - 防御
        return set()
    fragments: set[str] = set()
    for group in ("people", "orgs", "laws"):
        for zh in groups.get(group, {}):
            normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(zh)).lower()
            for i in range(len(normalized) - 1):
                fragments.add(normalized[i:i + 2])
    return fragments


def _same_event_titles(
    left: str,
    right: str,
    ratio_floor: float = 0.45,
    min_shared: int = 3,
) -> bool:
    """判断两条标题是否同一事件：相似度 + 有效双字组重合（排除专名碎片）。"""
    norm_left = _normalize_event_title(left)
    norm_right = _normalize_event_title(right)
    if not norm_left or not norm_right or min(len(norm_left), len(norm_right)) < 8:
        return False
    if SequenceMatcher(None, norm_left, norm_right).ratio() < ratio_floor:
        return False
    stop = _glossary_name_bigrams()
    bigrams_left = {norm_left[i:i + 2] for i in range(len(norm_left) - 1)}
    bigrams_right = {norm_right[i:i + 2] for i in range(len(norm_right) - 1)}
    shared = (bigrams_left & bigrams_right) - stop
    return len(shared) >= min_shared


def _sanitize_event_text(text: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    cleaned = text
    for label in _FORBIDDEN_LABELS:
        if label in cleaned:
            issues.append(f"标签残留: {label}")
            cleaned = cleaned.replace(label, "")
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in cleaned:
            issues.append(f"禁用套话: {phrase}")
            cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, issues


def _first_body_date(text: str, default_year: int | None = None) -> str:
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if match and default_year:
        return f"{default_year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return ""


def _body_date_allowed(body: str, allowed_body_dates: set[str], body_date_year: int | None) -> bool:
    first_date = _first_body_date(body, int(body_date_year) if body_date_year else None)
    return not (first_date and allowed_body_dates and first_date not in allowed_body_dates)


def _rewrite_body_date(body: str, allowed_body_dates: set[str], body_date_year: int | None) -> tuple[str, bool]:
    """把正文首个日期改成允许窗口内的日期，仅在正文里已经存在日期表达时使用。"""
    first_date = _first_body_date(body, int(body_date_year) if body_date_year else None)
    if not first_date or not allowed_body_dates or first_date in allowed_body_dates:
        return body, False

    replacement_date = sorted(allowed_body_dates)[-1]
    try:
        dt = datetime.fromisoformat(replacement_date)
    except ValueError:
        return body, False
    replacement_text = f"{dt.month} 月 {dt.day} 日"

    patterns = [
        r"(20\d{2})-(\d{1,2})-(\d{1,2})",
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    ]
    rewritten = body
    for pattern in patterns:
        rewritten, count = re.subn(pattern, replacement_text, rewritten, count=1)
        if count:
            return rewritten, True
    return body, False


def _structured_event_date_in_window(event: dict, allowed_body_dates: set[str]) -> bool:
    if not allowed_body_dates:
        return True
    for field in ("event_date", "freshness_date"):
        value = str(event.get(field) or "").strip()
        if value and value in allowed_body_dates:
            return True
    return False


def _event_to_headline_only(event: dict) -> dict | None:
    title = str(event.get("title_zh") or event.get("title") or "").strip()
    if not title:
        return None
    reader_body = _build_headline_only_reader_body(event)
    if not reader_body:
        reader_body = title
    return {
        **event,
        "title_zh": title,
        "reader_body": reader_body,
        "core_facts": reader_body,
        "summary": reader_body,
        "content": reader_body,
    }


def _validate_event(event: dict, gate_config: dict | None = None) -> list[str]:
    """验证单个事件的质量门禁。gate_config 为 None 时使用默认阈值。"""
    cfg = gate_config or {}
    min_chars = cfg.get("min_chars", 40)
    max_chars = cfg.get("max_chars", 260)
    min_sentences = cfg.get("min_sentences", 2)
    max_sentences = cfg.get("max_sentences", 4)
    require_date_in_body = bool(cfg.get("require_date_in_body", False))
    allowed_body_dates = {str(d) for d in cfg.get("allowed_body_dates", []) if str(d)}
    body_date_year = cfg.get("body_date_year")

    issues: list[str] = []
    body = str(event.get("reader_body", "")).strip()
    if not body:
        issues.append("reader_body 为空")
        return issues
    sentences = re.split(r"[。！？!?]", body)
    sentences = [s for s in sentences if s.strip()]
    if len(sentences) < min_sentences:
        issues.append(f"句数不足: {len(sentences)} 句（要求 {min_sentences}-{max_sentences} 句）")
    elif len(sentences) > max_sentences:
        issues.append(f"句数过多: {len(sentences)} 句（要求 {min_sentences}-{max_sentences} 句）")
    char_count = len(body)
    if char_count < min_chars:
        issues.append(f"字数过少: {char_count} 字（要求 {min_chars}-{max_chars} 字）")
    elif char_count > max_chars:
        issues.append(f"字数过多: {char_count} 字（要求 {min_chars}-{max_chars} 字）")
    if require_date_in_body and not re.search(
        r"(\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}-\d{1,2}-\d{1,2})",
        body,
    ):
        issues.append("缺少明确日期表达")
    first_date = _first_body_date(body, int(body_date_year) if body_date_year else None)
    if first_date and allowed_body_dates and first_date not in allowed_body_dates:
        issues.append(f"正文日期不在日报窗口: {first_date}")
    return issues


def sanitize_or_validate_events(
    events: list[dict],
    gate_config: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """清理并验证事件列表。gate_config 传给 _validate_event 用于阈值配置。"""
    all_issues: list[str] = []
    cleaned_events: list[dict] = []
    allowed_body_dates = {str(d) for d in (gate_config or {}).get("allowed_body_dates", []) if str(d)}
    body_date_year = (gate_config or {}).get("body_date_year")
    require_date_in_body = bool((gate_config or {}).get("require_date_in_body", False))

    for i, event in enumerate(events):
        title = event.get("title_zh", f"事件{i+1}")
        body = str(event.get("reader_body", "")).strip()
        cleaned_body, sanitize_issues = _sanitize_event_text(body)
        if sanitize_issues:
            for issue in sanitize_issues:
                all_issues.append(f"[{title}] {issue}")
            event = {**event, "reader_body": cleaned_body}
            if event.get("core_facts") == body:
                event["core_facts"] = cleaned_body

        if require_date_in_body and not _body_date_allowed(cleaned_body, allowed_body_dates, body_date_year):
            if not _structured_event_date_in_window(event, allowed_body_dates):
                validate_issues = _validate_event(event, gate_config)
            else:
                first_date = _first_body_date(cleaned_body, int(body_date_year) if body_date_year else None)
                rewritten_body, rewritten = _rewrite_body_date(cleaned_body, allowed_body_dates, body_date_year)
                if rewritten:
                    event = {**event, "reader_body": rewritten_body}
                    if event.get("core_facts") == cleaned_body:
                        event["core_facts"] = rewritten_body
                    cleaned_body = rewritten_body
                    replacement_date = sorted(allowed_body_dates)[-1]
                    all_issues.append(f"[{title}] 正文日期已重写: {first_date} -> {replacement_date}")
                validate_issues = _validate_event(event, gate_config)
        else:
            validate_issues = _validate_event(event, gate_config)
        for issue in validate_issues:
            all_issues.append(f"[{title}] {issue}")
        if any("正文日期不在日报窗口" in issue for issue in validate_issues):
            continue
        if not cleaned_body.strip():
            all_issues.append(f"[{title}] 严重: reader_body 清理后为空，已移除")
            continue
        cleaned_events.append(event)
    return cleaned_events, all_issues


def _to_candidate_dict(entry: dict) -> dict:
    return {
        "title": entry.get("title", ""),
        "title_zh": entry.get("title_zh", ""),
        "source": entry.get("source", ""),
        "score": entry.get("score", 0),
        "summary": entry.get("summary", ""),
        "content": entry.get("content", ""),
        "source_links": entry.get("source_links", []),
        "language": entry.get("language", ""),
        "tags": entry.get("tags", []),
        "event_key": entry.get("event_key", ""),
        "is_hard_news": entry.get("is_hard_news", False),
        "published": entry.get("published", ""),
        "fetched": entry.get("fetched", ""),
        "freshness_date": entry.get("freshness_date", ""),
        "event_date": entry.get("event_date", ""),
        "freshness_status": entry.get("freshness_status", ""),
        "column": entry.get("column", ""),
    }


def _event_identity(entry: dict) -> str:
    return str(entry.get("title_zh") or entry.get("title") or entry.get("event_key") or "").strip()


def _select_daily_column_items(
    scored_items: list[dict],
    fallback_items: list[dict],
    target_items: int,
    max_items: int,
    headline_items: int,
    min_score: float,
    global_source_counts: dict[str, int] | None = None,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """日报按数量优先补足主新闻和次要新闻，并限制单源占比。"""
    high_score = [item for item in scored_items if (item.get("score") or 0) >= min_score]
    low_score = [item for item in scored_items if (item.get("score") or 0) < min_score]

    detailed: list[dict] = []
    used: set[str] = set()
    column_source_counts: dict[str, int] = {}
    total_counts = global_source_counts if global_source_counts is not None else {}
    column_cap = _source_column_cap(max_items, target_items)
    metrics = {
        "detailed_filled_from_low_score": 0,
        "headline_filled_from_low_score": 0,
        "headline_filled_from_non_hard_news": 0,
        "source_quota_dropped": 0,
    }

    def _source_allowed(item: dict) -> bool:
        source = str(item.get("source") or "").strip()
        if not source:
            return True
        if column_source_counts.get(source, 0) >= column_cap:
            return False
        return total_counts.get(source, 0) < MAX_EVENTS_PER_SOURCE_TOTAL

    def _mark_source(item: dict) -> None:
        source = str(item.get("source") or "").strip()
        if not source:
            return
        column_source_counts[source] = column_source_counts.get(source, 0) + 1
        total_counts[source] = total_counts.get(source, 0) + 1

    detailed_target = max_items if max_items > 0 else target_items
    for pool_name, pool in (("high", high_score), ("low", low_score)):
        for item in pool:
            identity = _event_identity(item)
            if not identity or identity in used:
                continue
            if not _source_allowed(item):
                metrics["source_quota_dropped"] += 1
                continue
            detailed.append(_to_candidate_dict(item))
            used.add(identity)
            _mark_source(item)
            if pool_name == "low":
                metrics["detailed_filled_from_low_score"] += 1
            if len(detailed) >= detailed_target:
                break
        if len(detailed) >= detailed_target:
            break

    headline: list[dict] = []
    for pool_name, pool in (("high", high_score), ("low", low_score), ("non_hard", fallback_items)):
        for item in pool:
            identity = _event_identity(item)
            if not identity or identity in used:
                continue
            if not _source_allowed(item):
                metrics["source_quota_dropped"] += 1
                continue
            headline.append(_to_candidate_dict(item))
            used.add(identity)
            _mark_source(item)
            if pool_name == "low":
                metrics["headline_filled_from_low_score"] += 1
            if pool_name == "non_hard":
                metrics["headline_filled_from_non_hard_news"] += 1
            if len(headline) >= headline_items:
                break
        if len(headline) >= headline_items:
            break

    # 配额不得清空栏目：若候选存在但全被配额挡下，强制保留最高分一条
    if not detailed and not headline and headline_items + (max_items or target_items) > 0:
        for pool in (high_score, low_score, fallback_items):
            for item in pool:
                identity = _event_identity(item)
                if not identity or identity in used:
                    continue
                detailed.append(_to_candidate_dict(item))
                used.add(identity)
                metrics["source_quota_forced"] = metrics.get("source_quota_forced", 0) + 1
                break
            if detailed:
                break

    metrics["detailed_filled"] = len(detailed)
    metrics["headline_filled"] = len(headline)
    return detailed, headline, metrics


def _is_cryptic_headline_only_title(title: str) -> bool:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return True

    if re.search(r"[《》]", text) and re.search(r"(法案|决议|決議|修正案|草案)", text):
        return False

    compact = re.sub(r"[\s\-_/,.():;]", "", text)
    compact_lower = compact.lower()

    if re.fullmatch(r"[A-Z]{2,8}", text):
        return True
    if re.fullmatch(r"[A-Z0-9.\-]{2,12}", text):
        return True
    if re.fullmatch(r"(第?\s*\d+\s*(号|項|案|法案|决议|決議))", text):
        return True
    if (
        re.search(r"(第\s*\d+\s*号|H\.?\s?R\.?\s?\d+|S\.?\s?\d+)", text)
        and re.search(r"(法案|决议|決議|修正案|草案)", text)
        and not re.search(r"(通过|否决|签署|提出|提交|表决|推进|撤回|批准|驳回|生效|废除)", text)
    ):
        return True
    if re.fullmatch(r"(法案|决议|決議|修正案|草案)\s*[A-Z0-9.\-]{1,16}", text):
        return True
    if re.fullmatch(r"[a-z]{2,10}", compact_lower):
        return True

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    has_action = bool(re.search(r"(通过|否决|签署|起诉|调查|裁定|宣布|推进|施压|会晤|达成|反对|批准|要求|发布|警告|计划|暂停|扩大|收紧|下调|上调)", text))
    if has_cjk and not has_action and len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)) <= 8:
        return True

    return False


def _event_url_set(event: dict) -> set[str]:
    """收集事件来源链接（归一化），用于同源去重。"""
    urls: set[str] = set()
    for field in ("source_links", "sources", "links"):
        for item in event.get(field) or []:
            url = (item.get("url") or item.get("link")) if isinstance(item, dict) else item
            normalized = _normalize_link(url)
            if normalized:
                urls.add(normalized)
    for field in ("link", "url"):
        normalized = _normalize_link(event.get(field))
        if normalized:
            urls.add(normalized)
    return urls


_LIVE_BLOG_TITLE_RE = re.compile(r"^(直播|live)\s*[：:]", re.IGNORECASE)
_DANGLING_TITLE_TAIL = "称据的与对将把及或但而则又也"

def _is_live_blog_title(title: str) -> bool:
    """直播页标题（Live:/直播：）不适合作为单条要点。"""
    return bool(_LIVE_BLOG_TITLE_RE.search(str(title or "").strip()))


def _is_truncated_headline_title(title: str) -> bool:
    """标题被截断或悬空（省略号、标点收尾、虚词/动词收尾）。"""
    text = str(title or "").strip()
    if not text:
        return True
    if text.endswith(("…", "...", "，", ",", "、", "：", ":", "；", ";", "（", "(", "—")):
        return True
    return text[-1] in _DANGLING_TITLE_TAIL


def _build_headline_only_reader_body(item: dict) -> str:
    for field in ("summary", "content"):
        text = re.sub(r"\s+", " ", str(item.get(field, "") or "")).strip()
        if not text:
            continue
        if _looks_like_english_fragment(text):
            continue
        sentence_match = re.match(r"(.+?[。！？!?])", text)
        sentence = sentence_match.group(1).strip() if sentence_match else text[:80].rstrip(" ，,。；;:：")
        if sentence and sentence[-1] not in "。！？!?":
            sentence += "。"
        if _looks_like_english_fragment(sentence):
            continue
        if _is_uninformative_bill_sentence(sentence):
            continue
        if sentence:
            return sentence
    return ""


def _body_needs_translation(item: dict) -> bool:
    """判断条目正文是否为未翻译的英文（此时可用已翻译标题兜底展示）。"""
    for field in ("summary", "content"):
        text = re.sub(r"\s+", " ", str(item.get(field, "") or "")).strip()
        if text and _looks_like_english_fragment(text):
            return True
    return False


def _compact_headline_title(text: str, limit: int = 21) -> str:
    """要点标题压缩到 limit 字（含省略号不超过 limit+1），避免长句标题。"""
    title = str(text or "").strip()
    if len(title) <= limit + 1:
        return title
    return title[:limit].rstrip(" ，,。；;:：") + "…"


def _compact_headline_body(text: str, limit: int = 36) -> str:
    """压缩要点正文：完整首句优先，其次标点处收尾，无法可读截断返回空串。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    sentence_match = re.match(r"(.+?[。！？!?])", text)
    if sentence_match and len(sentence_match.group(1)) <= limit + 10:
        return sentence_match.group(1)
    cut = text[:limit]
    for punct in ("，", "、", "；", "：", "。"):
        idx = cut.rfind(punct)
        if idx >= limit // 3:
            return cut[: idx + 1]
    return ""


def _merge_headline_metrics(column_metrics_map: dict, col_key: str, new_metrics: dict) -> None:
    """累加合并要点过滤计数（第二次 normalize 不应覆盖第一次的丢弃数）。"""
    target = column_metrics_map.setdefault(col_key, {})
    for key, value in new_metrics.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _is_duplicate_headline_title(title: str, existing_titles: list[str]) -> bool:
    """要点标题与既有标题是否同一事件：常规阈值或「主题双字组≥4」强信号。"""
    return any(
        _same_event_titles(title, existing)
        or _same_event_titles(title, existing, ratio_floor=0.25, min_shared=4)
        for existing in existing_titles
    )


def _normalize_headline_only_by_column(
    column_headline_only: dict[str, list[dict]],
    detailed_titles: dict[str, list[str]] | None = None,
    detailed_events: dict[str, list[dict]] | None = None,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    normalized_columns: dict[str, list[dict]] = {}
    metrics: dict[str, dict[str, int]] = {}
    seen_headline_links: set[str] = set()
    seen_headline_titles: list[str] = []

    for col_key, items in column_headline_only.items():
        kept: list[dict] = []
        cryptic_dropped = 0
        unreadable_dropped = 0
        body_from_title = 0
        opinion_dropped = 0
        promo_dropped = 0
        duplicate_dropped = 0
        soft_dropped = 0
        live_blog_dropped = 0
        truncated_dropped = 0
        # 跨栏目去重：与所有栏目的明细标题比较，避免同一事件在不同栏目重复出现
        existing_titles = [
            title
            for titles in (detailed_titles or {}).values()
            for title in titles
        ]
        existing_links: set[str] = set()
        for events in (detailed_events or {}).values():
            for event in events:
                existing_links |= _event_url_set(event)

        for item in items:
            title_zh = str(item.get("title_zh") or item.get("title") or "").strip()
            if _looks_like_english_fragment(title_zh):
                unreadable_dropped += 1
                continue
            if _is_live_blog_title(title_zh):
                print(f"   [要点直播页] {col_key}: {title_zh[:36]}")
                live_blog_dropped += 1
                continue
            if _is_truncated_headline_title(title_zh):
                print(f"   [要点截断] {col_key}: {title_zh[:36]}")
                truncated_dropped += 1
                continue
            if _is_cryptic_headline_only_title(title_zh):
                cryptic_dropped += 1
                continue
            if _OPINION_TITLE_RE.search(title_zh):
                opinion_dropped += 1
                continue
            if _PROMO_WORD_RE.search(title_zh):
                promo_dropped += 1
                continue
            if _soft_news_regex().search(title_zh):
                print(f"   [要点软新闻] {col_key}: {title_zh[:36]}")
                soft_dropped += 1
                continue
            if _is_duplicate_headline_title(title_zh, existing_titles) or _is_duplicate_headline_title(
                title_zh, seen_headline_titles
            ):
                print(f"   [要点去重] {col_key}: {title_zh[:40]}")
                duplicate_dropped += 1
                continue
            item_links = _event_url_set(item)
            if item_links and (item_links & existing_links or item_links & seen_headline_links):
                print(f"   [要点同源] {col_key}: {title_zh[:40]}")
                duplicate_dropped += 1
                continue

            reader_body = _build_headline_only_reader_body(item)
            if not reader_body or not re.search(r"[\u4e00-\u9fff]", reader_body):
                has_text = any(str(item.get(field) or "").strip() for field in ("summary", "content"))
                if _body_needs_translation(item) or not has_text:
                    # 正文缺失或未翻译：回退到已翻译标题，渲染层可直接展示
                    reader_body = title_zh
                    body_from_title += 1
                else:
                    print(f"   [要点丢弃] {col_key}: {title_zh[:36]}（正文不可用）")
                    unreadable_dropped += 1
                    continue

            compacted_body = _compact_headline_body(reader_body)
            if not compacted_body:
                compacted_body = _compact_headline_title(title_zh)
            kept.append({
                **item,
                "title_zh": _compact_headline_title(title_zh),
                "reader_body": compacted_body,
            })
            seen_headline_links |= item_links
            seen_headline_titles.append(title_zh)

        normalized_columns[col_key] = kept
        metrics[col_key] = {
            "headline_cryptic_dropped": cryptic_dropped,
            "headline_reader_body_missing": unreadable_dropped,
            "headline_body_from_title": body_from_title,
            "headline_opinion_dropped": opinion_dropped,
            "headline_promo_dropped": promo_dropped,
            "headline_duplicate_dropped": duplicate_dropped,
            "headline_soft_dropped": soft_dropped,
            "headline_live_blog_dropped": live_blog_dropped,
            "headline_truncated_dropped": truncated_dropped,
        }

    return normalized_columns, metrics


async def _translate_headline_only_by_column(
    column_headline_only: dict[str, list[dict]],
    ai_config: dict,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """将次要新闻标题批量翻译成中文。"""
    translated_columns: dict[str, list[dict]] = {}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, items in column_headline_only.items():
        metrics[col_key] = {
            "headline_translated": 0,
            "headline_translation_failed": 0,
        }
        if not items:
            translated_columns[col_key] = []
            continue
        titles = [str(item.get("title") or "").strip() for item in items]
        try:
            translated_titles = await translate_headline_titles(titles, ai_config)
        except Exception:
            translated_titles = []

        translated_events: list[dict] = []
        for item, title_zh in zip(items, translated_titles):
            clean_title = str(title_zh).strip()
            fallback_reader_body = _build_headline_only_reader_body(item)
            if not clean_title and fallback_reader_body:
                # 翻译缺失时用正文首句压缩成短标题（上限 22 字），避免整句正文当标题
                compact_title = fallback_reader_body[:22].rstrip(" ，,。；;:：")
                if len(fallback_reader_body) > 22:
                    compact_title += "…"
                translated_events.append({
                    **item,
                    "title_zh": compact_title or fallback_reader_body,
                    "reader_body": fallback_reader_body,
                })
                metrics[col_key]["headline_translated"] += 1
                continue
            if not clean_title or _looks_like_english_fragment(clean_title):
                metrics[col_key]["headline_translation_failed"] += 1
                continue
            translated_events.append({
                **item,
                "title_zh": clean_title,
            })
            metrics[col_key]["headline_translated"] += 1

        dropped = len(items) - len(translated_events) - metrics[col_key]["headline_translation_failed"]
        if dropped > 0:
            metrics[col_key]["headline_translation_failed"] += dropped
        translated_columns[col_key] = translated_events

    return translated_columns, metrics


def _prepare_report_inputs(
    spec: ReportSpec,
    scored_events: list[dict],
    db,
    metrics: dict,
) -> ReportPreparation:
    print(f"\n[合并] 事件级合并...")
    merged_events = merge_events(scored_events)
    events_merged_duplicates = len(scored_events) - len(merged_events)
    metrics["events_merged_duplicates"] = events_merged_duplicates
    if events_merged_duplicates:
        print(f"   合并同事件重复: {events_merged_duplicates} 条")
    by_column_counts: dict[str, int] = {}
    for event in merged_events:
        column_key = event.get("column", "unknown")
        by_column_counts[column_key] = by_column_counts.get(column_key, 0) + 1
    print(f"   {len(scored_events)} 条 → {len(merged_events)} 个事件")
    for column_key in sorted(by_column_counts):
        print(f"     {column_key}: {by_column_counts[column_key]}")

    print(f"\n[分桶] 按栏目分桶...")
    by_column: dict[str, list[dict]] = {}
    for event in merged_events:
        column_key = event.get("column", "us_politics")
        by_column.setdefault(column_key, []).append(event)
    for column_key in by_column:
        by_column[column_key].sort(key=lambda item: item.get("score", 0) or 0, reverse=True)
    for column_key in sorted(by_column):
        print(f"   {column_key}: {len(by_column[column_key])} 条")
        metrics["columns"].setdefault(column_key, {})["post_merge_scored"] = len(by_column[column_key])

    cn_selected_by_column: dict[str, int] = {}
    for column_key, entries in by_column.items():
        cn_selected_by_column[column_key] = sum(
            1
            for entry in entries
            if str(entry.get("language", "")).lower().startswith("zh")
            or "cn_source" in {str(tag).lower() for tag in entry.get("tags", [])}
        )
    if cn_selected_by_column:
        metrics["cn_source_selected_by_column"] = cn_selected_by_column
        metrics["cn_source_selected"] = sum(cn_selected_by_column.values())

    print(f"\n[候选] 每栏按配额选择...")
    column_candidates: dict[str, list[dict]] = {}
    column_headline_only: dict[str, list[dict]] = {}
    global_source_counts: dict[str, int] = {}
    for column_key, column_cfg in spec.column_quotas.items():
        column_items = by_column.get(column_key, [])
        detailed_n = column_cfg.get("target_items", 5)
        max_n = column_cfg.get("max_items", detailed_n)
        headline_n = column_cfg.get("headline_items", 0) if spec.allow_headline_only else 0
        if spec.report_type == "daily":
            fallback_items = spec.fallback_candidates_by_column.get(column_key, [])
            detailed_items, headline_items, fill_metrics = _select_daily_column_items(
                scored_items=column_items,
                fallback_items=fallback_items,
                target_items=detailed_n,
                max_items=max_n,
                headline_items=headline_n,
                min_score=spec.min_llm_score,
                global_source_counts=global_source_counts,
            )
        else:
            detailed_items = [_to_candidate_dict(event) for event in column_items[:min(len(column_items), max_n)]]
            remaining = column_items[len(detailed_items):]
            headline_items = [_to_candidate_dict(event) for event in remaining[:headline_n]]
            fill_metrics = {
                "detailed_filled": len(detailed_items),
                "headline_filled": len(headline_items),
                "detailed_filled_from_low_score": 0,
                "headline_filled_from_low_score": 0,
                "headline_filled_from_non_hard_news": 0,
            }

        column_candidates[column_key] = detailed_items
        column_headline_only[column_key] = headline_items
        print(f"   {column_key}: 编号 {len(detailed_items)} + 无序 {len(headline_items)}")
        metrics["columns"].setdefault(column_key, {}).update(fill_metrics)
        metrics["columns"].setdefault(column_key, {})["post_score_filtered"] = sum(
            1 for item in column_items if (item.get("score") or 0) >= spec.min_llm_score
        )

    history_context = _load_history_context(db, spec.history_days)
    return ReportPreparation(
        merged_events=merged_events,
        by_column=by_column,
        column_candidates=column_candidates,
        column_headline_only=column_headline_only,
        history_context=history_context,
        metrics=metrics,
    )


# ── 核心编排 ──

def build_report(
    spec: ReportSpec,
    scored_events: list[dict],
    config: dict,
    ai_config: dict,
    db,
    phase_metrics: dict | None = None,
) -> dict:
    """
    共享报告编排器。

    scored_events: 已评分的 dict 列表（来自 score_batch 或数据库转换）。
    日报在调用前完成 fetch + score，周报/月报在调用前完成 DB 读取 + 格式转换。

    返回 stats dict。
    """
    start_time = datetime.now()
    digest_cfg = config.get("digest", {})
    columns_cfg = spec.column_quotas
    metrics = phase_metrics.copy() if phase_metrics else {}
    metrics.setdefault("columns", {})
    metrics.setdefault("ai", {})

    print("=" * 60)
    print(spec.title)
    print(f"时间: {start_time.isoformat()}")
    print(f"窗口: {spec.since.strftime('%Y-%m-%d %H:%M')} → {spec.until.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    if not scored_events:
        print("\n[警告] 无候选事件")
        return {"total_selected": 0}

    if spec.report_type in {"weekly", "monthly"}:
        gate = _build_periodical_gate_result(spec, scored_events, db, config)
        metrics["periodical_gate"] = gate
        if gate["gate_failed"]:
            reason_text = "；".join(gate["reasons"]) if gate["reasons"] else "未满足周期报告门禁"
            print(f"\n[错误] {spec.report_type} 门禁失败: {reason_text}")
            return {
                "total_selected": 0,
                "error": "periodical_gate_failed",
                "gate_failed": True,
                "gate_reasons": gate["reasons"],
                "metrics": metrics,
                "source_health_summary": gate.get("source_health_summary", {}),
            }

    preparation = _prepare_report_inputs(spec, scored_events, db, metrics)
    merged_events = preparation.merged_events
    by_column = preparation.by_column
    column_candidates = preparation.column_candidates
    column_headline_only = preparation.column_headline_only
    history_context = preparation.history_context
    metrics = preparation.metrics

    # ── 每栏生成 digest ──
    print(f"\n[写作] 每栏生成 digest...")
    column_results, digest_failures = asyncio.run(_generate_all_column_digests(
        columns_cfg=columns_cfg,
        column_candidates=column_candidates,
        history_context=history_context,
        ai_config=ai_config,
        word_count_min=spec.word_count_min,
        word_count_max=spec.word_count_max,
    ))
    metrics["ai"]["digest_failures"] = digest_failures
    for col_key, error in digest_failures.items():
        print(f"   [{col_key}] 栏目写作失败，已降级为候选摘要: {error}")

    if spec.report_type == "daily":
        column_headline_only, headline_metrics = asyncio.run(
            _translate_headline_only_by_column(column_headline_only, ai_config)
        )
        for col_key, translated_metrics in headline_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(translated_metrics)
        detailed_title_map = {
            key: [str(ev.get("title_zh") or ev.get("title") or "") for ev in events]
            for key, events in column_results.items()
        }
        column_headline_only, normalized_metrics = _normalize_headline_only_by_column(
            column_headline_only, detailed_titles=detailed_title_map, detailed_events=column_results,
        )
        for col_key, column_metrics in normalized_metrics.items():
            _merge_headline_metrics(metrics["columns"], col_key, column_metrics)
        column_results, detailed_metrics = _normalize_detailed_events_to_chinese(column_results)
        for col_key, column_metrics in detailed_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)
        # AI 兜底扩写优先：摘要过短/英文候选时用 AI 生成简讯正文（质量高于规则兜底）
        column_results, ai_fallback_metrics = _ai_expand_fallback_events(
            column_results, column_candidates, columns_cfg, ai_config,
        )
        for col_key, column_metrics in ai_fallback_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)
        column_results, fallback_metrics = _ensure_daily_detailed_events(column_results, column_candidates)
        for col_key, column_metrics in fallback_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)
        # 保底填充：AI 写作丢弃过多时，从候选中补充
        column_results, fill_metrics = _fill_underrepresented_columns(
            column_results, column_candidates, columns_cfg,
        )
        for col_key, column_metrics in fill_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)
        column_results, dedupe_metrics = _dedupe_daily_column_events(column_results)
        for col_key, column_metrics in dedupe_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)

    # ── 提炼要点 ──
    print(f"\n[要点] 提炼要点...")
    highlights = build_reader_highlights(column_results, limit=spec.highlights_limit)
    print(f"   要点: {len(highlights)} 条")

    # ── 质量门禁 ──
    print(f"\n[门禁] 质量检查...")
    gate_config = dict(config.get("rules", {}).get("quality_gate") or {})
    if spec.report_type == "daily":
        gate_config["require_date_in_body"] = bool(
            config.get("format_contract", {}).get("require_date_in_body", False)
        )
        try:
            report_date = datetime.fromisoformat(spec.report_key).date()
            gate_config["allowed_body_dates"] = [
                (report_date - timedelta(days=1)).isoformat(),
                report_date.isoformat(),
            ]
            gate_config["body_date_year"] = report_date.year
        except ValueError:
            pass
    total_issues = 0
    for col_key in list(column_results.keys()):
        events = column_results[col_key]
        if not events:
            continue
        cleaned_events: list[dict] = []
        downgraded_events: list[dict] = []
        issues_count = 0
        for event in events:
            cleaned, issues = sanitize_or_validate_events([event], gate_config)
            if issues:
                for issue in issues:
                    print(f"   [{col_key}] {issue}")
                issues_count += len(issues)
            if not cleaned:
                fallback_event = _event_to_headline_only(event)
                if fallback_event:
                    downgraded_events.append(fallback_event)
                continue

            validated_event = cleaned[0]
            has_fatal_issue = any(
                token in issue
                for issue in issues
                for token in ("正文日期不在日报窗口", "句数不足", "句数过多", "字数过少", "字数过多", "reader_body 为空")
            )
            if has_fatal_issue:
                fallback_event = _event_to_headline_only(validated_event)
                if fallback_event:
                    downgraded_events.append(fallback_event)
                continue

            cleaned_events.append(validated_event)

        if downgraded_events:
            metrics["columns"].setdefault(col_key, {})["detailed_downgraded_to_headline_only"] = len(downgraded_events)
            column_headline_only.setdefault(col_key, []).extend(downgraded_events)
        column_results[col_key] = cleaned_events
        total_issues += issues_count

    if spec.report_type == "daily":
        detailed_title_map = {
            key: [str(ev.get("title_zh") or ev.get("title") or "") for ev in events]
            for key, events in column_results.items()
        }
        column_headline_only, post_downgrade_headline_metrics = _normalize_headline_only_by_column(
            column_headline_only, detailed_titles=detailed_title_map, detailed_events=column_results,
        )
        for col_key, column_metrics in post_downgrade_headline_metrics.items():
            _merge_headline_metrics(metrics["columns"], col_key, column_metrics)
        for col_key in list(column_headline_only.keys()):
            before = len(column_headline_only[col_key])
            column_headline_only[col_key] = _limit_same_org_events(
                column_headline_only[col_key], MAX_HEADLINE_PER_ORG
            )
            capped = before - len(column_headline_only[col_key])
            if capped > 0:
                metrics["columns"].setdefault(col_key, {})["headline_org_capped"] = capped
    print(f"   {'全部通过' if not total_issues else f'{total_issues} 个质量问题（已清理）'}")

    # ── 组装 columns ──
    columns: dict[str, dict[str, list[dict] | str]] = {}
    for col_key in COLUMN_ORDER:
        columns[col_key] = {
            "analysis": "",
            "detailed_events": column_results.get(col_key, []),
            "headline_only_events": column_headline_only.get(col_key, []),
        }
        metrics["columns"].setdefault(col_key, {})["rendered_detailed"] = len(columns[col_key]["detailed_events"])
        metrics["columns"].setdefault(col_key, {})["rendered_headline_only"] = len(columns[col_key]["headline_only_events"])

    if spec.report_type == "daily":
        content_audit = _audit_daily_content(
            columns,
            gate_config.get("allowed_body_dates"),
            gate_config.get("body_date_year"),
        )
        metrics["content_audit"] = content_audit
        try:
            archive_dir = _write_candidates_archive(spec, scored_events, columns)
            metrics["candidates_archive"] = archive_dir
        except Exception as exc:  # noqa: BLE001 - 归档失败不影响发布
            print(f"   候选归档失败: {exc}")
        rejections = _summarize_rejections(metrics)
        if rejections:
            metrics["rejections"] = rejections
            parts = [
                f"{REJECT_REASONS.get(reason, reason)} {count}"
                for reason, count in sorted(rejections.items(), key=lambda item: -item[1])
            ]
            print(f"   拒绝汇总: {'，'.join(parts)}")
        print(
            "   内容审计: "
            f"重复标题 {content_audit['duplicate_titles']}，"
            f"旧日期正文 {content_audit['old_body_dates']}，"
            f"fallback 套话 {content_audit['fallback_boilerplate']}，"
            f"疑似截断标题 {content_audit['truncated_titles']}"
        )

    overview = PeriodicalOverview()
    daily_overview = ""
    if spec.report_type == "daily":
        try:
            daily_overview = asyncio.run(generate_daily_overview(
                title=spec.title,
                columns=columns,
                ai_config=ai_config,
            ))
        except Exception as exc:
            metrics["ai"]["daily_overview_failure"] = str(exc)
            print(f"   [daily overview] 生成失败，已降级为空导语: {exc}")
            daily_overview = ""
    if spec.report_type in {"weekly", "monthly"}:
        try:
            raw_overview = asyncio.run(generate_periodical_overview(
                report_type=spec.report_type,
                title=spec.title,
                highlights=highlights,
                columns=columns,
                ai_config=ai_config,
            ))
            overview = PeriodicalOverview.from_raw(raw_overview, list(columns.keys()))
        except Exception as exc:
            metrics["ai"]["overview_failure"] = str(exc)
            print(f"   [overview] 生成失败，已降级为空总览: {exc}")
            overview = _build_periodical_overview_fallback(spec.report_type, spec.title, highlights, columns)
        for col_key, analysis in overview.column_analyses.items():
            if col_key in columns:
                columns[col_key]["analysis"] = analysis

    overview_payload = overview.to_payload()

    # ── 构造统一发布元数据 ──
    manifest = build_manifest(
        product_key=spec.product_key,
        report_type=spec.report_type,
        report_key=spec.report_key,
        title=spec.title,
        pub_date=spec.pub_date or datetime.now(BEIJING_TZ),
        base_url=spec.base_url,
    )

    # ── 保存报告 ──
    print(f"\n[保存] 生成文件...")
    lead_event = _select_lead_event(columns, scored_events) if spec.report_type == "daily" else None
    if lead_event:
        metrics["lead"] = lead_event
    meta = {
        "title": spec.title,
        "lead": "" if spec.report_type == "daily" else overview_payload.get("summary", ""),
        "lead_event": lead_event,
        "highlights": highlights,
        "date": spec.report_key,
        "require_non_empty_columns": bool(
            config.get("format_contract", {}).get("require_non_empty_columns", False)
        ),
        "require_detailed_events": bool(
            config.get("format_contract", {}).get("require_detailed_events", False)
        ),
        "require_date_in_body": bool(
            config.get("format_contract", {}).get("require_date_in_body", False)
        ),
        "overview": overview_payload,
        "report_since": spec.since.isoformat(),
        "report_until": spec.until.isoformat(),
        "pub_date": manifest.pub_date.isoformat(),
    }

    md_path, html_path = save_daily_report(meta, columns, spec.output_dir, report_type=spec.report_type, manifest=manifest)
    print(f"   Markdown: {md_path}")
    print(f"   HTML: {html_path}")

    # ── 保存 RSS Feed ──
    print(f"\n[Feed] 更新 RSS...")
    save_feed(meta, columns, spec.feed_path, spec.base_url,
              report_type=spec.report_type, report_key=spec.report_key, manifest=manifest)
    print(f"   Feed: {spec.feed_path}")

    # ── 统计 ──
    duration = (datetime.now() - start_time).total_seconds()
    total_events = sum(len(evts) for evts in column_results.values())
    col_counts = {k: len(v) for k, v in column_results.items()}

    stats = {
        "duration_seconds": round(duration, 1),
        "report_type": spec.report_type,
        "report_key": spec.report_key,
        "total_input": len(scored_events),
        "total_merged": len(merged_events),
        "total_filtered": sum(len(v) for v in by_column.values()),
        "total_selected": total_events,
        "column_counts": col_counts,
        "outputs": {"markdown": md_path, "html": html_path, "feed": spec.feed_path},
        "metrics": {
            **metrics,
            "report": {
                "title": spec.title,
                "published_at": manifest.pub_date.isoformat(),
                "duration_seconds": round(duration, 1),
            },
        },
    }

    print(f"\n{'=' * 60}")
    print(f"完成: {spec.title}")
    print(f"{'=' * 60}")
    print(f"  耗时: {duration:.1f}s")
    print(f"  输入: {len(scored_events)} → 合并: {len(merged_events)} → 精选: {total_events}")
    for col, cnt in sorted(col_counts.items()):
        print(f"    {col}: {cnt}")

    return stats
