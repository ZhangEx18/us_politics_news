# 观察日报 -- AI 驱动的每日国际新闻长文日报

每天自动生成 5000-10000 字中文日报，覆盖美国政局、国际局势、科技前沿、经济走势四大维度。100+ 新闻源并发抓取，AI 评分筛选、事件合并、AI 写作，输出 Markdown + HTML + RSS 全文 Feed，部署在 GitHub Pages，Reader 订阅即读。

## 快速开始

```bash
# 克隆仓库
git clone <repo-url> && cd us_politics_news

# 安装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 AI_API_KEY（必需）、NEWSAPI_KEY / TIANAPI_KEY（可选）

# 运行
python3 src/run_pipeline.py
```

生成产物：
- `docs/daily/YYYY-MM-DD.md` -- 当日日报（Markdown）
- `docs/daily/YYYY-MM-DD.html` -- 当日日报（HTML）
- `docs/feed.xml` -- RSS 全文 Feed

## 部署

GitHub Actions 自动运行 `daily_run.sh`，产物推送到 `docs/` 分支，GitHub Pages 托管。

Reader 订阅地址：

```
https://<username>.github.io/us_politics_news/feed.xml
```

## Pipeline 流程

```
并发抓取 -> 跨源去重 -> AI 评分 -> 事件合并 -> AI 写作 -> 生成日报 -> 生成 Feed
```

| 步骤 | 说明 |
|------|------|
| 并发抓取 | 6 个抓取器异步并发，统一返回 ContentItem |
| 跨源 URL 去重 | 同一 URL 多源 -> 保留内容最丰富的，合并 metadata |
| AI 评分 | 来源权重 + 主题优先级 + 关键词命中 + AI 深度分析 |
| 事件合并 | 语义相似度识别同一事件的不同报道 |
| AI 写作 | 生成 5000-10000 字中文长文日报 |
| 生成日报 | Markdown + HTML，含目录、锚点、评分 |
| 生成 Feed | RSS 2.0 全文，Reader 订阅 |

## 四大维度

### 美国政局
白宫与行政 · 国会与立法 · 选举与竞选 · 最高法院

### 国际局势
中美关系 · 中东局势 · 俄乌冲突 · 外交政策

### 科技前沿
人工智能 · 半导体与芯片 · 科技公司 · 科技监管

### 经济走势
美联储与货币政策 · 宏观经济 · 贸易与关税 · 金融市场

## 目录结构

```
├── src/
│   ├── __init__.py
│   ├── run_pipeline.py       # 统一入口
│   ├── models.py             # Pydantic 数据模型
│   ├── database.py           # SQLite 存储层
│   ├── fetchers.py           # 异步并发抓取 + 去重
│   ├── scoring.py            # 规则评分 + 推荐理由
│   ├── ai_analyzer.py        # AI 深度分析
│   ├── topic_rules.py        # 主题分类规则
│   ├── report_renderer.py    # 日报渲染（Markdown + HTML）
│   └── feed_builder.py       # RSS Feed 生成
├── config/
│   ├── config.yaml           # 主配置
│   └── sources.yaml          # 100+ 新闻源配置
├── scripts/
│   └── daily_run.sh          # 定时任务脚本（含运行后校验）
├── docs/
│   ├── daily/                # 日报输出（运行后生成）
│   └── feed.xml              # RSS Feed（运行后生成）
├── data/                     # SQLite 数据库 + 历史抓取数据
├── .env.example              # 环境变量模板
└── requirements.txt
```

## 配置说明

### config/config.yaml

主配置文件，控制 Pipeline 行为：

- `sources` -- 数据源开关和参数
- `scoring` -- 评分权重和阈值
- `ai` -- AI 写作 provider 和 prompt 配置
- `output` -- 输出目录和 Feed 路径
- `storage` -- 数据库路径和保留天数

### config/sources.yaml

100+ 新闻源，按四大维度分类，每个源包含：

```yaml
- name: "源名称"
  url: "https://..."
  column: us_politics | global_affairs | technology | economy
  source_tier: 1 | 2 | 3 | 4    # 1=官方一线 2=主流 3=专业智库 4=聚合
  language: en | zh | multi
  enabled: true | false
```

### .env

环境变量通过 `.env` 文件管理，支持 `${VAR_NAME}` 在 YAML 中引用：

| 变量 | 说明 | 必需 |
|------|------|------|
| `AI_API_KEY` | AI 服务 API Key | 是 |
| `AI_PROVIDER` | openai / deepseek / moonshot 等 | 否（默认 openai） |
| `AI_BASE_URL` | API 端点 | 否（默认 OpenAI） |
| `AI_MODEL` | 模型名称 | 否（默认 gpt-4o-mini） |
| `NEWSAPI_KEY` | NewsAPI 密钥 | 否 |
| `TIANAPI_KEY` | TianAPI 密钥 | 否 |

## 定时运行

```bash
# 本机 cron（每天 8:00 执行）
0 8 * * * /path/to/scripts/daily_run.sh
```

脚本会在运行后自动校验：
- `docs/feed.xml` 包含 `content:encoded`（确保全文 Feed）
- 日报字数 > 5000（确保内容充实）

校验失败会 `exit 1`，便于 CI 告警。
