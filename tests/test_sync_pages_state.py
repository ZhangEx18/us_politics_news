"""sync_pages_state 脚本测试 — product-aware 版本"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_sync_pages_state_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "sync_pages_state.py"
    spec = importlib.util.spec_from_file_location("sync_pages_state", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitRunner:
    def __init__(self, responses: dict[tuple[str, ...], SimpleNamespace]):
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: list[str], cwd: Path):
        key = tuple(args)
        self.calls.append((key, cwd))
        return self.responses.get(key, SimpleNamespace(returncode=0, stdout="", stderr=""))


# ── restore 测试 ──


def test_restore_published_history_recovers_product_feeds_and_files(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    runner = FakeGitRunner(
        {
            ("ls-remote", "--exit-code", "--heads", "origin", "gh-pages"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("fetch", "origin", "gh-pages"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            # news feed
            ("cat-file", "-e", "origin/gh-pages:feeds/news.xml"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:feeds/news.xml"): SimpleNamespace(returncode=0, stdout="<rss news />", stderr=""),
            # algorithms feed
            ("cat-file", "-e", "origin/gh-pages:feeds/algorithms.xml"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:feeds/algorithms.xml"): SimpleNamespace(returncode=0, stdout="<rss algo />", stderr=""),
            # news canonical
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "news/daily"): SimpleNamespace(
                returncode=0, stdout="news/daily/2026-06-18.html\n", stderr="",
            ),
            ("cat-file", "-e", "origin/gh-pages:news/daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:news/daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="canonical daily", stderr=""),
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "news/weekly"): SimpleNamespace(
                returncode=0, stdout="", stderr="",
            ),
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "news/monthly"): SimpleNamespace(
                returncode=0, stdout="", stderr="",
            ),
            # algorithms canonical
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "algorithms/daily"): SimpleNamespace(
                returncode=0, stdout="algorithms/daily/2026-06-18.html\n", stderr="",
            ),
            ("cat-file", "-e", "origin/gh-pages:algorithms/daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:algorithms/daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="algo lesson", stderr=""),
            # legacy feed
            ("cat-file", "-e", "origin/gh-pages:feed.xml"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:feed.xml"): SimpleNamespace(returncode=0, stdout="<rss legacy />", stderr=""),
            # legacy daily/weekly/monthly
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "daily"): SimpleNamespace(
                returncode=0, stdout="daily/2026-06-17.html\n", stderr="",
            ),
            ("cat-file", "-e", "origin/gh-pages:daily/2026-06-17.html"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:daily/2026-06-17.html"): SimpleNamespace(returncode=0, stdout="legacy daily", stderr=""),
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "weekly"): SimpleNamespace(
                returncode=0, stdout="", stderr="",
            ),
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "monthly"): SimpleNamespace(
                returncode=0, stdout="", stderr="",
            ),
        }
    )

    summary = sync_pages_state.restore_published_history(
        docs_dir=docs_dir,
        repo_root=tmp_path,
        git_runner=runner,
    )

    assert summary.feed_restored is True
    # product feeds
    assert (docs_dir / "feeds" / "news.xml").read_text(encoding="utf-8") == "<rss news />"
    assert (docs_dir / "feeds" / "algorithms.xml").read_text(encoding="utf-8") == "<rss algo />"
    # canonical paths
    assert (docs_dir / "news" / "daily" / "2026-06-18.html").read_text(encoding="utf-8") == "canonical daily"
    assert (docs_dir / "algorithms" / "daily" / "2026-06-18.html").read_text(encoding="utf-8") == "algo lesson"
    # legacy
    assert (docs_dir / "feed.xml").read_text(encoding="utf-8") == "<rss legacy />"
    assert (docs_dir / "daily" / "2026-06-17.html").read_text(encoding="utf-8") == "legacy daily"


def test_restore_published_history_skips_when_gh_pages_missing(tmp_path, capsys):
    sync_pages_state = _load_sync_pages_state_script()
    runner = FakeGitRunner(
        {
            ("ls-remote", "--exit-code", "--heads", "origin", "gh-pages"): SimpleNamespace(returncode=2, stdout="", stderr=""),
        }
    )

    summary = sync_pages_state.restore_published_history(
        docs_dir=tmp_path / "docs",
        repo_root=tmp_path,
        git_runner=runner,
    )

    assert summary.feed_restored is False
    assert "跳过历史恢复" in capsys.readouterr().out


# ── build-index 测试 ──


def test_build_index_page_creates_global_and_product_indexes(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    # 创建 news daily 文件
    news_daily = docs_dir / "news" / "daily"
    news_daily.mkdir(parents=True)
    (news_daily / "2026-06-17.html").write_text("older", encoding="utf-8")
    (news_daily / "2026-06-18.html").write_text("latest", encoding="utf-8")
    # 创建 algorithms daily 文件
    algo_daily = docs_dir / "algorithms" / "daily"
    algo_daily.mkdir(parents=True)
    (algo_daily / "2026-06-18.html").write_text("algo lesson", encoding="utf-8")

    sync_pages_state.build_index_page(docs_dir=docs_dir)

    # 全局首页
    global_index = (docs_dir / "index.html").read_text(encoding="utf-8")
    assert "观察日报" in global_index
    assert "./news/" in global_index
    assert "./algorithms/" in global_index
    assert "./feeds/news.xml" in global_index
    assert "./feeds/algorithms.xml" in global_index

    # news 首页
    news_index = (docs_dir / "news" / "index.html").read_text(encoding="utf-8")
    assert "查看最新日报（2026-06-18）" in news_index
    assert "./daily/2026-06-18.html" in news_index
    assert "历史日报" in news_index

    # algorithms 首页
    algo_index = (docs_dir / "algorithms" / "index.html").read_text(encoding="utf-8")
    assert "每日算法专题" in algo_index
    assert "查看最新专题（2026-06-18）" in algo_index


def test_build_index_page_global_index_has_product_links(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    # 创建 news 文件
    news_daily = docs_dir / "news" / "daily"
    news_daily.mkdir(parents=True)
    (news_daily / "2026-06-18.html").write_text("latest", encoding="utf-8")
    # 创建 feeds
    feeds_dir = docs_dir / "feeds"
    feeds_dir.mkdir(parents=True)
    (feeds_dir / "news.xml").write_text("<rss />", encoding="utf-8")

    sync_pages_state.build_index_page(docs_dir=docs_dir)

    # 全局首页包含 product 导航卡片
    global_index = (docs_dir / "index.html").read_text(encoding="utf-8")
    assert "./news/" in global_index
    assert "./algorithms/" in global_index
    assert "./feeds/news.xml" in global_index
    assert "./feeds/algorithms.xml" in global_index


def test_sync_legacy_news_aliases_copies_files(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    # 创建 canonical 文件
    news_daily = docs_dir / "news" / "daily"
    news_daily.mkdir(parents=True)
    (news_daily / "2026-06-18.html").write_text("daily content", encoding="utf-8")
    feeds_dir = docs_dir / "feeds"
    feeds_dir.mkdir(parents=True)
    (feeds_dir / "news.xml").write_text("<rss news />", encoding="utf-8")

    sync_pages_state._sync_legacy_news_aliases(docs_dir)

    # legacy 路径应被同步
    assert (docs_dir / "feed.xml").read_text(encoding="utf-8") == "<rss news />"
    assert (docs_dir / "daily" / "2026-06-18.html").read_text(encoding="utf-8") == "daily content"


def test_discover_report_keys_filters_by_pattern(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-06-17.html").write_text("", encoding="utf-8")
    (daily_dir / "2026-06-18.html").write_text("", encoding="utf-8")
    (daily_dir / "notes.html").write_text("", encoding="utf-8")
    (daily_dir / "2026-06-18.md").write_text("", encoding="utf-8")

    pattern = sync_pages_state.PRODUCT_CONFIG["news"]["report_types"]["daily"]["pattern"]
    keys = sync_pages_state._discover_report_keys(daily_dir, pattern)

    assert keys == ["2026-06-18", "2026-06-17"]
    assert "notes" not in keys
