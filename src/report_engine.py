#!/usr/bin/env python3
"""
报告编排器 — 日报/周报/月报共享的统一 pipeline。

ReportSpec 定义报告类型差异，build_report() 执行共享阶段。
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai_analyzer import (
    generate_column_digest,
    generate_daily_overview,
    generate_periodical_overview,
    merge_events,
    translate_headline_titles,
)
from feed_builder import save_feed
from publish_manifest import build_manifest
from report_renderer import COLUMN_ORDER, save_daily_report

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


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
    """从最终入选事件提炼要点。"""
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
            text = _highlight_text(events[idx])
            if not text or text in highlights:
                continue
            highlights.append(text)
            if len(highlights) >= limit:
                return highlights
    return highlights


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


async def _generate_all_column_digests(
    columns_cfg: dict[str, dict],
    column_candidates: dict[str, list[dict]],
    history_context: str,
    ai_config: dict,
    word_count_min: int,
    word_count_max: int,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """并发生成四栏 digest。"""
    semaphore = asyncio.Semaphore(4)

    def _fallback_reader_body(candidate: dict) -> str:
        summary = str(candidate.get("summary", "")).strip()
        content = str(candidate.get("content", "")).strip()
        body = summary or content
        body = re.sub(r"\s+", " ", body)
        if len(body) > 220:
            body = body[:220].rstrip(" ，,。. ") + "。"
        return body or "该事件写作降级为简版概述，保留标题供后续人工复核。"

    def _fallback_events(candidates: list[dict]) -> list[dict]:
        events: list[dict] = []
        for candidate in candidates[:3]:
            title = str(candidate.get("title", "")).strip()
            if not title:
                continue
            events.append({
                "title_zh": title,
                "reader_body": _fallback_reader_body(candidate),
                "core_facts": _fallback_reader_body(candidate),
            })
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
    return column_results, failures


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


def _validate_event(event: dict, gate_config: dict | None = None) -> list[str]:
    """验证单个事件的质量门禁。gate_config 为 None 时使用默认阈值。"""
    cfg = gate_config or {}
    min_chars = cfg.get("min_chars", 80)
    max_chars = cfg.get("max_chars", 260)
    min_sentences = cfg.get("min_sentences", 2)
    max_sentences = cfg.get("max_sentences", 4)

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
    return issues


def sanitize_or_validate_events(
    events: list[dict],
    gate_config: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """清理并验证事件列表。gate_config 传给 _validate_event 用于阈值配置。"""
    all_issues: list[str] = []
    cleaned_events: list[dict] = []
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
        validate_issues = _validate_event(event, gate_config)
        for issue in validate_issues:
            all_issues.append(f"[{title}] {issue}")
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
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """日报按数量优先补足主新闻和次要新闻。"""
    high_score = [item for item in scored_items if (item.get("score") or 0) >= min_score]
    low_score = [item for item in scored_items if (item.get("score") or 0) < min_score]

    detailed: list[dict] = []
    used: set[str] = set()
    metrics = {
        "detailed_filled_from_low_score": 0,
        "headline_filled_from_low_score": 0,
        "headline_filled_from_non_hard_news": 0,
    }

    detailed_target = max_items if max_items > 0 else target_items
    for pool_name, pool in (("high", high_score), ("low", low_score)):
        for item in pool:
            identity = _event_identity(item)
            if not identity or identity in used:
                continue
            detailed.append(_to_candidate_dict(item))
            used.add(identity)
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
            headline.append(_to_candidate_dict(item))
            used.add(identity)
            if pool_name == "low":
                metrics["headline_filled_from_low_score"] += 1
            if pool_name == "non_hard":
                metrics["headline_filled_from_non_hard_news"] += 1
            if len(headline) >= headline_items:
                break
        if len(headline) >= headline_items:
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
    if re.fullmatch(r"(法案|决议|決議|修正案|草案)\s*[A-Z0-9.\-]{1,16}", text):
        return True
    if re.fullmatch(r"[a-z]{2,10}", compact_lower):
        return True

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    has_action = bool(re.search(r"(通过|否决|签署|起诉|调查|裁定|宣布|推进|施压|会晤|达成|反对|批准|要求|发布|警告|计划|暂停|扩大|收紧|下调|上调)", text))
    if has_cjk and not has_action and len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)) <= 8:
        return True

    return False


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


def _normalize_headline_only_by_column(
    column_headline_only: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    normalized_columns: dict[str, list[dict]] = {}
    metrics: dict[str, dict[str, int]] = {}

    for col_key, items in column_headline_only.items():
        kept: list[dict] = []
        cryptic_dropped = 0
        unreadable_dropped = 0

        for item in items:
            title_zh = str(item.get("title_zh") or item.get("title") or "").strip()
            if _looks_like_english_fragment(title_zh):
                unreadable_dropped += 1
                continue
            if _is_cryptic_headline_only_title(title_zh):
                cryptic_dropped += 1
                continue

            reader_body = _build_headline_only_reader_body(item)
            if not reader_body:
                unreadable_dropped += 1
                continue
            if not re.search(r"[\u4e00-\u9fff]", reader_body):
                unreadable_dropped += 1
                continue

            kept.append({
                **item,
                "title_zh": title_zh,
                "reader_body": reader_body,
            })

        normalized_columns[col_key] = kept
        metrics[col_key] = {
            "headline_cryptic_dropped": cryptic_dropped,
            "headline_reader_body_missing": unreadable_dropped,
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
        titles = [str(item.get("title") or "").strip() for item in items if str(item.get("title") or "").strip()]
        try:
            translated_titles = await translate_headline_titles(titles, ai_config)
        except Exception:
            translated_titles = []

        translated_events: list[dict] = []
        for item, title_zh in zip(items, translated_titles):
            clean_title = str(title_zh).strip()
            fallback_reader_body = _build_headline_only_reader_body(item)
            if not clean_title and fallback_reader_body:
                translated_events.append({
                    **item,
                    "title_zh": fallback_reader_body,
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
        column_headline_only, normalized_metrics = _normalize_headline_only_by_column(column_headline_only)
        for col_key, column_metrics in normalized_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)
        column_results, detailed_metrics = _normalize_detailed_events_to_chinese(column_results)
        for col_key, column_metrics in detailed_metrics.items():
            metrics["columns"].setdefault(col_key, {}).update(column_metrics)

    # ── 提炼要点 ──
    print(f"\n[要点] 提炼要点...")
    highlights = build_reader_highlights(column_results, limit=spec.highlights_limit)
    print(f"   要点: {len(highlights)} 条")

    # ── 质量门禁 ──
    print(f"\n[门禁] 质量检查...")
    gate_config = config.get("rules", {}).get("quality_gate")
    total_issues = 0
    for col_key in list(column_results.keys()):
        events = column_results[col_key]
        if not events:
            continue
        cleaned, issues = sanitize_or_validate_events(events, gate_config)
        if issues:
            for issue in issues:
                print(f"   [{col_key}] {issue}")
            total_issues += len(issues)
        column_results[col_key] = cleaned
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
            overview = PeriodicalOverview()
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
    meta = {
        "title": spec.title,
        "lead": "" if spec.report_type == "daily" else overview_payload.get("summary", ""),
        "highlights": highlights,
        "date": spec.report_key,
        "require_non_empty_columns": bool(
            config.get("format_contract", {}).get("require_non_empty_columns", False)
        ),
        "require_detailed_events": bool(
            config.get("format_contract", {}).get("require_detailed_events", False)
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
