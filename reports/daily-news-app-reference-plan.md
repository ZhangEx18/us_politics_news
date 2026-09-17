# 参考 daily-news-app 的设计与 Prompt 方案（综合版）

- 参考项目：[dingshuxin353/daily-news-app](https://github.com/dingshuxin353/daily-news-app)（v0.12.1，JS，Agent 驱动 + 主题化出版）
- 我们的项目：全自动抓取→评分→写作→发布的新闻日报管道
- 结论：**对方强在"协议与治理"，我们强在"自动化与质量闸门"**。综合方案 = 保留我们的自动化链路，吸收其 Candidate 协议、字段契约、优先级模型与文档化实践

---

## 一、对方设计拆解（可借鉴点）

### 1.1 架构：Candidate → Validator → Writer → Compiler

```
Agent（人或 AI）只产出候选 JSON
   ↓ 固定路径 publications/<id>/data/candidates/YYYY-MM-DD.json
Validator 字段级校验（validation.js：类型/长度/URL/日期/业务规则）
   ↓ stageJson → acquireDateLock → commitStages（原子写入 + 回滚）
Writer 合并进正式 issue（update=默认，replace 需显式授权）
   ↓
Compiler 编译页面（compiledIsCurrent 增量跳过）
```

关键机制：
- **候选与正式数据物理隔离**（Agent 不得写 `data/issues/`、`data/compiled/`、`config/`）
- **日期锁 + 原子提交**（`acquireDateLock`、`stageJson/restoreStage/commitStages`）
- **增量编译**（`compiledIsCurrent` 校验后跳过）
- **来源合并**（`mergeSources`：按 ID 匹配 → 任一来源 URL 匹配 → 来源并入）

### 1.2 内容协议（AGENT_CONTENT_GUIDE.md v1.3）

| 机制 | 内容 | 对我们的启发 |
|---|---|---|
| coverage 必填 | `coverage.start/end`；同日期首次创建后固定，变化即拒绝 | 我们的 window 只在日志里，候选归档应带 coverage |
| editorial 元数据 | `priority`（lead/important/normal）+ **`selectionReason` 必填** | 我们缺"为什么选这条"的显式记录 |
| 优先级配额 | `priorityLimits`：lead ≤1、important ≤2（可配） | 我们只有"重点/要点"数量，没有头条层 |
| 标题长度分档 | lead ≤42 / important ≤36 / normal ≤28 Unicode 字符 | 我们无标题长度上限 |
| 来源模型 | `sources[]`：`sources[0]` 主来源、`via` 记聚合发现路径、`publishedAt` 未知即省略（不许编造） | 我们 `source_links` 只有 title/url，聚合来源未标注 |
| 去重职责 | **Agent 做语义去重；代码只做 ID/URL 确定性匹配** | 我们已用 AI+相似度混合，此分工可显式化 |
| 禁止字段 | 不得输出 revision/layout/评分/删除指令等 | 我们无此清单（AI 目前也确实不输出这些） |
| 完成语义 | `candidate_ready` vs `published` 严格区分 | 我们 status 字段语义类似，可对齐命名 |
| 版本头 | 指南版本 1.3 / 产品版本 0.12.1 / 更新日期 | **我们的 prompt 是 Python 字符串，无版本、无 diff** |

### 1.3 治理设计

- **任务路由**（AGENTS.md）：按任务类型分流到不同指南，candidate 类型不得混用
- **授权闸门**：用户给参考图 ≠ 允许改源码；源码修改需明确确认
- **写入边界**：Agent / Validator / Writer / Compiler 各自的白名单与黑名单
- **不许谎报**：未经验证不得声称"已发布"

---

## 二、对照表：保留 / 借鉴 / 放弃

| 维度 | daily-news-app | 我们 | 综合结论 |
|---|---|---|---|
| 内容生产 | 外部 Agent 手工/半自动研究 | **全自动抓取+AI 评分+AI 写作** | 保留我们的自动化（对方需人工触发） |
| AI 产物 | Candidate JSON 落盘 | 内存 dict → DB | **借鉴：三阶段候选归档** |
| 校验 | Validator 显式协议 + 错误消息 | 规则散落在 report_engine/run_pipeline | **借鉴：拒绝原因枚举 + 契约文档** |
| 写入 | stage/commit + 日期锁 + 回滚 | 直接写 DB/文件 | 部分借鉴（metrics/发布文件用 stage 模式） |
| 优先级 | lead/important/normal | 重点解析/要点 两级 | **借鉴：加 lead 头条层** |
| 来源 | sources[] + via + 主来源 | source_links[{title,url}] | **借鉴：补 via/publishedAt/主来源** |
| Prompt | 外置 md + 版本号 + 日期 | Python 字符串常量 | **借鉴：外置 prompts/*.md + 版本化** |
| 完成语义 | candidate_ready/published | status（ok/ai_failed/…） | 对齐命名（低成本） |
| 治理 | 授权闸门/禁止字段/边界清单 | 无文档 | **借鉴：写 PIPELINE_CONTRACT.md** |
| 多出版物 | publication 隔离 + 主题 | product（news/algorithms） | 保留现有 product 机制 |
| 主题化 | 3 套预设主题 | 单模板 | 放弃（当前收益低） |
| 图片管线 | 严格图片模型 | 无 | 放弃（无图片源） |

---

## 三、综合方案

### P0（1-2 天，收益最高）

**1. Prompt 外置与版本化**
```
prompts/
  score.md            # 评分（含版本头 v1.0 / 更新日期）
  digest.md           # 栏目写作
  translate.md        # 标题翻译
  overview.md         # 日报总览
  periodical.md       # 周月报总览
  fallback_body.md    # 兜底扩写
```
- `ai_analyzer._load_prompt()` 已有雏形（读文件 + 占位符）；改为从 `prompts/` 读取
- 版本头格式：`<!-- version: 1.0 | updated: 2026-09-17 | regression: prompt_cases.yaml -->`
- 收益：prompt 改动有 git diff、可 review、可回滚；回归集直接对应

**2. 三阶段候选归档**
```
data/candidates/YYYY-MM-DD/
  score.json      # 评分候选（含 coverage、每条的 score/column/reason/五维）
  selection.json  # 选择结果（每条：candidate_id、进入哪一栏、detailed/headline、selection_reason）
  digest.json     # 写作产物（events + source_links）
```
- 每条带稳定 `candidate_id`（现有 url_hash 可用）
- 选择阶段补 **`selection_reason`**（现在只有埋点计数，没有"为什么选它"）
- 收益：可复盘（谁被拒、为什么）、可回归（跑同一份候选对比输出）

**3. 字段契约补强（对齐对方的字段级规则）**
| 字段 | 规则 | 现状 |
|---|---|---|
| 标题长度 | lead ≤30 / 重点 ≤26 / 要点 ≤22 字符 | 无限制（有截断检测，无上限） |
| sources[0] | 主来源必须非空且直接支持标题事实 | 有 source_links 但无主次 |
| via | 聚合/发现平台（Google News、AIHOT）记 via | 缺失 |
| publishedAt | 来源发布时间，未知省略不编造 | 已有 published 字段，未进 source_links |

### P1（3-5 天）

**4. lead 头条层**
- 配置：`digest.priority_limits: {lead: 1, important: 10, normal: 8}`
- 选择：全报最高 `newsworthiness` 事件作为 lead（或让评分输出 `editorial_priority`）
- 渲染：Markdown/HTML 增加头条样式（大标题 + 导语 + 正文）
- 验收：每日 1 条 lead，缺失时自动降级（不空头版）

**5. Validator 收敛与拒绝原因枚举**
```python
REJECT_REASONS = {
    "routine_notice": "例行公告",
    "low_newsworthiness": "低新闻价值",
    "soft_news": "软新闻",
    "opinion_piece": "观点稿",
    "duplicate_event": "同事件重复",
    "date_out_of_window": "日期越窗",
    "body_too_short": "正文过短",
    ...
}
```
- 现在拒绝散落在 6-7 处（预筛/评分门槛/要点过滤/门禁），统一为 `reject(reason)` 并写入 metrics
- 收益：一图看清"每天被拒的都是什么原因"

**6. `docs/PIPELINE_CONTRACT.md`（对齐对方的 AGENT 指南风格）**
- 阶段表：fetch → score → select → digest → validate → publish
- 每阶段的输入/输出/失败语义/重试策略
- 授权边界：自动动作（评分/写作/渲染）vs 需人工确认（禁源/改配置/改 prompt 阈值）
- 完成语义：`candidate_ready` / `published` / `degraded` / `failed`

### P2（1-2 周，可选）

7. **发布文件 stage/commit**：`_write_metrics_file`/日报写入先写 `.staging` 再原子 rename（对齐对方 `stageJson/commitStages`）
8. **主题化**：HTML 模板参数化（暗色/报纸风），收益低，暂缓

---

## 四、附：本次核对发现的线上 Bug（建议顺手修）

**metrics 文件在 gh-pages 被删除**（`/news/metrics/latest.json` 现 404）

- 根因：`sync_pages_state.restore_published_history` 只恢复 `feeds/*.xml` 与 `{product}/{report_type}/` 下的日报，**没有恢复 `{product}/metrics/latest.json`**
- 触发场景：**algorithms 产品的跑批**（23:45，1m20s）部署整个 `./docs`，其工作区里没有 news/metrics（restore 未恢复、news 也没跑），`peaceiris` 整目录同步即删除该文件
- 修复（3 行）：restore 循环中补
  ```python
  metrics_path = f"{product_key}/metrics/latest.json"
  _copy_git_file(branch_ref, metrics_path, docs_dir / metrics_path, ...)
  ```
- 另建议：为跨产品部署加"文件清单校验"（部署前断言 docs/ 下关键文件存在：各 product 的 metrics/feed/最新日报）

---

## 五、执行建议

```
P0-1 prompt 外置（半天）        ← 立刻改善可维护性
P0-2 候选归档 + selection_reason（半天）
P0-3 字段契约补强（2 小时）
P1-4 lead 层（1 天）
P1-5 Validator 收敛（1 天）
P1-6 PIPELINE_CONTRACT.md（半天）
P2-7 stage/commit（半天，可选）
修复：metrics restore 漏项（10 分钟）
```

**不动的东西**（避免把我们的优势改丢）：全自动链路、质量门禁阈值、配额与去重、术语表、回归集、force_rescore。
