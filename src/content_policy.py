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
    r"\bschedules?\s+(a\s+)?(public\s+)?(meeting|hearing|vote)\b",
    # 中文例行公告
    r"公开征求意见",
    r"征求意见",
    r"延长.{0,6}(评论|征询|意见)",
    r"拟议(规则|预算|政策|费用)",
    r"撤回.{0,6}(过时|失效)",
)

_ROUTINE_NOTICE_RE = re.compile("|".join(ROUTINE_NOTICE_PATTERNS), re.IGNORECASE)


def is_routine_notice(*texts: object) -> bool:
    """判断标题/正文是否属于例行公告（评论期、听证安排、拟议预算/费用等）。"""
    haystack = " ".join(str(text or "") for text in texts).strip()
    if not haystack:
        return False
    return bool(_ROUTINE_NOTICE_RE.search(haystack))
