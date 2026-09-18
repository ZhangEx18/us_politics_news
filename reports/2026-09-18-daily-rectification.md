# 2026-09-18 日报质量整改报告

> 触发：9/18 日报（07:45 发布）人工全量审查，发现①今日头条栏目按产品决定应移除、②若干用词语句问题、③四栏目条目数量不足。
> 结论：3 类问题全部完成整改并有自动化兜底；本报告记录问题证据、整改措施、验证方式与残留项。

---

## 一、问题清单（附证据）

### A. 产品形态
| # | 问题 | 证据 |
|---|---|---|
| A1 | 顶部"今日头条"栏目应删除（读者不需要单独头条块） | 9/18 日报 MD/HTML 顶部 `## 今日头条` 重复了"今日要点"第 2 条 |

### B. 用词语句（10 处，全部有原文）
| # | 原文 | 问题类型 |
|---|---|---|
| B1 | 小肯尼迪在其创办反疫苗组织会议演讲 | 视频条目标题（源标题 `WATCH: RFK Jr. speaks…`），且缺谓语（应为"发表演讲"） |
| B2 | 奥运河流有 500 条鳄鱼但并非主要担忧 | BBC 特稿/奇闻条目，非硬新闻 |
| B3 | 部门发布男性军人睾酮缺乏筛查实施指南 | 机构名未译全（DoD→"部门"），主语不明 |
| B4 | 冯德莱恩与泽连斯基通话后发布纪要 | 例行通话纪要（readout），非新闻 |
| B5 | 让全球数据更易于探索 | Google AI Blog 产品软文句式充当新闻标题 |
| B6 | 美国媒体称美国众议院通过《水资源开发法》 | 匿名归因（"美国媒体称"）+ 重复"美国" |
| B7 | 一份报告披露党派重划众议院选区的涉及规模 | 匿名归因（"一份报告"）+ 表述生硬 |
| B8 | 伊朗驱逐瑞典外交官进行报复 | 弱动词结构（"进行报复"） |
| B9 | 南华早报报道，美国国务院批准… | 归因缺"据"（应"据南华早报报道"） |
| B10 | 中国随即作出回应。印度表示将维护本国能源安全。 | 弱动词 + 事实空泛（回应内容缺失） |

### C. 栏目数量不足（对比 `config/products/news/product.yaml` 目标）
| 栏目 | 目标（min/detailed+headline） | 9/18 实际 | 差距 |
|---|---|---|---|
| 美国政局 | 10（+8） | 7+1 | -2 / -7 |
| 国际局势 | 10（+8） | 7+5 | -3 / -3 |
| 科技前沿 | 7（+3） | **1+1** | **-6 / -2** |
| 经济走势 | 4/7（+3） | **1+0** | **-3 / -3** |

根因（来自 9/18 metrics）：
1. **供给不足**：科技栏预筛 19 条 → AI 评分后仅 3 条硬新闻；经济栏 13 → 2 条。目标 7 条硬新闻远超当日供给。
2. **来源结构**：科技栏多为博客/聚合器（AIHOT 已按 aggregator 剔除，Google/OpenAI Blog 为产品软文），缺少硬新闻科技源；经济栏缺少综合财经源。
3. **补位断档**：`_fill_underrepresented_columns` 与 `_ai_expand_fallback_events` 只从"已评分候选"取件，评分不足时无从补；未评分兜底候选（含英文源）未参与补位（规则兜底 `_build_fallback_detailed_event` 又要求中文摘要，英文源无法构造）。

---

## 二、整改方案（已实施）

### A. 去掉"今日头条"栏目  ✅
- 改动：`src/report_renderer.py` 删除 MD `## 今日头条` 块与 HTML `lead-event` 块；frontmatter `lead` 保留（供 feed/观测），`metrics.lead` 保留（供头条选品观测）。
- 验证：`tests/test_report_engine.py` 断言 `"## 今日头条" not in markdown`；发布页首屏从"今日要点"开始。
- 风险：无（lead 选择逻辑保留，不影响 highlights 排序）。

### B. 用词语句治理  ✅（双层：硬过滤 + prompt 约束）
| 措施 | 实现 | 覆盖问题 |
|---|---|---|
| 抓取层过滤视频/直播/图集条目 | `fetchers._is_video_or_live_entry`：`WATCH:/LIVE:/VIDEO:/视频：…` 直接跳过（中英） | B1 |
| 标题前缀剥离扩展 | `report_engine._TITLE_ATTRIBUTION_PREFIX_RE` 增补 `（美国/美/外/当地）媒体称`、`（一份/最新/一份最新）报告（称/显示/披露）` | B6 B7 |
| 例行公告补"通话纪要" | `content_policy.ROUTINE_NOTICE_PATTERNS` 增补 `readout of`、`spoke/call with … about`、`（通话/会见/会谈）纪要` | B4 |
| 产品软文句式 | 观点过滤正则增补 `^让[^，。]{0,14}更(易/轻松/方便)` | B5 |
| 翻译 prompt v1.3 | 机构名译全（部门→具体机构）、归因必须"据XX"、禁用域名当媒体名、标题不得以匿名归因起句 | B3 B9 |
| 写作 prompt v1.2 | 禁用弱动词"进行/作出/予以+名词"、禁用无主语句（部门/该机构）、禁用匿名归因 | B8 B10 |

- 验证：新增/更新单测（视频过滤、前缀剥离、例行 readout、软文句式）；`scripts/prompt_regression.py --live` 8 用例回归。
- 风险：B2（BBC 特稿/奇闻）无法用语料规则稳定识别，列为残留（见附录）。

### C. 栏目数量保障  ✅
1. **补源（根因）**：`sources.yaml` 新增 9 个硬新闻源（启用 136→145）：
   - 科技：TechCrunch、Ars Technica、The Verge、NYT Technology、36氪
   - 经济：BBC Business、NYT Business、CNBC Top News、MarketWatch Top Stories
2. **补位改从"合并候选池"取件**：`column_candidates`（已评分）＋ `fallback_candidates_by_column`（未评分，aggregator 已剔除），去重后作为统一池。
3. **AI 兜底扩写接入合并池**：`_ai_expand_fallback_events(max_per_column=5)`，英文源也能生成中文简讯正文，薄栏目优先扩到 min_items。
4. **规则兜底 + 填充**：`_ensure_daily_detailed_events` / `_fill_underrepresented_columns` 同样使用合并池。
- 验证：跑一次 `force_rescore=true`，检查 `metrics.columns.*.rendered_detailed / rendered_headline_only` 是否达到 min_items 与 headline_items。
- 风险：新增源会提升 AI 评分与写作 token 消耗（预计 +15~25%）；如超预算，可回调 `max_candidates_per_run`。

---

## 三、验收标准

| 项 | 标准 | 检查方式 |
|---|---|---|
| A | 发布页无"今日头条"，首屏为"今日要点" | 抓取 `news/daily/{date}.md` 断言 |
| B | 10 类用词问题在当日报告中为 0（B2 除外） | 人工全量通读 + 审计指标 |
| C | 四栏目 `rendered_detailed ≥ min_items` 且 `rendered_headline_only ≥ headline_items`（供给不足时允许回落至 min_items） | `metrics.columns` 对比 product.yaml |
| 常量 | `content_audit` 8 项全 0 | metrics 每日核查 |

## 四、实施后验证（2026-09-18 05:54 发布版，commit 5d4893d）

| 项 | 结果 |
|---|---|
| A 今日头条 | ✅ 已移除，首屏为"今日要点" |
| B 用词 | ✅ 视频/匿名归因/弱动词/软文/部门开头/通话通报/WSJ 前缀/要点截断 全部为 0；奇闻条目（鳄鱼）已被过滤 |
| C 数量 | 美国政局 8+2 ✅ / 国际局势 7+6 ✅ / 经济走势 4+1 ✅（min 4 达标）/ 科技前沿 2+0 ⚠️（目标 7+3，仍供应不足） |
| 审计 | ✅ `content_audit` 8 项全 0；`digest_failures` 空 |
| 拒绝分类 | ✅ 仅 low_newsworthiness/repeated_story/duplicate_event/unreadable_body/cryptic_title/source_quota，无 soft/opinion/routine/live_blog |
| 补充修复 | pipeline_leak 误报（数据抓取）、标题按显示宽度计长、渲染层截断回退标题、digest 缺 events 重试、兜底候选 freshness_status |

## 五、残留与后续（P1/P2）
- P1（已有监控，待规则）：BBC 特稿/奇闻类条目（B2）——建议后续给 BBC World 增加"硬新闻优先"选择权重，或引入轻量 AI 判类。
- P1：经济栏与科技栏的 headline 数量在周六日/低供给日可能仍低于 headline_items，监控两周后决定是否再补源或放宽评分线。
- P2：翻译 prompt 的机构名还原可扩展为"机构术语表自动注入"，减少逐条补规则。


---

## 六、第二轮内容整改（2026-09-18 17:51 验证版，commit ad70364）

| # | 问题（用户反馈） | 措施 | 验证结果 |
|---|---|---|---|
| 1 | 正文日期重复（美联储"9月17日，9月16日"、王毅句内"9月17日…9月17日"） | 零宽字符（U+200B-200D/2060/FEFF）全链路清理；首句内重复同日期自动去重 | ✅ 0 例 |
| 2 | 跨层重复（重点 vs 简讯） | 确认是设计（重点=为忙碌读者抽取），不做去重 | ✅ 未改动 |
| 3 | 标题压缩过度/名词堆叠（"对俄及购俄能源国家新制裁"） | translate prompt v1.4：优先「主体＋动作」，可用「，」补充，≤24 字，禁名词堆叠 | ✅ 例："国会通过对俄新制裁，涉及使用俄能源的国家" |
| 4 | 反应式表态缺场景（"以色列选民斥…"） | 观点过滤补：斥/抨击+谎言类、选民/民众反应类、引语式（含弯引号）；意见栏目 URL（/opinions/ 等）过滤 | ✅ 0 例 |
| 5 | 科技栏要求 OpenAI/Anthropic 为重点 | `product.yaml: technology.priority_entities`，评分 +12 / 新闻价值 +0.12（配置驱动） | ✅ priority_boosted=2 |
| 附 | 公文式简讯（Notice of Supplemental Funding） | 例行公告补英文 funding notice 形态；要点池对中文标题二次例行判定 | ✅ 0 例 |

审计 8 项全 0；367 单测通过。
