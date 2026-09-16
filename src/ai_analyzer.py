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
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# 加载 .env
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

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
        fallback = config.get("fallback")
        if not fallback or config.get("_is_fallback"):
            raise
        _ai_log(
            f"主通道失败({type(exc).__name__}: {str(exc)[:80]})，"
            f"切换备用通道 {fallback.get('model')}"
        )
        return await _call_llm_once(prompt, {**fallback, "_is_fallback": True}, timeout)


def _build_llm_payload(prompt: str, config: dict) -> dict:
    """构造 OpenAI 兼容请求体，支持 max_tokens 上限。"""
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(config.get("temperature", 0.3)),
    }
    max_tokens = config.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
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
            msg = data["choices"][0]["message"]
            return msg.get("content") or msg.get("reasoning_content", "")


def _timeout_for(config: dict, scope: str, default: int) -> int:
    """按调用场景读取超时配置。"""
    return int(config.get(f"{scope}_timeout_seconds") or config.get("timeout_seconds") or default)


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


SCORE_PROMPT_TEMPLATE = """你是一个专业且严苛的新闻主编。请对以下候选新闻进行过滤、评分和信息提取。

## 评分标准（0-100）

**分档（按事实强度和新闻价值，不按来源是否官方）**：
- 【90-100】里程碑级：制度性变化、战争/停火、最高法院里程碑裁决、重大政策转折（需 impact≥0.8 且 timeliness≥0.8）
- 【80-89】重要政策、司法、外交、战争、财报、宏观或产业进展
- 【70-79】一般硬新闻，事实成立但增量有限
- 【60-69】二手信息、一般性新闻
- 【<60】低价值内容：纯情绪、广告、闲聊、评论、荐股单

**信息量上限（先判）**：如果输入 content 少于 120 字符、或缺少可用于判断日期的信息，`score` 上限 69，`is_hard_news` 必须为 false。

## 新闻价值五维（每条必须输出，0-1）

- `impact`（影响力）：1 = 制度性变化/战争/全国性后果；0.5 = 行业或群体级；0.2 = 个案
- `prominence`（主体显著性）：1 = 总统/最高法院/央行级主体；0.5 = 部长/大公司/国际组织；0.2 = 地方/个人
- `timeliness`（时效）：1 = 当日首发；0.5 = 当日跟进或前一日进展；0.2 = 旧事重提
- `novelty`（新奇度）：1 = 首次披露；0.5 = 已知事实的新进展；0.1 = 重复报道
- `conflict`（冲突性）：1 = 明确对抗/诉讼/战争；0.3 = 政策分歧；0 = 无冲突
- `routine`（例行程度）：0.8-1.0 = 程序性公告（评论期起止、听证排期、费用表、拟议预算、FAQ、撤回旧文件）；0 = 突发事件

重要：官方来源不等于高新闻价值。机构例行公告要如实标注高 `routine`、低 `impact` 和低 `novelty`，不要因为来源权威就抬分。

## 示例（判断基准，不得照抄到输出）

正例：某国最高法院裁定一项全国性行政令违宪并立即生效 → impact 0.9、prominence 0.9、timeliness 0.9、novelty 0.8 → 90 分以上
正例：某国央行意外加息 50 个基点 → impact 0.7、prominence 0.8、timeliness 0.9、novelty 0.7 → 80-89 分
负例：某监管机构宣布延长公众评论期 30 天 → impact 0.1、prominence 0.4、timeliness 0.5、novelty 0.1、routine 0.9 → 60 分以下，is_hard_news=false
负例：某公司博客发布产品功能更新 → impact 0.2、prominence 0.5、timeliness 0.6、novelty 0.3 → 65 分以下，is_hard_news=false

## 硬新闻准入

只保留以下硬新闻类型：
- 法院裁决、起诉、监管动作、行政命令、法案推进
- 白宫、国会、州政府、联邦机构的人事、调查、政策
- 选举、提名、初选、党内权力变化
- 外交协议、联盟关系、军事行动、国际组织博弈
- AI、芯片、半导体、平台、科研突破、科技监管
- 利率、通胀、就业、关税、贸易、财报、产业链、商品价格

以下类型默认不是硬新闻：
- 评论稿、观点稿、社论
- 媒体表现稿，例如“某人讲话语无伦次”“直播被切断”
- 纯转述分析稿、没有新事实的总结稿
- 荐股、观察名单、投资建议、榜单

如果条目不属于硬新闻，`is_hard_news` 必须为 false，`content_kind` 归为 `analysis` / `opinion` / `media_reaction` / `watchlist` 中最合适的一类。

## 栏目分类

每条新闻必须归入以下栏目之一，按“事件主轴”分类，而不是按“主角是谁”分类：
- `us_politics`：美国国内政治、国会、白宫、法院、州政治、选举、调查、人事、联邦政策
- `global_affairs`：外交、战争、军事、联盟关系、国际谈判、国际组织、对华/对俄/对伊博弈
- `technology`：AI、芯片、半导体、平台、科研突破、科技监管、技术产业竞争
- `economy`：利率、通胀、就业、贸易、关税、财报、商品价格、产业链、资本市场真实变化

反例：
- 美伊协议、G7 外交协调，不归 `us_politics`
- G7 外交事件若主线是联盟外交或对华协调，优先归 `global_affairs`
- 荐股/观察名单不归 `economy`

## 事件归并标识

为每条新闻生成 `event_key`：用 snake_case 格式标识该新闻所属的核心事件，同一事件的不同报道必须使用相同的 event_key。
- 格式：`{事件关键词}_{日期YYYYMMDD}`，如 `iran_deal_20260618`、`fed_rate_decision_20260618`
- 如果多条新闻讨论同一事件（如同一政策的不同媒体报道），它们的 event_key 必须完全相同
- event_key 应简短（3-6 个单词），能让人一眼看出是什么事件
- 对 G7、关税、对华限制、AI 治理、同一财报、同一外交协议这类高重复主题，必须尽量合并成同一 event_key

## 今日性判断

每条新闻必须输出 `event_date` 和 `freshness_status`：
- 输入里的 `freshness_date` 是系统按发布时间优先、抓取时间兜底得到的北京时间自然日
- `event_date` 应填写正文证据中能确认的新进展发生日，格式 YYYY-MM-DD；无法确认时使用 freshness_date；两者都没有则为空字符串
- `freshness_status` 只能是 `today`、`recent_followup`、`old_background`、`unknown_date`
- 如果这是 freshness_date 当日/前一日的新发布或新进展，填 `today`
- 如果是旧事件但输入明确给出当日/前一日的新进展，填 `recent_followup`
- 如果只是旧背景、历史盘点、无新增事实，填 `old_background`
- 如果无法判断日期，填 `unknown_date`

## 输出要求

必须返回纯 JSON 对象，顶层包含 `items` 数组，每个对象包含：
- `link`: 原文链接（必须保留原样）
- `score`: 整数评分（0-100）
- `column`: 栏目字符串（us_politics / global_affairs / technology / economy）
- `event_key`: 事件归并标识（snake_case，含日期）
- `event_date`: 事件日期（YYYY-MM-DD，无法确认时为空字符串）
- `freshness_status`: 今日性状态（today / recent_followup / old_background / unknown_date）
- `content_kind`: 内容类型（policy / judiciary / election / diplomacy / security / regulation / market / macro / corporate / analysis / opinion / media_reaction / watchlist）
- `is_hard_news`: 布尔值，是否属于硬新闻
- `tags`: 字符串数组（1-3 个，每个 2-12 字符，必须是具体关键词，禁止空泛标签）
- `summary`: 一句话客观摘要（50 字内，必须含"谁 + 做了什么"，禁止以"据报道"开头）
- `information_gain`: 信息增量（0-1）
- `event_stage`: 事件阶段（首发 / 跟进 / 总结 / 回应）
- `verifiability`: 可验证性（0-1）
- `impact` / `prominence` / `timeliness` / `novelty` / `conflict`: 新闻价值五维（0-1）
- `routine`: 例行程度（0-1）

## 输出格式（严格只输出 JSON，以 "{{" 开始，以 "}}" 结尾）

```json
{{
  "items": [
    {{
      "link": "https://example.com/article1",
      "score": 95,
      "column": "us_politics",
      "event_key": "iran_deal_20260618",
      "event_date": "2026-06-18",
      "freshness_status": "today",
      "content_kind": "judiciary",
      "is_hard_news": true,
      "tags": ["具体标签1", "具体标签2"],
      "summary": "一句话摘要（谁做了什么）。",
      "information_gain": 0.7,
      "event_stage": "首发",
      "verifiability": 0.8,
      "impact": 0.8,
      "prominence": 0.9,
      "timeliness": 0.9,
      "novelty": 0.7,
      "conflict": 0.5,
      "routine": 0.1
    }}
  ]
}}
```

## 重要提示

1. items 数组长度必须与输入相同
2. link 字段必须与输入一一对应
3. 只返回 JSON 对象，不要添加额外文字
4. 标签用英文逗号分隔，字符串内英文双引号用 \\" 转义
5. 同一事件的不同报道必须使用相同的 event_key
6. 非硬新闻必须将 `is_hard_news` 设为 false

## 输入数据

```json
{entries_json}
```"""


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
            {**config, "temperature": 0, "max_tokens": 16000},
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
            source_links.append({
                "title": item.get("title", ""),
                "url": link,
            })
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
            (c for c in clusters if c["key"] == key or _titles_similar(norm, c["norm"])),
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


COLUMN_DIGEST_PROMPT_TEMPLATE = """你是一位顶级的新闻日报主编。你的任务是为「{column_label}」栏目生成结构化事件卡片。

## 事实边界（最高优先级）

- 只能使用输入数据中的 title、summary、evidence、source_links 所提供的信息。
- 不得补写输入中没有出现的人名、机构、票数、金额、比例、日期、地点、法律条款或市场价格。
- 不得把示例、历史常识、模型记忆或推断当作当天事实写入正文。
- 如果输入信息不足，只写可证实的“发生了什么”，不要扩展成政策结论或市场结论。
- 如果一个事件缺少足够事实支撑，必须从 events 中丢弃，不要为了凑数生成。
- 如果输入主要来自媒体转述、聚合页面、法案列表页或标题摘录，必须显式收缩表述力度，只写“文件显示 / 页面列出 / 报道称 / 公开材料显示”等可归因句式。
- 任何未经官方确认、仅由媒体报道的说法，不得写成既定事实；必须保留“据某媒体报道”或“报道显示”这类归因。
- 对法案、决议、行政文件类条目，如果输入没有提供法案内容、推进动作或影响对象，只能丢弃，不能靠编号或名称扩写。

## 栏目定义

{column_definition}

## 结构要求

每条事件必须包含以下字段：
- **title_zh**：中文标题，简洁准确
- **reader_body**：Reader 专用正文，倒金字塔三句版（导语 → 细节 → 可选影响），2-4 句，目标 60-120 字
- **core_facts**：站内兼容字段，使用与 reader_body 一致的内容
- **source_links**：相关阅读，格式 [{{“title”: “来源名”, “url”: “https://...”}}]
- **is_followup**：布尔值，是否为历史事件的持续跟踪

## reader_body 写作规范（核心）

按新闻倒金字塔写作，默认三句，每句一个事实：

**第 1 句（导语）：What + Who + When**
- 最重要的事实放最前，日期置句首："9 月 16 日，……"
- 只写输入材料能确认的 action 和主体；日期必须来自 event_date 或 freshness_date
- 禁止用"据报道""据悉""有消息称"开头；媒体转述用"某媒体称"放在句中

**第 2 句（细节）：关键数字或第二个事实**
- 补充最重要数字（金额/票数/人数/比例）或第二个可核实事实，可含 Where/Why
- 输入没有数字就不写，不得估算

**第 3 句（可选）：只写材料支持的影响或后续**
- 必须能在输入材料中找到依据；否则直接省略，只写两句
- 不得用"意味着""将受……影响""值得关注"等推断句式

## 硬规则

1. **一段只写一个事实**：每句只承载一个信息点，禁止一句里堆多个动作/数字/主体
2. **不发议论**：只陈述事实，不评论、不推断、不总结
3. **5W1H 完备性**：Who/What/When 必须齐全；Where/Why/How 有则写，没有不补

## 数字与称谓规范

- 数字：金额/人数用阿拉伯数字；万、亿不混写（"3.8 亿"不写"3亿8千万"）；百分比保留一位小数
- 称谓：首次"职务+全名"（美国总统特朗普），后文用姓（特朗普）；机构首次用全称（美国联邦贸易委员会），后文可用简称（FTC）
- 时间：统一"9 月 16 日"格式，跨年补年份；不写"昨天/今天"

## 常见错误（禁止模仿，示例为虚构）

❌ 「9 月 16 日，多家媒体报道，某国议会通过决议，7 名议员赞成，相关条款同时提出，此前争议集中在授权问题。」
→ 一句堆 4 个事实。正确写法：拆成 2-3 句，每句一个事实。

❌ 「接下来，某国总统的权限和部长的职位将受这一程序影响。」
→ 无材料支撑的推断。材料没有写影响就不要写第三句。

❌ 「此次裁决意味着该国行政与立法关系进入新阶段。」
→ 议论。"意味着"属于禁止词。

❌ 「某机构发布新规，将减少 20% 的财政拨款。」
→ 材料只写了"拟议规则变更"，"将减少 20%"是把草案写成已生效。禁止事实升级。

✅ 正确示例（虚构）：
「9 月 16 日，某国最高法院裁定一项全国性行政令违宪并立即生效。该裁决以 6 比 3 通过，涉及 12 个州的执行安排。裁决书要求行政部门在 30 日内提交整改方案。」

## 禁止清单

以下内容一律禁止出现在 reader_body 中：

**禁止的开头**：据报道、据悉、有消息称
**禁止的连接词**：值得注意的是、需要指出的是
**禁止的空泛动词**：凸显了、反映了、意味着、标志着
**禁止的收尾**：引发了讨论、增添了变数、存在不确定性、产生深远影响、仍需观察
**禁止的套话**：对于读者来说、值得关注的是
**禁止的标签**：核心事实：、背景脉络：、背景与影响：、可能影响：、为什么值得关注：
**禁止的事实升级**：把“报道显示 / 页面列出 / 文件写明 / 草案提出”直接改写成“已经实施 / 已经生效 / 已被证实”

## 输出前自检（逐条打勾，任何一条不通过就重写）

1. 每句是否只含一个事实？
2. 导语是否包含 Who + What + When？
3. 是否出现输入材料中没有的数字、人名或机构？
4. 是否出现评论/推断词（意味着/凸显/标志着/将受……影响）？
5. 第三句（如有）是否能在输入材料中找到依据？
6. 正文是否 2-4 句、目标 60-120 字？
7. 是否使用了输入中不存在的实体或数字？

## 本栏总字数目标

{word_count_min}-{word_count_max} 字

## 新旧剥离与去重规则

{history_section}

## 负面清单（必须剔除）

- KOL 个人动态、公关软文
- 纯情绪发泄、未经验证的小道消息
- 无实质内容的闲聊
- 评论稿、观点稿、媒体表现稿、荐股观察单
- 今天的信息如果只是重复已报道的事实，请直接丢弃

## 输出格式

必须返回严格 JSON 对象，以 “{{“ 开始，以 “}}” 结尾：

```json
{{
  “events”: [
    {{
      “title_zh”: “中文标题”,
      “reader_body”: “9 月 16 日，导语事实一句。关键数字或第二事实一句。可选影响一句（无依据则省略）。”,
      “core_facts”: “与 reader_body 一致。”,
      “source_links”: [{{“title”: “来源名”, “url”: “https://...”}}],
      “is_followup”: false
    }}
  ]
}}
```

## 重要提示

1. 只返回 JSON 对象，不要添加额外文字
2. events 数组中的每条事件都必须来自下方输入数据
3. source_links 必须保留原文链接，不要编造
4. 只输出硬新闻，不要输出评论稿和观察名单
5. 同一主线事件不要拆成多个近义条目
6. reader_body 按倒金字塔三句版写作：导语（Who+What+When）→ 细节 → 可选影响；每句一个事实
7. 禁止输出”核心事实：””背景与影响：””为什么值得关注：”等标签
8. 每句一个事实，不堆砌；同一主语不连续出现超过 2 次
9. 不得复用抽象示例中的实体、数字或表述；示例不是新闻素材
10. 对经济走势栏目尤其严格：只有输入明确给出市场价格、政策动作或财报事实时，才可写市场影响
11. 对存在争议、未确认或媒体独家报道的内容，必须保留归因，不得用陈述句包装为已确认事实
12. reader_body 必须包含明确日期表达，例如”6 月 26 日”或”6 月 27 日”；没有可用日期的事件不要输出
13. 同一国家/地区/组织的连续冲突或对抗事件，最多保留 2 条重点解析；超出的必须合并到已有条目或降级丢弃
14. 如果多个候选讨论同一核心事件的不同角度（如 A 打 B、B 威胁反击、C 调停），必须合并为 1 条综合叙述，不要拆成 3 条

## 输入数据（共 {count} 条候选事件）

```json
{events_json}
```
"""


HEADLINE_TRANSLATION_PROMPT_TEMPLATE = """你是中文新闻编辑。请把输入的英文新闻标题翻译成简洁、准确、自然的中文标题。

要求：
1. 只翻译，不补充不存在的信息
2. 不保留英文原题
3. 每条输出一个中文标题
4. 保持硬新闻风格，不写评论口吻
5. 必须返回严格 JSON 对象

输出格式：
{{
  "items": [
    {{"title_zh": "中文标题"}}
  ]
}}

输入标题：
{titles_json}
"""


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


PERIODICAL_OVERVIEW_PROMPT_TEMPLATE = """你是一位资深中文新闻主编。请基于已经写好的栏目摘要，为一份{report_label}生成“先总览后分析”的结构化总览。

## 任务目标

你需要输出：
1. **summary**：整份{report_label}开头使用的总述，120-180 字，先概括这段周期最重要的变化，再指出主线如何展开。
2. **themes**：2-4 条核心主题，每条 8-20 字。
3. **watchlist**：2-4 条接下来最值得观察的点，每条 8-24 字。
4. **column_analyses**：四个栏目各 1 段前置分析，40-90 字，说明这一栏在本周期里的主线，而不是重复事件正文。

## 事实边界（最高优先级）

- 只能使用输入中的 highlights、栏目标题、top_titles、count、analysis、reader_body。
- 不得补写输入里没有出现的人名、机构、数字、票数、时间、地点、法律条款或市场变化。
- 不得写泛泛空话或编辑腔套话，例如“形势复杂”“影响深远”“仍需观察”“值得关注”“可以看出”“总体来看”。
- summary/themes/watchlist 必须优先引用输入里明确出现的主题、事件或栏目主线，不允许只抽象概括。
- 如果某栏没有足够材料，可以把对应 column_analyses 设为空字符串，但不得编造。

## 写作要求

- summary 要有“本周期最重要变化 → 结构性主线 → 接下来关注点”的顺序，必须尽量点到具体主题。
- themes 应提炼跨事件主线，不要直接复述栏目名，优先使用输入里出现过的名词短语。
- watchlist 要具体指出后续观察对象或冲突延续点，最好可对应到某一栏或某个事件。
- column_analyses 要解释“这一栏为什么值得看”，不能和 reader_body 逐句重复。
- 每段都要避免空话：能写具体对象就不要写抽象判断，能写方向就不要写笼统评价。
- 保持新闻编辑口吻，克制、具体、无修辞堆砌。

## 输出格式

必须返回严格 JSON 对象：

```json
{{
  "summary": "...",
  "themes": ["..."],
  "watchlist": ["..."],
  "column_analyses": {{
    "us_politics": "...",
    "global_affairs": "...",
    "technology": "...",
    "economy": "..."
  }}
}}
```

## 输入数据

标题：{title}
已有要点：{highlights_json}
栏目摘要：
```json
{columns_json}
```
"""


DAILY_OVERVIEW_PROMPT_TEMPLATE = """你是一位资深中文新闻主编。请基于已经写好的日报栏目内容，为整篇日报生成一段开头总览。

## 任务目标

- 只输出一段 **summary**，120-180 字。
- 这段 summary 用在整份日报最顶部，必须先交代当天最重要的变化，再串联主要主线，最后点出接下来值得继续观察的方向。

## 事实边界（最高优先级）

- 只能使用输入中的栏目标题、top_titles、count、analysis、reader_body。
- 不得补写输入里没有出现的人名、机构、数字、票数、时间、地点、法律条款或市场变化。
- 不得写泛泛空话或编辑腔套话，例如“形势复杂”“影响深远”“仍需观察”“值得关注”“可以看出”“总体来看”。
- 不得把法案编号、缩写、技术代号或纯标题翻译当作叙述主角；要写“谁做了什么、局势怎么变了”。

## 写作要求

- summary 必须先写当天最重要的一件事（含主体+动作），再串联主线，最后点出后续观察方向（最重要在前）
- 必须写成**整篇总览**，不要按栏目顺序依次点名罗列
- 不得与栏目正文重复：同一事实只能用更高抽象层级概括，禁止照抄 reader_body 句子
- 必须优先提炼跨栏目主线，说明这些事件如何共同构成当天的政治/外交/科技/经济画面
- 能写具体动作就不要写抽象判断，能写具体对象就不要写空泛概括
- 保持中文硬新闻口吻，克制、具体、连贯

## 输出格式

必须返回严格 JSON 对象：

```json
{{
  "summary": "..."
}}
```

## 输入数据

标题：{title}
栏目摘要：
```json
{columns_json}
```
"""


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
    prompt = prompt.replace(
        "{events_json}",
        json.dumps(events_for_llm, ensure_ascii=False, indent=2),
    )

    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0.3, "max_tokens": 16000},
        timeout=_timeout_for(ai_config, "digest", 180),
    )

    try:
        parsed = _parse_jsonish_object(response)
    except ValueError as exc:
        raise RuntimeError(f"generate_column_digest JSON 解析失败: {response[:300]}") from exc

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
        {**ai_config, "temperature": 0.3, "max_tokens": 16000},
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
        {**ai_config, "temperature": 0.3, "max_tokens": 8000},
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
    """将次要新闻标题批量翻译为中文。"""
    cleaned_titles = [str(title).strip() for title in titles if str(title).strip()]
    if not cleaned_titles:
        return []

    prompt = HEADLINE_TRANSLATION_PROMPT_TEMPLATE.replace(
        "{titles_json}",
        json.dumps(cleaned_titles, ensure_ascii=False, indent=2),
    )
    response = await _call_llm(
        prompt,
        {**ai_config, "temperature": 0, "max_tokens": 4000},
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

    if len(translated) < len(cleaned_titles):
        translated.extend([""] * (len(cleaned_titles) - len(translated)))
    return translated[:len(cleaned_titles)]


FALLBACK_BODY_PROMPT_TEMPLATE = """你是中文新闻编辑。请把下面的候选新闻改写成日报的简讯正文。

## 要求
- 中文，2-3 句，总长 50-120 字
- 倒金字塔：第 1 句导语写最重要事实（Who+What+When，日期置句首 "M 月 D 日"）；第 2 句补关键数字或第二事实；第 3 句仅在材料支持时写影响，否则省略
- 每句只写一个事实；只陈述事实，不评论、不推断
- 优先使用候选 summary（中文摘要）与 content（原文片段，可能为英文）里的具体事实，可用中文转述
- 信息有限时写短即可，禁止重复或凑字数
- 禁止输出英文原文、URL；禁止复述"来源为/链接为"等字段
- 禁止使用"据报道、据悉、值得关注的是、现有材料未提供更多可核验细节"等套话
- 数字与称谓：金额/人数用阿拉伯数字，万/亿不混写；首次"职务+全名"，后文用姓
- 不要输出标题，只输出正文

## 候选列表
{entries_json}

## 输出（严格 JSON，link 原样返回）
{{"items": [{{"link": "原链接", "body": "中文简讯正文"}}]}}
"""


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
            {**ai_config, "temperature": 0.2, "max_tokens": 4000},
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
