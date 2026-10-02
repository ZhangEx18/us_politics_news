"""Kagi 07:30 发布门禁：已有完整合刊时跳过，失败槽位继续重试。"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import load_product_config


def choose_report_date(config: dict, *, now: datetime, requested_date: str = "", force: bool = False) -> tuple[bool, str]:
    local_now = now.astimezone(ZoneInfo(config["schedule"]["timezone"]))
    report_date = requested_date or local_now.date().isoformat()
    date.fromisoformat(report_date)
    if not requested_date and local_now.time() < time.fromisoformat(config["schedule"]["publish_at"]):
        return False, report_date
    if force:
        return True, report_date
    publish = config["publish"]
    report_dir = Path(publish["site_root"]) / "daily"
    paths = [report_dir / f"{report_date}.{extension}" for extension in ("md", "html", "json")]
    feed = Path(publish["feed_path"])
    if not all(path.is_file() and path.stat().st_size > 0 for path in [*paths, feed]):
        return True, report_date
    try:
        issue = json.loads(paths[2].read_text(encoding="utf-8"))
        expected = {sid for ids in issue["source_story_ids"].values() for sid in ids}
        actual = [item["id"] for column in issue["columns"] for item in column["items"]]
        if issue["report_date"] != report_date or not expected or set(actual) != expected or len(actual) != len(expected):
            return True, report_date
        root = ET.parse(feed).getroot()
    except (ET.ParseError, json.JSONDecodeError, KeyError, TypeError):
        return True, report_date
    published = any(item.findtext("guid") == f"kagi_digest/daily/{report_date}"
                    and item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                    for item in root.findall("channel/item"))
    return not published, report_date


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run, report_date = choose_report_date(load_product_config("kagi_digest"), now=datetime.now(ZoneInfo("Asia/Shanghai")),
                                          requested_date=args.report_date, force=args.force)
    print(f"should_run={'true' if run else 'false'}")
    print(f"report_date={report_date}")
