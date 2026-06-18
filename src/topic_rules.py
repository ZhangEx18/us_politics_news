#!/usr/bin/env python3
"""
主题分类规则 — 四大栏目

美国政情 + 国际风云 + 科技前沿 + 财经脉动
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicRule:
    name: str
    keywords: tuple
    priority: int  # 1=重点, 2=观察, 3=低优先级


TOPIC_RULES: list[TopicRule] = [
    # ══════════════════════════════════════
    # 美国政治
    # ══════════════════════════════════════
    TopicRule(
        name="白宫与行政",
        keywords=("trump", "white house", "administration", "executive order", "president", "biden"),
        priority=1,
    ),
    TopicRule(
        name="国会与立法",
        keywords=("congress", "senate", "house", "bill", "legislation", "vote", "speaker", "filibuster"),
        priority=1,
    ),
    TopicRule(
        name="选举与竞选",
        keywords=("election", "midterm", "primary", "campaign", "ballot", "voter", "poll", "nomination"),
        priority=1,
    ),
    TopicRule(
        name="最高法院",
        keywords=("supreme court", "scotus", "justice", "ruling", "constitutional", "judicial"),
        priority=1,
    ),
    TopicRule(
        name="美国政治其他",
        keywords=("republican", "democrat", "gop", "dnc", "rnc", "political", "partisan"),
        priority=2,
    ),

    # ══════════════════════════════════════
    # 国际局势
    # ══════════════════════════════════════
    TopicRule(
        name="中美关系",
        keywords=("china", "beijing", "taiwan", "tariff", "trade war", "huawei", "tiktok"),
        priority=1,
    ),
    TopicRule(
        name="中东局势",
        keywords=("iran", "israel", "gaza", "middle east", "hezbollah", "houthi", "hamas", "ceasefire"),
        priority=1,
    ),
    TopicRule(
        name="俄乌冲突",
        keywords=("russia", "ukraine", "zelensky", "putin", "nato", "crimea", "donbas"),
        priority=1,
    ),
    TopicRule(
        name="外交政策",
        keywords=("foreign policy", "state department", "diplomacy", "sanctions", "treaty", "alliance", "united nations"),
        priority=2,
    ),
    TopicRule(
        name="国际其他",
        keywords=("european union", "eu", "japan", "korea", "india", "africa", "latin america"),
        priority=3,
    ),

    # ══════════════════════════════════════
    # 科技发展
    # ══════════════════════════════════════
    TopicRule(
        name="人工智能",
        keywords=("artificial intelligence", "ai ", "chatgpt", "openai", "anthropic", "gemini", "llm",
                   "machine learning", "deep learning", "neural", "gpt", "claude"),
        priority=1,
    ),
    TopicRule(
        name="半导体与芯片",
        keywords=("semiconductor", "chip", "nvidia", "tsmc", "intel", "amd", "qualcomm", "fab",
                   "silicon", "gpu", "asic", "euv"),
        priority=1,
    ),
    TopicRule(
        name="科技公司",
        keywords=("apple", "google", "microsoft", "meta", "amazon", "tesla", "spacex",
                   "startup", "unicorn"),
        priority=2,
    ),
    TopicRule(
        name="科技监管",
        keywords=("antitrust", "regulation", "data privacy", "gdpr", "section 230",
                   "big tech", "silicon valley"),
        priority=2,
    ),
    TopicRule(
        name="前沿科技",
        keywords=("quantum", "blockchain", "crypto", "bitcoin", "robotics", "autonomous",
                   "fusion", "biotech", "crispr"),
        priority=3,
    ),

    # ══════════════════════════════════════
    # 经济发展
    # ══════════════════════════════════════
    TopicRule(
        name="美联储与货币政策",
        keywords=("federal reserve", "fed ", "interest rate", "rate cut", "rate hike", "inflation",
                   "cpi", "pce", "monetary policy", "powell"),
        priority=1,
    ),
    TopicRule(
        name="宏观经济",
        keywords=("gdp", "recession", "economic growth", "unemployment", "jobs report", "nonfarm",
                   "consumer spending", "retail sales"),
        priority=1,
    ),
    TopicRule(
        name="贸易与关税",
        keywords=("trade", "tariff", "import duty", "export control", "trade deficit", "supply chain",
                   "wto"),
        priority=2,
    ),
    TopicRule(
        name="金融市场",
        keywords=("stock market", "wall street", "s&p 500", "nasdaq", "dow jones", "bond",
                   "treasury", "yield", "rally", "crash"),
        priority=2,
    ),
    TopicRule(
        name="企业动态",
        keywords=("earnings", "revenue", "profit", "ipo", "merger", "acquisition", "layoff",
                   "bankruptcy", "ceo"),
        priority=3,
    ),
]


def classify_topic(title: str, summary: str = "") -> tuple[str, int]:
    """
    对新闻进行主题分类

    Returns:
        (主题名称, 优先级) - 未匹配时返回 ("其他", 3)
    """
    text = f"{title} {summary}".lower()

    for rule in TOPIC_RULES:
        if any(kw in text for kw in rule.keywords):
            return rule.name, rule.priority

    return "其他", 3


def get_topic_keywords() -> dict[str, list[str]]:
    """获取所有主题的关键词映射"""
    return {rule.name: list(rule.keywords) for rule in TOPIC_RULES}


def get_topics_by_column() -> dict[str, list[str]]:
    """按四大栏目分组返回主题列表"""
    return {
        "美国政情": ["白宫与行政", "国会与立法", "选举与竞选", "最高法院", "美国政治其他"],
        "国际风云": ["中美关系", "中东局势", "俄乌冲突", "外交政策", "国际其他"],
        "科技前沿": ["人工智能", "半导体与芯片", "科技公司", "科技监管", "前沿科技"],
        "财经脉动": ["美联储与货币政策", "宏观经济", "贸易与关税", "金融市场", "企业动态"],
    }
