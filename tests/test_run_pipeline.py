"""run_pipeline 回归测试"""

import pytest
from datetime import datetime

from database import Article
from models import ContentItem
from run_pipeline import main


def test_database_article_can_map_to_content_item_without_id_field():
    article = Article(
        url="https://example.com/a",
        title="测试文章",
        summary="测试摘要",
        source="Example",
        source_type="rss",
        published_at=datetime.now(),
        fetched_at=datetime.now(),
        column="us_politics",
        source_tier=2,
        event_key="test_event_20260618",
        source_url_normalized="example.com/a",
        topic="测试主题",
        score=88,
        reason="测试原因",
        level="重点",
    )

    item = ContentItem(
        id="db:example.com/a",
        source_type=article.source_type,
        title=article.title,
        url=article.url,
        content=article.summary,
        source_name=article.source,
        published_at=article.published_at,
        column=article.column,
        source_tier=article.source_tier,
        event_key=article.event_key,
        source_url_normalized=article.source_url_normalized,
        topic=article.topic,
        score=article.score,
        reason=article.reason,
        level=article.level,
    )

    assert item.id.startswith("db:")
    assert item.column == "us_politics"
    assert item.event_key == "test_event_20260618"
    assert item.source_tier == 2


def test_main_digest_only_exits_when_no_content_generated(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--digest-only"])
    monkeypatch.setattr("run_pipeline.run_digest_only", lambda hours=24: {"total_selected": 0})

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
