#!/usr/bin/env python3
"""
AI 分析层 — 两段式流程

1. score_batch: 批量评分，输出 score/column/tags/summary/event_key
2. generate_digest: 生成完整日报正文（YAML frontmatter + Markdown）
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
        msg = f"批次{batch_index + 1} 评分失败: {e}"
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


# ── generate_digest ──

DIGEST_PROMPT_TEMPLATE = """你是一位顶级的新闻日报主编。你的任务是将今天杂乱无章、来源各异的信息碎片，熔炼、重组成一篇结构极度清晰、主次分明、洞察深刻的日报。

## 核心排版与整合规则（必须严格遵守）：

1. **新旧剥离与进展追踪**：
   - 对比今天的新信息与近几天已推送上下文。如果今天的信息只是重复已报道的事实，请直接丢弃。
   - 如果今天的信息是历史事件的延续，请将标题标记为 `[持续跟踪]`，并在正文中清晰划分"前情提要"与"最新突破"。

2. **精准的事件级融合**：
   - 正确做法：把讨论同一具体事件的多方信息合并为一条新闻，提炼全貌。
   - 错误做法：不要把两个毫不相关的事件强行塞进同一条！独立事件请独立输出。

3. **精选原则**：
   - 只挑选真正有价值的事件。每条必须对行业、技术演进或公众利益有宏观价值。
   - 绝对剔除：KOL 个人动态、公关软文、纯情绪发泄、未经验证的小道消息、无实质内容的闲聊。

4. **每条事件的结构（必须严格遵守）**：
   - **核心事实**（2-4 句）：客观陈述发生了什么，关键数据和人物
   - **背景/影响**（1-2 句）：这件事的来龙去脉，或对行业/社会的影响
   - **为什么值得关注**（1 句）：用一句话点明这件事对读者的意义
   - **原文链接**：列出所有相关来源

5. **字数控制**：
   - 今日导读：{lead_min}-{lead_max} 字
   - 普通事件：{event_min}-{event_max} 字
   - 重点事件（score >= {important_score}）：{important_min}-{important_max} 字
   - 全文总字数目标：{word_count_min}-{word_count_max} 字

6. **客观专业**：用简练中文，像分析师一样指出事件的行业意义。保留所有原文链接。

7. **避免风格趋同**：前言导读必须从今日素材本身的具体事实出发，严禁评价性措辞 / 总结性套话 / 宏大叙事框架。

8. **正文不要写开头引言**：正文直接从第一条新闻 `### 1.` 开始。导语由 frontmatter 的 `lead` 字段承载。

## 四大板块

正文按以下板块分组，每个板块用 `## 板块名` 二级标题：

- `## 美国政情`：美国国内政治、国会、总统、选举、政策
- `## 国际风云`：国际关系、地缘政治、外交、军事冲突
- `## 科技前沿`：科技前沿、AI、互联网、半导体、新能源
- `## 经济财经`：经济数据、金融市场、贸易、就业、通胀

如果某个板块今日无素材，直接省略该板块标题。

## 栏目配额

{column_quota_text}

## 输出格式

输出必须以 YAML frontmatter 起始，紧跟空行后接 markdown 正文。

frontmatter 字段：
- `title`: 短标题，核心事实陈述，8-30 字
- `lead`: {lead_min}-{lead_max} 字前言导读，只陈述今日具体发生的事实
- `highlights`: 2-3 条最值得关注的事件，每条 15-30 字

示例：

```yaml
---
title: "国会通过芯片补贴法案，Fed 维持利率不变"
lead: "美国参议院以 64 票对 33 票通过 520 亿美元芯片补贴法案；美联储宣布维持基准利率 5.25% 不变，暗示年内仍有加息可能。"
highlights:
  - "参议院通过 520 亿美元芯片补贴法案"
  - "美联储维持利率 5.25% 不变"
---
```

## 参考数据

### 待处理的高分候选（共 {count} 条）：

```json
{entries}
```

### 近几天已推送上下文（仅供查重，严禁模仿其措辞和结构）：

<RECENT_PUSH_CONTEXT>
{recent_push_context}
</RECENT_PUSH_CONTEXT>

### 近几天已处理的碎片化信息（供洞察参考）：

{context}
"""


async def generate_digest(
    entries: list[dict],
    recent_context: list[dict],
    config: Optional[dict] = None,
    recent_push_context: str = "",
    digest_config: Optional[dict] = None,
) -> str:
    """生成完整日报正文。

    Args:
        entries: 高分候选列表（已评分，含 score/column/tags/summary/content）
        recent_context: 近几天已处理的碎片化信息（用于去重参考）
        config: AI 配置，None 则从环境变量读取
        recent_push_context: 近几天已推送事件的文本摘要
        digest_config: 字数目标和栏目配额配置，包含：
            - target_word_count_min: 最低字数
            - target_word_count: 目标字数
            - target_word_count_max: 最高字数
            - columns: 栏目配额配置

    Returns:
        LLM 生成的 YAML frontmatter + Markdown 正文
    """
    if config is None:
        config = _load_ai_config()

    # 读取字数目标配置
    dc = digest_config or {}
    word_count_min = dc.get("target_word_count_min", 5000)
    word_count_max = dc.get("target_word_count_max", 10000)
    important_score = dc.get("important_score", 85)

    # 构建栏目配额文本
    columns_cfg = dc.get("columns", {})
    column_quota_lines = []
    col_label_map = {
        "us_politics": "美国政情",
        "global_affairs": "国际风云",
        "technology": "科技前沿",
        "economy": "经济财经",
    }
    for col_key, label in col_label_map.items():
        col_cfg = columns_cfg.get(col_key, {})
        min_items = col_cfg.get("min_items", 5)
        target_items = col_cfg.get("target_items", 7)
        max_items = col_cfg.get("max_items", 10)
        column_quota_lines.append(f"- {label}（{col_key}）：{min_items}-{max_items} 条，目标 {target_items} 条")
    column_quota_text = "\n".join(column_quota_lines) if column_quota_lines else "(无配额限制)"

    # 构建 context 文本
    context_lines = []
    for c in recent_context:
        tags_str = ", ".join(c.get("tags", [])) if c.get("tags") else ""
        context_lines.append(
            f"[score: {c.get('score', 0)}] {c.get('title', '')}\n"
            f"published: {c.get('published', '')}\n"
            f"tags: {tags_str}\n"
            f"source: {c.get('source', '')}\n"
            f"summary: {c.get('summary', '')}"
        )

    prompt = DIGEST_PROMPT_TEMPLATE
    # 替换字数相关占位符
    prompt = prompt.replace("{word_count_min}", str(word_count_min))
    prompt = prompt.replace("{word_count_max}", str(word_count_max))
    prompt = prompt.replace("{lead_min}", "150")
    prompt = prompt.replace("{lead_max}", "300")
    prompt = prompt.replace("{event_min}", "150")
    prompt = prompt.replace("{event_max}", "250")
    prompt = prompt.replace("{important_score}", str(important_score))
    prompt = prompt.replace("{important_min}", "300")
    prompt = prompt.replace("{important_max}", "500")
    prompt = prompt.replace("{column_quota_text}", column_quota_text)
    # 替换数据占位符
    prompt = prompt.replace("{count}", str(len(entries)))
    prompt = prompt.replace("{entries}", json.dumps(entries, ensure_ascii=False, indent=2))
    prompt = prompt.replace("{recent_push_context}", recent_push_context or "(无)")
    prompt = prompt.replace("{context}", "\n\n".join(context_lines) or "(无)")

    return await _call_llm(prompt, config, timeout=180)


def has_ai_config() -> bool:
    """检查是否配置了 AI API Key"""
    config = _load_ai_config()
    return bool(config.get("api_key"))


# 导出别名
merge_scores_to_items = _merge_scores
