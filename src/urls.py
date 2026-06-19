#!/usr/bin/env python3
"""URL 规范化 — 全项目唯一实现"""

from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """将 URL 规范化为 host+path 格式，用于去重和数据库索引。

    规则：
    - 去掉 www. 前缀
    - 去掉末尾 /
    - 保留 scheme 之外的 host + path
    """
    parsed = urlparse(str(url))
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}"
