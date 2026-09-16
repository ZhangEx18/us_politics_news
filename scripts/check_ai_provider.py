#!/usr/bin/env python3
"""在昂贵的抓取和评分前验证 OpenAI-compatible AI 配置。"""

from __future__ import annotations

import json
import os
import socket
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
CLIENT_USER_AGENT = "us-politics-news-crawler/1.0"
SESSION_ID = uuid.uuid4().hex
TIMEOUT_SECONDS = 30


def build_chat_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    if normalized.endswith("/api"):
        return f"{normalized}/v1/chat/completions"
    return f"{normalized}/chat/completions"


def _error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300] or "empty response"
    if not isinstance(payload, dict):
        return str(payload)[:300]
    error = payload.get("error", payload)
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error)[:300]


def _request_body(model: str) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "temperature": 0,
            "max_tokens": 2,
        }
    ).encode("utf-8")


def _load_config() -> tuple[str, str, str]:
    api_key = os.getenv("AI_API_KEY", "").strip()
    base_url = os.getenv("AI_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.getenv("AI_MODEL", DEFAULT_MODEL).strip()
    if not api_key:
        raise RuntimeError("AI_API_KEY is not configured")
    if not model:
        raise RuntimeError("AI_MODEL is not configured")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError("AI_BASE_URL must be an HTTP(S) URL")
    return api_key, base_url, model


def _send_preflight(api_key: str, base_url: str, model: str) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": CLIENT_USER_AGENT,
    }
    if "opencode.ai" in base_url:
        headers["x-opencode-session"] = SESSION_ID
    request = Request(
        build_chat_url(base_url),
        data=_request_body(model),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError(f"AI preflight returned HTTP {response.status}")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"AI preflight returned HTTP {error.code}: {_error_message(body)}"
        ) from error
    except (URLError, TimeoutError, socket.timeout) as error:
        raise RuntimeError(f"AI preflight network error: {error}") from error


def run_preflight() -> None:
    api_key, base_url, model = _load_config()
    _send_preflight(api_key, base_url, model)

    host = urlparse(base_url).netloc or "configured endpoint"
    print(f"AI preflight passed: {host}")


if __name__ == "__main__":
    try:
        run_preflight()
    except RuntimeError as error:
        raise SystemExit(f"AI preflight failed: {error}") from error
