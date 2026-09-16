#!/usr/bin/env python3
"""Prompt 回归集：对评分/写作/翻译/兜底 prompt 跑真实调用并校验硬指标。

用法：
    python3 scripts/prompt_regression.py --list              # 列出用例（不调用）
    python3 scripts/prompt_regression.py --live              # 全量回归（真实调用，约 10-15 次）
    python3 scripts/prompt_regression.py --live --case major_ruling
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from ai_analyzer import (  # noqa: E402
    _load_ai_config,
    coerce_unit_interval,
    generate_column_digest,
    generate_fallback_bodies,
    score_batch,
    translate_headline_titles,
)

FIXTURES_PATH = _ROOT / "tests" / "fixtures" / "prompt_cases.yaml"
_DATE_RE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日")


def load_cases(path: Path = FIXTURES_PATH) -> list[dict]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases") or []
    return [case for case in cases if isinstance(case, dict)]


def evaluate_score_case(expect: dict, entry: dict | None) -> list[str]:
    if entry is None:
        return ["未返回该条评分"]
    issues: list[str] = []
    score = entry.get("score") or 0
    if "score_min" in expect and score < expect["score_min"]:
        issues.append(f"score={score} < {expect['score_min']}")
    if "score_max" in expect and score > expect["score_max"]:
        issues.append(f"score={score} > {expect['score_max']}")
    if "is_hard_news" in expect and bool(entry.get("is_hard_news")) != expect["is_hard_news"]:
        issues.append(f"is_hard_news={entry.get('is_hard_news')} != {expect['is_hard_news']}")
    routine = coerce_unit_interval(entry.get("routine"))
    if "routine_min" in expect and (routine or 0) < expect["routine_min"]:
        issues.append(f"routine={routine} < {expect['routine_min']}")
    nw = coerce_unit_interval(entry.get("newsworthiness"))
    if "newsworthiness_min" in expect and (nw or 0) < expect["newsworthiness_min"]:
        issues.append(f"newsworthiness={nw} < {expect['newsworthiness_min']}")
    return issues


def evaluate_digest_case(expect: dict, events: list[dict]) -> list[str]:
    if not events:
        return ["AI 未返回任何事件"]
    issues: list[str] = []
    body = " ".join(str(event.get("reader_body", "")) for event in events)
    for pattern in expect.get("banned_regex", []):
        if re.search(pattern, body):
            issues.append(f"命中禁用正则: {pattern}")
    for phrase in expect.get("banned_phrases", []):
        if phrase in body:
            issues.append(f"命中禁用词: {phrase}")
    if expect.get("must_have_date") and not _DATE_RE.search(body):
        issues.append("正文缺少日期表达")
    max_sentence_chars = expect.get("max_sentence_chars")
    if max_sentence_chars:
        for event in events:
            for sentence in re.split(r"(?<=[。！？])", str(event.get("reader_body", ""))):
                sentence = sentence.strip()
                if sentence and len(sentence) > max_sentence_chars:
                    issues.append(f"超长句({len(sentence)}字): {sentence[:24]}…")
                    break
    max_sentences = expect.get("max_sentences")
    if max_sentences:
        for event in events:
            count = len(re.findall(r"[。！？]", str(event.get("reader_body", ""))))
            if count > max_sentences:
                issues.append(f"句数过多({count}): {str(event.get('title_zh',''))[:16]}")
                break
    return issues


def evaluate_translate_case(expect: dict, titles: list[str]) -> list[str]:
    joined = " ".join(str(title) for title in titles)
    issues: list[str] = []
    for term in expect.get("must_contain", []):
        if term not in joined:
            issues.append(f"缺少术语: {term}")
    return issues


def evaluate_fallback_case(expect: dict, bodies: dict[str, str]) -> list[str]:
    body = next((text for text in bodies.values() if text), "")
    if not body:
        return ["未返回正文"]
    issues: list[str] = []
    if len(body) < expect.get("min_chars", 0):
        issues.append(f"字数不足: {len(body)} < {expect['min_chars']}")
    if len(body) > expect.get("max_chars", 10 ** 9):
        issues.append(f"字数超限: {len(body)} > {expect['max_chars']}")
    if expect.get("must_have_date") and not _DATE_RE.search(body):
        issues.append("缺少日期表达")
    latin_ratio = len(re.findall(r"[A-Za-z]", body)) / max(len(body), 1)
    if latin_ratio > expect.get("no_latin_ratio", 1.0):
        issues.append(f"英文字母占比过高: {latin_ratio:.2f}")
    return issues


async def run_score_case(case: dict, ai_config: dict) -> tuple[dict | None, list[str]]:
    entries = case["input"]["entries"]
    scored, errors = await score_batch(entries, {**ai_config, "temperature": 0})
    if errors and not scored:
        return None, [f"评分调用失败: {errors[0][:80]}"]
    entry = next((item for item in scored if item.get("link") == entries[0].get("link")), None)
    return entry, evaluate_score_case(case.get("expect", {}), entry)


async def run_case(case: dict, ai_config: dict, score_cache: dict) -> list[str]:
    case_type = case.get("type")
    if case_type == "score":
        entry, issues = await run_score_case(case, ai_config)
        score_cache[case["id"]] = entry
        return issues
    if case_type == "score_pair":
        (id_a, id_b) = case["inputs"][0]
        score_a = (score_cache.get(id_a) or {}).get("score") or 0
        score_b = (score_cache.get(id_b) or {}).get("score") or 0
        gap = score_b - score_a
        minimum = case.get("expect", {}).get("min_score_gap", 20)
        if gap < minimum:
            return [f"分差不足: {gap} < {minimum}（{id_a}={score_a}, {id_b}={score_b}）"]
        return []
    if case_type == "digest":
        events = case["input"]["events"]
        result = await generate_column_digest(
            column_key="us_politics",
            column_label="美国政局",
            events=events,
            history_context="",
            ai_config=ai_config,
            word_count_min=300,
            word_count_max=800,
        )
        return evaluate_digest_case(case.get("expect", {}), result)
    if case_type == "translate":
        titles = case["input"]["titles"]
        translated = await translate_headline_titles(titles, ai_config)
        return evaluate_translate_case(case.get("expect", {}), translated)
    if case_type == "fallback":
        entries = case["input"]["entries"]
        bodies = await generate_fallback_bodies(entries, ai_config)
        return evaluate_fallback_case(case.get("expect", {}), bodies)
    return [f"未知用例类型: {case_type}"]


async def run_all(cases: list[dict], ai_config: dict) -> int:
    score_cache: dict = {}
    failures = 0
    for case in cases:
        issues = await run_case(case, ai_config, score_cache)
        status = "PASS" if not issues else "FAIL"
        print(f"[{status}] {case['id']}: {case.get('description', '')}")
        for issue in issues:
            print(f"        - {issue}")
        if issues:
            failures += 1
    print(f"\n回归结果: {len(cases) - failures}/{len(cases)} 通过")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Prompt 回归集")
    parser.add_argument("--live", action="store_true", help="执行真实 AI 调用")
    parser.add_argument("--case", help="只跑指定用例 id")
    parser.add_argument("--list", action="store_true", help="列出用例")
    args = parser.parse_args()

    cases = load_cases()
    if args.case:
        cases = [case for case in cases if case.get("id") == args.case]

    if args.list or not args.live:
        for case in cases:
            print(f"- {case['id']} [{case.get('type')}] {case.get('description', '')}")
        if not args.live:
            print("\n提示：加 --live 才会执行真实调用")
        return

    ai_config = _load_ai_config()
    exit_code = asyncio.run(run_all(cases, ai_config))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
