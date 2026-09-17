#!/usr/bin/env python3
"""
AI 分析层 — 两段式流程

1. score_batch: 批量评分，输出 score/column/tags/summary/event_key
2. generate_column_digest: 按栏目生成结构化事件卡片
"""

import asyncio
import json
import os
import re
import time
import uuid
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# 加载 .env
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

# ── Prompt 模板（外置 prompts/*.md，带版本头） ──
_PROMPTS_DIR = _project_root / "prompts"
_PROMPT_HEADER_RE = re.compile(r"^<!--.*?-->\s*", re.DOTALL)


def _load_prompt_template(name: str) -> str:
    """从 prompts/<name>.md 读取模板并剥离版本头注释。"""
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"缺少 prompt 模板文件: {path}") from exc
    return _PROMPT_HEADER_RE.sub("", text, count=1).strip() + "\n"

# OpenCode Zen Go 要求自定义 User-Agent + 稳定会话标识
_CLIENT_USER_AGENT = "us-politics-news-crawler/1.0"
_SESSION_ID = uuid.uuid4().hex

# ── AI 配置 ──


def _load_ai_config() -> dict:
    """从环境变量加载 AI 配置，无 key 时 raise；支持配置备用通道。"""
    api_key = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "未配置 AI_API_KEY 环境变量，请在 .env 或系统环境变量中设置"
        )
    config = {
        "api_key": api_key,
        "base_url": os.getenv("AI_BASE_URL") or "https://opencode.ai/zen/go/v1",
        "model": os.getenv("AI_MODEL") or "deepseek-v4.1-flash",
    }
    fallback_base_url = os.getenv("AI_FALLBACK_BASE_URL", "").strip()
    fallback_model = os.getenv("AI_FALLBACK_MODEL", "").strip()
    if fallback_base_url and fallback_model:
        config["fallback"] = {
            "api_key": os.getenv("AI_FALLBACK_API_KEY", "").strip() or api_key,
            "base_url": fallback_base_url,
            "model": fallback_model,
        }
    return config


# ── Prompt 加载 ──


def _load_prompt(path: str, **kwargs) -> str:
    """加载提示词模板并替换 {key} 占位符"""
    text = Path(path).read_text(encoding="utf-8")
    # 保护已有的 {{ }} 不被 format 误伤
    text = text.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")
    for k, v in kwargs.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")


# ── LLM 调用 ──


async def _call_llm(prompt: str, config: dict, timeout: int = 120) -> str:
    """调用 LLM；主通道失败时自动切换备用通道（config["fallback"]）。"""
    try:
        return await _call_llm_once(prompt, config, timeout)
    except Exception as exc:
        message = str(exc)
        if "max_tokens 截断" in message:
            current_cap = int(config.get("max_tokens") or 0)
            new_cap = min(max(current_cap * 2, current_cap + 2000), 32000)
            if current_cap and new_cap > current_cap:
                _ai_log(f"输出被截断，max_tokens 提升至 {new_cap} 重试")
                return await _call_llm(prompt, {**config, "max_tokens": new_cap}, timeout)
        if "错误 400" in message:
            if config.get("json_schema"):
                _ai_log("json_schema 不被支持，降级为 json_object 重试")
                return await _call_llm(
                    prompt, {**config, "json_schema": None, "json_object": True}, timeout,
                )
            if config.get("json_object"):
                _ai_log("json_object 不被支持，降级为普通模式重试")
                return await _call_llm(prompt, {**config, "json_object": False}, timeout)
        fallback = config.get("fallback")
        if not fallback or config.get("_is_fallback"):
            raise
        _ai_log(
            f"主通道失败({type(exc).__name__}: {message[:80]})，"
            f"切换备用通道 {fallback.get('model')}"
        )
        return await _call_llm_once(
            prompt,
            {**fallback, "_is_fallback": True, "json_schema": config.get("json_schema")},
            timeout,
        )


_SCORE_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "link", "score", "column", "event_key", "event_date", "freshness_status",
        "content_kind", "is_hard_news", "tags", "summary",
        "impact", "prominence", "timeliness", "novelty", "conflict", "routine",
    ],
    "properties": {
        "link": {"type": "string"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "column": {"type": "string", "enum": ["us_politics", "global_affairs", "technology", "economy"]},
        "event_key": {"type": "string"},
        "event_date": {"type": "string"},
        "freshness_status": {
            "type": "string",
            "enum": ["today", "recent_followup", "old_background", "unknown_date"],
        },
        "content_kind": {"type": "string"},
        "is_hard_news": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "impact": {"type": "number", "minimum": 0, "maximum": 1},
        "prominence": {"type": "number", "minimum": 0, "maximum": 1},
        "timeliness": {"type": "number", "minimum": 0, "maximum": 1},
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "conflict": {"type": "number", "minimum": 0, "maximum": 1},
        "routine": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

SCORE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": _SCORE_ITEM_SCHEMA}},
}

_DIGEST_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title_zh", "reader_body", "core_facts", "source_links", "is_followup"],
    "properties": {
        "title_zh": {"type": "string"},
        "reader_body": {"type": "string"},
        "core_facts": {"type": "string"},
        "source_links": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url"],
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            },
        },
        "is_followup": {"type": "boolean"},
    },
}

DIGEST_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {"events": {"type": "array", "items": _DIGEST_EVENT_SCHEMA}},
}

JSON_SCHEMAS: dict[str, dict] = {
    "score_items": SCORE_JSON_SCHEMA,
    "digest_events": DIGEST_JSON_SCHEMA,
}


def _build_llm_payload(prompt: str, config: dict) -> dict:
    """构造 OpenAI 兼容请求体，支持 max_tokens 与 json_schema 强约束。"""
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(config.get("temperature", 0.3)),
    }
    max_tokens = config.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    schema_name = config.get("json_schema")
    if schema_name and schema_name in JSON_SCHEMAS:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": JSON_SCHEMAS[schema_name],
            },
        }
    elif config.get("json_object"):
        payload["response_format"] = {"type": "json_object"}
    return payload


async def _call_llm_once(prompt: str, config: dict, timeout: int = 120) -> str:
    """调用 OpenAI 兼容 API（兼容推理模型 content / reasoning_content）"""
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": _CLIENT_USER_AGENT,
    }
    if "opencode.ai" in config["base_url"]:
        headers["x-opencode-session"] = _SESSION_ID
    payload = _build_llm_payload(prompt, config)
    base_url = config["base_url"].rstrip("/")
    if base_url == "https://openrouter.ai/api":
        base_url = f"{base_url}/v1"
    url = f"{base_url}/chat/completions"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM API 错误 {resp.status}: {body[:300]}")
            data = await resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            content = str(msg.get("content") or "").strip()
            if not content and choice.get("finish_reason") == "length":
                raise RuntimeError(
                    f"输出被 max_tokens 截断（推理 token 占满预算，cap={payload.get('max_tokens')}）"
                )
            return content or msg.get("reasoning_content", "")


def _timeout_for(config: dict, scope: str, default: int) -> int:
    """按调用场景读取超时配置。"""
    return int(config.get(f"{scope}_timeout_seconds") or config.get("timeout_seconds") or default)


# ── 术语表（glossary） ──

_GLOSSARY_PATH = _project_root / "config" / "glossary.yaml"
_AUDITED_GLOSSARY_GROUPS = ("people", "laws")


@lru_cache(maxsize=1)
def _load_glossary() -> dict[str, dict[str, tuple[str, ...]]]:
    """加载术语表：{group: {中文译名: (英文变体...)}}。"""
    try:
        import yaml

        payload = yaml.safe_load(_GLOSSARY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    groups: dict[str, dict[str, tuple[str, ...]]] = {}
    for group, entries in payload.items():
        if not isinstance(entries, dict):
            continue
        normalized: dict[str, tuple[str, ...]] = {}
        for zh, variants in entries.items():
            if isinstance(variants, str):
                variants = [variants]
            values = tuple(str(v).strip() for v in (variants or []) if str(v).strip())
            if values:
                normalized[str(zh).strip()] = values
        if normalized:
            groups[str(group)] = normalized
    return groups


def glossary_hint(*texts: str, limit: int = 12) -> str:
    """扫描文本中命中的术语，生成注入 prompt 的中英对照表。"""
    groups = _load_glossary()
    if not groups:
        return ""
    haystack = " ".join(str(text or "") for text in texts).lower()
    lines: list[str] = []
    seen_zh: set[str] = set()
    for entries in groups.values():
        for zh, variants in entries.items():
            if zh in seen_zh:
                continue
            hit = next((variant for variant in variants if variant.lower() in haystack), "")
            if not hit:
                continue
            seen_zh.add(zh)
            lines.append(f"- {zh} = {hit}")
            if len(lines) >= limit:
                return "\n## 术语对照（必须使用中文译名）\n" + "\n".join(lines) + "\n"
    if not lines:
        return ""
    return "\n## 术语对照（必须使用中文译名）\n" + "\n".join(lines) + "\n"


def count_untranslated_terms(text: str) -> int:
    """统计正文中未中文化的专名（只审计 people/laws；品牌名保留英文不计）。"""
    groups = _load_glossary()
    content = str(text or "")
    lowered = content.lower()
    count = 0
    for group in _AUDITED_GLOSSARY_GROUPS:
        for zh, variants in groups.get(group, {}).items():
            if zh in content:
                continue
            if any(variant.lower() in lowered for variant in variants):
                count += 1
    return count


def _ai_log(message: str) -> None:
    """统一 AI 阶段日志，确保长任务在本地和 CI 都能及时看到进度。"""
    print(f"  [AI] {message}", flush=True)


def _format_iso_date_for_reader(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return ""
    return f"{int(match.group(2))} 月 {int(match.group(3))} 日"


# ── JSON 解析工具 ──


def _strip_markdown_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹"""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _normalize_jsonish_text(text: str) -> str:
    """修正常见的模型 JSON 近似输出，例如中文弯引号。"""
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "：": ":",
        "，": ",",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def _parse_jsonish_object(response: str) -> dict:
    """解析模型返回的 JSON 对象，兼容 markdown 包裹和少量中文标点。"""
    text = _strip_markdown_fence(response)
    candidates = [text, _normalize_jsonish_text(text)]
    for candidate in candidates:
        variants = [candidate]
        if "{{" in candidate:
            variants.append(candidate.replace("{{", "{").replace("}}", "}"))
        for variant in variants:
            try:
                parsed = json.loads(variant)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

            m = re.search(r"\{.*\}", variant, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass

    raise ValueError(f"无法从响应中解析 JSON 对象: {response[:300]}")


def _parse_score_response(response: str) -> list[dict]:
    """解析评分 LLM 响应，兼容 {"items":[...]} / [...] / markdown 包裹 / JSON-ish 输出。"""
    text = _strip_markdown_fence(response)
    candidates = [text, _normalize_jsonish_text(text)]
    parsed = None

    def _try_load(raw: str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    for candidate in candidates:
        normalized = candidate.replace("{{", "{").replace("}}", "}")

        parsed = _try_load(normalized)
        if parsed is not None:
            break

        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, normalized, re.DOTALL)
            if not m:
                continue
            parsed = _try_load(m.group())
            if parsed is not None:
                break
        if parsed is not None:
            break

    if parsed is None:
        raise ValueError(f"无法从评分响应中解析 JSON: {response[:200]}")

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("items", "results", "data", "scores"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        list_vals = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_vals) == 1:
            return list_vals[0]

    raise ValueError(f"评分响应中未找到数组: {response[:200]}")


# ── 分批逻辑（参考 llm.py） ──


def _split_entries_for_batch(
    entries: list[dict],
    max_prompt_chars: int = 12000,
    prompt_template_chars: int = 1500,
    content_limit: int = 2000,
) -> list[list[dict]]:
    """将 entries 按字符数分批，每批不超过 max_prompt_chars"""
    if not entries:
        return []

    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for entry in entries:
        entry_chars = len(json.dumps({
            "link": entry.get("link", ""),
            "title": entry.get("title", "")[:100],
            "source": entry.get("source", ""),
            "published": entry.get("published", ""),
            "content": entry.get("content", "")[:content_limit],
        }, ensure_ascii=False))

        if current_chars + entry_chars + prompt_template_chars > max_prompt_chars and current:
            batches.append(current)
            current = [entry]
            current_chars = entry_chars
        else:
            current.append(entry)
            current_chars += entry_chars

    if current:
        batches.append(current)
    return batches


def _merge_scores(entries: list[dict], scores: list[dict]) -> list[dict]:
    """将评分结果按 link/url 合并回原始 entries"""
    score_map = {}
    for s in scores:
        key = s.get("link") or s.get("url", "")
        if key:
            score_map[key] = s
    merged = []
    for entry in entries:
        link = entry.get("link") or entry.get("url", "")
        s = score_map.get(link, {})
        score_val = s.get("score", entry.get("score"))
        if isinstance(score_val, str):
            try:
                score_val = int(score_val)
            except (ValueError, TypeError):
                score_val = 0
        column_val = s.get("column", entry.get("column", ""))
        merged_item = {
            **entry,
            "score": score_val,
            "column": column_val,
            "content_kind": s.get("content_kind", entry.get("content_kind", "analysis")),
            "is_hard_news": bool(s.get("is_hard_news", entry.get("is_hard_news", False))),
            "tags": s.get("tags", entry.get("tags", [])),
            "summary": s.get("summary", entry.get("summary", "")),
            "event_key": s.get("event_key", entry.get("event_key", "")),
            "event_date": s.get("event_date", entry.get("event_date", "")),
            "freshness_date": entry.get("freshness_date", s.get("freshness_date", "")),
            "freshness_status": s.get("freshness_status", entry.get("freshness_status", "")),
            "source_tier": entry.get("source_tier", s.get("source_tier", 4)),
            "language": entry.get("language", s.get("language", "")),
            "information_gain": s.get("information_gain", entry.get("information_gain", "")),
            "event_stage": s.get("event_stage", entry.get("event_stage", "")),
            "verifiability": s.get("verifiability", entry.get("verifiability", "")),
            "impact": s.get("impact", entry.get("impact", "")),
            "prominence": s.get("prominence", entry.get("prominence", "")),
            "timeliness": s.get("timeliness", entry.get("timeliness", "")),
            "novelty": s.get("novelty", entry.get("novelty", "")),
            "conflict": s.get("conflict", entry.get("conflict", "")),
            "routine": s.get("routine", entry.get("routine", "")),
            "impact_scope": s.get("impact_scope", entry.get("impact_scope", "")),
        }
        merged_item["newsworthiness"] = derive_newsworthiness(
            merged_item,
            s.get("newsworthiness") or entry.get("newsworthiness"),
        )
        merged.append(merged_item)
    return merged


def coerce_unit_interval(value: object) -> Optional[float]:
    """把 0-1 评分字段归一化为 float；无法解析时返回 None。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


NEWSWORTHINESS_WEIGHTS: dict[str, float] = {
    "impact": 0.35,
    "prominence": 0.2,
    "timeliness": 0.2,
    "novelty": 0.15,
    "conflict": 0.1,
}


def derive_newsworthiness(item: dict, fallback: object = None) -> float | str:
    """由新闻价值五维加权推导 newsworthiness；维度缺失时回退模型原值。"""
    dims = {
        key: coerce_unit_interval(item.get(key))
        for key in NEWSWORTHINESS_WEIGHTS
    }
    dims = {key: value for key, value in dims.items() if value is not None}
    if not dims:
        fallback_value = coerce_unit_interval(fallback)
        return fallback_value if fallback_value is not None else ""
    total_weight = sum(NEWSWORTHINESS_WEIGHTS[key] for key in dims)
    value = sum(NEWSWORTHINESS_WEIGHTS[key] * dim for key, dim in dims.items()) / total_weight
    return round(value, 3)


def entry_newsworthiness_ok(
    entry: dict,
    min_newsworthiness: float = 0.5,
    max_routine: float = 0.6,
) -> bool:
    """新闻价值门槛：例行度过高或价值过低则剔除；字段缺失时放行（兼容历史数据）。"""
    routine = coerce_unit_interval(entry.get("routine"))
    if routine is not None and routine >= max_routine:
        return False
    newsworthiness = coerce_unit_interval(entry.get("newsworthiness"))
    if newsworthiness is not None and newsworthiness < min_newsworthiness:
        return False
    return True


# ── score_batch ──


SCORE_PROMPT_TEMPLATE = _load_prompt_template("score")


async def _score_single_batch(
    entries: list[dict], config: dict, batch_index: int = 0
) -> tuple[list[dict], list[str]]:
    """对单批 entries 评分，返回 (matched_scores, errors)"""
    content_limit = int(config.get("score_content_chars", 400))
    base_url = str(config.get("base_url") or "").rstrip("/")
    if base_url == "https://openrouter.ai/api":
        content_limit = min(content_limit, 220)
    entries_for_llm = [
        {
            "link": e.get("link", ""),
            "title": e.get("title", "无标题"),
            "source": e.get("source", "未知来源"),
            "published": e.get("published", ""),
            "fetched": e.get("fetched", ""),
            "freshness_date": e.get("freshness_date", ""),
            "content": (e.get("content", "") or "")[:content_limit],
        }
        for e in entries
    ]
    entries_json = json.dumps(entries_for_llm, ensure_ascii=False, indent=2)
    prompt = SCORE_PROMPT_TEMPLATE.replace("{entries_json}", entries_json)

    try:
        response = await _call_llm(
            prompt,
            {**config, "temperature": 0, "max_tokens": 16000, "json_object": True},
            timeout=_timeout_for(config, "score", 120),
        )
        results = _parse_score_response(response)
        if not isinstance(results, list):
            raise ValueError(f"LLM 返回非数组: {type(results)}")

        # 按 link 过滤，只保留输入中有的
        entry_links = {e.get("link") for e in entries if e.get("link")}
        matched = [r for r in results if isinstance(r, dict) and r.get("link") in entry_links]

        errors = []
        if len(matched) != len(entries):
            missing = sorted(entry_links - {r.get("link") for r in matched})
            msg = (f"批次{batch_index + 1} 结果不完整: "
                   f"输入{len(entries)}, 匹配{len(matched)}, 缺失{missing}")
            _ai_log(msg)
            errors.append(msg)

        return matched, errors

    except Exception as e:
        detail = str(e).strip() or repr(e)
        msg = f"批次{batch_index + 1} 评分失败: {type(e).__name__}: {detail}"
        _ai_log(msg)
        return [], [msg]


def _is_retryable_score_error(errors: list[str]) -> bool:
    """超时、结果不完整、连接/载荷波动或风控拒绝时，值得拆小重试。"""
    if not errors:
        return False
    retryable_tokens = (
        "TimeoutError",
        "结果不完整",
        "high risk",
        "ClientConnectorError",
        "ClientPayloadError",
        "TransferEncodingError",
    )
    return any(token in err for err in errors for token in retryable_tokens)


async def _score_batch_with_retry(
    entries: list[dict],
    config: dict,
    batch_index: int = 0,
    depth: int = 0,
) -> tuple[list[dict], list[str]]:
    """对单批执行评分；失败时拆分重试，尽量恢复覆盖率。"""
    start = time.monotonic()
    depth_suffix = f", depth={depth}" if depth else ""
    _ai_log(f"批次{batch_index + 1} 开始: {len(entries)} 条{depth_suffix}")
    scores, errors = await _score_single_batch(entries, config, batch_index)
    elapsed = time.monotonic() - start
    _ai_log(
        f"批次{batch_index + 1} 完成: 输入{len(entries)}, "
        f"匹配{len(scores)}, 耗时{elapsed:.1f}s{depth_suffix}"
    )
    if not entries:
        return scores, errors

    matched_links = {
        score.get("link") or score.get("url", "")
        for score in scores
        if isinstance(score, dict)
    }
    missing_entries = [
        entry for entry in entries
        if (entry.get("link") or entry.get("url", "")) not in matched_links
    ]

    retry_depth = int(config.get("score_retry_split_depth", 3))
    if (
        depth >= retry_depth
        or not _is_retryable_score_error(errors)
        or not missing_entries
    ):
        return scores, errors

    _ai_log(
        f"批次{batch_index + 1} 拆分重试: "
        f"{len(missing_entries)} 条, depth={depth + 1}/{retry_depth}"
    )

    if len(missing_entries) == 1:
        retry_scores, retry_errors = await _score_batch_with_retry(
            missing_entries,
            config,
            batch_index,
            depth + 1,
        )
        merged_scores = {
            (item.get("link") or item.get("url", "")): item
            for item in scores + retry_scores
            if isinstance(item, dict) and (item.get("link") or item.get("url", ""))
        }
        if len(merged_scores) == len(entries):
            return list(merged_scores.values()), []
        unresolved_link = missing_entries[0].get("link") or missing_entries[0].get("url", "")
        return list(merged_scores.values()), retry_errors + [
            f"批次{batch_index + 1} 拆分后仍缺失 1 条: ['{unresolved_link}']"
        ]

    mid = len(missing_entries) // 2
    left_scores, left_errors = await _score_batch_with_retry(
        missing_entries[:mid], config, batch_index, depth + 1
    )
    right_scores, right_errors = await _score_batch_with_retry(
        missing_entries[mid:], config, batch_index, depth + 1
    )

    merged_scores: dict[str, dict] = {}
    for item in scores + left_scores + right_scores:
        if not isinstance(item, dict):
            continue
        link = item.get("link") or item.get("url", "")
        if link:
            merged_scores[link] = item

    recovered_links = set(merged_scores.keys())
    unresolved = [
        entry for entry in entries
        if (entry.get("link") or entry.get("url", "")) not in recovered_links
    ]
    if unresolved:
        unresolved_links = [entry.get("link") or entry.get("url", "") for entry in unresolved]
        retry_errors = left_errors + right_errors
        retry_errors.append(
            f"批次{batch_index + 1} 拆分后仍缺失 {len(unresolved)} 条: {unresolved_links}"
        )
        return list(merged_scores.values()), retry_errors

    return list(merged_scores.values()), []


async def score_batch(
    entries: list[dict],
    config: Optional[dict] = None,
    max_prompt_chars: int = 12000,
    max_concurrent: int = 3,
) -> tuple[list[dict], list[str]]:
    """批量评分 -- 按 max_prompt_chars 分批，并发控制。

    Args:
        entries: 候选新闻列表，每条需含 link/title/source/content
        config: AI 配置，None 则从环境变量读取
        max_prompt_chars: 每批最大字符数
        max_concurrent: 最大并发批次数

    Returns:
        (merged_entries, errors) -- 每条 entry 附带 score/column/tags/summary/event_key
    """
    if config is None:
        config = _load_ai_config()

    if not entries:
        return [], []

    max_prompt_chars = int(config.get("score_max_prompt_chars", max_prompt_chars))
    max_concurrent = int(config.get("score_max_concurrent", max_concurrent))
    wall_timeout = float(config.get("score_wall_timeout_seconds", 0) or 0)
    content_limit = int(config.get("score_content_chars", 400))

    base_url = str(config.get("base_url") or "").rstrip("/")
    if base_url == "https://openrouter.ai/api":
        max_concurrent = 1
        max_prompt_chars = min(max_prompt_chars, 5000)
        content_limit = min(content_limit, 220)

    batches = _split_entries_for_batch(
        entries,
        max_prompt_chars=max_prompt_chars,
        content_limit=content_limit,
    )
    _ai_log(f"评分: {len(entries)} 条 -> {len(batches)} 批")

    if len(batches) == 1:
        if wall_timeout > 0:
            try:
                scores, errors = await asyncio.wait_for(
                    _score_batch_with_retry(batches[0], config, 0),
                    timeout=wall_timeout,
                )
            except asyncio.TimeoutError:
                errors = [f"评分总耗时超过 {wall_timeout}s，已取消单批任务"]
                _ai_log(errors[0])
                return _merge_scores(entries, []), errors
        else:
            scores, errors = await _score_batch_with_retry(batches[0], config, 0)
        return _merge_scores(entries, scores), errors

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _limited(idx: int, batch: list[dict]):
        async with semaphore:
            return await _score_batch_with_retry(batch, config, idx)

    tasks = [
        asyncio.create_task(_limited(i, b), name=f"score-batch-{i + 1}")
        for i, b in enumerate(batches)
    ]
    done, pending = await asyncio.wait(tasks, timeout=wall_timeout or None)
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        _ai_log(f"评分总耗时超过 {wall_timeout}s，取消 {len(pending)} 个未完成批次")

    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as exc:
            results.append(([], [f"{task.get_name()} 评分失败: {type(exc).__name__}: {exc}"]))

    all_scores, all_errors = [], []
    for scores, errors in results:
        all_scores.extend(scores)
        all_errors.extend(errors)
    if pending:
        all_errors.append(
            f"评分总耗时超过 {wall_timeout}s，已取消 {len(pending)} 个未完成批次"
        )

    return _merge_scores(entries, all_scores), all_errors


# ── merge_events ──


def _normalize_event_title(title: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(title or "")).lower()


def _titles_similar(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 8:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.85


def _event_key_parts(event_key: str) -> tuple[set[str], str]:
    """拆 event_key 为（关键词集合，日期后缀）。"""
    tokens: set[str] = set()
    date = ""
    for part in str(event_key or "").lower().split("_"):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"20\d{6}", part):
            date = part
        else:
            tokens.add(part)
    return tokens, date


def _event_keys_mergeable(key_a: str, key_b: str) -> bool:
    """同日期 + 共享 ≥2 个关键词的 event_key 视为同一事件（跨表述/跨栏目兜底）。"""
    if not key_a or not key_b:
        return False
    tokens_a, date_a = _event_key_parts(key_a)
    tokens_b, date_b = _event_key_parts(key_b)
    if not date_a or date_a != date_b:
        return False
    return len(tokens_a & tokens_b) >= 2


def _merge_event_group(group: list[dict], event_key: str) -> dict:
    """把一个事件组内的多条报道合并为一条（最高分为主条目）。"""
    group.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
    primary = group[0]

    seen_links: set[str] = set()
    source_links: list[dict] = []
    evidence_blocks: list[str] = []

    for item in group:
        link = item.get("link", "")
        if link and link not in seen_links:
            seen_links.add(link)
            link_entry = {
                "title": item.get("title", ""),
                "url": link,
            }
            source_name = str(item.get("source") or "")
            if "Google News" in source_name:
                link_entry["via"] = "Google News"
            elif "AIHOT" in source_name:
                link_entry["via"] = "AIHOT"
            source_links.append(link_entry)
        summary = (item.get("summary") or "").strip()
        content = (item.get("content") or "").strip()
        evidence_parts = []
        if summary:
            evidence_parts.append(f"摘要：{summary}")
        if content:
            evidence_parts.append(f"原文片段：{content[:1200]}")
        evidence = "\n".join(evidence_parts).strip()
        if evidence and evidence not in evidence_blocks:
            evidence_blocks.append(evidence)

    if source_links:
        source_links[0]["primary"] = True

    all_tags: list[str] = []
    seen_tags: set[str] = set()
    for item in group:
        for tag in item.get("tags", []):
            if tag and tag not in seen_tags:
                seen_tags.add(tag)
                all_tags.append(tag)

    return {
        **primary,
        "event_key": event_key or str(primary.get("event_key") or ""),
        "source_links": source_links,
        "content": "\n\n".join(evidence_blocks),
        "tags": all_tags[:5],
    }


def merge_events(items: list[dict]) -> list[dict]:
    """按 event_key + 标题相似度合并同一事件的多源报道。

    - 同一 event_key 的多条合并为一条
    - event_key 缺失或不同、但主标题高度相似的条目也合并
    - 保留最高分的作为主条目
    - 合并所有来源链接到 source_links
    - 合并所有 summary 到 content
    """
    if not items:
        return []

    groups: dict[str, list[dict]] = {}
    no_key: list[dict] = []

    for item in items:
        key = (item.get("event_key") or "").strip()
        if key:
            groups.setdefault(key, []).append(item)
        else:
            no_key.append(item)

    def _primary_norm(bucket: list[dict]) -> str:
        best = max(bucket, key=lambda x: x.get("score", 0) or 0)
        return _normalize_event_title(best.get("title", ""))

    clusters: list[dict] = []
    for key in sorted(groups):
        bucket = list(groups[key])
        norm = _primary_norm(bucket)
        target = next(
            (
                c
                for c in clusters
                if c["key"] == key
                or _titles_similar(norm, c["norm"])
                or _event_keys_mergeable(key, c["key"])
            ),
            None,
        )
        if target is None:
            clusters.append({"key": key, "items": bucket, "norm": norm})
        else:
            target["items"].extend(bucket)
            target["norm"] = _primary_norm(target["items"])

    for item in no_key:
        norm = _normalize_event_title(item.get("title", ""))
        target = next((c for c in clusters if _titles_similar(norm, c["norm"])), None)
        if target is None:
            clusters.append({"key": "", "items": [item], "norm": norm})
        else:
            target["items"].append(item)
            target["norm"] = _primary_norm(target["items"])

    return [_merge_event_group(cluster["items"], cluster["key"]) for cluster in clusters]


# ── generate_column_digest ──


def _build_digest_evidence(event: dict) -> str:
    """为写作模型构造低风险证据摘要，避免把长原文直接送入 digest。"""
    evidence_lines: list[str] = []
    summary = str(event.get("summary") or "").strip()
    if summary:
        evidence_lines.append(f"摘要：{summary}")
    language = str(event.get("language") or "").strip()
    if language:
        evidence_lines.append(f"语言：{language}")
    source = str(event.get("source") or "").strip()
    if source:
        evidence_lines.append(f"主来源：{source}")
    source_tier = event.get("source_tier")
    if source_tier is not None:
        evidence_lines.append(f"来源层级：{source_tier}")
    freshness_date = str(event.get("freshness_date") or "").strip()
    if freshness_date:
        evidence_lines.append(f"发布时间口径：{freshness_date}")
    event_date = str(event.get("event_date") or "").strip()
    if event_date:
        evidence_lines.append(f"事件日期：{event_date}")
    freshness_status = str(event.get("freshness_status") or "").strip()
    if freshness_status:
        evidence_lines.append(f"今日性状态：{freshness_status}")
    source_titles = []
    for source_link in event.get("source_links", []):
        if not isinstance(source_link, dict):
            continue
        title = str(source_link.get("title") or "").strip()
        if title and title not in source_titles:
            source_titles.append(title)
    if source_titles:
        evidence_lines.append("来源标题：" + "；".join(source_titles[:5]))
    return "\n".join(evidence_lines)


COLUMN_DIGEST_PROMPT_TEMPLATE = _load_prompt_template("digest")


HEADLINE_TRANSLATION_PROMPT_TEMPLATE = _load_prompt_template("translate")


# 栏目定义映射
_COLUMN_DEFINITIONS: dict[str, str] = {
    "us_politics": "只写美国国内权力结构、法院、国会、白宫、州政治、选举、调查、人事和联邦政策；不写以外交、战争、盟友关系、对华/对伊互动为主轴的事件。",
    "global_affairs": "写外交、战争、军事、联盟关系、国际谈判、国际组织和对华/对俄/对伊博弈；即使主角是美国，只要主线是对外事务，也归这里。",
    "technology": "写 AI、芯片、半导体、平台、科研突破、科技监管和技术产业竞争；不写只是把外交新闻换成 AI 角度的重复条目。",
    "economy": "写利率、通胀、就业、贸易、关税、财报、商品价格、产业链和资本市场真实变化；不写荐股、观察名单和泛投资建议。",
}

_PERIODICAL_TYPE_LABELS: dict[str, str] = {
    "weekly": "周报",
    "monthly": "月报",
}


_PERIODICAL_OVERVIEW_BANNED_PHRASES = (
    "形势复杂",
    "影响深远",
    "仍需观察",
    "值得关注",
    "可以看出",
    "总体来看",
    "整体来看",
    "可以认为",
    "不难看出",
    "需要注意的是",
    "值得注意的是",
)


def _build_periodical_overview_columns(columns: dict[str, dict]) -> dict[str, dict]:
    compact_columns: dict[str, dict] = {}
    for col_key, col_data in columns.items():
        if isinstance(col_data, dict):
            detailed = col_data.get("detailed_events", [])
            analysis = str(col_data.get("analysis", "") or "").strip()
        else:
            detailed = col_data
            analysis = ""

        events = []
        top_titles = []
        for event in detailed[:4]:
            title_zh = str(event.get("title_zh") or event.get("title") or "").strip()
            reader_body = str(event.get("reader_body", "") or "").strip()
            if title_zh and title_zh not in top_titles:
                top_titles.append(title_zh)
            events.append({
                "title_zh": title_zh,
                "reader_body": reader_body,
            })

        compact_columns[col_key] = {
            "analysis": analysis,
            "count": len(detailed),
            "top_titles": top_titles,
            "events": events,
        }
    return compact_columns


def _clean_periodical_overview_text(text: object, limit: int | None = None) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    for phrase in _PERIODICAL_OVERVIEW_BANNED_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*([，,。．;；:：])\s*", r"\1", cleaned)
    cleaned = re.sub(r"([，,。．;；:：]){2,}", r"\1", cleaned)
    cleaned = cleaned.strip(" ，,。．;；:：")
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" ，,。．;；:：")
    return cleaned


PERIODICAL_OVERVIEW_PROMPT_TEMPLATE = _load_prompt_template("periodical")


DAILY_OVERVIEW_PROMPT_TEMPLATE = _load_prompt_template("overview")


async def generate_column_digest(
    column_key: str,
    column_label: str,
    events: list[dict],
    history_context: str,
    ai_config: dict,
    word_count_min: int = 5000,
    word_count_max: int = 10000,
) -> list[dict]:
    """
    为单个栏目生成结构化事件卡片

    Args:
        column_key: 栏目 key (us_politics/global_affairs/technology/economy)
        column_label: 栏目中文名
        events: 该栏目的候选事件列表 [{"title", "source", "score", "summary", "content", "source_links"}]
        history_context: 近几天已推送事件文本
        ai_config: AI 配置 {api_key, base_url, model}
        word_count_min/max: 字数目标

    Returns:
        [{"title_zh", "core_facts", "background_impact", "why_it_matters", "source_links", "is_followup"}, ...]
    """
    if not events:
        return []

    # 构建历史上下文段落
    if history_context:
        history_section = (
            "对比近几天已推送的事件上下文。如果今天的信息只是重复已报道的事实，请直接丢弃。\n"
            "如果今天的信息是历史事件的延续，将 is_followup 设为 true。\n\n"
            f"<RECENT_PUSH_CONTEXT>\n{history_context}\n</RECENT_PUSH_CONTEXT>"
        )
    else:
        history_section = "(无历史上下文)"

    # 简化事件数据给 LLM
    events_for_llm = []
    for e in events:
        freshness_date = str(e.get("freshness_date") or "").strip()
        event_date = str(e.get("event_date") or "").strip()
        events_for_llm.append({
            "title": e.get("title", ""),
            "source": e.get("source", ""),
            "score": e.get("score", 0),
            "summary": e.get("summary", ""),
            "freshness_date": freshness_date,
            "event_date": event_date,
            "freshness_date_formatted": _format_iso_date_for_reader(freshness_date),
            "event_date_formatted": _format_iso_date_for_reader(event_date),
            "freshness_status": e.get("freshness_status", ""),
            "evidence": _build_digest_evidence(e)[:int(ai_config.get("digest_content_chars", 1000))],
            "source_links": e.get("source_links", []),
        })

    column_definition = _COLUMN_DEFINITIONS.get(column_key, column_label)

    prompt = COLUMN_DIGEST_PROMPT_TEMPLATE
    prompt = prompt.replace("{column_label}", column_label)
    prompt = prompt.replace("{column_definition}", column_definition)
    prompt = prompt.replace("{word_count_min}", str(word_count_min))
    prompt = prompt.replace("{word_count_max}", str(word_count_max))
    prompt = prompt.replace("{history_section}", history_section)
    prompt = prompt.replace("{count}", str(len(events)))
    events_json_text = json.dumps(events_for_llm, ensure_ascii=False, indent=2)
    prompt = prompt.replace("{events_json}", events_json_text)
    hint = glossary_hint(events_json_text)
    if hint:
        prompt += hint

    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0.3, "max_tokens": 16000, "json_object": True},
        timeout=_timeout_for(ai_config, "digest", 180),
    )

    try:
        parsed = _parse_jsonish_object(response)
    except ValueError as exc:
        # 模型偶发输出分析文本而非 JSON：追加约束后重试一次
        _ai_log("栏目写作返回非 JSON，追加约束后重试一次")
        retry_prompt = (
            prompt
            + "\n\n注意：只输出 JSON 对象（以 { 开头、以 } 结尾），不要输出任何分析、说明或过程文字。"
        )
        response = await _call_llm(
            retry_prompt,
            {**ai_config, "temperature": 0.2, "max_tokens": 16000, "json_object": True},
            timeout=_timeout_for(ai_config, "digest", 180),
        )
        try:
            parsed = _parse_jsonish_object(response)
        except ValueError as retry_exc:
            raise RuntimeError(
                f"generate_column_digest JSON 解析失败: {response[:300]}"
            ) from retry_exc

    # 提取 events 数组
    if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
        normalized_events = []
        for event in parsed["events"]:
            if not isinstance(event, dict):
                continue
            reader_body = str(event.get("reader_body", "") or event.get("core_facts", "")).strip()
            normalized = {
                **event,
                "reader_body": reader_body,
                "core_facts": reader_body or event.get("core_facts", ""),
                "detail_level": "standard",
                # 兼容站内旧字段（reader_body 已包含完整内容，不再单独生成）
                "background_impact": "",
            }
            normalized_events.append(normalized)
        return normalized_events

    raise RuntimeError(
        f"generate_column_digest 响应中未找到 events 数组: {response[:300]}"
    )


async def generate_periodical_overview(
    report_type: str,
    title: str,
    highlights: list[str],
    columns: dict[str, dict],
    ai_config: dict,
) -> dict:
    """为周报/月报生成总览结构。"""
    if report_type not in _PERIODICAL_TYPE_LABELS:
        return {}

    compact_columns = _build_periodical_overview_columns(columns)

    prompt = PERIODICAL_OVERVIEW_PROMPT_TEMPLATE
    prompt = prompt.replace("{report_label}", _PERIODICAL_TYPE_LABELS[report_type])
    prompt = prompt.replace("{title}", title)
    prompt = prompt.replace("{highlights_json}", json.dumps(highlights, ensure_ascii=False))
    prompt = prompt.replace("{columns_json}", json.dumps(compact_columns, ensure_ascii=False, indent=2))

    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0.3, "max_tokens": 16000, "json_object": True},
        timeout=_timeout_for(ai_config, "digest", 180),
    )
    try:
        parsed = _parse_jsonish_object(response)
    except ValueError as exc:
        raise RuntimeError(f"generate_periodical_overview JSON 解析失败: {response[:300]}") from exc

    summary = _clean_periodical_overview_text(parsed.get("summary", ""), limit=220)

    themes = []
    for item in parsed.get("themes", []):
        text = _clean_periodical_overview_text(item, limit=24)
        if not text:
            continue
        if text not in themes:
            themes.append(text)
        if len(themes) >= 4:
            break

    watchlist = []
    for item in parsed.get("watchlist", []):
        text = _clean_periodical_overview_text(item, limit=32)
        if not text:
            continue
        if text not in watchlist:
            watchlist.append(text)
        if len(watchlist) >= 4:
            break

    raw_column_analyses = parsed.get("column_analyses", {})
    column_analyses: dict[str, str] = {}
    if isinstance(raw_column_analyses, dict):
        for col_key in columns:
            column_analyses[col_key] = _clean_periodical_overview_text(raw_column_analyses.get(col_key, ""), limit=110)

    return {
        "summary": summary,
        "themes": themes,
        "watchlist": watchlist,
        "column_analyses": column_analyses,
    }


async def generate_daily_overview(
    title: str,
    columns: dict[str, dict],
    ai_config: dict,
) -> str:
    """为日报生成整篇总览导语。"""
    compact_columns = _build_periodical_overview_columns(columns)

    prompt = DAILY_OVERVIEW_PROMPT_TEMPLATE
    prompt = prompt.replace("{title}", title)
    prompt = prompt.replace("{columns_json}", json.dumps(compact_columns, ensure_ascii=False, indent=2))

    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0.3, "max_tokens": 8000, "json_object": True},
        timeout=_timeout_for(ai_config, "digest", 180),
    )
    try:
        parsed = _parse_jsonish_object(response)
    except ValueError as exc:
        raise RuntimeError(f"generate_daily_overview JSON 解析失败: {response[:300]}") from exc

    return _clean_periodical_overview_text(parsed.get("summary", ""), limit=220)


async def translate_headline_titles(
    titles: list[str],
    ai_config: dict,
) -> list[str]:
    """将次要新闻标题批量翻译为中文；保持与输入等长（空标题位置原样返回空串）。"""
    cleaned_titles = [str(title).strip() for title in titles]
    indexed_titles = [(idx, title) for idx, title in enumerate(cleaned_titles) if title]
    if not indexed_titles:
        return ["" for _ in cleaned_titles]
    pending_titles = [title for _, title in indexed_titles]

    prompt = HEADLINE_TRANSLATION_PROMPT_TEMPLATE.replace(
        "{titles_json}",
        json.dumps(pending_titles, ensure_ascii=False, indent=2),
    )
    hint = glossary_hint(json.dumps(pending_titles, ensure_ascii=False))
    if hint:
        prompt += hint
    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0, "max_tokens": 8000, "json_object": True},
        timeout=_timeout_for(ai_config, "meta", 120),
    )

    try:
        parsed = _parse_jsonish_object(response)
    except ValueError as exc:
        raise RuntimeError(f"translate_headline_titles JSON 解析失败: {response[:300]}") from exc

    if not isinstance(parsed.get("items"), list):
        raise RuntimeError(f"translate_headline_titles JSON 解析失败: {response[:300]}")

    translated: list[str] = []
    for item in parsed["items"]:
        if not isinstance(item, dict):
            translated.append("")
            continue
        translated.append(str(item.get("title_zh", "")).strip())

    if len(translated) < len(pending_titles):
        translated.extend([""] * (len(pending_titles) - len(translated)))
    translated = translated[:len(pending_titles)]

    result = ["" for _ in cleaned_titles]
    for (idx, _), value in zip(indexed_titles, translated):
        result[idx] = value
    return result


FALLBACK_BODY_PROMPT_TEMPLATE = _load_prompt_template("fallback_body")


async def generate_fallback_bodies(entries: list[dict], ai_config: dict) -> dict[str, str]:
    """把评分候选扩写成中文简讯正文，用于栏目重点解析兜底。"""
    if not entries:
        return {}
    prompt = FALLBACK_BODY_PROMPT_TEMPLATE.replace(
        "{entries_json}",
        json.dumps(entries, ensure_ascii=False, indent=2),
    )
    try:
        response = await _call_llm(
            prompt,
            {**ai_config, "temperature": 0.2, "max_tokens": 8000, "json_object": True},
            timeout=_timeout_for(ai_config, "meta", 120),
        )
        parsed = _parse_jsonish_object(response)
    except Exception as exc:  # noqa: BLE001 - 兜底失败时降级为无正文
        _ai_log(f"兜底正文生成失败: {type(exc).__name__}: {str(exc)[:80]}")
        return {}

    items = parsed.get("items")
    if not isinstance(items, list):
        return {}

    bodies: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        body = re.sub(r"\s+", " ", str(item.get("body") or "")).strip()
        if link and body:
            bodies[link] = body
    return bodies


def has_ai_config() -> bool:
    """检查是否配置了 AI API Key（无 key 时返回 False，不抛异常）"""
    try:
        config = _load_ai_config()
        return bool(config.get("api_key"))
    except RuntimeError:
        return False


# 导出别名
merge_scores_to_items = _merge_scores
