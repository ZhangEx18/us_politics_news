#!/usr/bin/env python3
"""输出指定 product + report_type 的报告 key。

复用实际生成管线的 key 计算逻辑，确保 validate step 和生成产物一致。

用法：
  python3 scripts/report_key.py --product news --report-type daily
  python3 scripts/report_key.py --product news --report-type weekly
  python3 scripts/report_key.py --product news --report-type monthly
  python3 scripts/report_key.py --product algorithms --report-type daily
"""

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _news_daily_key() -> str:
    from run_pipeline import _get_report_window
    from config import load_config
    _, _, report_date = _get_report_window(config=load_config())
    return report_date


def _news_weekly_key() -> str:
    from run_weekly import _get_weekly_window
    _, _, report_key = _get_weekly_window()
    return report_key


def _news_monthly_key() -> str:
    from run_monthly import _get_monthly_window
    _, _, report_key = _get_monthly_window()
    return report_key


def _algorithms_daily_key() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def get_report_key(product_key: str, report_type: str) -> str:
    if product_key == "news":
        if report_type == "daily":
            return _news_daily_key()
        if report_type == "weekly":
            return _news_weekly_key()
        if report_type == "monthly":
            return _news_monthly_key()
    if product_key == "algorithms":
        if report_type == "daily":
            return _algorithms_daily_key()
    raise ValueError(f"不支持的组合: {product_key}/{report_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="输出报告 key")
    parser.add_argument("--product", required=True, help="product key")
    parser.add_argument("--report-type", required=True, help="report type")
    args = parser.parse_args()

    key = get_report_key(args.product, args.report_type)
    print(key)


if __name__ == "__main__":
    main()
