#!/usr/bin/env python3
"""评分与推荐理由生成：基于规则对新闻进行评分"""

from dataclasses import dataclass

from topic_rules import classify_topic


# 来源权重：高可信来源得分更高
SOURCE_WEIGHTS: dict[str, float] = {
    # 一线媒体
    "reuters": 1.0,
    "associated press": 1.0,
    "ap news": 1.0,
    "bbc": 0.9,
    "npr": 0.9,
    "pbs": 0.9,
    # 主流媒体
    "cnn": 0.8,
    "fox news": 0.8,
    "the guardian": 0.8,
    "the hill": 0.8,
    "politico": 0.8,
    "axios": 0.8,
    # 政策专业媒体
    "foreign affairs": 0.9,
    "foreign policy": 0.9,
    "brookings": 0.9,
    "council on foreign relations": 0.9,
    # 科技媒体
    "wired": 0.7,
    "techcrunch": 0.7,
    "the verge": 0.7,
    # 默认
    "hacker news": 0.6,
    "google news": 0.7,
}

# 高优先级关键词（出现时加分）
HIGH_PRIORITY_KEYWORDS: list[str] = [
    "breaking",
    "exclusive",
    "confirmed",
    "official",
    "announced",
    "ruling",
    "verdict",
    "executive order",
    "legislation passed",
    "vote",
]


@dataclass
class ScoredArticle:
    """评分后的文章 — 用于日报生成"""
    url: str
    title: str
    summary: str
    source: str
    source_type: str
    topic: str
    topic_priority: int
    score: float
    reason: str
    level: str  # "重点" | "观察" | "低优先级"
    # AI 分析结果
    title_zh: str = ""
    summary_zh: str = ""
    analysis: str = ""
    ai_tags: list[str] = None
    # 四栏目字段
    column: str = ""
    source_tier: int = 4
    # 核心分析字段
    core_facts: str = ""  # 核心事实
    background_impact: str = ""  # 背景/影响
    # 事件级字段
    event_key: str = ""
    event_title_zh: str = ""
    one_line_hook: str = ""
    key_points: list[str] = None
    why_it_matters: str = ""
    source_links: list[dict] = None
    is_followup: bool = False

    def __post_init__(self):
        if self.ai_tags is None:
            self.ai_tags = []
        if self.key_points is None:
            self.key_points = []
        if self.source_links is None:
            self.source_links = []


def get_source_weight(source: str) -> float:
    """获取来源权重"""
    source_lower = source.lower()
    for key, weight in SOURCE_WEIGHTS.items():
        if key in source_lower:
            return weight
    return 0.5  # 默认权重


def calculate_score(title: str, summary: str, source: str, topic_priority: int) -> float:
    """
    计算文章综合评分

    评分维度：
    - 来源权重（0-1）
    - 主题优先级（1=重点→0.3, 2=观察→0.2, 3=低→0.1）
    - 关键词命中（0-0.2）
    """
    # 来源权重（40%）
    source_score = get_source_weight(source) * 0.4

    # 主题优先级（30%）
    priority_score = {1: 0.3, 2: 0.2, 3: 0.1}.get(topic_priority, 0.1)

    # 关键词命中（30%）
    text = f"{title} {summary}".lower()
    keyword_hits = sum(1 for kw in HIGH_PRIORITY_KEYWORDS if kw in text)
    keyword_score = min(keyword_hits * 0.1, 0.3)

    return round(source_score + priority_score + keyword_score, 2)


def generate_reason(title: str, summary: str, source: str, topic: str, topic_priority: int) -> str:
    """生成推荐理由"""
    reasons = []

    # 来源可信度
    weight = get_source_weight(source)
    if weight >= 0.9:
        reasons.append("一线信源")
    elif weight >= 0.8:
        reasons.append("主流媒体")

    # 主题重要性
    if topic_priority == 1:
        reasons.append("核心议题")

    # 关键词匹配
    text = f"{title} {summary}".lower()
    if "breaking" in text or "exclusive" in text:
        reasons.append("突发/独家")
    if any(kw in text for kw in ["official", "confirmed", "announced"]):
        reasons.append("官方确认")

    # 主题标签
    reasons.append(topic)

    return " · ".join(reasons) if reasons else "一般报道"


def determine_level(topic_priority: int, score: float) -> str:
    """确定文章层级"""
    if topic_priority == 1 and score >= 0.6:
        return "重点"
    elif topic_priority <= 2 and score >= 0.4:
        return "观察"
    else:
        return "低优先级"


def score_article(url: str, title: str, summary: str, source: str, source_type: str) -> ScoredArticle:
    """对单篇文章进行完整评分"""
    topic, topic_priority = classify_topic(title, summary)
    score = calculate_score(title, summary, source, topic_priority)
    reason = generate_reason(title, summary, source, topic, topic_priority)
    level = determine_level(topic_priority, score)

    return ScoredArticle(
        url=url,
        title=title,
        summary=summary,
        source=source,
        source_type=source_type,
        topic=topic,
        topic_priority=topic_priority,
        score=score,
        reason=reason,
        level=level,
    )
