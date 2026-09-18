#!/usr/bin/env python3
"""内容政策工具：例行公告识别等跨模块共用的文本规则。"""

from __future__ import annotations

import re

# 例行公告/程序性文件的关键词模式（英文为主，命中即视为低新闻价值）
ROUTINE_NOTICE_PATTERNS: tuple[str, ...] = (
    r"\bseeks?\s+(public\s+|additional\s+|further\s+)?comments?\b",
    r"\bsolicits?\s+(public\s+|additional\s+)?comments?\b",
    r"\brequests?\s+(public\s+|additional\s+)?comments?\b",
    r"\bpublic\s+hearing\b",
    r"\bextends?\s+(the\s+)?(public\s+)?(comment|deadline|period)\b",
    r"\bcomment\s+period\b",
    r"\bproposed\s+(20\d\d\s+)?(budget|rule|policy\s+statement|policy\b|fee)",
    r"\bfees?\s+to\s+(access|register|participate)\b",
    r"\bwithdraws?\s+(an?\s+)?(obsolete|outdated|out-of-date)\b",
    r"\bpublishes?\s+(price\s+transparency\s+)?faqs?\b",
    r"\bfrequently\s+asked\s+questions\b",
    r"\bnotice\s+of\s+(proposed|intent)\b",
    r"(issues?|releases?|publishes?)[^.\n]{0,40}\bfomc\s+statement\b",
    r"(release[sd]?|summary\s+of)\s+(the\s+)?(economic\s+projections|beige\s+book)\b",
    r"(releases?|publishes?)\s+(the\s+)?(minutes|meeting\s+minutes)\b",
    r"\breadout\s+of\b",
    r"\b(spoke|speaks|call|call\s+with|phone\s+call)\s+(with|to)\b[^.\n]{0,60}\babout\b",
    r"\bschedules?\s+(a\s+)?(public\s+)?(meeting|hearing|vote)\b",
    # 中文例行公告
    r"公开征求意见",
    r"征求意见",
    r"延长.{0,6}(评论|征询|意见)",
    r"拟议(规则|预算|政策|费用)",
    r"撤回.{0,6}(过时|失效)",
    r"发布.{0,10}(FOMC|联邦公开市场委员会).{0,6}(声明|预测|纪要)",
    r"美联储.{0,12}发布.{0,8}(声明|预测|纪要)",
    r"(通话|会见|会谈).{0,8}(纪要|readout)",
    r"发布.{0,6}(通话|会谈)纪要",
)

_ROUTINE_NOTICE_RE = re.compile("|".join(ROUTINE_NOTICE_PATTERNS), re.IGNORECASE)


def is_routine_notice(*texts: object) -> bool:
    """判断标题/正文是否属于例行公告（评论期、听证安排、拟议预算/费用等）。"""
    haystack = " ".join(str(text or "") for text in texts).strip()
    if not haystack:
        return False
    return bool(_ROUTINE_NOTICE_RE.search(haystack))


# 拒绝原因枚举（统一观测口径）
REJECT_REASONS: dict[str, str] = {
    "routine_notice": "例行公告",
    "low_newsworthiness": "低新闻价值",
    "soft_news": "软新闻",
    "opinion_piece": "观点/分析稿",
    "promo_piece": "公关稿",
    "cryptic_title": "标题不可读",
    "unreadable_body": "正文不可用",
    "duplicate_event": "同事件重复",
    "source_quota": "来源配额",
    "date_out_of_window": "日期越窗",
    "body_too_short": "正文过短",
    "live_blog": "直播页",
}
