"""配置加载测试 — base config 与 products 配置"""

import os

import yaml
from config import load_base_config, load_product_config, list_products

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")
NEWS_SOURCES_PATH = os.path.join(PROJECT_ROOT, "config", "products", "news", "sources.yaml")

REQUIRED_SOURCE_KEYS = {"name", "url", "fetch_mode", "column", "source_tier", "language", "enabled"}
EXPECTED_COLUMNS = {"us_politics", "global_affairs", "technology", "economy"}
VALID_FETCH_MODES = {"rss", "rsshub", "google_news", "custom", "hacker_news"}


def _load_yaml(path: str) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── config.yaml ──


def test_products_can_be_listed():
    assert {"news", "algorithms"}.issubset(set(list_products()))


def test_config_loads_successfully():
    config = load_product_config("news")
    assert isinstance(config, dict), "config.yaml 应解析为字典"


def test_digest_columns_contains_four_columns():
    config = load_product_config("news")
    columns = config.get("digest", {}).get("columns", {})
    assert set(columns.keys()) == EXPECTED_COLUMNS, (
        f"期望四栏目 {EXPECTED_COLUMNS}，实际 {set(columns.keys())}"
    )


def test_digest_targets_match_publish_constraints():
    config = load_product_config("news")
    digest = config.get("digest", {})
    column = digest.get("column", {})
    total = digest.get("total", {})
    columns = digest.get("columns", {})
    runtime = config.get("runtime", {})
    llm = config.get("llm", {})

    assert column.get("target_word_count_min") == 2500
    assert column.get("target_word_count_max") == 5000
    assert total.get("target_word_count_min") == 10000
    assert total.get("target_word_count_max") == 20000
    assert runtime.get("min_score_coverage") == 0.7
    schedule = config.get("schedule", {})
    assert llm.get("score_max_concurrent") == 2
    assert llm.get("score_max_prompt_chars") == 9000
    assert llm.get("score_timeout_seconds") == 120
    assert llm.get("score_content_chars") == 400
    assert llm.get("score_retry_split_depth") == 3
    assert llm.get("digest_timeout_seconds") == 240
    assert llm.get("digest_content_chars") == 1000
    assert llm.get("meta_timeout_seconds") == 120
    assert digest.get("total_min_items") == 34
    assert digest.get("total_target_items") == 56
    assert digest.get("total_max_items") == 56
    assert config.get("analysis", {}).get("freshness_hours") == 30
    assert schedule.get("timezone") == "Asia/Shanghai"
    assert schedule.get("cutoff_hour") == 7
    assert schedule.get("fetch_at") == "07:00"
    assert schedule.get("publish_at") == "07:45"

    assert columns["us_politics"]["min_items"] == 10
    assert columns["us_politics"]["target_items"] == 10
    assert columns["us_politics"]["max_items"] == 10
    assert columns["us_politics"]["headline_items"] == 8
    assert columns["us_politics"]["prefilter_items"] == 25

    assert columns["global_affairs"]["min_items"] == 10
    assert columns["global_affairs"]["target_items"] == 10
    assert columns["global_affairs"]["max_items"] == 10
    assert columns["global_affairs"]["headline_items"] == 8
    assert columns["global_affairs"]["prefilter_items"] == 40

    assert columns["technology"]["min_items"] == 7
    assert columns["technology"]["target_items"] == 7
    assert columns["technology"]["max_items"] == 7
    assert columns["technology"]["headline_items"] == 3
    assert columns["technology"]["prefilter_items"] == 24

    assert columns["economy"]["min_items"] == 7
    assert columns["economy"]["target_items"] == 7
    assert columns["economy"]["max_items"] == 7
    assert columns["economy"]["headline_items"] == 3
    assert columns["economy"]["prefilter_items"] == 24

    for col_cfg in columns.values():
        total_items = col_cfg.get("max_items", 0) + col_cfg.get("headline_items", 0)
        assert total_items >= col_cfg.get("min_items", 0)
        assert col_cfg.get("prefilter_items", 0) >= col_cfg.get("target_items", 0)


# ── sources.yaml ──


def test_sources_loads_successfully():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    assert isinstance(sources, list), "sources.yaml 应解析为列表"
    assert len(sources) > 0, "sources.yaml 不应为空"


def test_enabled_sources_count_gt_100():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    enabled = [s for s in sources if s.get("enabled", False)]
    assert len(enabled) > 100, f"启用源应 > 100，实际 {len(enabled)}"


def test_each_column_has_at_least_22_enabled_sources():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    enabled = [s for s in sources if s.get("enabled", False)]
    by_column: dict[str, int] = {}
    for s in enabled:
        col = s.get("column", "")
        by_column[col] = by_column.get(col, 0) + 1

    for col in EXPECTED_COLUMNS:
        count = by_column.get(col, 0)
        assert count >= 22, f"栏目 {col} 启用源不足 22 个，实际 {count}"


def test_each_source_has_required_keys():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    for i, s in enumerate(sources):
        missing = REQUIRED_SOURCE_KEYS - set(s.keys())
        assert not missing, f"源 #{i} ({s.get('name', '?')}) 缺少字段: {missing}"


def test_each_source_has_valid_fetch_mode():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    for i, s in enumerate(sources):
        fetch_mode = s.get("fetch_mode")
        assert fetch_mode in VALID_FETCH_MODES, (
            f"源 #{i} ({s.get('name', '?')}) fetch_mode 非法: {fetch_mode}"
        )


def test_custom_sources_require_fetcher_key():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    custom_sources = [s for s in sources if s.get("fetch_mode") == "custom"]
    assert custom_sources, "至少应存在一个 custom 源"
    for s in custom_sources:
        assert s.get("fetcher_key"), f"custom 源缺少 fetcher_key: {s.get('name')}"


def test_cn_sources_cover_all_four_columns():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    cn_sources = [s for s in sources if s.get("language") == "zh"]
    assert cn_sources, "必须存在中文源"
    covered = {s.get("column") for s in cn_sources}
    assert EXPECTED_COLUMNS.issubset(covered), f"中文源未覆盖四栏，实际覆盖: {covered}"


def test_priority_cn_sources_exist():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    by_name = {s["name"]: s for s in sources}
    expected = {
        "财新国际 - 美国政治": "us_politics",
        "联合早报 - 国际": "global_affairs",
        "36氪 - 科技": "technology",
        "华尔街见闻 - 宏观/全球": "economy",
    }
    for name, column in expected.items():
        assert name in by_name, f"缺少中文优先源: {name}"
        assert by_name[name]["column"] == column
        assert by_name[name]["language"] == "zh"
        assert "cn_source" in by_name[name].get("tags", [])


def test_global_affairs_contains_cfr_and_foreign_affairs():
    sources = _load_yaml(NEWS_SOURCES_PATH)
    by_name = {s["name"]: s for s in sources}
    assert by_name["Council on Foreign Relations"]["column"] == "global_affairs"
    assert by_name["Foreign Affairs"]["column"] == "global_affairs"


# ── rules 配置 ──


def test_rules_config_exists():
    """config.yaml 应包含 rules 下的关键词和门禁配置"""
    config = load_product_config("news")
    rules = config.get("rules", {})
    assert "soft_news_keywords" in rules
    assert "hard_news_keywords" in rules
    assert "quality_gate" in rules


def test_algorithms_product_has_topics_file_and_daily_only():
    config = load_product_config("algorithms")
    assert config["content_type"] == "topic_lesson"
    assert config["report_types"] == ["daily"]
    assert config["topics_file"].endswith("topics.yaml")


def test_base_config_loads_successfully():
    config = load_base_config()
    assert "llm" in config
    assert "rules" in config
