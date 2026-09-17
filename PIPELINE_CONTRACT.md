# 观察日报 Pipeline 契约

- 版本：1.0 | 更新：2026-09-17
- 适用：`news` product（daily）
- 用途：定义各阶段输入/输出/失败语义/授权边界，供维护者与 AI Agent 共同遵守
- 关联：`prompts/*.md`（模板与版本头）、`tests/fixtures/prompt_cases.yaml`（回归集）、`config/base.yaml`（阈值）

---

## 1. 阶段总览

```
fetch → filter(freshness) → prefilter → score → select → digest → validate → archive → render → deploy
```

| # | 阶段 | 代码位置 | 输入 | 输出 | 失败语义 |
|---|---|---|---|---|---|
| 1 | fetch | `src/fetchers.py` | sources.yaml（136 启用源） | ContentItem[] | 单源失败仅记录，不阻断 |
| 2 | freshness | `run_pipeline._filter_items_to_daily_dates` | items + 窗口 | 窗口内 items | 全空则终止（0 条） |
| 3 | prefilter | `run_pipeline._prefilter_items_for_scoring` | items | 每栏 ≤limit 候选（含 cn 保底） | 上限内截断 |
| 4 | score | `ai_analyzer.score_batch` | 候选 entries | score + 五维 + event_key | 覆盖率 <70% → `ai_failed` 退出 |
| 5 | select | `run_pipeline`（硬新闻/例行/价值/配额） | scored entries | hard_news_scored | 全部剔除则报错退出 |
| 6 | digest | `ai_analyzer.generate_column_digest` | 每栏候选 + history | events（title_zh/reader_body） | JSON 失败重试一次，仍失败 → 栏目降级 |
| 7 | validate | `report_engine`（门禁/降级/去重/配额） | columns | 质检通过的 columns | 降级而非丢弃；空栏触发 AI 兜底 |
| 8 | archive | `report_engine._write_candidates_archive` | scored + columns | `{site}/candidates/{date}/{score,selection}.json` | 归档失败仅记录，不阻断 |
| 9 | render | `report_renderer` | meta + columns | md/html/feed | 格式校验失败 → `render_failed` |
| 10 | deploy | workflow `publish-product.yml` | docs/ | gh-pages | 校验不过 → 不部署 |

---

## 2. 关键数据结构

### 2.1 评分候选（score.json）

```json
{
  "schemaVersion": 1,
  "date": "2026-09-17",
  "coverage": {"start": "2026-09-16T07:00:00+08:00", "end": "2026-09-17T07:00:00+08:00"},
  "items": [{
    "candidate_id": "iran_war_powers_20260917",
    "link": "https://...", "title": "...", "source": "...", "column": "us_politics",
    "score": 88, "is_hard_news": true,
    "newsworthiness": 0.9, "routine": 0.1, "event_key": "..."
  }]
}
```

### 2.2 选择结果（selection.json）

```json
{
  "schemaVersion": 1, "date": "2026-09-17", "coverage": {...},
  "items": [{
    "candidate_id": "...", "column": "us_politics", "slot": "detailed",
    "title_zh": "...", "score": 88,
    "selection_reason": "score=88, nw=0.9, stage=首发, slot=detailed",
    "sources": [{"title": "来源名", "url": "https://...", "primary": true, "via": "Google News"}]
  }]
}
```

字段规则（对齐外部参考协议的字段契约）：

| 字段 | 规则 |
|---|---|
| `coverage` | 日报窗口；同日重跑不变 |
| `title_zh` | 重点 ≤26 字；要点 ≤22 字（`content_audit.long_titles` 计数） |
| `sources[0]` | 主来源，必须直接支持标题事实（`primary: true`） |
| `via` | 聚合发现路径（Google News / AIHOT），正文不得提及 |
| `selection_reason` | 必填，由评分维度合成（score/nw/stage/slot） |
| `newsworthiness` | 服务端由五维加权推导（0.35/0.2/0.2/0.15/0.1） |
| 评分关联 | 写作产物不带评分：按 event_key → 标题 → 来源链接（归一化去 query/尾斜杠）回填 |

评分关联口径（lead 选择与归档共用）：

- 命中优先级：`event_key` 精确 → `title_zh`/`title` 归一化 → `source_links`/`sources` URL 归一化。
- lead 取全报关联成功事件中 `(newsworthiness, score)` 最大者，写入 `metrics.lead`。
- 关联失败时 lead 回退到首栏首条，`score/newsworthiness` 留空（可观测为 null）。

跨天去重口径：

- 数据源：近两日（`report_date-2` 起）`report_events`（`quality_status=ok`）。
- 命中键：`event_key` 精确 ∪ 来源链接归一化；命中即拒（`repeated_story`），不再进入写作。
- 范围：事件身份级匹配（同一事件同一来源）；同一议题的新进展（stage=进展）不视为重复。

---

## 3. 拒绝原因枚举（`metrics.rejections`）

| reason | 标签 | 触发位置 |
|---|---|---|
| `routine_notice` | 例行公告 | 预筛降权 + 选择剔除 |
| `low_newsworthiness` | 低新闻价值 | `routine≥0.6` 或 `nw<0.5` |
| `soft_news` | 软新闻 | 要点池（配置词表 + 中文兜底） |
| `opinion_piece` | 观点/分析稿 | 要点标题正则 |
| `promo_piece` | 公关稿 | 要点标题正则 |
| `cryptic_title` | 标题不可读 | 标题规则 |
| `unreadable_body` | 正文不可用 | 正文缺失且无法回退 |
| `duplicate_event` | 同事件重复 | 事件合并 + 跨栏/跨层去重 |
| `repeated_story` | 跨天已上稿 | 近两日 `report_events` 命中（event_key 或来源链接归一化） |
| `source_quota` | 来源配额 | 单源单栏 ≤30%、全报 ≤4 |
| `date_out_of_window` | 日期越窗 | 正文日期门禁 |
| `body_too_short` | 正文过短 | 字数门禁（<40 字） |

---

## 4. 授权边界（AI 自动 vs 需人工确认）

| 动作 | 归属 | 说明 |
|---|---|---|
| 抓取/评分/写作/渲染/发布 | **自动** | CI 全自动，失败按指标告警 |
| 调整阈值（min_chars/配额/覆盖率） | 需确认 | 改 `config/` 并过回归集 |
| 修改 prompt | 需确认 | 改 `prompts/*.md`，版本号 +1，跑 `prompt_regression.py --live` |
| 禁用/启用源 | 需确认 | `sources.yaml`；源健康脚本只建议不直接改 |
| 强制重评分 | 自动（显式开关） | `force_rescore=true`，用于验证 prompt/模型变更 |
| 改写已发布日报 | **禁止** | 只能通过重跑生成新版本 |

---

## 5. 完成语义

| 状态 | 含义 | 判据 |
|---|---|---|
| `candidate_ready` | 评分与归档完成 | `candidates/{date}/selection.json` 写入 |
| `published` | 站点可访问 | gh-pages 部署成功 + 校验通过 |
| `degraded` | 部分栏目降级 | `digest_failures` 非空或 `ai_fallback_added > 0` |
| `ai_failed` | AI 覆盖率不足 | `coverage < min_score_coverage` |
| `freshness_failed` | 今日性不足 | `freshness_ratio < 0.95` |
| `render_failed` | 渲染/校验失败 | 格式校验异常 |

---

## 6. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.1 | 2026-09-17 | 新增 `repeated_story` 拒绝原因、评分关联与跨天去重口径 |
| 1.0 | 2026-09-17 | 首版：阶段契约、数据结构、拒绝枚举、授权边界、完成语义 |
