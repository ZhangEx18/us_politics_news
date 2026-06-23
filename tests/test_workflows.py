"""GitHub Actions workflow 配置回归测试"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_workflow(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _workflow_triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow[True]


# ── thin wrapper 测试 ──


def test_daily_rss_publish_is_thin_wrapper_to_publish_product():
    workflow = _load_workflow("daily-rss-publish.yml")
    dispatch = _workflow_triggers(workflow)["workflow_dispatch"]

    assert list(_workflow_triggers(workflow)) == ["workflow_dispatch"]
    assert "digest_only" in dispatch["inputs"]
    assert dispatch["inputs"]["digest_only"]["type"] == "boolean"
    assert workflow["concurrency"]["group"] == "publish-news-daily"

    delegate_job = workflow["jobs"]["delegate"]
    assert delegate_job["uses"] == "ZhangEx18/us_politics_news/.github/workflows/publish-product.yml@main"
    assert delegate_job["with"]["product_key"] == "news"
    assert delegate_job["with"]["report_type"] == "daily"
    assert delegate_job["secrets"] == "inherit"


def test_weekly_publish_is_thin_wrapper_to_publish_product():
    workflow = _load_workflow("weekly-publish.yml")

    assert _workflow_triggers(workflow) == {"workflow_dispatch": None}
    assert workflow["concurrency"]["group"] == "publish-news-weekly"

    delegate_job = workflow["jobs"]["delegate"]
    assert delegate_job["uses"] == "ZhangEx18/us_politics_news/.github/workflows/publish-product.yml@main"
    assert delegate_job["with"]["product_key"] == "news"
    assert delegate_job["with"]["report_type"] == "weekly"
    assert delegate_job["secrets"] == "inherit"


def test_monthly_publish_is_thin_wrapper_to_publish_product():
    workflow = _load_workflow("monthly-publish.yml")

    assert _workflow_triggers(workflow) == {"workflow_dispatch": None}
    assert workflow["concurrency"]["group"] == "publish-news-monthly"

    delegate_job = workflow["jobs"]["delegate"]
    assert delegate_job["uses"] == "ZhangEx18/us_politics_news/.github/workflows/publish-product.yml@main"
    assert delegate_job["with"]["product_key"] == "news"
    assert delegate_job["with"]["report_type"] == "monthly"
    assert delegate_job["secrets"] == "inherit"


# ── publish-product.yml 测试 ──


def test_publish_product_workflow_has_required_inputs():
    workflow = _load_workflow("publish-product.yml")
    dispatch = _workflow_triggers(workflow)["workflow_dispatch"]
    workflow_call = _workflow_triggers(workflow)["workflow_call"]

    assert "product_key" in dispatch["inputs"]
    assert "report_type" in dispatch["inputs"]
    assert dispatch["inputs"]["report_type"]["type"] == "choice"
    assert set(dispatch["inputs"]["report_type"]["options"]) == {"daily", "weekly", "monthly"}
    assert workflow_call["inputs"]["product_key"]["type"] == "string"
    assert workflow_call["inputs"]["report_type"]["type"] == "string"


def test_publish_product_workflow_concurrency():
    workflow = _load_workflow("publish-product.yml")
    assert "publish-" in workflow["concurrency"]["group"]


def test_publish_product_workflow_steps():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]
    step_names = [step.get("name") for step in publish_job["steps"]]

    assert "Read product config" in step_names
    assert "Restore published pages state" in step_names
    assert "Run product pipeline" in step_names
    assert "Build site indexes" in step_names
    assert "Validate output" in step_names
    assert "Deploy to GitHub Pages" in step_names
    assert "Persist state database" in step_names


def test_publish_product_workflow_reads_config_dynamically():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    config_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Read product config"
    )
    assert "load_product_config" in config_step["run"]
    assert "db_path" in config_step["run"]
    assert "state_branch" in config_step["run"]


def test_publish_product_restore_state_uses_branch_ref_with_fallback_paths():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    restore_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Restore state database"
    )
    run = restore_step["run"]

    assert 'git show "origin/${STATE_BRANCH}:${ref_path}"' in run
    assert 'restore_db_from_ref "$DB_PATH" "$DB_PATH"' in run
    assert 'restore_db_from_ref "$LEGACY_DB_PATH" "$DB_PATH"' in run
    assert 'ls -lh "$DB_PATH"' in run


def test_publish_product_workflow_uses_run_product():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    run_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Run product pipeline"
    )
    assert "python3 src/run_product.py" in run_step["run"]
    assert "--product" in run_step["run"]
    assert "--report-type" in run_step["run"]


def test_publish_product_sync_legacy_aliases_uses_heredoc_python():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    sync_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Sync legacy aliases"
    )
    run = sync_step["run"]

    assert sync_step["if"] == "inputs.product_key == 'news'"
    assert "python3 - <<'PY'" in run
    assert "_sync_legacy_news_aliases" in run
    assert 'Path("docs")' in run


def test_publish_product_validate_checks_all_report_types():
    """validate step 对 news 四栏目校验覆盖 daily/weekly/monthly。"""
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    validate_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Validate output"
    )
    run = validate_step["run"]

    # 校验 feed
    assert "content:encoded" in run
    # 校验报告存在
    assert "REPORT_FILE" in run
    # 校验字数（daily/weekly/monthly 都有）
    assert "日报字数过少" in run
    assert "周报字数过少" in run
    assert "月报字数过少" in run
    # news 四栏目校验
    assert "一、美国政局" in run
    assert "二、国际局势" in run
    assert "三、科技前沿" in run
    assert "四、经济走势" in run
    # 兼容 feed 校验
    assert "feed.xml" in run


def test_publish_product_persist_state_keeps_legacy_news_db_alias():
    workflow = _load_workflow("publish-product.yml")
    publish_job = workflow["jobs"]["publish"]

    persist_step = next(
        step for step in publish_job["steps"]
        if step.get("name") == "Persist state database"
    )
    run = persist_step["run"]

    assert 'LEGACY_DB_PATH="${{ steps.config.outputs.legacy_db_path }}"' in run
    assert 'cp "$DB_PATH" "$TMP_DIR/$LEGACY_DB_PATH"' in run
    assert 'git add "$LEGACY_DB_PATH"' in run


# ── legacy fetch/publish 测试 ──


def test_legacy_fetch_and_publish_workflows_are_manual_only():
    fetch_workflow = _load_workflow("fetch.yml")
    publish_workflow = _load_workflow("publish.yml")

    assert _workflow_triggers(fetch_workflow) == {"workflow_dispatch": None}
    assert _workflow_triggers(publish_workflow) == {"workflow_dispatch": None}
