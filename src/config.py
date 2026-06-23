#!/usr/bin/env python3
"""多 product 配置加载。"""

import os
from copy import deepcopy

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_root = os.path.join(_project_root, "config")
_products_root = os.path.join(_config_root, "products")


def _deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_base_config() -> dict:
    """加载通用默认配置。"""
    return _load_yaml(os.path.join(_config_root, "base.yaml"))


def list_products() -> list[str]:
    """列出已注册的 product_key。"""
    if not os.path.isdir(_products_root):
        return []
    return sorted(
        entry
        for entry in os.listdir(_products_root)
        if os.path.isfile(os.path.join(_products_root, entry, "product.yaml"))
    )


def load_product_config(product_key: str) -> dict:
    """加载单个 product 配置，并叠加全局默认值。"""
    product_path = os.path.join(_products_root, product_key, "product.yaml")
    if not os.path.exists(product_path):
        raise FileNotFoundError(f"未找到 product 配置: {product_key}")
    return _deep_merge(load_base_config(), _load_yaml(product_path))


def load_config() -> dict:
    """兼容入口：默认加载 news product。"""
    legacy_path = os.path.join(_config_root, "config.yaml")
    if os.path.exists(legacy_path):
        legacy_cfg = _load_yaml(legacy_path)
        product_key = legacy_cfg.get("product_key", "news")
        return load_product_config(product_key)
    return load_product_config("news")


def augment_ai_config_with_runtime(ai_config: dict, config: dict) -> dict:
    """把 config.yaml 里的 LLM 运行参数注入 AI 配置。"""
    llm_cfg = config.get("llm", {})
    ai_config.update({
        "score_max_prompt_chars": llm_cfg.get("score_max_prompt_chars", llm_cfg.get("max_prompt_chars", 12000)),
        "score_max_concurrent": llm_cfg.get("score_max_concurrent", max(1, min(llm_cfg.get("max_concurrent", 3), 2))),
        "score_timeout_seconds": llm_cfg.get("score_timeout_seconds", llm_cfg.get("timeout_seconds", 180)),
        "score_content_chars": llm_cfg.get("score_content_chars", 400),
        "score_retry_split_depth": llm_cfg.get("score_retry_split_depth", 3),
        "digest_timeout_seconds": llm_cfg.get("digest_timeout_seconds", llm_cfg.get("timeout_seconds", 180)),
        "digest_content_chars": llm_cfg.get("digest_content_chars", 1000),
        "meta_timeout_seconds": llm_cfg.get("meta_timeout_seconds", 120),
    })
    return ai_config
