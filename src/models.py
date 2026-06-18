#!/usr/bin/env python3
"""
统一数据模型 — 四板块日报 v2

新增字段：column、source_tier、event_key、llm_score、llm_summary 等
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class SourceType(str, Enum):
    """支持的信息源类型"""
    RSS = "rss"
    NEWSAPI = "newsapi"
    GDELT = "gdelt"
    HACKERNEWS = "hackernews"
    GOOGLE_NEWS = "google_news"
    TIANAPI = "tianapi"


class Column(str, Enum):
    """四大栏目"""
    US_POLITICS = "us_politics"
    GLOBAL_AFFAIRS = "global_affairs"
    TECHNOLOGY = "technology"
    ECONOMY = "economy"


class ContentItem(BaseModel):
    """
    统一内容条目模型

    所有抓取器最终都返回此模型。
    """
    id: str  # 格式: {source}:{native_id} 或 url_hash
    source_type: SourceType
    title: str
    url: HttpUrl
    content: Optional[str] = None  # 正文/摘要
    author: Optional[str] = None
    source_name: str = ""  # 来源名称（如 "BBC News"）
    published_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # 四栏目分类
    column: str = ""  # us_politics | global_affairs | technology | economy
    source_tier: int = 4  # 1=官方/一线 | 2=主流 | 3=专业/智库 | 4=聚合/社区

    # 事件级去重
    event_key: str = ""  # 事件唯一标识（AI 生成）
    source_url_normalized: str = ""  # 规范化 URL

    # LLM 评分结果
    llm_score: Optional[float] = None  # 0-100
    llm_summary: Optional[str] = None  # 一句话摘要
    llm_tags: list[str] = Field(default_factory=list)
    llm_reason: Optional[str] = None

    # 规则评分（预筛用）
    topic: str = ""
    topic_priority: int = 3
    score: float = 0.0
    reason: str = ""
    level: str = ""

    # 元数据
    metadata: dict = Field(default_factory=dict)

    class Config:
        use_enum_values = True


class ScoredArticle(BaseModel):
    """
    评分后的文章 — 用于日报生成

    包含 LLM 分析结果和事件级信息。
    """
    url: str
    title: str
    summary: str
    source: str
    source_type: str
    column: str = ""
    topic: str = ""
    topic_priority: int = 3
    score: float = 0.0
    reason: str = ""
    level: str = ""

    # LLM 分析结果
    title_zh: str = ""
    summary_zh: str = ""
    analysis: str = ""
    ai_tags: list[str] = Field(default_factory=list)

    # 事件级字段
    event_key: str = ""
    event_title_zh: str = ""  # 事件级中文标题
    one_line_hook: str = ""  # 一句核心亮点
    key_points: list[str] = Field(default_factory=list)  # 2-4 个要点
    why_it_matters: str = ""  # 为什么值得关注
    source_links: list[dict] = Field(default_factory=list)  # 多来源链接
    is_followup: bool = False  # 是否持续跟踪

    source_tier: int = 4

    class Config:
        arbitrary_types_allowed = True


class DailyReport(BaseModel):
    """日报数据结构"""
    date: str
    title: str = ""
    lead: str = ""  # 导语
    highlights: list[str] = Field(default_factory=list)  # 2-3 条重点
    total_fetched: int = 0
    total_selected: int = 0
    column_counts: dict[str, int] = Field(default_factory=dict)
    articles: list[ScoredArticle] = Field(default_factory=list)


def item_to_db_tuple(item: ContentItem) -> tuple:
    """将 ContentItem 转换为数据库插入元组"""
    return (
        str(item.url),
        item.title,
        item.content or "",
        item.source_name,
        item.source_type,
        item.published_at.isoformat() if item.published_at else None,
        item.fetched_at.isoformat(),
        item.topic,
        item.score,
        item.topic_priority,
        item.reason,
        item.level,
        item.column,
        item.source_tier,
        item.event_key,
        item.source_url_normalized,
        item.llm_score,
        item.llm_summary,
        ",".join(item.llm_tags),
        item.llm_reason,
    )
