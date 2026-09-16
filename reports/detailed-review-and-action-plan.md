# 线上日报逐条审查 + 详细可执行方案

- 审查对象：2026-09-16 线上已发布日报（旧 prompt 产出，21 段详细解析 + 21 条要点）
- 本报告目标：逐条列出问题 → 归纳失效模式 → 给出可直接执行的改造方案（含代码/prompt 片段与验收）
- 相关文件：`src/ai_analyzer.py`、`src/fetchers.py`、`src/report_engine.py`、`config/glossary.yaml`(新建)

---

## 一、逐条审查（详细解析部分）

标注：🔴 严重（事实/规范错误）· 🟠 中等（写作质量）· 🟡 轻微

### 美国政局

| # | 问题句（原文） | 级别 | 问题类型 |
|---|---|---|---|
| 政1 | "此次再次表决把国会与白宫在对伊朗动武权限上的分歧重新推到台面" | 🟠 | 元评论（无事实） |
| 政1 | "《卫报》称这次行动具有象征性，并提到中期选举前可能是最后一次" | 🟠 | 引用分析观点当新闻事实 |
| 政1 | "后续影响集中在国会战争权、投赞成票的共和党人以及中期选举前是否还有再次表决空间" | 🟠 | 推断（无归因） |
| 政2 | "马西把弹劾条款提交到众议院层面，使赫格塞思成为国会战争权争议中的直接问责对象" | 🟠 | 同义反复 + 定性 |
| 政2 | 首句"9月16日…推动投票"与次句"PBS 在 9月15日称…提出条款" | 🟡 | 同一事实两个日期，时间线混乱 |
| 政3 | "选举管理进入更清晰的执行口径" | 🟠 | 空话 |
| 政4 | "分歧点在于监管法案是否应同时约束特朗普相关投资" | 🟠 | 推断 |
| 政5 | "该行动把执法焦点放在支付处理环节，指向为欺诈商户提供支付通道的行为" | 🟠 | 同义反复 |
| 要点 | "尽管有警告，特朗普为何仍全力押注人工智能" | 🔴 | 观点稿混入硬新闻 |
| 要点 | "美国 FTC 发布汽车经销商价格透明度常见问题解答" | 🔴 | 例行公告残留 |
| 要点 | "Noura Erakat / Israel Bonds / RFFA" | 🟠 | 专名未中文化 |

### 国际局势

| # | 问题句（原文） | 级别 | 问题类型 |
|---|---|---|---|
| 国1 | "这一召见把争议直接抬到外交层面" | 🟠 | 元评论 |
| 国1 | "此前两国在该海域的互动依据双边协议处理" | 🟡 | 无来源背景补充 |
| 国2 | **"同一日 Google News 聚合条目也收录了这条 BBC 稿件"** | 🔴 | **管道元数据泄漏进正文** |
| 国3 | "这次表态把太空军事部署从外界推测转为公开确认的说法" | 🟠 | 拗口元评论 |
| 国4 | "此次讨论若落实，将改变相关商品跨境流转的成本安排" | 🟠 | 条件句预测 |
| 国5 | "报道提到特朗普曾称这场战争是'小事一桩'" | 🔴 | **引语无出处（哪家媒体/何时）** |
| 国5 | "这份估算把军费规模摆到台面上" | 🟠 | 元评论 |
| 国6 | "把焦点放在俄情报机构的行动链条上" / "案件进展取决于法院" | 🟠 | 元评论 + 废话 |
| 国7 | "这次警告把美方 AI 模型的网络攻击潜力直接列为公开提及的对象" | 🟠 | 拗口元评论 |
| 国8 | "这次倒塌把当地救援需求集中到废墟搜救环节" | 🟠 | 元评论 |
| 国8 vs 要点 | 死亡数字 **20 vs 16** 冲突未标注 | 🔴 | 多源数字冲突未合并 |
| 国9 | "这次把人员迁移一并提出" / "若该方向转为实际安排…" | 🟠 | 元评论 + 预测 |
| 要点 | "欧盟领导人支持加拿大成为准成员国" + "冯德莱恩提议设立欧洲安全理事会并加强与加拿大伙伴关系" | 🟠 | 同一事件拆两条 |

### 科技前沿

| # | 问题句（原文） | 级别 | 问题类型 |
|---|---|---|---|
| 科1 | "此前平台广告收费安排由平台自行设定" | 🟠 | 编造背景 |
| 科2 | 与美国政局#4 为同一事件 | 🔴 | 跨栏重复 |
| 科2 | "报道把这一呼吁与美国立法受挫直接挂钩，讨论焦点从…转向…" | 🟠 | 元评论 |
| 科3 | "此次被证实的是三家前沿实验室之间的持续沟通" | 🟠 | 同义反复 |
| 科4 | "报道把这一动作定位为对 OpenAI 的追赶" / "此次公布的是新设据点的计划，把区域布局落到具体城市的办公室" | 🟠 | 元评论 + 同义反复 |
| 科5 | "此次发布为最终版本，制定流程到此收尾" / "适用于需要管理用户身份的机构及其安全团队" | 🟠 | 废话 + 推断 |
| 要点 | "Google 发布 AI 与经济 ATLAS v1.0，提供 AI 对经济影响的新洞察" | 🟠 | 公关语（"新洞察"） |
| 要点 | `&quot;Deep Think Mathematica&quot;` | 🔴 | **HTML 转义泄漏（双重编码）** |

### 经济走势

| # | 问题句（原文） | 级别 | 问题类型 |
|---|---|---|---|
| 经1 | "此次上升由能源成本推动，直接构成英国央行政策决定前的物价背景" | 🟠 | 同义反复 |
| 经1 | "次日央行政策决定将作用于其融资与借贷环境" | 🟠 | 推断 |
| 经2 | "此次变化集中在以五年周期规划金融中心定位" / "香港金融机构和企业将面对该计划如何转化为具体执行措施的下一步" | 🟠 | 同义反复 + 废话 |
| 要点 | "美国 8 月零售销售数据强劲反弹…显示消费者…依然具备韧性" | 🟠 | 评论（"显示"）+ 无来源 |

---

## 二、失效模式归纳（10 类，带出现次数）

| # | 模式 | 次数 | 典型句式 | 根因 |
|---|---|---|---|---|
| M1 | 元评论（描述"报道怎么写的"而不是"发生了什么"） | **≥12** | "报道把…定位为…""讨论焦点从…转向…""把…列为…对象" | 旧 prompt 的"变化/后果"结构 + 无禁止清单 |
| M2 | 同义反复 | **≥6** | "此次发布为最终版本，制定流程到此收尾""此次被证实的是…" | 无"每句必须新信息"规则 |
| M3 | 推断/条件预测 | **≥8** | "若…将…""接下来影响…""将作用于…" | 旧 prompt 鼓励写"后果" |
| M4 | 元数据泄漏 | 1 | "Google News 聚合条目也收录了这条 BBC 稿件" | 输入含 source_links/来源信息，无禁止规则 |
| M5 | 引语无出处 | 1 | "特朗普曾称…'小事一桩'" | 无引语规范 |
| M6 | 多源数字冲突 | 1（20 vs 16） | — | 事件合并未覆盖 |
| M7 | 跨栏重复 | 1（加密法案） | — | 合并只在同栏做 |
| M8 | 专名未中文化 | ≥5 | Noura Erakat / Clarity Act / RFFA / Israel Bonds / Deep Think | 无术语表 |
| M9 | HTML 转义泄漏 | 1 | `&amp;quot;` | `fetchers.py:309/473` 未 `unescape()` |
| M10 | 低价值残留 | ≥3 | 观点稿 / 例行公告 / 公关语 | 评分与要点筛选规则 |

注：M1-M3 是**旧 prompt 的必然产物**——P0 已改（2026-09-16 上线），但尚未在线上跑过一整份日报；M4-M10 是 P0 未覆盖的，需要本方案补齐。

---

## 三、方案：5 个工作包（WP）

### WP1 写作规范补丁（0.5 天）

**目标**：消除 M1/M2/M3/M4/M5，与 P0 倒金字塔改造配套。

**改动 1：DIGEST prompt 禁止清单新增**（`ai_analyzer.py` COLUMN_DIGEST）

```
**禁止的元评论句式**（描述报道本身而非事实）：
"报道把…定位为…""讨论焦点从…转向…""这次表态把…转为…""把…列为…对象"
"此次被证实的是…""此次公布的是…""这一发布/表态把…"

**每句必须提供新信息**：不得复述导语已写过的事实；禁止同义反复
**禁止管道信息**：不得提及"聚合条目/收录/转载/抓取/来源层级"等系统或采集信息
**引语规范**：直接引语必须标明出处（媒体名+时间）；没有出处的引语不得加引号
**禁止条件句预测**："若/如果…将…" 一律不写；只写材料已说明的影响
```

**改动 2：内容审计新增 2 项检测**（`report_engine.py _audit_daily_content`）

```python
"meta_commentary": 命中正则 r"(报道把|讨论焦点|这次表态把|此次被证实|此次公布的是|把.{0,6}列为.{0,6}对象)"
"pipeline_leak": 命中 r"(聚合条目|收录了这条|抓取|来源层级|转载自)"
```

**改动 3：要点低价值过滤**（`report_engine.py` 或 `run_pipeline.py` 选择层）

```python
_OPINION_TITLE_RE = re.compile(r"(为何|为什么|如何|解读|观察|分析|盘点|展望|一文看懂)")
_PROMO_WORD_RE = re.compile(r"(新洞察|赋能|重磅|颠覆|引爆)")
# 命中 → 不进入要点（headline_only），记录 opinion_dropped 指标
```

**验收**：抽 10 条新日报：meta_commentary=0、pipeline_leak=0、同义反复句=0、引语均带出处。

### WP2 JSON Schema 强约束（1 天）

**目标**：消除"推理文本混入 content 导致整批解析失败"与字段漂移。

**改动**（`ai_analyzer.py`）：

```python
SCORE_ITEM_SCHEMA = {
  "type": "object",
  "required": ["link","score","column","is_hard_news","event_key","freshness_status",
               "summary","impact","prominence","timeliness","novelty","conflict","routine"],
  "properties": {
    "link": {"type": "string"},
    "score": {"type": "integer", "minimum": 0, "maximum": 100},
    "column": {"enum": ["us_politics","global_affairs","technology","economy"]},
    "is_hard_news": {"type": "boolean"},
    "impact": {"type": "number", "minimum": 0, "maximum": 1},
    # …其余同构
  }
}

def _build_llm_payload(prompt, config):
    payload = {...}
    if config.get("json_schema"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": config["json_schema"], "strict": True,
                            "schema": SCHEMAS[config["json_schema"]]},
        }
    return payload
```

**降级策略**：provider 返回 400（不支持 json_schema）→ 自动重试一次不带 `response_format`，命中即写 `schema_fallback=1` 指标。

**校验**：解析后过一遍 `_validate_schema(item)`，违规记 `schema_violation` 并走现有清洗路径。

**验收**：JSON 解析失败率 <0.5%（当前偶发整批失败）；字段缺失率 0。

### WP3 glossary 术语表（0.5 天）

**目标**：消除 M8。

**新建 `config/glossary.yaml`**（首批 ~30 条）：

```yaml
people:
  特朗普: [Trump, Donald Trump]
  赫格塞思: [Hegseth, Pete Hegseth]
  马西: [Massie, Thomas Massie]
  冯德莱恩: [von der Leyen, Ursula von der Leyen]
  习近平: [Xi Jinping]
  李家超: [John Lee]
orgs:
  美国联邦贸易委员会: [FTC]
  美国证券交易委员会: [SEC]
  美国国家标准与技术研究院: [NIST]
  最高法院: [Supreme Court, SCOTUS]
  美联储: [Federal Reserve, Fed]
  北约: [NATO]
  欧盟委员会: [European Commission]
laws:
  清晰法案: [Clarity Act]
medias:
  半岛电视台: [Al Jazeera]
  南华早报: [South China Morning Post, SCMP]
  纽约时报: [New York Times, NYT]
```

**注入**（`ai_analyzer.py`）：在 DIGEST / HEADLINE_TRANSLATION prompt 末尾追加命中条目：

```
## 术语对照（必须使用中文译名）
霍格塞思 = Hegseth；清晰法案 = Clarity Act；…
```

**审计**（`report_engine.py`）：新增 `untranslated_terms`：正文命中 glossary 英文原词且未出现中文译名 → 计数。

**验收**：日报中"白名单外英文专名"=0；同一日报同名实体译法一致。

### WP4 事件合并增强（1 天）

**目标**：消除 M6/M7。

**改动 1：跨栏合并**（`ai_analyzer.merge_events`，现有 event_key+标题相似度 0.85）
- 新增规则：`event_date` 相同 + 共享实体 ≥2（人/机构/法案词的规范化命中）→ 合并
- 合并后 `column` 保留主轴栏目（更高分或非 technology 优先）

**改动 2：数字冲突处理**（`report_engine`）
- 合并同事件后，若 source_links 来源对同一指标数字不一致 → 正文规则（DIGEST prompt 增补）：

```
同一事件多来源数字不一致时：主来源数字写正文，另一数字写成"另有报道为 X"，不得只保留一个
```

**验收**：加沙类数字冲突在成品中同时出现两个数字并标注来源；跨栏重复 = 0。

### WP5 回归集 + 转义修复（1 天）

**目标**：防止回归；消除 M9。

**改动 1：转义修复**（`fetchers.py:309` RSSFetcher / `:473` GoogleNewsFetcher）

```python
title = unescape(entry.get("title", "")).strip()
content = unescape(_extract_content(entry))
```

**改动 2：回归集**（新建 `tests/fixtures/prompt_cases.yaml` + `scripts/prompt_regression.py`）

```yaml
- id: routine_notice        # 例行公告必须低分
  type: score
  expect: {score_max: 69, is_hard_news: false, routine_min: 0.8}
- id: major_ruling          # 重大裁决必须高分
  type: score
  expect: {score_min: 85, newsworthiness_min: 0.8}
- id: digest_no_meta_commentary   # 不得出现元评论
  type: digest
  expect: {banned_regex: ["报道把", "讨论焦点", "此次被证实"]}
- id: digest_one_fact_per_sentence  # 无超长句
  type: digest
  expect: {max_sentence_chars: 60}
- id: digest_conflicting_numbers    # 数字冲突需标注
  type: digest
  expect: {must_contain_any: ["另有报道", "另有说法"]}
- id: translate_glossary            # 术语必须中文化
  type: translate
  expect: {must_contain: ["赫格塞思", "清晰法案"]}
- id: fallback_short                # 短摘要兜底
  type: fallback
  expect: {min_chars: 50, has_date: true, no_latin: true}
```

检查器（硬指标，不依赖 LLM judge）+ `--live` 模式（约 25 次调用，<$0.1）。

**验收**：改 prompt / 改 schema 必跑；5 项指标全绿（见第四节）。

---

## 四、总验收指标

| 指标 | 现状（本报告实测） | 目标 | 由谁解决 |
|---|---|---|---|
| 元评论句 | ≥12 处 | 0 | WP1 |
| 同义反复句 | ≥6 处 | 0 | WP1 |
| 条件预测句 | ≥8 处 | 0 | WP1（P0+WP1） |
| 管道元数据泄漏 | 1 处 | 0 | WP1 |
| 引语无出处 | 1 处 | 0 | WP1 |
| 超长句（>60 字） | 15 句 | ≤5% | P0 已改（待线上验证） |
| 跨栏重复 | 1 处 | 0 | WP4 |
| 数字冲突未标注 | 1 处 | 0 | WP4 |
| 专名未中文化 | ≥5 处 | 0（白名单外） | WP3 |
| 转义泄漏 | 1 处 | 0 | WP5 |
| JSON 整批解析失败 | 偶发 | <0.5% | WP2 |
| 例行公告上榜 | 1 处 | 0 | P0 已改（待线上验证） |

---

## 五、实施顺序与风险

```
WP1 写作规范补丁 ─┐
WP5-1 转义修复   ─┼─→ 可立即上线（低风险，纯规则/一行修复）
WP3 glossary     ─┘        ↓
WP2 Schema 强约束 ─→ 需实测 provider 支持度，保留降级开关
WP4 事件合并增强  ─→ 改动核心合并逻辑，需回归验证不加重复
WP5-2 回归集      ─→ 最后收口，锁住前四项
```

| 风险 | 缓解 |
|---|---|
| Schema 不被 provider 支持 | 自动降级 + `schema_fallback` 指标 |
| glossary 注入变长 prompt | 仅注入命中条目（≤15 条），控制在 400 字内 |
| 合并增强导致误合并 | 新规则先 shadow 模式（只记录指标不改输出）跑 3 天再启用 |
| 观点稿过滤误杀 | 白名单：标题含"分析"但正文为首发事实时放行（先记录指标观察） |

**建议排期**：WP1+WP5-1（今天）→ WP3（明天）→ WP2（后天）→ WP4（周末）→ WP5-2（下周一）。
