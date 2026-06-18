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
