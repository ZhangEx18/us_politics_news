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


def _timeout_for(config: dict, scope: str, default: int) -> int:
    """按调用场景读取超时配置。"""
    return int(config.get(f"{scope}_timeout_seconds") or config.get("timeout_seconds") or default)


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
            "content_kind": s.get("content_kind", entry.get("content_kind", "analysis")),
            "is_hard_news": bool(s.get("is_hard_news", entry.get("is_hard_news", False))),
            "tags": s.get("tags", entry.get("tags", [])),
            "summary": s.get("summary", entry.get("summary", "")),
            "event_key": s.get("event_key", entry.get("event_key", "")),
        })
    return merged


# ── score_batch ──


SCORE_PROMPT_TEMPLATE = """你是一个专业且严苛的新闻主编。请对以下候选新闻进行过滤、评分和信息提取。

## 评分标准（0-100）

**核心约束（先判这三条，再进入分档）**：
1. 90+ 必须同时满足：(a) 主题与美国政局 / 国际局势 / 科技前沿 / 经济走势强相关；(b) 来源为当事方官方账号或官方博客（非 KOL / 媒体转述）；(c) 属于首发
2. 非硬新闻、非核心主题（娱乐、体育、生活方式等）无论多重大，上限 79 分
3. 重磅新闻若来源是 KOL / 媒体转述，上限 89 分

**分档**：
- 【90-100】核心领域 + 官方首发 + 里程碑级事件
- 【80-89】重要政策、司法、外交、战争、财报、宏观或产业进展
- 【70-79】一般硬新闻，事实成立但增量有限
- 【60-69】二手信息、一般性新闻
- 【<60】低价值内容：纯情绪、广告、闲聊、评论、荐股单

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

## 输出要求

必须返回纯 JSON 对象，顶层包含 `items` 数组，每个对象包含：
- `link`: 原文链接（必须保留原样）
- `score`: 整数评分（0-100）
- `column`: 栏目字符串（us_politics / global_affairs / technology / economy）
- `event_key`: 事件归并标识（snake_case，含日期）
- `content_kind`: 内容类型（policy / judiciary / election / diplomacy / security / regulation / market / macro / corporate / analysis / opinion / media_reaction / watchlist）
- `is_hard_news`: 布尔值，是否属于硬新闻
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
      "content_kind": "judiciary",
      "is_hard_news": true,
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
    entries_for_llm = [
        {
            "link": e.get("link", ""),
            "title": e.get("title", "无标题"),
            "source": e.get("source", "未知来源"),
            "published": e.get("published", ""),
            "content": (e.get("content", "") or "")[:content_limit],
        }
        for e in entries
    ]
    entries_json = json.dumps(entries_for_llm, ensure_ascii=False, indent=2)
    prompt = SCORE_PROMPT_TEMPLATE.replace("{entries_json}", entries_json)

    try:
        response = await _call_llm(
            prompt,
            config,
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
            print(f"  [AI] {msg}")
            errors.append(msg)

        return matched, errors

    except Exception as e:
        detail = str(e).strip() or repr(e)
        msg = f"批次{batch_index + 1} 评分失败: {type(e).__name__}: {detail}"
        print(f"  [AI] {msg}")
        return [], [msg]


def _is_retryable_score_error(errors: list[str]) -> bool:
    """只有超时或结果不完整才值得拆小重试。"""
    if not errors:
        return False
    retryable_tokens = ("TimeoutError", "结果不完整")
    return any(token in err for err in errors for token in retryable_tokens)


async def _score_batch_with_retry(
    entries: list[dict],
    config: dict,
    batch_index: int = 0,
    depth: int = 0,
) -> tuple[list[dict], list[str]]:
    """对单批执行评分；失败时拆分重试，尽量恢复覆盖率。"""
    scores, errors = await _score_single_batch(entries, config, batch_index)
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

    print(
        f"  [AI] 批次{batch_index + 1} 拆分重试: "
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

    batches = _split_entries_for_batch(entries, max_prompt_chars)
    print(f"  [AI] 评分: {len(entries)} 条 -> {len(batches)} 批")

    if len(batches) == 1:
        scores, errors = await _score_batch_with_retry(batches[0], config, 0)
        return _merge_scores(entries, scores), errors

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _limited(idx: int, batch: list[dict]):
        async with semaphore:
            return await _score_batch_with_retry(batch, config, idx)

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
- **reader_body**：Reader 专用正文，按”事实 → 变化 → 后果”结构写成单段 2-4 句，目标 120-200 字
- **core_facts**：站内兼容字段，使用与 reader_body 一致的内容
- **source_links**：相关阅读，格式 [{{“title”: “来源名”, “url”: “https://...”}}]
- **is_followup**：布尔值，是否为历史事件的持续跟踪

## reader_body 写作规范（核心）

每条 reader_body 必须按以下逻辑链组织，写成一段连贯叙述：

**第 1-2 句：发生了什么（事实）**
- 直接陈述主体、动作、结果
- 必须包含具体数字、机构、人名、金额、比例、时间、地点中至少两项
- 禁止用”据报道””据悉””有消息称”开头

**第 3 句：这改变了什么（变化）**
- 说清”以前怎样，现在怎样”
- 用对比句式，例如”此前……此次裁定意味着……”
- 给一个锚点定位事件在大图景中的位置

**第 4 句：接下来影响谁（后果）**
- 指向具体对象：选民、党派、法院、国会、市场、消费者、企业、产业链、地区安全
- 必须有具体方向，不能以”存在不确定性””增添了变数”收尾
- 可以保留分歧，但要说清分歧点在哪

## 示例（仅作结构参考，不要复制内容）

### 示例 1：政策类
标题：最高法院裁定大麻使用者不丧失持枪权
正文：最高法院以 6 比 3 裁定，单纯使用大麻不构成剥夺持枪权的联邦法律依据。案件源于一名德克萨斯州合法大麻使用者的上诉——她因持有大麻被联邦法认定为”非法药物使用者”而禁止购枪。此裁定意味着 24 个已实现大麻合法化的州中，合法使用者的持枪权将获得明确联邦保护。这是最高法院继 2022 年 Bruen 案后，再次以”历史传统”标准收紧联邦枪权限制的信号。

### 示例 2：科技类
标题：AI 推理公司 Baseten 据报正进行 15 亿美元融资
正文：AI 推理部署公司 Baseten 据报正在进行 15 亿美元新融资，估值将达 130 亿美元，距其上一轮融资仅隔数月。Baseten 的业务是将大模型高效部署为生产级 API，客户包括 Cursor 和 Perplexity。推理环节正成为 AI 落地的关键瓶颈——模型训练完成后，企业需要低成本、低延迟的方式把模型跑起来，Baseten 正卡在这个位置。但 130 亿估值对应约 25 亿美元年收入，倍数远超同阶段 SaaS 公司，市场对 AI 基础设施的定价是否合理仍有分歧。

### 示例 3：航天类
标题：NASA 要求诺斯罗普·格鲁曼停止月球门户 HALO 模块工作
正文：NASA 要求诺斯罗普·格鲁曼停止月球门户空间站 HALO 居住舱模块的后续工作。HALO 原是门户站的核心舱段，宇航员在月球轨道的生活和工作都在这个模块里完成。停掉它意味着 NASA 正在重新评估门户站的整体方案——此前波音已就门户站合同重组与 NASA 谈判，HALO 的暂停可能是谈判结果的一部分。门户站是阿尔忒弥斯重返月球计划中”月球轨道中转站”概念的核心硬件，如果最终取消，整个登月路线可能从”轨道中转”退回到”直飞月面”。

### 示例 4：财经类
标题：苹果确认将提高产品价格 存储芯片成本上升是主因
正文：苹果 CEO 蒂姆·库克确认，因存储芯片成本上升，公司计划提高部分产品价格。存储芯片价格自去年下半年以来持续走高，三星和美光的 NAND 闪存报价涨幅已超过 20%，苹果作为全球最大存储芯片采购方之一，成本压力直接传导至终端定价。苹果提价可能为整个消费电子行业树立价格基准，推动手机和笔记本电脑的平均售价上行。

## 禁止清单

以下内容一律禁止出现在 reader_body 中：

**禁止的开头**：据报道、据悉、有消息称
**禁止的连接词**：值得注意的是、需要指出的是
**禁止的空泛动词**：凸显了、反映了、意味着、标志着
**禁止的收尾**：引发了讨论、增添了变数、存在不确定性、产生深远影响、仍需观察
**禁止的套话**：对于读者来说、值得关注的是
**禁止的标签**：核心事实：、背景脉络：、背景与影响：、可能影响：、为什么值得关注：

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
      “reader_body”: “事实 → 变化 → 后果单段正文，120-200 字。”,
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
6. reader_body 必须讲一个完整的故事：发生了什么、改变了什么、谁会受到影响
7. 禁止输出”核心事实：””背景与影响：””为什么值得关注：”等标签
8. 每句一个事实，不堆砌；同一主语不连续出现超过 2 次

## 输入数据（共 {count} 条候选事件）

```json
{events_json}
```
"""


# 栏目定义映射
_COLUMN_DEFINITIONS: dict[str, str] = {
    "us_politics": "只写美国国内权力结构、法院、国会、白宫、州政治、选举、调查、人事和联邦政策；不写以外交、战争、盟友关系、对华/对伊互动为主轴的事件。",
    "global_affairs": "写外交、战争、军事、联盟关系、国际谈判、国际组织和对华/对俄/对伊博弈；即使主角是美国，只要主线是对外事务，也归这里。",
    "technology": "写 AI、芯片、半导体、平台、科研突破、科技监管和技术产业竞争；不写只是把外交新闻换成 AI 角度的重复条目。",
    "economy": "写利率、通胀、就业、贸易、关税、财报、商品价格、产业链和资本市场真实变化；不写荐股、观察名单和泛投资建议。",
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
            "content": (e.get("content", "") or "")[:int(ai_config.get("digest_content_chars", 1000))],
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
        ai_config,
        timeout=_timeout_for(ai_config, "digest", 180),
    )

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


def has_ai_config() -> bool:
    """检查是否配置了 AI API Key（无 key 时返回 False，不抛异常）"""
    try:
        config = _load_ai_config()
        return bool(config.get("api_key"))
    except RuntimeError:
        return False


# 导出别名
merge_scores_to_items = _merge_scores
