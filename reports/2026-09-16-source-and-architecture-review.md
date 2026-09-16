# 观察日报：信息源与架构深度评审报告

- 日期：2026-09-16
- 范围：新闻源生态（3 轮调研 + 逐条实测）、管道架构对照、源健康体检、改造路线图
- 数据基线：当日 4 次线上跑批（GitHub Actions）+ 本地 RSSHub/直连实测 + 状态库（news-data 分支）
- 结论面向：`config/products/news/sources.yaml`、`src/` 管道、`.github/workflows/publish-product.yml`

---

## 0. 执行摘要

三个最重要的发现：

1. **主流通讯社层实际已死**。配置里的 Reuters（404×2）、AP（403）、CNN（最新条目 2022-12）、Axios（404）、New Yorker（404）、Daily Beast（503）、NBC/USA Today（0 条）、Anthropic/Meta 博客（404）、BLS（403）全部失效。这才是「FTC/Al Jazeera 霸屏」的真正背景——管道缺了核心 wire 层，只剩少数能抓到的源在竞争。
2. **中文源基建已重建**。RSSHub（CI 内 service 容器）+ 财新直连日期过滤 + AIHOT，本地实测 6 条路由共 127 条/日，财新 14 条带日期。
3. **架构差距集中在 6 处**：AI 调用无降级链、正文抽取靠手写正则、语义去重缺失、prompt 无回归评测、源健康无自动告警、投递无推送。

量化基线：

| 指标 | 数值 |
|---|---|
| 配置源 / 启用 | 152 / 140 |
| 今日有产出源 | 60 |
| 零产出启用源 | 80（含约 12 个确认失效） |
| 候选池规模（当晚两次复评分） | 57 → 81 |
| 成稿量（重点解析+要点） | 10 → 24（改造后，含当晚复评分） |
| 目标配额 | 56（10+8 / 10+8 / 7+3 / 7+3） |
| AI 成本估算 | 约 $0.02–0.03/次（DeepSeek 非高峰价，估算，未埋点） |

---

## 1. 现状盘点

### 1.1 数据流（含代码位置）

```
抓取 fetch_all_sources            src/fetchers.py:466
  └─ RSS/RSSHub/GoogleNews/Custom/GDELT（GDELT 硬编码 9 查询，全 429）
跨源去重 merge_cross_source_duplicates
时效过滤 _filter_items_to_daily_dates   （窗口 = 前一日 07:00 → 当日 07:00 北京）
预筛 _prefilter_items_for_scoring       src/run_pipeline.py:479
  ├─ 信号 = 规则分 + tier(30/20/10/0) + 时效(15/10/5/0) + 内容长度 + 关键词
  ├─ 例行公告 ×0.3（content_policy.is_routine_notice）
  └─ 单源 cap（sources.yaml max_candidates_per_run，默认 3）
AI 评分 score_batch（串行 max_concurrent=1，约 40-60s/批）  src/ai_analyzer.py
  └─ 输出：score/column/event_key/三维（information_gain、newsworthiness、routine、impact_scope）
选择过滤（run_pipeline.py:1521-1580）
  ├─ 硬新闻（is_hard_news）
  ├─ 例行剔除 is_routine_notice
  ├─ 低价值剔除 entry_newsworthiness_ok（routine≥0.6 或 nw<0.5）
  └─ 今日性 gate（freshness ratio ≥0.95）
事件合并 merge_events（event_key + 标题相似度 ≥0.85）  src/ai_analyzer.py:723
栏目选择 _select_daily_column_items（源配额：单栏 ≤30% 规模、全报 ≤4）  src/report_engine.py
AI 写作（4 栏 digest + 要点）
质量门禁 + 降级（日期重写、headline-only 降级、内容审计）
渲染 + Feed → GitHub Pages
```

### 1.2 已经领先于同类开源项目的部分

| 能力 | 说明 |
|---|---|
| 例行公告过滤 | 规则（中英文正则）+ AI 维度双保险 |
| 新闻价值三维 | newsworthiness / routine / impact_scope，且明确「官方≠高价值」 |
| 源配额防霸屏 | 单源单栏 ≤30%、全报 ≤4、要点机构 ≤2 |
| 事件级合并 | event_key + 标题相似度 + 跨栏目去重 |
| 质量门禁链 | freshness gate、降级而非丢弃、四栏非空契约、内容审计指标 |
| 状态库复用 | SQLite 跨 run 事件库（同日报复用避免重复 AI 花费） |
| CI 内自托管 RSSHub | service 容器 + 等待步骤，无需外部依赖 |

### 1.3 源清单结构

| 维度 | 数值 |
|---|---|
| 总源 / 启用 | 152 / 140 |
| 按栏目（启用） | us_politics ~35 · global_affairs ~43 · technology ~32 · economy ~33 |
| 层级 | tier1（官方/一线）约 44 · tier2 主流约 47 · tier3 约 38 · tier4 聚合约 11 |
| 抓取模式 | rss 109+ · rsshub 9 · google_news 15 · custom 15 · hacker_news 2（禁用） |
| 中文源 | 11（RSSHub 路由 6 + 财新直连 3 + PingWest 禁用 + AIHOT 归入 AI 源） |

### 1.4 2026-09-16 完成的改造

| 提交 | 内容 |
|---|---|
| `5042edd` | 例行公告过滤 + 源/机构/栏目三维配额 |
| `a7c30b8` | custom 源日期抽取（URL/meta/JSON-LD）+ 抓取窗口按日比较 |
| `79f84a9` | 新闻价值三维评分与门槛 |
| `1b031db` | 事件级聚类合并（event_key + 标题相似度） |
| `82fd23e` | AIHOT 接入 + 中文源 RSSHub 化 + 财新 URL 过滤 |
| `aa75647` | 7 个官方一手源 + aiohttp 头部限制修复 |
| `65d14d0` | README 更新 |
| `107ed9b` | 双班次（改造 6）按决策回滚 |

---

## 2. 第一轮调研：源生态与同类项目

| 项目 | Star | 可借鉴点 | 采用情况 |
|---|---|---|---|
| sansan0/TrendRadar | 62.3k | 35 平台聚合、关键词筛选、多渠道推送 | 推送层候选 |
| DIYgod/RSSHub | 46.2k | RSS 路由生成 | ✅ 已用（CI service + 本地 docker） |
| RSSNext/Folo | 39.0k | AI RSS 阅读器体验 | 未采用 |
| newsnext/newsnow | 21.7k | 实时热榜阅读器 + MCP | 发现层候选 |
| FreshRSS/FreshRSS | 16.0k | 自托管聚合、XPath 抓取 | 备选（未采用） |
| cooderl/wewe-rss | 9.7k | 公众号 RSS | 中文源扩充候选 |
| finaldie/auto-news | 905 | 多源（Twitter/Reddit/YT）+LLM | 发现层候选 |
| vigorX777/ai-daily-digest | 1.6k | 多维评分日报 | 与我们的三维评分思路一致 |
| JackyST0/hotpush | 191 | 13 平台热榜推送 | 推送层候选 |

---

## 3. 第二轮调研：类 aihot 的可接入信息源（逐条 curl 实测）

### 3.1 官方一手源（免 key，已接入 7 个）

| 源 | 栏目 | 实测 | 备注 |
|---|---|---|---|
| White House News | us_politics | ✅ 2 条 | 白宫一手 |
| White House Presidential Actions | us_politics | ✅ 低频 | 行政令/任命 |
| U.S. State Department | global_affairs | ✅ 5 条 | 需 aiohttp 头部放宽（已修） |
| U.S. Department of Defense | global_affairs | ✅ 2 条 | 五角大楼 |
| SCOTUSblog | us_politics | ✅ 4 条 | 最高法院 |
| SEC Press Releases | economy | ✅ 低频 | 监管执法 |
| Federal Register | us_politics | ✅ 104 条/日（cap 2） | 联邦法规 |

### 3.2 AI 资讯源（决策：只用 AIHOT）

| 源 | 实测 | 决策 |
|---|---|---|
| **AIHOT** `aihot.news/feed/all.xml` | ✅ 50 条/日 | ✅ 已接入（科技栏，tier2，cap4） |
| smol.ai | ⚠️ 最新条目 9/9（停更） | ❌ 移除 |
| The Decoder | ✅ 7 条 | ❌ 移除（AI 源只留 AIHOT） |
| Techmeme | ✅ 15 条 | 备选 |

### 3.3 验证失败/不可用

| 源 | 问题 |
|---|---|
| Reddit JSON | 403（数据中心 IP 拒绝） |
| Congress.gov most-viewed-bills | 周报式单条，价值低 → 跳过 |
| 机器之心 / 澎湃 直连 | JS 渲染，需 RSSHub 路由 |
| FDA / EPA / CBO / Supreme Court 官方 RSS | 403/404/405 |

---

## 4. 第三轮调研：架构层可借鉴项目

| 项目 | Star | 对应我们的环节 | 差距 |
|---|---|---|---|
| BerriAI/litellm | 58.9k | AI 调用 | 单 provider 直连，无降级链/熔断 |
| adbar/trafilatura | 6.8k | custom 源正文抽取 | 手写正则，导航垃圾/日期缺失 |
| kingname/GeneralNewsExtractor | 3.8k | 中文正文抽取 | 同上（中文专用） |
| ekzhu/datasketch | 3.0k | 事件去重 | difflib O(n²)，仅标题字面相似 |
| sentence-transformers | 19.1k | 语义去重 | 无语义向量能力 |
| promptfoo | 25.2k | prompt 质量 | 评分 prompt 无回归评测 |
| langfuse | 34.7k | LLM 可观测 | 无单次调用 token/成本追踪 |
| caronc/apprise / ntfy | 17.3k / 34.3k | 投递 | 无推送渠道 |
| changedetection.io | 34.3k | 源健康 | 源失效数月无人知 |
| crawl4ai / firecrawl | 83.7k / 181k | 抓取兜底 | 失效源无 fallback |
| jieba / TextRank4ZH | 35.2k / 3.4k | 中文 NLP | 无分词/关键词能力 |
| RSS-Bridge | 9.2k | RSS 生成 | 备选（已选 RSSHub） |

---

## 5. 源健康深度体检（本次新增实证）

### 5.1 确认失效源（HTTP 实测）

| 源 | 栏目 | 实测 | 修复方案 |
|---|---|---|---|
| Reuters Politics / World | us/global | 404 | Google News `site:reuters.com` 兜底（实测 100 条/次） |
| Associated Press Politics / World | us/global | 403（rsshub.app） | 同 `site:apnews.com`；或自托管 RSSHub 路由 |
| CNN Politics | us | 200 但最新 2022-12 | 换 `site:cnn.com` 兜底 |
| Axios Politics | us | 404 | `site:axios.com` 或弃用 |
| NBC News Politics | us | 0 条 | 弃用/替换 |
| USA Today Politics | us | 0 条 | 弃用/替换 |
| Daily Beast | us | 503 | 弃用 |
| The New Yorker - Politics | us | 404 | 弃用 |
| Anthropic Blog / Meta AI Blog | tech | 404 | 弃用（AI 源以 AIHOT 为主） |
| Bureau of Labor Statistics | economy | 403 | 弃用或换 UA（BLS 封锁数据中心） |

### 5.2 零产出分布（80 个启用源）

| 栏目 | 零产出数 | 主要构成 |
|---|---|---|
| us_politics | 18 | 失效 wire（Reuters/AP/CNN/Axios 等）+ 低频周刊 |
| global_affairs | 23 | 智库周刊（CFR/RAND/Chatham）+ 失效 wire |
| technology | 14 | 企业博客（OpenAI/Anthropic/Meta/Microsoft/SemiAnalysis） |
| economy | 25 | 官方月度源（Fed/BLS/World Bank/IMF/NBER）+ 失效源 |

注：零产出 ≠ 失效。低频源（Fed 最新 9/11、IMF 月度）属正常；上表 5.1 为确认失效项。

### 5.3 候选级重复漏检实验（现有去重阈值 0.85）

| 标题对 | 相似度 | 结果 |
|---|---|---|
| 参议院民主党阻止加密货币监管法案 ↔ 美参议院否决加密法案（香港视角） | 0.37 | **漏**（语义同事件） |
| 美国限制南非官员签证，两国争端升级 ↔ 美国宣布限制南非官员签证 | 0.71 | **漏** |
| FTC, States Sue Amazon Over Secret Ad Surcharge Scheme ↔ FTC and States Sue Amazon…Surcharges | 0.91 | 命中 |
| Fed holds interest rates steady ↔ Federal Reserve keeps rates unchanged | 0.47 | **漏** |

结论：现方案只能抓「几乎同名」；跨表述、跨语言、跨视角的同事件需语义层（embedding/MinHash+实体）。

---

## 6. 架构差距分析

| # | 环节 | 现状 | 差距 | 优先级 |
|---|---|---|---|---|
| 1 | AI 调用韧性 | 单 provider 直连，整批失败即失败 | 无降级链/熔断/告警（曾停更一周） | **P0** |
| 2 | 正文/日期抽取 | 手写正则 + 简易文本化 | 抽取质量差（导航垃圾、日期缺失） | **P0** |
| 3 | 失效源修复 | 12 个确认失效仍在配置中 | 无自愈/告警；主流 wire 缺失 | **P0** |
| 4 | Prompt 质量 | 无回归评测 | 评分/写作 prompt 改动靠人肉 | **P1** |
| 5 | 语义去重 | 标题字面相似度 | 跨表述同事件漏检（见 5.3） | **P1** |
| 6 | 源健康告警 | 有 summary，无动作 | 源失效无人知 | **P1** |
| 7 | LLM 可观测 | duration/覆盖率 | 无 token/成本/失败类型追踪 | P2 |
| 8 | 投递渠道 | Pages + RSS | 无主动推送 | P2 |
| 9 | 抓取兜底 | RSSHub + 正则 | 失效源无 fallback | P2 |

---

## 7. 路线图

### P0（本周）

| 任务 | 做法 | 验收 |
|---|---|---|
| AI 降级链 | `ai_analyzer._load_ai_config` 支持 `AI_FALLBACK_BASE_URL/AI_FALLBACK_MODEL`；评分批次失败后自动切换一次；失败仍走覆盖率门禁 | 模拟主 provider 404，运行自动切换并成功 |
| 失效源修复 | 用 Google News `site:` 查询替换 Reuters/AP/CNN/Axios（实测 100 条/次）；确认失效的直接 disable | 主流 wire 覆盖恢复，候选池 ≥120 |
| 正文抽取 | 引入 trafilatura（通用）+ GNE（中文），仅接 custom 源；保留正则兜底 | 财新条目正文完整率 ≥90%，导航垃圾为 0 |

### P1（1-2 周）

| 任务 | 做法 | 验收 |
|---|---|---|
| 源健康告警 | `scripts/source_health.py`：连续 3 天 0 产出/失败 → 自动写回 `enabled: false` + 开 GitHub Issue | 失效源 3 天内自动下线并告警 |
| Prompt 回归集 | promptfoo（或 pytest 子集）: 20-30 条真实用例（例行公告、低价值、跨栏重复、日期偏差） | 改 prompt 必须跑回归并附报告 |
| 语义去重 | 候选级 MinHash-LSH（datasketch）+ 实体重合度；先不做向量库 | 5.3 表中 3 个漏检对至少命中 2 个 |

### P2（1 个月）

| 任务 | 做法 |
|---|---|
| LLM 可观测 | 评分/写作调用落 JSONL（model、tokens、latency、error_type）；可选 langfuse 自托管 |
| 投递渠道 | ntfy 或 Apprise（飞书/TG），日报发布后推送摘要+链接 |
| 抓取兜底 | firecrawl API 作为"解析失败源"的 fallback fetcher（仅失败时调用） |
| 中文 NLP | jieba/TextRank4ZH 用于标题去重与关键词；中文摘要兜底 |
| 双班次重启 | 改造 6 代码已回滚，待 P0/P1 稳定后按需重新启用 |

---

## 8. 风险与取舍

| 不做的事 | 原因 |
|---|---|
| Airflow/Prefect 等编排 | GH Actions + CF cron 已覆盖日更；引入编排对当前规模是负担 |
| 向量库（Milvus/pgvector） | 目前候选池 ~100 条/日，MinHash/embedding 小模型即可；无需库 |
| 自建前端/阅读器 | Pages + RSS 已满足；Folo/Reeder 等客户端成熟 |
| 多产品扩张 | 先把 news 的源健康与质量闭环做扎实 |
| 抓取架构重写 | RSSHub + custom + 兜底 fetcher 的组合已够用；改造成本大于收益 |

---

## 附录 A：已验证可用源（2026-09-16）

**官方一手（7）**：White House News / Presidential Actions、U.S. State Department、Dept of Defense、SCOTUSblog、SEC、Federal Register

**中文（RSSHub 6 + 直连 3 + AI 1）**：FT中文×2、联合早报、观察者网、量子位、36氪、华尔街见闻（RSSHub）；财新国际×3（直连+URL 过滤）；AIHOT

**替代兜底（Google News `site:`，实测 100 条/次）**：reuters.com、apnews.com、cnn.com、axios.com

## 附录 B：方法与验证方式

- 调研：agent-reach（GitHub 搜索/API）+ 逐 URL curl 实测（HTTP 状态、条目数、最新 pubDate）
- 数据：GitHub Actions 跑批日志（run 35086209234 / 35087494366 / 35089672304）、状态库 `news-data` 分支 SQLite、`docs/news/metrics/latest.json`
- 本地验证：RSSHub 容器（diygod/rsshub，localhost:1200）+ 直连源实测
- 实验：标题相似度 difflib 对比（5.3）、源 HTTP 状态抽查（5.1）
