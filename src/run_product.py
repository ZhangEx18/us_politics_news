#!/usr/bin/env python3
"""多 product 统一运行入口。"""

from __future__ import annotations

import argparse
import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_product_config
from run_pipeline import run_pipeline, run_digest_only
from run_weekly import run_weekly
from run_monthly import run_monthly


def _validate_report_type(config: dict, report_type: str) -> None:
    supported = config.get("report_types", [])
    if report_type not in supported:
        raise ValueError(f"product {config.get('product_key')} 不支持 report_type={report_type}")


def run_product(product_key: str, report_type: str, hours: int = 24, digest_only: bool = False) -> dict:
    config = load_product_config(product_key)
    _validate_report_type(config, report_type)
    content_type = config.get("content_type")

    if content_type == "news_digest":
        if report_type == "daily":
            return run_digest_only(hours=hours, report_type=report_type) if digest_only else run_pipeline(hours=hours, report_type=report_type)
        if report_type == "weekly":
            return run_weekly()
        if report_type == "monthly":
            return run_monthly()

    if content_type == "topic_lesson":
        from topic_lesson import run_topic_lesson_daily
        if report_type == "daily":
            return run_topic_lesson_daily(product_key=product_key)
        raise ValueError(f"topic_lesson 暂不支持 report_type={report_type}")

    raise ValueError(f"未知 content_type: {content_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行指定 product 的发布流程")
    parser.add_argument("--product", required=True, help="product key，例如 news / algorithms")
    parser.add_argument("--report-type", default="daily", choices=["daily", "weekly", "monthly"])
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--digest-only", action="store_true")
    args = parser.parse_args()

    stats = run_product(args.product, args.report_type, hours=args.hours, digest_only=args.digest_only)
    if stats.get("total_selected", stats.get("total_fetched", 1)) == 0:
        raise SystemExit(1)
    print(stats)


if __name__ == "__main__":
    main()
