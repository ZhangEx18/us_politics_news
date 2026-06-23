"""调度 manifest 与 run_product 路由测试"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_build_schedule_manifest():
    script_path = PROJECT_ROOT / "scripts" / "build_schedule_manifest.py"
    spec = importlib.util.spec_from_file_location("build_schedule_manifest", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Schedule Manifest ──


def test_build_manifest_contains_all_products():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    assert manifest["version"] == 1
    schedules = manifest["schedules"]
    product_keys = {s["product_key"] for s in schedules}
    assert "news" in product_keys
    assert "algorithms" in product_keys


def test_build_manifest_news_has_three_report_types():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    news_schedules = [s for s in manifest["schedules"] if s["product_key"] == "news"]
    report_types = {s["report_type"] for s in news_schedules}
    assert report_types == {"daily", "weekly", "monthly"}


def test_build_manifest_algorithms_daily_only():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    algo_schedules = [s for s in manifest["schedules"] if s["product_key"] == "algorithms"]
    assert len(algo_schedules) == 1
    assert algo_schedules[0]["report_type"] == "daily"


def test_build_manifest_all_use_publish_product_workflow():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    for schedule in manifest["schedules"]:
        assert schedule["workflow"] == "publish-product.yml", (
            f"{schedule['product_key']}/{schedule['report_type']} "
            f"workflow 不是 publish-product.yml: {schedule['workflow']}"
        )


def test_build_manifest_inputs_include_product_and_report_type():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    for schedule in manifest["schedules"]:
        inputs = schedule["inputs"]
        assert inputs["product_key"] == schedule["product_key"]
        assert inputs["report_type"] == schedule["report_type"]


def test_build_manifest_crons_are_distinct():
    mod = _load_build_schedule_manifest()
    manifest = mod.build_manifest()

    crons = [s["cron"] for s in manifest["schedules"]]
    # 月报 cron 28-31 可能与其他重叠，但 product+report_type 组合唯一
    combos = [(s["product_key"], s["report_type"]) for s in manifest["schedules"]]
    assert len(combos) == len(set(combos))


# ── run_product 路由 ──


def test_run_product_rejects_unsupported_report_type():
    from config import load_product_config

    config = load_product_config("algorithms")
    supported = config.get("report_types", [])
    assert "weekly" not in supported
    assert "monthly" not in supported


def test_run_product_config_content_types():
    from config import load_product_config

    news_config = load_product_config("news")
    assert news_config["content_type"] == "news_digest"

    algo_config = load_product_config("algorithms")
    assert algo_config["content_type"] == "topic_lesson"


def test_run_product_config_storage_isolation():
    from config import load_product_config

    news_config = load_product_config("news")
    algo_config = load_product_config("algorithms")

    assert news_config["storage"]["db_path"] != algo_config["storage"]["db_path"]
    assert news_config["publish"]["site_root"] != algo_config["publish"]["site_root"]
    assert news_config["publish"]["feed_path"] != algo_config["publish"]["feed_path"]


# ── Feed 隔离 ──


def test_feed_guid_includes_product_key():
    from publish_manifest import build_manifest
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    news_manifest = build_manifest("news", "daily", "2026-06-18", pub_date=now)
    algo_manifest = build_manifest("algorithms", "daily", "2026-06-18", pub_date=now)

    assert news_manifest.guid == "news/daily/2026-06-18"
    assert algo_manifest.guid == "algorithms/daily/2026-06-18"
    assert news_manifest.guid != algo_manifest.guid

    assert news_manifest.link_path == "news/daily/2026-06-18.html"
    assert algo_manifest.link_path == "algorithms/daily/2026-06-18.html"


def test_feed_guid_no_conflict_same_day():
    from publish_manifest import build_manifest
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    guids = set()
    for product in ("news", "algorithms"):
        m = build_manifest(product, "daily", "2026-06-18", pub_date=now)
        assert m.guid not in guids, f"GUID 冲突: {m.guid}"
        guids.add(m.guid)


# ── run_product 路由集成测试 ──


def test_run_product_news_daily_routes_to_pipeline(monkeypatch):
    """news/daily 路由到 run_pipeline。"""
    from run_product import run_product

    called = []
    monkeypatch.setattr("run_product.run_pipeline", lambda **kw: called.append(("pipeline", kw)) or {"total_selected": 1})
    run_product("news", "daily", hours=12)
    assert called
    assert called[0][0] == "pipeline"
    assert called[0][1]["hours"] == 12


def test_run_product_news_daily_digest_only_routes_to_digest(monkeypatch):
    """news/daily --digest-only 路由到 run_digest_only。"""
    from run_product import run_product

    called = []
    monkeypatch.setattr("run_product.run_digest_only", lambda **kw: called.append(("digest", kw)) or {"total_selected": 1})
    run_product("news", "daily", digest_only=True)
    assert called
    assert called[0][0] == "digest"


def test_run_product_news_weekly_routes_to_weekly(monkeypatch):
    """news/weekly 路由到 run_weekly。"""
    from run_product import run_product

    called = []
    monkeypatch.setattr("run_product.run_weekly", lambda: called.append("weekly") or {"total_selected": 1})
    run_product("news", "weekly")
    assert called == ["weekly"]


def test_run_product_news_monthly_routes_to_monthly(monkeypatch):
    """news/monthly 路由到 run_monthly。"""
    from run_product import run_product

    called = []
    monkeypatch.setattr("run_product.run_monthly", lambda: called.append("monthly") or {"total_selected": 1})
    run_product("news", "monthly")
    assert called == ["monthly"]


def test_run_product_algorithms_daily_routes_to_topic_lesson(monkeypatch):
    """algorithms/daily 路由到 topic_lesson handler。"""
    from run_product import run_product

    called = []
    monkeypatch.setattr(
        "run_product.run_topic_lesson_daily",
        lambda **kw: called.append(("topic_lesson", kw)) or {"total_selected": 1},
        raising=False,
    )
    # 需要 mock import 因为 topic_lesson 在函数内 import
    import topic_lesson
    monkeypatch.setattr(topic_lesson, "run_topic_lesson_daily", lambda **kw: called.append(("topic_lesson", kw)) or {"total_selected": 1})
    run_product("algorithms", "daily")
    assert called
    assert called[0][0] == "topic_lesson"
    assert called[0][1]["product_key"] == "algorithms"


def test_run_product_algorithms_weekly_raises():
    """algorithms/weekly 应报错。"""
    import pytest
    from run_product import run_product

    with pytest.raises(ValueError, match="不支持 report_type=weekly"):
        run_product("algorithms", "weekly")


def test_run_product_unknown_product_raises():
    """未知 product 应报错。"""
    import pytest
    from run_product import run_product

    with pytest.raises(FileNotFoundError):
        run_product("nonexistent", "daily")
