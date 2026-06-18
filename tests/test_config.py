"""配置加载测试 — config.yaml 与 sources.yaml"""

import os

import yaml

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")

REQUIRED_SOURCE_KEYS = {"name", "url", "column", "source_tier"}
EXPECTED_COLUMNS = {"us_politics", "global_affairs", "technology", "economy"}


def _load_yaml(path: str) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── config.yaml ──


def test_config_loads_successfully():
    config = _load_yaml(CONFIG_PATH)
    assert isinstance(config, dict), "config.yaml 应解析为字典"


def test_digest_columns_contains_four_columns():
    config = _load_yaml(CONFIG_PATH)
    columns = config.get("digest", {}).get("columns", {})
    assert set(columns.keys()) == EXPECTED_COLUMNS, (
        f"期望四栏目 {EXPECTED_COLUMNS}，实际 {set(columns.keys())}"
    )


def test_digest_targets_match_publish_constraints():
    config = _load_yaml(CONFIG_PATH)
    digest = config.get("digest", {})
    column = digest.get("column", {})
    total = digest.get("total", {})
    columns = digest.get("columns", {})
    runtime = config.get("runtime", {})

    assert column.get("target_word_count_min") == 2500
    assert column.get("target_word_count_max") == 5000
    assert total.get("target_word_count_min") == 10000
    assert total.get("target_word_count_max") == 20000
    assert runtime.get("min_score_coverage") == 0.7
    assert digest.get("total_min_items") == 28
    assert digest.get("total_target_items") == 35
    assert digest.get("total_max_items") == 45

    assert columns["us_politics"]["min_items"] == 8
    assert columns["us_politics"]["target_items"] == 9
    assert columns["us_politics"]["max_items"] == 10
    assert columns["us_politics"]["prefilter_items"] == 18

    assert columns["global_affairs"]["min_items"] == 10
    assert columns["global_affairs"]["target_items"] == 12
    assert columns["global_affairs"]["max_items"] == 15
    assert columns["global_affairs"]["prefilter_items"] == 22

    assert columns["technology"]["min_items"] == 5
    assert columns["technology"]["target_items"] == 7
    assert columns["technology"]["max_items"] == 10
    assert columns["technology"]["prefilter_items"] == 14

    assert columns["economy"]["min_items"] == 5
    assert columns["economy"]["target_items"] == 7
    assert columns["economy"]["max_items"] == 10
    assert columns["economy"]["prefilter_items"] == 14

    for col_cfg in columns.values():
        assert col_cfg.get("prefilter_items", 0) >= col_cfg.get("target_items", 0)


# ── sources.yaml ──


def test_sources_loads_successfully():
    sources = _load_yaml(SOURCES_PATH)
    assert isinstance(sources, list), "sources.yaml 应解析为列表"
    assert len(sources) > 0, "sources.yaml 不应为空"


def test_enabled_sources_count_gt_100():
    sources = _load_yaml(SOURCES_PATH)
    enabled = [s for s in sources if s.get("enabled", False)]
    assert len(enabled) > 100, f"启用源应 > 100，实际 {len(enabled)}"


def test_each_column_has_at_least_22_enabled_sources():
    sources = _load_yaml(SOURCES_PATH)
    enabled = [s for s in sources if s.get("enabled", False)]
    by_column: dict[str, int] = {}
    for s in enabled:
        col = s.get("column", "")
        by_column[col] = by_column.get(col, 0) + 1

    for col in EXPECTED_COLUMNS:
        count = by_column.get(col, 0)
        assert count >= 22, f"栏目 {col} 启用源不足 22 个，实际 {count}"


def test_each_source_has_required_keys():
    sources = _load_yaml(SOURCES_PATH)
    for i, s in enumerate(sources):
        missing = REQUIRED_SOURCE_KEYS - set(s.keys())
        assert not missing, f"源 #{i} ({s.get('name', '?')}) 缺少字段: {missing}"
