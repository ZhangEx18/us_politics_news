# 线上日报整体审查 + P1 机制方案

- 审查对象：`https://zhangex18.github.io/us_politics_news/news/daily/2026-09-16.md`（线上已发布版，21 段详细解析 + 21 条要点）
- 生成时点：P0 Prompt 优化**之前**（旧"事实→变化→后果"模板 + 旧评分 prompt）→ 本报告同时验证了 P0 改造的必要性
- 附：`JSON Schema 强约束` / `glossary 术语表` / `prompt 回归集` 三个 P1 机制的设计方案

---

## 一、量化体检（21 个详细段落）

| 指标 | 结果 | 判定 |
|---|---|---|
| 段落句数分布 | **3 句 ×7 / 4 句 ×14（100% 都是 3-4 句）** | 旧模板的机械产出 |
| 模板词「此前」 | **15 次（71% 段落）** | 模板化 |
| 模板词「此次」 | **12 次（57% 段落）** | 模板化 |
| 「受影响/后续影响」句式 | 6 次 | 推断句 |
| 超长句（>60 字） | **15 句（约 20%）** | 违反"一句一事实" |
| 中英混杂实词 | OpenAI×9、FTC×8、Anthropic×5、Google×4… + **Noura Erakat / Clarity Act / Israel Bonds / RFFA / Deep Think** | 专名未中文化 |
| HTML 转义泄漏 | `&amp;quot;Deep Think Mathematica&amp;quot;` | **渲染 bug** |
| 跨栏同事件 | 加密法案（美国政局#4 + 科技#2 + 要点）3 处 | 事件合并未跨栏 |
| 同事件数字冲突 | 加沙倒塌「至少 20 人死」（国际#8）vs「至少 16 人死」（要点） | 未合并/未标注分歧 |

---

## 二、逐条问题清单（按类型）

### A. 结构问题（倒金字塔未执行）

| # | 原文片段 | 问题 |
|---|---|---|
| A1 | "此前该决议已两次在众议院通过，此次再次表决把国会与白宫在对伊朗动武权限上的分歧重新推到台面。" | 「把……推到台面」是评论，不是事实 |
| A2 | "马西把弹劾条款提交到众议院层面，使赫格塞思成为国会战争权争议中的直接问责对象。" | 「使……成为」是定性 |
| A3 | "这一召见把争议直接抬到外交层面。" | 评论 |
| A4 | "该行动把执法焦点放在支付处理环节，指向为欺诈商户提供支付通道的行为。" | 同义反复 + 评论 |
| A5 | "此次发布为最终版本，制定流程到此收尾。" | 无信息量填充 |
| A6 | "案件进入司法程序后的进展取决于法院。" | 废话收尾 |
| A7 | "接下来影响国防部长职位、共和党内部对伊朗动武授权的分歧，以及众议院是否推进相关投票。" | 无归因推断 |

### B. 内容价值问题

| # | 原文片段 | 问题 |
|---|---|---|
| B1 | 要点："尽管有警告，特朗普为何仍全力押注人工智能" | 观点/分析稿混入硬新闻要点 |
| B2 | 要点："美国 FTC 发布汽车经销商价格透明度常见问题解答" | 例行公告（P0 前入库的存量） |
| B3 | 要点："Google 发布 AI 与经济 ATLAS v1.0，提供 AI 对经济影响的新洞察" | 公关语（"新洞察"） |
| B4 | 科技#2 "香港业界呼吁抓住战略窗口" 与 美国政局#4 加密法案 | 同一事件跨栏重复 |
| B5 | 国际#8 与 要点加沙死亡数字 20 vs 16 | 同事件数字冲突未合并 |

### C. 语言规范问题

| # | 原文片段 | 规范 |
|---|---|---|
| C1 | `&amp;quot;Deep Think Mathematica&amp;quot;` | 转义 bug（双重编码） |
| C2 | "人权律师 Noura Erakat 在佛州反对 Israel Bonds 发言后被捕" | 人名/组织未中文化 |
| C3 | "美国参议院否决了 Clarity Act 加密法案" | 法案名未中文化 |
| C4 | "堕胎权组织 RFFA 投入约 100 万美元" | 缩写未展开 |
| C5 | "美国以涉嫌歧视白人为由限制南非官员签证" | "涉嫌"未标明是美方口径（应写"美方称"） |
| C6 | 多处 "PBS NewsHour / France24 / The Record / Business Insider Africa" | 媒体名未中文化（可接受，但应统一规则） |

### D. 归因问题

| # | 原文片段 | 问题 |
|---|---|---|
| D1 | 国际#3 "美国首次承认已在太空部署武器" | 重大主张仅"报道没有给出……"；导语应带归因（标题已带，正文首句应"据 NPR 报道"） |
| D2 | 国际#5 "报道提到特朗普曾称这场战争是"小事一桩"" | 引语无原始出处（哪家媒体、何时） |

---

## 三、三个 P1 机制：问题映射与设计

### 3.1 JSON Schema 强约束

**要解决的问题（今天实测过的）**：
- 评分批次出现 `无法从评分响应中解析 JSON: We need answer pure JSON...`（推理模型把思考写进 content 且被截断）→ 整批 0 匹配
- 字段漂移：模型偶发漏字段（`newsworthiness` 缺失导致门禁放行）
- 现有兜底是 `_parse_jsonish_object` + 正则清洗，属于"事后补救"

**设计**：

```python
# ai_analyzer 统一 schema（摘要）
SCORE_SCHEMA = {
  "type": "object",
  "required": ["items"],
  "properties": {"items": {"type": "array", "items": {
    "type": "object",
    "required": ["link","score","column","is_hard_news","event_key","freshness_status",
                 "summary","impact","prominence","timeliness","novelty","conflict","routine"],
    "properties": {
      "score": {"type":"integer","minimum":0,"maximum":100},
      "column": {"enum":["us_politics","global_affairs","technology","economy"]},
      "impact": {"type":"number","minimum":0,"maximum":1}, ... 
    }}}}
```

调用侧：
```python
payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "score_items", "schema": SCORE_SCHEMA, "strict": True}}
```
- 兼容性：OpenCode Zen Go 的 deepseek/glm 走 OpenAI 兼容协议，需实测各模型支持度；**不支持时自动降级**为现有 `json_object`+解析（保留兜底）
- 校验：解析后用 pydantic 校验并记录 `schema_violation` 指标（可观测）
- 预期收益：JSON 解析失败率 → <0.5%；字段缺失 → 0（required 强制）

**工作量**：1 天（schema 定义 + 注入 + 降级开关 + 指标）

### 3.2 glossary 术语表

**要解决的问题（本次审查实测）**：C2-C4 中英混杂、译名可能不一致（如"赫格塞思"vs"赫格塞特"、"马西"vs"Massie"）、法案名直译缺失。

**设计**：`config/glossary.yaml`

```yaml
people:
  特朗普: [Trump, Donald Trump]
  赫格塞思: [Hegseth]
  冯德莱恩: [von der Leyen]
  马西: [Massie]
  习近平: [Xi Jinping]
orgs:
  美国联邦贸易委员会: [FTC]
  美国证券交易委员会: [SEC]
  美国国家标准与技术研究院: [NIST]
  最高法院: [Supreme Court, SCOTUS]
  美联储: [Federal Reserve, Fed]
laws:
  清晰法案: [Clarity Act]
medias:
  半岛电视台: [Al Jazeera]
  南华早报: [South China Morning Post, SCMP]
```

注入方式：
1. **DIGEST/TRANSLATION**：prompt 末尾附"术语对照（必须使用中文译名）"片段（仅注入命中的条目，控制 prompt 长度）
2. **后置检查**：`_audit_daily_content` 增加 `untranslated_terms` 计数（正文命中 glossary 的英文原词且无对应中文 → 警告）
3. 新词补录流程：审查日报时把新出现的专名补进 glossary（手工，每周一次）

**工作量**：0.5 天（结构 + 注入 + 审计项；内容持续补充）

### 3.3 prompt 回归集

**要解决的问题**：P0 改完 prompt 只做了 2 例人工抽检；`max_tokens` 截断问题靠手动发现；模板化问题直到人工通读才暴露。

**设计**：`tests/fixtures/prompt_cases.yaml` + `scripts/prompt_regression.py`

```yaml
# 用例结构
- id: routine_notice
  type: score
  input: {title: "FTC Seeks Public Comment...", content: "..."}
  expect: {score_max: 69, is_hard_news: false, routine_min: 0.8}
- id: major_ruling
  type: score
  input: {title: "Supreme Court strikes down...", content: "..."}
  expect: {score_min: 85, newsworthiness_min: 0.8}
- id: digest_three_sentence
  type: digest
  input: {events: [...]}
  expect: {body_sentence_max: 3, banned_phrases: [意味着, 凸显, 值得关注], must_have_date: true}
- id: digest_conflicting_sources
  type: digest
  input: {events: [同上事件两家媒体不同死亡数字]}
  expect: {must_keep_attribution: true}
- id: fallback_short_summary
  type: fallback_body
  expect: {min_chars: 50, has_date: true, no_english: true}
- id: translation_consistency
  type: translate
  input: ["Hegseth faces impeachment...", "Trump-Xi tariff talks"]
  expect: {must_contain: ["赫格塞思", "特朗普", "习近平"]}
```

检查器（不依赖 LLM judge 的硬指标优先）：
- 句数/字数/日期表达/禁用词/CJK 比例/英文词白名单
- 评分用例断言分数区间与 `routine`/`newsworthiness` 阈值（对应验收：例行 vs 重大差 ≥20 分）
- 每次改 prompt 必须跑：`python3 scripts/prompt_regression.py --live`（约 20-30 次调用，成本 <$0.1）

**验收指标（取自 P0 方案）**：
| 指标 | 目标 |
|---|---|
| 套话率（意味着/凸显/值得关注/后续影响） | 0 |
| 每句单事实（无 >60 字长句、无分号堆叠） | ≥90% |
| 导语含 Who+What+When | ≥90% |
| 评分区分度（例行 vs 重大） | ≥20 分 |
| JSON 解析失败率 | <0.5% |

**工作量**：1 天（用例 20-30 条 + 检查器 + 脚本）

---

## 四、实施顺序与依赖

| 顺序 | 任务 | 依赖 | 验收 |
|---|---|---|---|
| 1 | **JSON Schema 强约束** | 无 | schema 违规率 <0.5%，解析失败自动降级可观测 |
| 2 | **glossary 术语表**（结构+注入+审计） | 无 | 日报中英混杂项 = 0（白名单外）；同日报译名一致率 100% |
| 3 | **prompt 回归集**（含 P0 用例） | 1、2 完成后一起回归 | 上表 5 项指标全部达标 |
| 4 | 渲染转义修复（C1，独立小修） | 无 | `&amp;quot;` 不再出现 |

**注**：C1（HTML 转义泄漏）是独立 bug，建议随本批次一起修：定位 `report_renderer`/`_build_item_xml` 的二次转义路径，加回归用例。

---

## 五、本报告结论

1. 线上日报当前的主要问题不是"信息不足"，而是**写作模板化 + 评论混入 + 专名未中文化**——三者都已在 P0 或本方案的 P1 中有对应机制。
2. P0 优化（已上线）预期直接消除 A 类问题与 71% 的「此前/此次」模板句；B 类（跨栏重复/数字冲突）依赖事件合并与配额，本轮已部分覆盖，B1/B2/B3 需持续用源配额与价值门槛治理。
3. C1 转义与 C2-C4 中英混杂需要代码与 glossary 两个动作，建议本轮一并完成。
