"""AI 通道降级测试 — ai_analyzer._call_llm"""

import asyncio

import pytest

import ai_analyzer


def _fallback_config() -> dict:
    return {
        "api_key": "k",
        "base_url": "https://primary.example",
        "model": "primary-model",
        "fallback": {
            "api_key": "k",
            "base_url": "https://backup.example",
            "model": "backup-model",
        },
    }


def test_call_llm_falls_back_when_primary_fails(monkeypatch):
    calls: list[str] = []

    async def fake_once(prompt, config, timeout=120):
        calls.append(config["model"])
        if config["model"] == "primary-model":
            raise RuntimeError("LLM API 错误 404: model_not_found")
        return "ok"

    monkeypatch.setattr(ai_analyzer, "_call_llm_once", fake_once)

    result = asyncio.run(ai_analyzer._call_llm("prompt", _fallback_config()))

    assert result == "ok"
    assert calls == ["primary-model", "backup-model"]


def test_call_llm_raises_without_fallback(monkeypatch):
    async def fake_once(prompt, config, timeout=120):
        raise RuntimeError("LLM API 错误 404: model_not_found")

    monkeypatch.setattr(ai_analyzer, "_call_llm_once", fake_once)

    config = {"api_key": "k", "base_url": "https://primary.example", "model": "primary-model"}
    with pytest.raises(RuntimeError):
        asyncio.run(ai_analyzer._call_llm("prompt", config))


def test_call_llm_does_not_loop_fallback(monkeypatch):
    calls: list[str] = []

    async def fake_once(prompt, config, timeout=120):
        calls.append(config["model"])
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_analyzer, "_call_llm_once", fake_once)

    with pytest.raises(RuntimeError):
        asyncio.run(ai_analyzer._call_llm("prompt", _fallback_config()))

    assert calls == ["primary-model", "backup-model"]


def test_load_ai_config_reads_fallback(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "primary-key")
    monkeypatch.setenv("AI_BASE_URL", "https://primary.example")
    monkeypatch.setenv("AI_MODEL", "primary-model")
    monkeypatch.setenv("AI_FALLBACK_BASE_URL", "https://backup.example")
    monkeypatch.setenv("AI_FALLBACK_MODEL", "backup-model")

    config = ai_analyzer._load_ai_config()

    assert config["fallback"]["base_url"] == "https://backup.example"
    assert config["fallback"]["model"] == "backup-model"
    assert config["fallback"]["api_key"] == "primary-key"


def test_generate_fallback_bodies_parses_items(monkeypatch):
    async def fake_call(prompt, config, timeout=120):
        return '{"items": [{"link": "https://a.com", "body": "9 月 16 日，众议院推进决议。"}]}'

    monkeypatch.setattr(ai_analyzer, "_call_llm", fake_call)

    bodies = asyncio.run(ai_analyzer.generate_fallback_bodies(
        [{"link": "https://a.com", "title": "T", "summary": "摘要", "event_date": "2026-09-16"}], {},
    ))

    assert bodies == {"https://a.com": "9 月 16 日，众议院推进决议。"}


def test_generate_fallback_bodies_returns_empty_on_error(monkeypatch):
    async def fake_call(prompt, config, timeout=120):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_analyzer, "_call_llm", fake_call)

    bodies = asyncio.run(ai_analyzer.generate_fallback_bodies(
        [{"link": "https://a.com", "title": "T"}], {},
    ))

    assert bodies == {}


# ── WP3: glossary 术语表 ──


def test_glossary_hint_injects_matched_terms():
    from ai_analyzer import glossary_hint

    hint = glossary_hint("Hegseth faces impeachment vote; Clarity Act blocked in Senate")

    assert "赫格塞思 = Hegseth" in hint
    assert "清晰法案 = Clarity Act" in hint


def test_glossary_hint_empty_when_no_match():
    from ai_analyzer import glossary_hint

    assert glossary_hint("nothing here matches the glossary") == ""


def test_count_untranslated_terms_flags_missing_chinese():
    from ai_analyzer import count_untranslated_terms

    assert count_untranslated_terms("Trump met Xi Jinping") == 2
    assert count_untranslated_terms("特朗普与习近平会面") == 0
    # orgs 组（品牌/机构）不在审计范围
    assert count_untranslated_terms("FTC sues Amazon") == 0


def test_digest_prompt_injects_glossary_hint(monkeypatch):
    import ai_analyzer

    captured: dict = {}

    async def fake_call(prompt, config, timeout=120):
        captured["prompt"] = prompt
        return '{"events": []}'

    monkeypatch.setattr(ai_analyzer, "_call_llm", fake_call)

    try:
        asyncio.run(ai_analyzer.generate_column_digest(
            column_key="us_politics",
            column_label="美国政局",
            events=[{
                "title": "Hegseth faces impeachment vote",
                "summary": "赫格塞思面临弹劾投票。",
                "source": "NPR",
                "source_tier": 1,
                "freshness_date": "2026-09-16",
                "event_date": "2026-09-16",
            }],
            history_context="",
            ai_config={"api_key": "k", "base_url": "https://example.com", "model": "m"},
        ))
    except Exception:
        pass

    assert "赫格塞思 = Hegseth" in captured.get("prompt", "")
