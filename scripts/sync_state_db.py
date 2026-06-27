#!/usr/bin/env python3
"""从远端 state 分支恢复产品数据库到本地。

用法:
    python3 scripts/sync_state_db.py                  # 恢复 news 数据库
    python3 scripts/sync_state_db.py --product news   # 同上
    python3 scripts/sync_state_db.py --dry-run         # 只检查，不写入

逻辑:
    1. 读取 product 配置，确定 db_path 和 state_branch
    2. 从远端 state 分支恢复数据库文件到本地 db_path
    3. 输出恢复结果：来源分支、目标路径、文章数、最晚 fetched_at
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
os.chdir(_project_root)
sys.path.insert(0, str(_project_root / "src"))

from config import load_product_config


def _run_git(args: list[str], cwd: Path = _project_root) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_state_branch(product_key: str) -> str:
    """确定产品的远端状态分支名。"""
    if product_key == "news":
        return "news-data"
    return f"{product_key}-state"


def _resolve_db_paths(config: dict) -> tuple[str, str | None]:
    """返回 (canonical_db_path, legacy_db_path | None)。"""
    db_path = config.get("storage", {}).get("db_path", "data/products/news/news.db")
    legacy_db_path = None
    if config.get("product_key") == "news":
        legacy_db_path = "data/news.db"
    return db_path, legacy_db_path


def _restore_db_from_branch(
    state_branch: str,
    db_path: str,
    legacy_db_path: str | None,
    dry_run: bool = False,
) -> dict:
    """从远端 state 分支恢复数据库。返回恢复结果 dict。"""
    result = {
        "state_branch": state_branch,
        "target_db_path": db_path,
        "restored": False,
        "source_ref": None,
        "article_count": 0,
        "latest_fetched_at": None,
        "error": None,
    }

    # 检查远端分支是否存在
    ls_result = _run_git(["ls-remote", "--exit-code", "--heads", "origin", state_branch])
    if ls_result.returncode != 0:
        result["error"] = f"远端分支 origin/{state_branch} 不存在"
        return result

    if dry_run:
        result["restored"] = True
        result["source_ref"] = f"origin/{state_branch}"
        return result

    # 拉取远端分支
    fetch_result = _run_git(["fetch", "origin", state_branch])
    if fetch_result.returncode != 0:
        result["error"] = f"拉取 origin/{state_branch} 失败: {fetch_result.stderr.strip()}"
        return result

    # 尝试恢复：优先 canonical 路径，其次 legacy 路径
    tmp_dir = tempfile.mkdtemp()
    try:
        canonical_candidate = os.path.join(tmp_dir, "canonical.db")
        legacy_candidate = os.path.join(tmp_dir, "legacy.db")
        canonical_size = 0
        legacy_size = 0

        # 尝试 canonical 路径
        cat_result = _run_git(["show", f"origin/{state_branch}:{db_path}"])
        if cat_result.returncode == 0:
            Path(canonical_candidate).write_bytes(cat_result.stdout.encode("latin-1"))
            canonical_size = Path(canonical_candidate).stat().st_size

        # 尝试 legacy 路径
        if legacy_db_path:
            cat_result = _run_git(["show", f"origin/{state_branch}:{legacy_db_path}"])
            if cat_result.returncode == 0:
                Path(legacy_candidate).write_bytes(cat_result.stdout.encode("latin-1"))
                legacy_size = Path(legacy_candidate).stat().st_size

        # 选择更大的那个
        source = None
        if canonical_size > 0 and canonical_size >= legacy_size:
            source = canonical_candidate
            result["source_ref"] = f"origin/{state_branch}:{db_path}"
        elif legacy_size > 0:
            source = legacy_candidate
            result["source_ref"] = f"origin/{state_branch}:{legacy_db_path}"

        if source is None:
            result["error"] = f"分支存在但数据库文件为空"
            return result

        # 写入目标路径
        target = Path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        result["restored"] = True

        # 读取恢复后的数据库状态
        try:
            from database import NewsDatabase
            db = NewsDatabase(db_path)
            result["article_count"] = db.article_count()
            # 查询最晚 fetched_at
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT MAX(fetched_at) FROM articles"
                ).fetchone()
                if row and row[0]:
                    result["latest_fetched_at"] = row[0]
        except Exception as e:
            result["error"] = f"恢复成功但读取数据库状态失败: {e}"

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


def sync_product_db(product_key: str = "news", dry_run: bool = False) -> dict:
    """同步指定产品的数据库状态。"""
    config = load_product_config(product_key)
    db_path, legacy_db_path = _resolve_db_paths(config)
    state_branch = _resolve_state_branch(product_key)
    return _restore_db_from_branch(state_branch, db_path, legacy_db_path, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="从远端 state 分支恢复产品数据库")
    parser.add_argument("--product", default="news", help="产品标识（默认 news）")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写入")
    args = parser.parse_args()

    result = sync_product_db(args.product, dry_run=args.dry_run)

    # 输出结果
    print(f"状态分支: {result['state_branch']}")
    print(f"目标路径: {result['target_db_path']}")

    if result.get("error"):
        print(f"[错误] {result['error']}")
        if not result["restored"]:
            sys.exit(1)

    if result["restored"]:
        print(f"来源: {result.get('source_ref', '未知')}")
        if result.get("article_count"):
            print(f"文章总数: {result['article_count']}")
        if result.get("latest_fetched_at"):
            print(f"最晚抓取时间: {result['latest_fetched_at']}")
        print("恢复完成")
    else:
        print("未恢复（远端状态不存在）")


if __name__ == "__main__":
    main()
