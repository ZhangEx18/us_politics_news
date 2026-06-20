"""GitHub Actions workflow 配置回归测试"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_workflow(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _workflow_triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow[True]


def test_daily_rss_publish_keeps_manual_trigger_and_beijing_report_date_validation():
    workflow = _load_workflow("daily-rss-publish.yml")
    validate_step = next(
        step
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("name") == "Validate output"
    )

    assert _workflow_triggers(workflow) == {"workflow_dispatch": None}
    assert workflow["concurrency"]["group"] == "daily-rss-publish"
    assert "REPORT_DATE=$(python3 scripts/report_date.py)" in validate_step["run"]
    assert '[ -f "$REPORT_FILE" ]' in validate_step["run"]


def test_legacy_fetch_and_publish_workflows_are_manual_only():
    fetch_workflow = _load_workflow("fetch.yml")
    publish_workflow = _load_workflow("publish.yml")

    assert _workflow_triggers(fetch_workflow) == {"workflow_dispatch": None}
    assert _workflow_triggers(publish_workflow) == {"workflow_dispatch": None}
