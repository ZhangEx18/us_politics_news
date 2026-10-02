"""Kagi 完整采集、译文门禁与 RSS 发布契约。"""

import asyncio
import copy
import importlib.util
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import kagi_digest
from ai_analyzer import _load_ai_config
from config import load_product_config
from feed_builder import RSS_NS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kagi_schedule_gate", ROOT / "scripts/kagi_schedule_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SUMMARY = "据报道，政府公布新的交通建设计划，拟于明年启动并分阶段实施，相关资金仍待审批。主管部门尚未确认最终预算和施工时间，各方将继续讨论具体安排。"
TITLE = "政府公布交通建设计划预算仍待进一步审批"


@pytest.fixture
def config(tmp_path):
    config = load_product_config("kagi_digest")
    config["publish"]["site_root"] = str(tmp_path / "kagi_digest")
    config["publish"]["feed_path"] = str(tmp_path / "feeds/kagi_digest.xml")
    config["storage"]["db_path"] = str(tmp_path / "cache.db")
    return config


def make_source():
    categories = {}
    for slug in ["usa", "world", "tech", "science", "business"]:
        sid = "tech" if slug == "science" else slug
        categories[slug] = [{"id": sid, "cluster_number": 1, "title": "A transport plan",
                             "short_summary": "The government announced a plan. Funding is not approved.",
                             "articles": [{"link": "https://example.com/story?a=1&b=2"}, {"link": "javascript:alert(1)"}]}]
    return {"report_date": "2026-10-02", "source_date": "2026-10-01", "batch_id": "batch",
            "batch_created_at": "2026-10-01T12:00:00Z", "categories": categories}


def make_issue(config):
    source = make_source()
    translated = {story["id"]: {"id": story["id"], "title_zh": TITLE, "summary_zh": SUMMARY}
                  for rows in source["categories"].values() for story in rows}
    return kagi_digest.assemble_issue(source, translated, config)


def test_pinned_batch_fetches_all_five_categories_and_pages(monkeypatch, config):
    requested = []

    async def api(session, url, **params):
        requested.append((url, params))
        if url.endswith("/api/batches"):
            assert params["from"].startswith("2026-10-01")
            return {"batches": [{"id": "batch", "createdAt": "2026-10-01T12:00:00Z"}]}
        if url.endswith("/categories"):
            return {"categories": [{"categoryId": slug, "id": slug} for slug in make_source()["categories"]]}
        slug = url.split("/")[-2]
        source = make_source()["categories"][slug][0]
        row = {**source, "id": f"{slug}-{params['offset']}", "cluster_number": params["offset"] + 1}
        return {"batchId": "batch", "totalStories": 2, "stories": [row]}

    monkeypatch.setattr(kagi_digest, "_get_json", api)
    result = asyncio.run(kagi_digest.fetch_issue(config, "2026-10-02"))
    assert set(result["categories"]) == {"world", "usa", "tech", "science", "business"}
    assert all(len(rows) == 2 for rows in result["categories"].values())
    assert all("/batch/" in url for url, _ in requested if url.endswith("/stories"))


@pytest.mark.parametrize("failure", ["missing_batch", "missing_category", "wrong_batch", "empty_stories", "duplicate_ids"])
def test_source_failures_prevent_partial_issue(monkeypatch, config, failure):
    async def api(session, url, **params):
        if url.endswith("/api/batches"):
            return {"batches": [] if failure == "missing_batch" else [{"id": "batch", "createdAt": "2026-10-01T12:00:00Z"}]}
        if url.endswith("/categories"):
            slugs = ["usa"] if failure == "missing_category" else make_source()["categories"]
            return {"categories": [{"categoryId": slug, "id": slug} for slug in slugs]}
        row = {"id": "one", "cluster_number": 1, "title": "title", "short_summary": "summary"}
        rows = [] if failure == "empty_stories" else [row, row] if failure == "duplicate_ids" else [row]
        return {"batchId": "other" if failure == "wrong_batch" else "batch", "totalStories": len(rows), "stories": rows}

    monkeypatch.setattr(kagi_digest, "_get_json", api)
    with pytest.raises(ValueError):
        asyncio.run(kagi_digest.fetch_issue(config, "2026-10-02"))


def test_translation_repairs_invalid_output_and_caches_validated_items(monkeypatch, config):
    story = make_source()["categories"]["usa"][0]
    responses = iter([json.dumps({"items": []}), json.dumps({"items": [{"id": "usa", "title_zh": TITLE, "summary_zh": SUMMARY}]})])

    async def llm(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(kagi_digest, "_call_llm", llm)
    ai_config = {"model": "test", "base_url": "https://example.com"}
    result = asyncio.run(kagi_digest.translate_stories([story], config, ai_config))
    assert result["usa"]["summary_zh"] == SUMMARY

    async def unavailable(*args, **kwargs):
        raise AssertionError("已验证缓存应支持无网络重跑")

    monkeypatch.setattr(kagi_digest, "_call_llm", unavailable)
    assert asyncio.run(kagi_digest.translate_stories([story], config, ai_config)) == result


def test_translation_uses_repair_and_audit_models_by_stage(monkeypatch, config):
    story = make_source()["categories"]["usa"][0]
    valid = {"items": [{"id": "usa", "title_zh": TITLE, "summary_zh": SUMMARY}]}
    audit_failure = {"items": [{"id": "usa", "ok": False, "reason": "标题需要更准确"}]}
    calls = []
    responses = iter([json.dumps(valid), json.dumps(audit_failure), json.dumps(valid), json.dumps({"items": [{"id": "usa", "ok": True, "reason": ""}]})])

    async def llm(prompt, options, **kwargs):
        calls.append(options["model"])
        return next(responses)

    monkeypatch.setattr(kagi_digest, "_call_llm", llm)
    ai_config = {
        "model": "gpt-5.4-mini", "repair_model": "gpt-5.4",
        "audit_model": "gpt-6.1-sol", "base_url": "https://example.com",
    }
    result = asyncio.run(kagi_digest.translate_stories([story], config, ai_config))
    assert result["usa"]["title_zh"] == TITLE
    assert calls == ["gpt-5.4-mini", "gpt-6.1-sol", "gpt-5.4", "gpt-6.1-sol"]


def test_ai_stage_models_read_from_environment(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key")
    monkeypatch.setenv("AI_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("AI_REPAIR_MODEL", "gpt-5.4")
    monkeypatch.setenv("AI_AUDIT_MODEL", "gpt-6.1-sol")
    config = _load_ai_config()
    assert config["model"] == "gpt-5.4-mini"
    assert config["repair_model"] == "gpt-5.4"
    assert config["audit_model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("changes", [{"summary_zh": "Short English summary"}, {"summary_zh": "字" * 121},
                                     {"title_zh": "标题"}, {"summary_zh": SUMMARY + "<script>"}])
def test_translation_contract_rejects_unusable_content(changes):
    with pytest.raises(ValueError):
        kagi_digest.validate_translation({"id": "story", "title_zh": TITLE, "summary_zh": SUMMARY, **changes})


def test_rss_full_content_attribution_dedup_and_same_date_replacement(config):
    issue = make_issue(config)
    kagi_digest.publish_issue(issue, config)
    first_publication = issue["published_at"]
    kagi_digest.publish_issue(make_issue(config), config)
    kagi_digest.validate_outputs(config, issue["report_date"])
    root = ET.parse(config["publish"]["feed_path"]).getroot()
    items = root.findall("channel/item")
    assert len(items) == 1
    body = items[0].findtext(f"{{{RSS_NS}}}encoded")
    assert body.count("<h2>") == 4
    assert body.count("<h3>") == 4
    assert "Kagi News" in body and "example.com" in body
    assert "javascript:" not in body and "a=1&amp;b=2" in body
    assert items[0].findtext("link").startswith("https://zhangex18.github.io/")
    assert root.find("channel/{http://www.w3.org/2005/Atom}link").attrib["href"].endswith("/feeds/kagi_digest.xml")
    saved = json.loads((Path(config["publish"]["site_root"]) / "daily/2026-10-02.json").read_text())
    assert saved["published_at"] == first_publication


def test_incomplete_translation_keeps_previous_feed(config):
    issue = make_issue(config)
    kagi_digest.publish_issue(issue, config)
    feed = Path(config["publish"]["feed_path"])
    original = feed.read_bytes()
    broken = copy.deepcopy(issue)
    broken["columns"][0]["items"].clear()
    with pytest.raises(ValueError, match="遗漏"):
        kagi_digest.publish_issue(broken, config)
    assert feed.read_bytes() == original


def test_morning_gate_and_published_slot_idempotency(config):
    tz = ZoneInfo("Asia/Shanghai")
    assert gate.choose_report_date(config, now=datetime(2026, 10, 2, 7, 29, tzinfo=tz)) == (False, "2026-10-02")
    assert gate.choose_report_date(config, now=datetime(2026, 10, 2, 7, 30, tzinfo=tz)) == (True, "2026-10-02")
    kagi_digest.publish_issue(make_issue(config), config)
    assert gate.choose_report_date(config, now=datetime(2026, 10, 2, 8, 30, tzinfo=tz)) == (False, "2026-10-02")
    assert gate.choose_report_date(config, now=datetime(2026, 10, 2, 8, 30, tzinfo=tz), force=True)[0]


def test_malformed_archive_is_retried_instead_of_skipped(config):
    kagi_digest.publish_issue(make_issue(config), config)
    archive = Path(config["publish"]["site_root"]) / "daily/2026-10-02.json"
    archive.write_text("{", encoding="utf-8")
    assert gate.choose_report_date(config, now=datetime(2026, 10, 2, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))[0]
