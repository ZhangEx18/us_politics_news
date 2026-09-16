#!/usr/bin/env python3
"""评分诊断：查看某栏目当日候选的评分结果，定位"候选多但硬新闻少"的原因。

用法：
    python3 scripts/inspect_scoring.py --column economy
    python3 scripts/inspect_scoring.py --column technology --limit 30
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from config import load_product_config  # noqa: E402


def _db_path() -> str:
    config = load_product_config("news")
    return str(config.get("storage", {}).get("db_path", "data/products/news/news.db"))


def fetch_rows(db_path: str, since: str, column: str | None, limit: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = """SELECT title, source, column, score, reason, fetched_at
             FROM articles
             WHERE fetched_at >= ?
             ORDER BY score DESC
             LIMIT ?"""
    params: list[object] = [since, limit]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if column:
        rows = [row for row in rows if (row["column"] or "") == column]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="评分诊断")
    parser.add_argument("--column", help="栏目过滤：us_politics/global_affairs/technology/economy")
    parser.add_argument("--limit", type=int, default=60, help="最多显示条数（默认 60）")
    parser.add_argument("--hours", type=int, default=26, help="回看小时数（默认 26）")
    args = parser.parse_args()

    since = (datetime.now() - timedelta(hours=args.hours)).strftime("%Y-%m-%d")
    db_path = _db_path()
    if not os.path.exists(db_path):
        raise SystemExit(f"数据库不存在: {db_path}（请先同步 news-data 分支的 state 库）")

    rows = fetch_rows(db_path, since, args.column, args.limit)
    if not rows:
        print(f"（{since} 之后没有匹配记录）")
        return

    scored = [row for row in rows if (row["score"] or 0) > 0]
    print(f"记录 {len(rows)} 条，其中有评分 {len(scored)} 条（{since} 之后，column={args.column or 'all'}）\n")
    print(f"{'score':>5}  {'column':<14} {'source':<26} title")
    for row in rows:
        score = row["score"] if row["score"] is not None else "-"
        column = (row["column"] or "")[:13]
        source = (row["source"] or "")[:25]
        title = (row["title"] or "")[:46]
        print(f"{str(score):>5}  {column:<14} {source:<26} {title}")

    print("\n提示：score<=65 或 column 与目标栏目不符的条目，即为栏目缺稿的主要来源。")


if __name__ == "__main__":
    main()
