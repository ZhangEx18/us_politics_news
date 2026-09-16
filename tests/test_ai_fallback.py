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
