"""sync_pages_state 脚本回归测试"""

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


def test_restore_published_history_recovers_feed_and_daily_files(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    runner = FakeGitRunner(
        {
            ("ls-remote", "--exit-code", "--heads", "origin", "gh-pages"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("fetch", "origin", "gh-pages"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("cat-file", "-e", "origin/gh-pages:feed.xml"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:feed.xml"): SimpleNamespace(returncode=0, stdout="<rss />", stderr=""),
            ("ls-tree", "-r", "--name-only", "origin/gh-pages", "daily"): SimpleNamespace(
                returncode=0,
                stdout="daily/2026-06-17.html\ndaily/2026-06-18.html\n",
                stderr="",
            ),
            ("cat-file", "-e", "origin/gh-pages:daily/2026-06-17.html"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:daily/2026-06-17.html"): SimpleNamespace(returncode=0, stdout="old report", stderr=""),
            ("cat-file", "-e", "origin/gh-pages:daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="", stderr=""),
            ("show", "origin/gh-pages:daily/2026-06-18.html"): SimpleNamespace(returncode=0, stdout="new report", stderr=""),
        }
    )

    summary = sync_pages_state.restore_published_history(
        docs_dir=docs_dir,
        repo_root=tmp_path,
        git_runner=runner,
    )

    assert summary.feed_restored is True
    assert summary.daily_files_restored == 2
    assert (docs_dir / "feed.xml").read_text(encoding="utf-8") == "<rss />"
    assert (docs_dir / "daily" / "2026-06-17.html").read_text(encoding="utf-8") == "old report"
    assert (docs_dir / "daily" / "2026-06-18.html").read_text(encoding="utf-8") == "new report"


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
    assert summary.daily_files_restored == 0
    assert "跳过历史恢复" in capsys.readouterr().out


def test_build_index_page_lists_latest_and_history(tmp_path):
    sync_pages_state = _load_sync_pages_state_script()
    docs_dir = tmp_path / "docs"
    daily_dir = docs_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-06-17.html").write_text("older", encoding="utf-8")
    (daily_dir / "2026-06-18.html").write_text("latest", encoding="utf-8")
    (daily_dir / "notes.html").write_text("ignore", encoding="utf-8")

    index_path = sync_pages_state.build_index_page(docs_dir=docs_dir)
    index_html = index_path.read_text(encoding="utf-8")

    assert '查看最新日报（2026-06-18）' in index_html
    assert './daily/2026-06-18.html' in index_html
    assert './daily/2026-06-17.html' in index_html
    assert '历史日报' in index_html
    assert index_html.index("2026-06-18 日报") < index_html.index("2026-06-17 日报")
    assert "notes.html" not in index_html
