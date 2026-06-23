#!/usr/bin/env python3
"""同步 GitHub Pages 历史产物，并重建首页入口。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
import re
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
DAILY_DIR = DOCS_DIR / "daily"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RestoreSummary(NamedTuple):
    feed_restored: bool
    daily_files_restored: int


def _run_git(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()
    raise RuntimeError(f"{action} 失败: {detail or '未知错误'}")


def _copy_git_file(
    branch_ref: str,
    repo_path: str,
    destination: Path,
    git_runner=_run_git,
    repo_root: Path = ROOT,
) -> bool:
    exists = git_runner(["cat-file", "-e", f"{branch_ref}:{repo_path}"], cwd=repo_root)
    if exists.returncode != 0:
        return False

    content = git_runner(["show", f"{branch_ref}:{repo_path}"], cwd=repo_root)
    _require_success(content, f"读取 {repo_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content.stdout, encoding="utf-8")
    return True


def restore_published_history(
    branch_ref: str = "origin/gh-pages",
    docs_dir: Path = DOCS_DIR,
    repo_root: Path = ROOT,
    git_runner=_run_git,
) -> RestoreSummary:
    docs_dir.mkdir(parents=True, exist_ok=True)

    remote_branch = git_runner(["ls-remote", "--exit-code", "--heads", "origin", "gh-pages"], cwd=repo_root)
    if remote_branch.returncode != 0:
        print("origin/gh-pages 不存在，跳过历史恢复")
        return RestoreSummary(feed_restored=False, daily_files_restored=0)

    fetch = git_runner(["fetch", "origin", "gh-pages"], cwd=repo_root)
    _require_success(fetch, "拉取 gh-pages")

    feed_restored = _copy_git_file(branch_ref, "feed.xml", docs_dir / "feed.xml", git_runner=git_runner, repo_root=repo_root)

    daily_tree = git_runner(["ls-tree", "-r", "--name-only", branch_ref, "daily"], cwd=repo_root)
    _require_success(daily_tree, "读取 gh-pages daily 目录")

    restored_count = 0
    for repo_path in (line.strip() for line in daily_tree.stdout.splitlines()):
        if not repo_path:
            continue
        restored = _copy_git_file(branch_ref, repo_path, docs_dir / repo_path, git_runner=git_runner, repo_root=repo_root)
        if restored:
            restored_count += 1

    print(f"历史 feed 恢复: {'是' if feed_restored else '否'}")
    print(f"历史日报恢复数量: {restored_count}")
    return RestoreSummary(feed_restored=feed_restored, daily_files_restored=restored_count)


def _discover_report_dates(daily_dir: Path) -> list[str]:
    if not daily_dir.exists():
        return []

    report_dates = []
    for path in daily_dir.glob("*.html"):
        if DATE_PATTERN.match(path.stem):
            report_dates.append(path.stem)
    return sorted(report_dates, reverse=True)


def build_index_html(report_dates: list[str]) -> str:
    latest_link = ""
    archive_links = ""

    if report_dates:
        latest_date = report_dates[0]
        latest_link = f'      <a href="./daily/{latest_date}.html">查看最新日报（{latest_date}）</a>'
        archive_links = "\n".join(
            f'        <li><a href="./daily/{report_date}.html">{report_date} 日报</a></li>'
            for report_date in report_dates
        )
    else:
        latest_link = '      <span class="muted">暂无已发布日报</span>'
        archive_links = '        <li class="muted">历史日报将在首次发布后显示</li>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>观察日报</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                   "Microsoft YaHei", "Noto Sans SC", sans-serif;
      max-width: 760px;
      margin: 0 auto;
      padding: 48px 20px 72px;
      line-height: 1.7;
      color: #1a1a1a;
      background: #fafafa;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 2rem;
    }}
    h2 {{
      margin: 36px 0 12px;
      font-size: 1.2rem;
    }}
    p {{
      margin: 0 0 16px;
      color: #444;
    }}
    .links {{
      display: grid;
      gap: 12px;
      margin-top: 28px;
    }}
    a {{
      color: #0f5e3a;
      text-decoration: none;
      font-weight: 600;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .archive {{
      margin: 0;
      padding-left: 20px;
    }}
    .archive li {{
      margin: 8px 0;
    }}
    .meta,
    .muted {{
      color: #666;
    }}
    .meta {{
      margin-top: 36px;
      font-size: 0.95rem;
    }}
  </style>
</head>
<body>
  <main>
    <h1>观察日报</h1>
    <p>每日聚合美国政局、国际局势、科技前沿、经济走势四大栏目，生成适合 Reader 订阅的长文日报。</p>
    <div class="links">
      <a href="./feed.xml">订阅 RSS Feed</a>
{latest_link}
      <a href="https://github.com/ZhangEx18/us_politics_news">查看项目仓库</a>
    </div>
    <section>
      <h2>历史日报</h2>
      <ul class="archive">
{archive_links}
      </ul>
    </section>
    <p class="meta">如果 Reader 中还看不到最新内容，请先确认 <code>feed.xml</code> 可以在浏览器直接打开。</p>
  </main>
</body>
</html>
"""


def build_index_page(docs_dir: Path = DOCS_DIR) -> Path:
    report_dates = _discover_report_dates(docs_dir / "daily")
    index_path = docs_dir / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(build_index_html(report_dates), encoding="utf-8")
    print(f"首页已更新，日报数量: {len(report_dates)}")
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 GitHub Pages 历史产物并重建首页")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("restore", help="从 gh-pages 恢复历史 feed 和日报文件")
    subparsers.add_parser("build-index", help="根据 docs/daily 重新生成首页")
    args = parser.parse_args()

    if args.command == "restore":
        restore_published_history()
        return

    if args.command == "build-index":
        build_index_page()
        return

    raise SystemExit(f"未知命令: {args.command}")


if __name__ == "__main__":
    main()
