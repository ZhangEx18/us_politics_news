# 四维日报 v3 实施方案：分栏长文引擎

## 目标

从"单篇 5k-10k 字"升级为"四栏目各 5k-10k 字，全文 20k-40k 字"。
Reader 每天看到一篇完整长文日报，四栏目结构固定，格式干净。

## 当前阻塞问题

| P | 问题 | 根因 | 解法 |
|---|------|------|------|
| P0 | Reader 格式乱（`**`裸露、链接未渲染） | `_md_to_html` 正则太简陋 | 引入 mistune 或直接结构化生成 HTML |
| P0 | 正文不按四栏目稳定输出 | 模型自由发挥，代码无校验 | 内容协议定死 + 代码模板生成 |
| P0 | 没有云端抓取 | 本机网络受限 | GitHub Actions 定时抓取 |
| P1 | digest 上下文过长导致截断 | 一次塞 28 条进单个 prompt | 分栏分批生成 |
| P1 | prompt 里"经济财经"与"财经脉动"不一致 | 命名漂移 | 统一修正 |

## 架构变更

### 旧流程（一次性生成）
```
抓取 → 评分 → 事件合并 → 配额选择 → 1次 digest 整篇 → 保存
```

### 新流程（分栏生成 + 代码组装）
```
抓取 → 评分 → 事件合并 → 按四栏分桶
  → 每栏筛出 8-15 候选 → 每栏单独 digest 5000-10000 字
  → 生成总标题/导语/highlights
  → 代码模板组装总稿 → 输出 HTML + RSS
```

## 一、配置变更

### config/config.yaml

```yaml
output:
  daily_dir: "docs/daily"
  feed_path: "docs/feed.xml"
  base_url: ""

analysis:
  window_hours: 24
  history_context_days: 3
  min_llm_score: 65
  important_score: 85

llm:
  provider: openai
  model: mimo-v2.5-pro
  base_url: "https://token-plan-cn.xiaomimimo.com/v1"
  max_concurrent: 5
  max_prompt_chars: 120000
  timeout_seconds: 180

digest:
  language: zh
  column:
    target_word_count_min: 5000
    target_word_count: 7000
    target_word_count_max: 10000
  total:
    target_word_count_min: 20000
    target_word_count: 28000
    target_word_count_max: 40000
  columns:
    us_politics:
      label: "美国政情"
      min_items: 6
      target_items: 8
      max_items: 10
    global_affairs:
      label: "国际风云"
      min_items: 6
      target_items: 8
      max_items: 10
    technology:
      label: "科技前沿"
      min_items: 6
      target_items: 8
      max_items: 10
    economy:
      label: "财经脉动"
      min_items: 6
      target_items: 8
      max_items: 10
  total_min_items: 24
  total_target_items: 32
  total_max_items: 40

feed:
  include_full_content: true
  keep_days: 30
  stable_daily_guid: true
  description_max_chars: 300

storage:
  db_path: "data/news.db"
  retention_days: 30

runtime:
  require_ai: true
  proxy_from_env: true
```

## 二、内容协议（代码模板生成，不由 LLM 直接输出）

### 正文结构

```markdown
---
title: "美伊协议全文曝光，G7联手去中国化，美联储暗示降息"
lead: "150-300 字跨栏目信号总结..."
highlights:
  - "美伊14点协议全文公开，涉核与制裁"
  - "G7达成关键矿产去中国化路线图"
  - "美联储暗示年内降息两次"
date: "2026-06-18"
---

## 一、美国政情

### 1. 事件标题

**核心事实**：2-4 句客观陈述。

**背景与影响**：1-2 句来龙去脉。

**为什么值得关注**：1 句点明意义。

**相关阅读**：
- [来源名1](链接1)
- [来源名2](链接2)

---

## 二、国际风云

### 1. ...

---

## 三、科技前沿

### 1. ...

---

## 四、财经脉动

### 1. ...
```

### LLM 输出契约（结构化 JSON，不是自由 Markdown）

**单栏 digest 输出**：
```json
{
  "events": [
    {
      "title_zh": "中文标题",
      "core_facts": "核心事实 2-4 句",
      "background_impact": "背景与影响 1-2 句",
      "why_it_matters": "为什么值得关注 1 句",
      "source_links": [{"title": "来源名", "url": "https://..."}],
      "is_followup": false
    }
  ]
}
```

**总导语 digest 输出**：
```json
{
  "title": "短标题 8-30 字",
  "lead": "150-300 字导语",
  "highlights": ["重点1", "重点2", "重点3"]
}
```

## 三、代码改动清单

### 3.1 src/ai_analyzer.py

**新增 `generate_column_digest`**：
- 输入：单栏目 8-15 候选事件 + 历史上文
- 输出：结构化 JSON 数组
- 每条含 title_zh/core_facts/background_impact/why_it_matters/source_links
- 普通事件 150-250 字，重点事件 300-500 字
- 总字数 5000-10000

**新增 `generate_meta_digest`**：
- 输入：四栏各自的标题和前 3 条事件
- 输出：title + lead + highlights
- 只做总编排，不重复正文

**删除旧 `generate_digest`**：被上述两个函数替代

### 3.2 src/report_renderer.py

**新增 `render_structured_html`**：
- 直接从结构化数据生成干净 HTML
- 不经过 Markdown → HTML 转换
- 每个事件渲染为 `<div class="event">` 含 h3/facts/impact/why/links

**新增 `render_structured_markdown`**：
- 从结构化数据生成 Markdown

**删除 `_md_to_html`**：不再需要

**更新 `save_daily_report`**：
- 签名改为 `(meta, columns, output_dir)`

### 3.3 src/feed_builder.py

- description 只放 highlights 拼接，< 300 字
- content:encoded 放完整 HTML（render_structured_html 输出）

### 3.4 src/run_pipeline.py

- 新增 `--fetch-only` 和 `--digest-only` 模式
- 流程改为分栏生成：
  1. 抓取 → 评分 → 事件合并
  2. 按四栏分桶
  3. 每栏 8-15 候选
  4. 每栏单独 generate_column_digest
  5. generate_meta_digest 生成总导语
  6. 代码模板组装 + 保存

### 3.5 新增依赖

```
mistune>=3.0
```

### 3.6 发布前校验

```python
def validate_output(meta, columns, html):
    errors = []
    required = ["us_politics", "global_affairs", "technology", "economy"]
    for col in required:
        if col not in columns or not columns[col]:
            errors.append(f"栏目 {col} 为空")
    if "**" in html:
        errors.append("HTML 残留 ** 加粗语法")
    if "](http" in html:
        errors.append("HTML 残留 Markdown 链接语法")
    return errors
```

## 四、云端部署

### GitHub Actions: fetch job（每 2 小时）

```yaml
name: Fetch News
on:
  schedule:
    - cron: '0 */2 * * *'
  workflow_dispatch:
jobs:
  fetch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: python3 src/run_pipeline.py --fetch-only
        env:
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
```

### GitHub Actions: digest job（每天 8:00）

```yaml
name: Daily Digest
on:
  schedule:
    - cron: '0 8 * * *'
  workflow_dispatch:
jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: python3 src/run_pipeline.py --digest-only
        env:
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
      - uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./docs
```

## 五、实施顺序

| 步骤 | 内容 | 文件 |
|------|------|------|
| 1 | config 升级 | config/config.yaml |
| 2 | 分栏 digest 函数 | ai_analyzer.py |
| 3 | 结构化渲染 | report_renderer.py + mistune |
| 4 | 分栏 pipeline | run_pipeline.py |
| 5 | feed 短摘要 | feed_builder.py |
| 6 | 发布校验 | run_pipeline.py |
| 7 | 云端 fetch | .github/workflows/fetch.yml |
| 8 | 云端 digest | .github/workflows/digest.yml |

## 六、验收标准

- [ ] Reader 里无 `**`、无裸 Markdown 语法
- [ ] 正文严格包含 `## 一、美国政情` / `二、国际风云` / `三、科技前沿` / `四、财经脉动`
- [ ] 每栏目 6-10 条事件，每条有核心事实/背景/为什么值得关注/相关阅读
- [ ] 全文 20000-40000 字
- [ ] RSS description < 300 字，content:encoded 为完整 HTML
- [ ] GitHub Actions 每 2 小时抓取，每天 8:00 出刊
