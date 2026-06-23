#!/usr/bin/env python3
"""
构建调度 manifest，供 Cloudflare Worker 读取。

从各 product 配置中提取 schedule，生成统一的 JSON manifest，
Worker 按 cron 命中项 dispatch 对应的 workflow。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root / "src"))

from config import load_base_config, load_product_config, list_products


def build_manifest() -> dict:
    """
    构建调度 manifest。

    输出格式：
    {
      "version": 1,
      "schedules": [
        {
          "product_key": "news",
          "report_type": "daily",
          "workflow": "publish-product.yml",
          "cron": "30 23 * * *",
          "inputs": {
            "product_key": "news",
            "report_type": "daily"
          }
        },
        ...
      ]
    }
    """
    products = list_products()
    schedules = []

    for product_key in products:
        try:
            config = load_product_config(product_key)
        except Exception as e:
            print(f"[警告] 加载 {product_key} 配置失败: {e}")
            continue

        schedule_cfg = config.get("schedule", {})
        report_types = config.get("report_types", [])
        base_workflow = schedule_cfg.get("workflow", "publish-product.yml")

        for report_type in report_types:
            rt_schedule = schedule_cfg.get(report_type, {})
            cron = rt_schedule.get("cron")
            if not cron:
                continue

            workflow = rt_schedule.get("workflow", base_workflow)
            entry = {
                "product_key": product_key,
                "report_type": report_type,
                "workflow": workflow,
                "cron": cron,
                "inputs": {
                    "product_key": product_key,
                    "report_type": report_type,
                },
            }

            # digest_only 仅对 news/daily 有意义
            if product_key == "news" and report_type == "daily":
                entry["inputs"]["digest_only"] = False

            schedules.append(entry)

    return {
        "version": 1,
        "schedules": schedules,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="构建调度 manifest")
    parser.add_argument(
        "-o", "--output",
        default="cloudflare/schedule-manifest.json",
        help="输出路径（默认 cloudflare/schedule-manifest.json）",
    )
    args = parser.parse_args()

    manifest = build_manifest()

    output_path = Path(_project_root / args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"调度 manifest 已生成: {output_path}")
    print(f"共 {len(manifest['schedules'])} 个调度项:")
    for entry in manifest["schedules"]:
        print(f"  {entry['product_key']}/{entry['report_type']} → {entry['cron']} → {entry['workflow']}")


if __name__ == "__main__":
    main()
