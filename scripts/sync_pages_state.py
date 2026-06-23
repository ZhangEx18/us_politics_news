#!/usr/bin/env python3
"""同步 GitHub Pages 历史产物，并重建首页入口（product-aware 版本）。

支持两种模式：
- restore: 从 gh-pages 恢复历史 feed 和报告文件
- build-index: 根据 docs/ 目录结构重建首页

canonical 结构：
  docs/{product}/index.html
  docs/{product}/{report_type}/*.html|md
  docs/feeds/{product}.xml

news 兼容别名：
  docs/feed.xml → docs/feeds/news.xml
  docs/daily/* → docs/news/daily/*
  docs/weekly/* → docs/news/weekly/*
  docs/monthly/* → docs/news/monthly/*
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"

# 报告类型 → 默认 UI 文案（产品配置未覆盖时的 fallback）
_REPORT_TYPE_DEFAULTS: dict[str, dict] = {
    "daily": {
        "label": "日报",
        "latest_label": "查看最新日报",
        "empty_label": "暂无已发布日报",
        "empty_archive": "历史日报将在首次发布后显示",
        "section_title": "历史日报",
        "pattern": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    },
    "weekly": {
        "label": "周报",
        "latest_label": "查看最新周报",
        "empty_label": "暂无已发布周报",
        "empty_archive": "历史周报将在首次发布后显示",
        "section_title": "历史周报",
        "pattern": re.compile(r"^\d{4}-W\d{2}$"),
    },
    "monthly": {
        "label": "月报",
        "latest_label": "查看最新月报",
        "empty_label": "暂无已发布月报",
        "empty_archive": "历史月报将在首次发布后显示",
        "section_title": "历史月报",
        "pattern": re.compile(r"^\d{4}-\d{2}$"),
    },
}

# 产品级 UI 文案覆盖（仅文案，不重复定义产品存在性）
_PRODUCT_UI: dict[str, dict] = {
    "news": {
        "label": "观察日报",
        "description": "每日国际新闻精选：美国政局 · 国际局势 · 科技前沿 · 经济走势",
    },
    "algorithms": {
        "label": "每日算法专题",
        "description": "每日算法与数据结构专题教程，从直觉到实现",
        "report_type_labels": {
            "daily": {"label": "专题", "latest_label": "查看最新专题", "section_title": "历史专题"},
        },
    },
}


def _load_product_config_from_yaml() -> dict[str, dict]:
    """从 product 配置文件动态读取产品列表和 report_types。"""
    sys.path.insert(0, str(ROOT / "src"))
    from config import list_products, load_product_config

    result = {}
    for product_key in list_products():
        try:
            config = load_product_config(product_key)
        except Exception:
            continue
        report_types = config.get("report_types", [])
        legacy_aliases = config.get("publish", {}).get("legacy_aliases", False)
        rt_dict = {}
        for rt in report_types:
            defaults = _REPORT_TYPE_DEFAULTS.get(rt, {})
            ui_overrides = _PRODUCT_UI.get(product_key, {}).get("report_type_labels", {}).get(rt, {})
            rt_dict[rt] = {**defaults, **ui_overrides}
        ui = _PRODUCT_UI.get(product_key, {})
        result[product_key] = {
            "label": ui.get("label", product_key),
            "description": ui.get("description", ""),
            "report_types": rt_dict,
            "legacy_aliases": bool(legacy_aliases),
        }
    return result


PRODUCT_CONFIG = _load_product_config_from_yaml()


class RestoreSummary(NamedTuple):
    feed_restored: bool
    report_files_restored: dict[str, int]


# ── Git 工具 ──


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


# ── 恢复 ──


def restore_published_history(
    branch_ref: str = "origin/gh-pages",
    docs_dir: Path = DOCS_DIR,
    repo_root: Path = ROOT,
    git_runner=_run_git,
) -> RestoreSummary:
    """从 gh-pages 恢复历史 feed 和报告文件（product-aware）。"""
    docs_dir.mkdir(parents=True, exist_ok=True)

    remote_branch = git_runner(["ls-remote", "--exit-code", "--heads", "origin", "gh-pages"], cwd=repo_root)
    if remote_branch.returncode != 0:
        print("origin/gh-pages 不存在，跳过历史恢复")
        return RestoreSummary(feed_restored=False, report_files_restored={})

    fetch = git_runner(["fetch", "origin", "gh-pages"], cwd=repo_root)
    _require_success(fetch, "拉取 gh-pages")

    feed_restored = False
    restored_counts: dict[str, int] = {}

    for product_key, product_cfg in PRODUCT_CONFIG.items():
        # 恢复 product feed
        product_feed_path = f"feeds/{product_key}.xml"
        restored = _copy_git_file(
            branch_ref, product_feed_path,
            docs_dir / product_feed_path,
            git_runner=git_runner, repo_root=repo_root,
        )
        if restored:
            feed_restored = True
            print(f"  [{product_key}] feed 恢复: 是")

        # 恢复 product 报告文件
        for report_type in product_cfg["report_types"]:
            canonical_dir = f"{product_key}/{report_type}"
            report_tree = git_runner(
                ["ls-tree", "-r", "--name-only", branch_ref, canonical_dir],
                cwd=repo_root,
            )
            count = 0
            if report_tree.returncode == 0:
                for repo_path in (line.strip() for line in report_tree.stdout.splitlines()):
                    if not repo_path:
                        continue
                    if _copy_git_file(
                        branch_ref, repo_path,
                        docs_dir / repo_path,
                        git_runner=git_runner, repo_root=repo_root,
                    ):
                        count += 1
            restored_counts[f"{product_key}/{report_type}"] = count
            print(f"  [{product_key}] {report_type} 恢复: {count} 份")

        # news 兼容别名：恢复旧路径
        if product_cfg.get("legacy_aliases"):
            # 恢复顶层 feed.xml
            legacy_feed = _copy_git_file(
                branch_ref, "feed.xml",
                docs_dir / "feed.xml",
                git_runner=git_runner, repo_root=repo_root,
            )
            if legacy_feed:
                feed_restored = True
                print(f"  [news] 旧 feed.xml 恢复: 是")

            # 恢复旧 daily/weekly/monthly 目录
            for report_type in ("daily", "weekly", "monthly"):
                if report_type not in product_cfg["report_types"]:
                    continue
                report_tree = git_runner(
                    ["ls-tree", "-r", "--name-only", branch_ref, report_type],
                    cwd=repo_root,
                )
                count = 0
                if report_tree.returncode == 0:
                    for repo_path in (line.strip() for line in report_tree.stdout.splitlines()):
                        if not repo_path:
                            continue
                        if _copy_git_file(
                            branch_ref, repo_path,
                            docs_dir / repo_path,
                            git_runner=git_runner, repo_root=repo_root,
                        ):
                            count += 1
                if count > 0:
                    restored_counts[f"legacy/{report_type}"] = count
                    print(f"  [news] 旧 {report_type} 恢复: {count} 份")

    return RestoreSummary(feed_restored=feed_restored, report_files_restored=restored_counts)


# ── 索引构建 ──


def _discover_report_keys(report_dir: Path, pattern: re.Pattern[str]) -> list[str]:
    if not report_dir.exists():
        return []

    report_keys = []
    for path in report_dir.glob("*.html"):
        if pattern.match(path.stem):
            report_keys.append(path.stem)
    return sorted(report_keys, reverse=True)


def _build_product_index_html(
    product_key: str,
    product_cfg: dict,
    archives: dict[str, list[str]],
    base_url: str = "",
) -> str:
    """构建单个 product 的首页 HTML。"""
    label = product_cfg["label"]
    description = product_cfg["description"]

    latest_links: list[str] = []
    archive_sections: list[str] = []

    for report_type, rt_cfg in product_cfg["report_types"].items():
        report_keys = archives.get(report_type, [])
        if report_keys:
            latest_key = report_keys[0]
            latest_links.append(
                f'      <a href="./{report_type}/{latest_key}.html">{rt_cfg["latest_label"]}（{latest_key}）</a>'
            )
            archive_links = "\n".join(
                f'        <li><a href="./{report_type}/{report_key}.html">{report_key} {rt_cfg["label"]}</a></li>'
                for report_key in report_keys
            )
        else:
            latest_links.append(f'      <span class="muted">{rt_cfg["empty_label"]}</span>')
            archive_links = f'        <li class="muted">{rt_cfg["empty_archive"]}</li>'

        archive_sections.append(
            "    <section>\n"
            f"      <h2>{rt_cfg['section_title']}</h2>\n"
            '      <ul class="archive">\n'
            f"{archive_links}\n"
            "      </ul>\n"
            "    </section>"
        )

    feed_link = f"../feeds/{product_key}.xml"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{label}</title>
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
    .back {{
      display: inline-block;
      margin-bottom: 24px;
      font-size: 0.9em;
    }}
  </style>
</head>
<body>
  <main>
    <a class="back" href="../index.html">← 返回首页</a>
    <h1>{label}</h1>
    <p>{description}</p>
    <div class="links">
      <a href="{feed_link}">订阅 RSS Feed</a>
{"\n".join(latest_links)}
    </div>
{"\n".join(archive_sections)}
    <p class="meta">如果 Reader 中还看不到最新内容，请先确认 Feed 地址可以在浏览器直接打开。</p>
  </main>
</body>
</html>
"""


def _build_global_index_html(products: dict[str, dict]) -> str:
    """构建全局导航首页。"""
    product_cards: list[str] = []
    for product_key, product_cfg in products.items():
        label = product_cfg["label"]
        description = product_cfg["description"]
        product_cards.append(
            f'      <div class="card">\n'
            f'        <h2><a href="./{product_key}/">{label}</a></h2>\n'
            f'        <p>{description}</p>\n'
            f'        <a href="./feeds/{product_key}.xml">RSS Feed</a>\n'
            f'      </div>'
        )

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
    p {{
      margin: 0 0 16px;
      color: #444;
    }}
    .cards {{
      display: grid;
      gap: 20px;
      margin-top: 28px;
    }}
    .card {{
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      padding: 20px 24px;
      background: #fff;
    }}
    .card h2 {{
      margin: 0 0 8px;
      font-size: 1.3rem;
    }}
    .card p {{
      margin: 0 0 12px;
      color: #555;
    }}
    a {{
      color: #0f5e3a;
      text-decoration: none;
      font-weight: 600;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    footer {{
      margin-top: 48px;
      padding-top: 16px;
      border-top: 1px solid #e0e0e0;
      color: #999;
      font-size: 0.85em;
      text-align: center;
    }}
  </style>
</head>
<body>
  <main>
    <h1>观察日报</h1>
    <p>AI 驱动的自动化内容发布平台。</p>
    <div class="cards">
{"\n".join(product_cards)}
    </div>
  </main>
  <footer>
    <p>自动生成，Reader 订阅即读。</p>
  </footer>
</body>
</html>
"""


def build_product_index(product_key: str, docs_dir: Path = DOCS_DIR) -> Path | None:
    """为单个 product 构建首页。"""
    product_cfg = PRODUCT_CONFIG.get(product_key)
    if not product_cfg:
        print(f"未知 product: {product_key}，跳过")
        return None

    product_dir = docs_dir / product_key
    archives = {}
    for report_type, rt_cfg in product_cfg["report_types"].items():
        archives[report_type] = _discover_report_keys(
            product_dir / report_type, rt_cfg["pattern"]
        )

    html = _build_product_index_html(product_key, product_cfg, archives)
    index_path = product_dir / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(html, encoding="utf-8")

    summary = ", ".join(
        f"{product_cfg['report_types'][rt]['label']} {len(keys)} 份"
        for rt, keys in archives.items()
    )
    print(f"[{product_key}] 首页已更新: {summary}")
    return index_path


def build_global_index(docs_dir: Path = DOCS_DIR) -> Path:
    """构建全局导航首页。"""
    html = _build_global_index_html(PRODUCT_CONFIG)
    index_path = docs_dir / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(html, encoding="utf-8")
    print("全局首页已更新")
    return index_path


def build_legacy_news_index(docs_dir: Path = DOCS_DIR) -> Path | None:
    """
    为 news 构建顶层兼容首页（旧格式）。

    保留旧的 docs/index.html 格式，但内容来自 docs/news/ 数据，
    同时保持旧路径 daily/weekly/monthly 的链接可用。
    """
    product_cfg = PRODUCT_CONFIG.get("news")
    if not product_cfg:
        return None

    # 发现文件：检查 canonical 和 legacy 路径
    archives: dict[str, list[str]] = {}
    for report_type, rt_cfg in product_cfg["report_types"].items():
        # 优先 canonical 路径
        canonical_keys = _discover_report_keys(
            docs_dir / "news" / report_type, rt_cfg["pattern"]
        )
        # 旧路径
        legacy_keys = _discover_report_keys(
            docs_dir / report_type, rt_cfg["pattern"]
        )
        # 合并去重
        all_keys = sorted(set(canonical_keys + legacy_keys), reverse=True)
        archives[report_type] = all_keys

    latest_links: list[str] = []
    archive_sections: list[str] = []

    for report_type, rt_cfg in product_cfg["report_types"].items():
        report_keys = archives.get(report_type, [])
        if report_keys:
            latest_key = report_keys[0]
            latest_links.append(
                f'      <a href="./{report_type}/{latest_key}.html">{rt_cfg["latest_label"]}（{latest_key}）</a>'
            )
            archive_links = "\n".join(
                f'        <li><a href="./{report_type}/{report_key}.html">{report_key} {rt_cfg["label"]}</a></li>'
                for report_key in report_keys
            )
        else:
            latest_links.append(f'      <span class="muted">{rt_cfg["empty_label"]}</span>')
            archive_links = f'        <li class="muted">{rt_cfg["empty_archive"]}</li>'

        archive_sections.append(
            "    <section>\n"
            f"      <h2>{rt_cfg['section_title']}</h2>\n"
            '      <ul class="archive">\n'
            f"{archive_links}\n"
            "      </ul>\n"
            "    </section>"
        )

    html = f"""<!DOCTYPE html>
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
    <p>自动生成日报、周报与月报，覆盖美国政局、国际局势、科技前沿、经济走势四大栏目，并输出适合 Reader 订阅的长文归档。</p>
    <div class="links">
      <a href="./feed.xml">订阅 RSS Feed</a>
{"\n".join(latest_links)}
      <a href="https://github.com/ZhangEx18/us_politics_news">查看项目仓库</a>
    </div>
{"\n".join(archive_sections)}
    <p class="meta">如果 Reader 中还看不到最新内容，请先确认 <code>feed.xml</code> 可以在浏览器直接打开。</p>
  </main>
</body>
</html>
"""
    index_path = docs_dir / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(html, encoding="utf-8")
    print("news 兼容首页已更新")
    return index_path


def build_index_page(docs_dir: Path = DOCS_DIR) -> None:
    """重建所有首页：全局导航 + 各 product 首页 + news 兼容别名。"""
    # news 兼容：同步 canonical 到旧路径
    _sync_legacy_news_aliases(docs_dir)

    # 构建各 product 首页
    for product_key in PRODUCT_CONFIG:
        build_product_index(product_key, docs_dir)

    # 构建全局导航首页（放在最后，覆盖旧的 docs/index.html）
    build_global_index(docs_dir)


def _sync_legacy_news_aliases(docs_dir: Path = DOCS_DIR) -> None:
    """将 news canonical 路径同步到旧路径（兼容别名）。"""
    product_cfg = PRODUCT_CONFIG.get("news")
    if not product_cfg or not product_cfg.get("legacy_aliases"):
        return

    # 同步 feed
    canonical_feed = docs_dir / "feeds" / "news.xml"
    legacy_feed = docs_dir / "feed.xml"
    if canonical_feed.exists():
        legacy_feed.parent.mkdir(parents=True, exist_ok=True)
        legacy_feed.write_text(canonical_feed.read_text(encoding="utf-8"), encoding="utf-8")
        print("[news] feed.xml 兼容别名已同步")

    # 同步 daily/weekly/monthly
    for report_type in product_cfg["report_types"]:
        canonical_dir = docs_dir / "news" / report_type
        legacy_dir = docs_dir / report_type
        if not canonical_dir.exists():
            continue
        legacy_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in canonical_dir.glob("*"):
            if f.is_file():
                dest = legacy_dir / f.name
                dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                count += 1
        if count > 0:
            print(f"[news] {report_type}/ 兼容别名已同步: {count} 个文件")


# ── CLI ──


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 GitHub Pages 历史产物并重建首页")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("restore", help="从 gh-pages 恢复历史 feed 和报告文件")
    subparsers.add_parser("build-index", help="重建所有首页")
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
