#!/usr/bin/env python3
"""
AI 分析层 — 三段式流程

1. score_batch: 批量评分，输出 score/column/tags/summary/event_key
2. generate_column_digest: 按栏目生成结构化事件卡片
3. generate_meta_digest: 从四栏摘要生成总标题/导语/highlights
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# 加载 .env
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

# ── AI 配置 ──


def _load_ai_config() -> dict:
    """从环境变量加载 AI 配置，无 key 时 raise"""
    api_key = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "未配置 AI_API_KEY 环境变量，请在 .env 或系统环境变量中设置"
        )
    return {
        "api_key": api_key,
        "base_url": os.getenv("AI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("AI_MODEL", "gpt-4o-mini"),
    }


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
    """调用 OpenAI 兼容 API（兼容推理模型 content / reasoning_content）"""
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    url = f"{config['base_url'].rstrip('/')}/chat/completions"

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


def _parse_score_response(response: str) -> list[dict]:
    """解析评分 LLM 响应，兼容 {"items":[...]} / [...] / markdown 包裹"""
    text = _strip_markdown_fence(response)
    parsed = None

    # 直接解析
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON 对象或数组
        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    break
                except json.JSONDecodeError:
                    continue

    if parsed is None:
        raise ValueError(f"无法从评分响应中解析 JSON: {response[:200]}")

    # 提取列表
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
            "content": entry.get("content", "")[:2000],
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
        merged.append({
            **entry,
            "score": score_val,
            "column": column_val,
            "tags": s.get("tags", entry.get("tags", [])),
            "summary": s.get("summary", entry.get("summary", "")),
            "event_key": s.get("event_key", entry.get("event_key", "")),
        })
    return merged


# ── score_batch ──


SCORE_PROMPT_TEMPLATE = """你是一个专业且严苛的新闻主编。请对以下候选新闻进行过滤、评分和信息提取。

## 评分标准（0-100）

**核心约束（先判这三条，再进入分档）**：
1. 90+ 必须同时满足：(a) 主题与美国政情 / 国际风云 / 科技前沿 / 财经脉动强相关；(b) 来源为当事方官方账号或官方博客（非 KOL / 媒体转述）；(c) 属于首发
2. 非核心主题（娱乐、体育、生活方式等）无论多重大，上限 79 分
3. 重磅新闻若来源是 KOL / 媒体转述，上限 89 分

**分档**：
- 【90-100】核心领域 + 官方首发 + 里程碑级事件
- 【80-89】重要进展、知名人物核心观点、深度分析；或重磅新闻通过媒体转述
- 【70-79】实用工具、行业报告；非核心主题的重磅新闻
- 【60-69】二手信息、一般性新闻
- 【<60】低价值内容：纯情绪、广告、闲聊

## 栏目分类

每条新闻必须归入以下栏目之一：
- `us_politics`：美国国内政治、国会、总统、选举、政策
- `global_affairs`：国际关系、地缘政治、外交、军事冲突
- `technology`：科技前沿、AI、互联网、半导体、新能源
- `economy`：经济数据、金融市场、贸易、就业、通胀

## 事件归并标识

为每条新闻生成 `event_key`：用 snake_case 格式标识该新闻所属的核心事件，同一事件的不同报道必须使用相同的 event_key。
- 格式：`{事件关键词}_{日期YYYYMMDD}`，如 `iran_deal_20260618`、`fed_rate_decision_20260618`
- 如果多条新闻讨论同一事件（如同一政策的不同媒体报道），它们的 event_key 必须完全相同
- event_key 应简短（3-6 个单词），能让人一眼看出是什么事件

## 输出要求

必须返回纯 JSON 对象，顶层包含 `items` 数组，每个对象包含：
- `link`: 原文链接（必须保留原样）
- `score`: 整数评分（0-100）
- `column`: 栏目字符串（us_politics / global_affairs / technology / economy）
- `event_key`: 事件归并标识（snake_case，含日期）
- `tags`: 字符串数组（1-3 个，每个 2-12 字符，必须是具体关键词，禁止空泛标签）
- `summary`: 一句话客观摘要（50 字内）

## 输出格式（严格只输出 JSON，以 "{{" 开始，以 "}}" 结尾）

```json
{{
  "items": [
    {{
      "link": "https://example.com/article1",
      "score": 95,
      "column": "us_politics",
      "event_key": "iran_deal_20260618",
      "tags": ["具体标签1", "具体标签2"],
      "summary": "一句话摘要。"
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

## 输入数据

```json
{entries_json}
```"""


async def _score_single_batch(
    entries: list[dict], config: dict, batch_index: int = 0
) -> tuple[list[dict], list[str]]:
    """对单批 entries 评分，返回 (matched_scores, errors)"""
    entries_for_llm = [
        {
            "link": e.get("link", ""),
            "title": e.get("title", "无标题"),
            "source": e.get("source", "未知来源"),
            "published": e.get("published", ""),
            "content": (e.get("content", "") or "")[:2000],
        }
        for e in entries
    ]
    entries_json = json.dumps(entries_for_llm, ensure_ascii=False, indent=2)
    prompt = SCORE_PROMPT_TEMPLATE.replace("{entries_json}", entries_json)

    try:
        response = await _call_llm(prompt, config)
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
            print(f"  [AI] {msg}")
            errors.append(msg)

        return matched, errors

    except Exception as e:
        detail = str(e).strip() or repr(e)
        msg = f"批次{batch_index + 1} 评分失败: {type(e).__name__}: {detail}"
        print(f"  [AI] {msg}")
        return [], [msg]


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

    batches = _split_entries_for_batch(entries, max_prompt_chars)
    print(f"  [AI] 评分: {len(entries)} 条 -> {len(batches)} 批")

    if len(batches) == 1:
        scores, errors = await _score_single_batch(batches[0], config, 0)
        return _merge_scores(entries, scores), errors

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _limited(idx: int, batch: list[dict]):
        async with semaphore:
            return await _score_single_batch(batch, config, idx)

    results = await asyncio.gather(*[_limited(i, b) for i, b in enumerate(batches)])

    all_scores, all_errors = [], []
    for scores, errors in results:
        all_scores.extend(scores)
        all_errors.extend(errors)

    return _merge_scores(entries, all_scores), all_errors


# ── merge_events ──


def merge_events(items: list[dict]) -> list[dict]:
    """按 event_key 合并同一事件多源报道。

    - 同一 event_key 的多条合并为一条
    - 保留最高分的作为主条目
    - 合并所有来源链接到 source_links
    - 合并所有 summary 到 content

    Args:
        items: score_batch 返回的 dict 列表（含 event_key, link, source, score, summary 等）

    Returns:
        合并后的 dict 列表，每条含 source_links: [{title, url}, ...]
    """
    if not items:
        return []

    # 按 event_key 分组
    groups: dict[str, list[dict]] = {}
    no_key: list[dict] = []

    for item in items:
        key = (item.get("event_key") or "").strip()
        if key:
            groups.setdefault(key, []).append(item)
        else:
            no_key.append(item)

    merged: list[dict] = []

    for event_key, group in groups.items():
        # 按 score 降序，最高分作为主条目
        group.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
        primary = group[0]

        # 收集所有来源链接（去重）
        seen_links: set[str] = set()
        source_links: list[dict] = []
        summaries: list[str] = []

        for item in group:
            link = item.get("link", "")
            if link and link not in seen_links:
                seen_links.add(link)
                source_links.append({
                    "title": item.get("title", ""),
                    "url": link,
                })
            summary = (item.get("summary") or "").strip()
            if summary and summary not in summaries:
                summaries.append(summary)

        # 合并 tags（去重保序）
        all_tags: list[str] = []
        seen_tags: set[str] = set()
        for item in group:
            for tag in item.get("tags", []):
                if tag and tag not in seen_tags:
                    seen_tags.add(tag)
                    all_tags.append(tag)

        merged.append({
            **primary,
            "event_key": event_key,
            "source_links": source_links,
            "content": "\n".join(summaries),
            "tags": all_tags[:5],
        })

    # 无 event_key 的条目保持原样
    merged.extend(no_key)
    return merged


# ── generate_column_digest ──


COLUMN_DIGEST_PROMPT_TEMPLATE = """你是一位顶级的新闻日报主编。你的任务是为「{column_label}」栏目生成结构化事件卡片。

## 栏目定义

{column_definition}

## 结构要求

每条事件必须包含以下字段：
- **title_zh**：中文标题，简洁准确
- **detail_level**：`full` 或 `brief`
- **core_facts**：核心事实。`full` 事件写 2-4 句，`brief` 事件只写 1 段
- **background_context**：仅 `full` 事件必填。交代背景脉络
- **possible_impact**：仅 `full` 事件必填。说明可能影响
- **why_it_matters**：仅 `full` 事件必填。说明为什么值得关注
- **source_links**：相关阅读，列出所有相关来源，格式 [{{"title": "来源名", "url": "https://..."}}]
- **is_followup**：布尔值，是否为历史事件的持续跟踪

## 字数控制

- `brief` 事件：{brief_min}-{brief_max} 字（core_facts 部分）
- `full` 事件（score >= {important_score}）：{important_min}-{important_max} 字（core_facts 部分）
- 本栏总字数目标：{word_count_min}-{word_count_max} 字

## 新旧剥离与去重规则

{history_section}

## 负面清单（必须剔除）

- KOL 个人动态、公关软文
- 纯情绪发泄、未经验证的小道消息
- 无实质内容的闲聊
- 今天的信息如果只是重复已报道的事实，请直接丢弃

## 输出格式

必须返回严格 JSON 对象，以 "{{" 开始，以 "}}" 结尾：

```json
{{
  "events": [
    {{
      "title_zh": "中文标题",
      "detail_level": "full",
      "core_facts": "核心事实 2-4 句",
      "background_context": "背景脉络 1-2 句",
      "possible_impact": "可能影响 1-2 句",
      "why_it_matters": "为什么值得关注 1 句",
      "source_links": [{{"title": "来源名", "url": "https://..."}}],
      "is_followup": false
    }},
    {{
      "title_zh": "中文标题",
      "detail_level": "brief",
      "core_facts": "核心事实 1 段",
      "source_links": [{{"title": "来源名", "url": "https://..."}}],
      "is_followup": false
    }}
  ]
}}
```

## 重要提示

1. 只返回 JSON 对象，不要添加额外文字
2. events 数组中的每条事件都必须来自下方输入数据
3. source_links 必须保留原文链接，不要编造
4. 高分事件必须使用 `full`，低于阈值的事件必须使用 `brief`
5. `brief` 事件禁止输出 background_context / possible_impact / why_it_matters
6. 用简练中文，像分析师一样指出事件的行业意义

## 输入数据（共 {count} 条候选事件）

```json
{events_json}
```
"""


# 栏目定义映射
_COLUMN_DEFINITIONS: dict[str, str] = {
    "us_politics": "美国国内政治、国会、总统、选举、政策",
    "global_affairs": "国际关系、地缘政治、外交、军事冲突",
    "technology": "科技前沿、AI、互联网、半导体、新能源",
    "economy": "经济数据、金融市场、贸易、就业、通胀",
}


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
        events_for_llm.append({
            "title": e.get("title", ""),
            "source": e.get("source", ""),
            "score": e.get("score", 0),
            "summary": e.get("summary", ""),
            "content": (e.get("content", "") or "")[:3000],
            "source_links": e.get("source_links", []),
        })

    column_definition = _COLUMN_DEFINITIONS.get(column_key, column_label)

    prompt = COLUMN_DIGEST_PROMPT_TEMPLATE
    prompt = prompt.replace("{column_label}", column_label)
    prompt = prompt.replace("{column_definition}", column_definition)
    prompt = prompt.replace("{normal_min}", "150")
    prompt = prompt.replace("{normal_max}", "250")
    prompt = prompt.replace("{brief_min}", "80")
    prompt = prompt.replace("{brief_max}", "180")
    prompt = prompt.replace("{important_score}", "85")
    prompt = prompt.replace("{important_min}", "300")
    prompt = prompt.replace("{important_max}", "500")
    prompt = prompt.replace("{word_count_min}", str(word_count_min))
    prompt = prompt.replace("{word_count_max}", str(word_count_max))
    prompt = prompt.replace("{history_section}", history_section)
    prompt = prompt.replace("{count}", str(len(events)))
    prompt = prompt.replace(
        "{events_json}",
        json.dumps(events_for_llm, ensure_ascii=False, indent=2),
    )

    response = await _call_llm(prompt, ai_config, timeout=180)

    # 解析 JSON 响应（兼容 markdown 代码块包裹）
    text = _strip_markdown_fence(response)
    parsed = None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        raise RuntimeError(f"generate_column_digest JSON 解析失败: {response[:300]}")

    # 提取 events 数组
    if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
        normalized_events = []
        for event in parsed["events"]:
            if not isinstance(event, dict):
                continue
            detail_level = str(event.get("detail_level", "brief")).strip().lower()
            if detail_level not in {"full", "brief"}:
                detail_level = "brief"

            normalized = {
                **event,
                "detail_level": detail_level,
                # 兼容站内旧字段
                "background_impact": event.get("background_context", event.get("background_impact", "")),
            }
            normalized_events.append(normalized)
        return normalized_events

    raise RuntimeError(
        f"generate_column_digest 响应中未找到 events 数组: {response[:300]}"
    )


# ── generate_meta_digest ──


META_DIGEST_PROMPT_TEMPLATE = """你是一位顶级的新闻日报总编辑。你的任务是从四个栏目的摘要中，生成日报的重点提示。

## 输入

以下是四个栏目的标题和前 3 条事件摘要：

{column_summaries_text}

## 输出要求

- **highlights**：4-8 条，每条 20-45 字，必须是完整事实句或高密度概括

## 严格约束

1. 严禁评价性措辞、套话、宏大叙事
2. 只做总编排，不重复正文细节
3. highlights 每条必须指向一个具体事件
4. 禁止空泛总结，禁止重复标题原文，禁止“今日值得关注的是”这类套话

## 输出格式

必须返回严格 JSON 对象，以 "{{" 开始，以 "}}" 结尾：

```json
{{
  "highlights": ["重点1（20-45字）", "重点2（20-45字）", "重点3（20-45字）", "重点4（20-45字）"]
}}
```

## 重要提示

1. 只返回 JSON 对象，不要添加额外文字
2. highlights 数组长度 4-8 条
3. 所有内容用简练中文
"""


async def generate_meta_digest(
    column_summaries: dict[str, str],
    ai_config: dict,
) -> dict:
    """
    从四栏摘要生成 Reader 顶部要点

    Args:
        column_summaries: {"us_politics": "前3条标题摘要", "global_affairs": ...}
        ai_config: AI 配置

    Returns:
        {"highlights": ["重点1", "重点2", ...]}
    """
    # 构建栏目摘要文本
    col_label_map = {
        "us_politics": "美国政情",
        "global_affairs": "国际风云",
        "technology": "科技前沿",
        "economy": "经济财经",
    }
    sections = []
    for col_key, label in col_label_map.items():
        summary = column_summaries.get(col_key, "").strip()
        if summary:
            sections.append(f"### {label}（{col_key}）\n{summary}")

    column_summaries_text = "\n\n".join(sections) if sections else "(无栏目摘要)"

    prompt = META_DIGEST_PROMPT_TEMPLATE.replace(
        "{column_summaries_text}", column_summaries_text
    )

    response = await _call_llm(prompt, ai_config, timeout=120)

    # 解析 JSON 响应（兼容 markdown 代码块包裹）
    text = _strip_markdown_fence(response)
    parsed = None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        raise RuntimeError(f"generate_meta_digest JSON 解析失败: {response[:300]}")

    # 验证必要字段
    if not isinstance(parsed, dict):
        raise RuntimeError(f"generate_meta_digest 响应非对象: {response[:300]}")

    required_fields = ("highlights",)
    missing = [f for f in required_fields if f not in parsed]
    if missing:
        raise RuntimeError(
            f"generate_meta_digest 响应缺少字段 {missing}: {response[:300]}"
        )

    return parsed


def has_ai_config() -> bool:
    """检查是否配置了 AI API Key"""
    config = _load_ai_config()
    return bool(config.get("api_key"))


# 导出别名
merge_scores_to_items = _merge_scores
