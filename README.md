# 美国政治新闻雷达

每日自动抓取、筛选、评分、分类，生成可在 Reader 中阅读的日报。

## 它做什么

每天自动完成一件事：从多个新闻源抓取内容，筛选出美国政治、国际局势、科技发展、经济发展四个维度的重点新闻，生成结构化日报和 RSS Feed，供 Reader 订阅阅读。

## 快速开始

```bash
# 安装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 配置环境变量（可选）
cp .env.example .env
# 编辑 .env 填入 NEWSAPI_KEY、TIANAPI_KEY

# 运行
python3 src/run_pipeline.py
```

生成的日报在 `docs/daily/`，RSS Feed 在 `docs/feed.xml`。

## Pipeline 流程

```
并发抓取 → 跨源去重 → 评分 → 语义去重 → 均衡摘要 → 生成日报 → 生成 Feed
```

| 步骤 | 说明 |
|------|------|
| 并发抓取 | 6 个抓取器异步并发，统一返回 ContentItem |
| 跨源 URL 去重 | 同一 URL 多源 → 保留内容最丰富的，合并 metadata |
| 规则评分 | 来源权重 + 主题优先级 + 关键词命中 → 综合评分 |
| 语义去重 | 标题相似度识别同一事件的不同报道 |
| 均衡摘要 | 按主题配额控制，避免单一主题占满日报 |
| 生成日报 | Markdown + HTML，Horizon 风格格式（TOC + 锚点 + 评分） |
| 生成 Feed | RSS 2.0，Reader 订阅 |

## 四大维度

### 美国政治
白宫与行政 · 国会与立法 · 选举与竞选 · 最高法院

### 国际局势
中美关系 · 中东局势 · 俄乌冲突 · 外交政策

### 科技发展
人工智能 · 半导体与芯片 · 科技公司 · 科技监管

### 经济发展
美联储与货币政策 · 宏观经济 · 贸易与关税 · 金融市场

## 数据源

| 数据源 | 类型 | 维度 | 需要 Key |
|--------|------|------|----------|
| NPR Politics | RSS | 美国政治 | 否 |
| The Hill | RSS | 美国政治 | 否 |
| Fox News | RSS | 美国政治 | 否 |
| BBC | RSS | 美国政治 / 国际 | 否 |
| The Guardian | RSS | 国际局势 | 否 |
| Al Jazeera | RSS | 国际局势 | 否 |
| DW News | RSS | 国际局势 | 否 |
| France24 | RSS | 国际局势 | 否 |
| TechCrunch | RSS | 科技发展 | 否 |
| Ars Technica | RSS | 科技发展 | 否 |
| The Verge | RSS | 科技发展 | 否 |
| Wired | RSS | 科技发展 | 否 |
| CNBC Economy | RSS | 经济发展 | 否 |
| Hacker News | API | 科技 / 社会 | 否 |
| Google News | RSS | 全维度 | 否 |
| GDELT | API | 全维度 | 否 |
| NewsAPI | REST | 全维度 | NEWSAPI_KEY |
| TianAPI | REST | 中文热搜 | TIANAPI_KEY |

免费源已覆盖全部四个维度，付费源可选。

## 目录结构

```
├── src/
│   ├── models.py             # Pydantic 数据模型
│   ├── database.py           # SQLite 存储层
│   ├── fetchers.py           # 异步并发抓取 + 去重
│   ├── scoring.py            # 规则评分 + 推荐理由
│   ├── topic_rules.py        # 主题分类规则（20 个主题）
│   ├── report_renderer.py    # 日报渲染（Markdown + HTML）
│   ├── feed_builder.py       # RSS Feed 生成
│   └── run_pipeline.py       # 统一入口
├── config/config.yaml        # 唯一配置入口
├── scripts/daily_run.sh      # 定时任务脚本
├── data/                     # 数据库和历史数据
├── docs/daily/               # 日报输出（运行后生成）
├── .env.example              # 环境变量模板
└── requirements.txt
```

## 配置

所有配置在 `config/config.yaml`：

- `sources` — 数据源开关和参数
- `output` — 输出目录和 Feed 路径
- `storage` — 数据库路径和保留天数

环境变量通过 `.env` 文件管理，支持 `${VAR_NAME}` 在配置中引用。

## 定时运行

```bash
# 本机 cron（每天 8:00 执行）
0 8 * * * /path/to/scripts/daily_run.sh
```

## Reader 订阅

部署 `docs/` 到 GitHub Pages 后，Reader 订阅：

```
https://<username>.github.io/us_politics_news/feed.xml
```

每天自动出现一篇新日报，点进去是完整可读页面。

## 技术参考

项目架构参考 [Horizon](https://github.com/Thysrael/Horizon)：
- 统一 ContentItem 数据模型
- 异步并发抓取
- 跨源 URL 去重 + 语义去重
- 均衡摘要配额控制
- Horizon 风格日报格式
