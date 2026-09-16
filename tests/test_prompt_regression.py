"""Prompt 回归集检查器测试（不调用真实 AI）"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "prompt_regression", _ROOT / "scripts" / "prompt_regression.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_cases_reads_fixtures():
    module = _load_module()

    cases = module.load_cases()

    assert len(cases) >= 6
    ids = {case["id"] for case in cases}
    assert {"routine_notice", "major_ruling", "digest_no_meta_commentary"} <= ids


def test_evaluate_score_case_flags_violations():
    module = _load_module()

    issues = module.evaluate_score_case(
        {"score_max": 69, "is_hard_news": False},
        {"score": 88, "is_hard_news": True, "routine": 0.9},
    )

    assert any("score=88" in issue for issue in issues)
    assert any("is_hard_news" in issue for issue in issues)


def test_evaluate_digest_case_flags_banned_patterns_and_long_sentences():
    module = _load_module()

    events = [{
        "title_zh": "测试",
        "reader_body": "9 月 16 日，报道把这一动作定位为重要进展。这是一个明显超过六十个字符长度的句子，用于验证超长句检测逻辑是否能够正确触发报警，后续内容继续拉长以满足测试需要，直到超过限制为止。",
    }]

    issues = module.evaluate_digest_case(
        {"banned_regex": ["报道把"], "max_sentence_chars": 60, "must_have_date": True},
        events,
    )

    assert any("报道把" in issue for issue in issues)
    assert any("超长句" in issue for issue in issues)


def test_evaluate_fallback_case_flags_latin_and_missing_date():
    module = _load_module()

    issues = module.evaluate_fallback_case(
        {"min_chars": 50, "must_have_date": True, "no_latin_ratio": 0.05},
        {"https://a": "Trump said the House voted to end the war today with wide support."},
    )

    assert any("缺少日期表达" in issue for issue in issues)
    assert any("英文字母占比过高" in issue for issue in issues)
