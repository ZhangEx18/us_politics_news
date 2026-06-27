# 观察日报 -- AI 驱动的每日国际新闻长文日报

每天自动生成中文新闻日报，覆盖美国政局、国际局势、科技前沿、经济走势四大维度。多接入方式新闻源并发抓取，AI 评分筛选、事件合并、AI 写作，输出 Markdown + HTML + RSS 全文 Feed，部署在 GitHub Pages，Reader 订阅即读。

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

# 抓取 + 生成 news/daily
python3 src/run_product.py --product news --report-type daily

# 只用数据库已有内容补跑当天日报
python3 src/run_product.py --product news --report-type daily --digest-only
```

生成产物：
- `docs/news/daily/YYYY-MM-DD.md` -- news 产品当日日报（Markdown）
- `docs/news/daily/YYYY-MM-DD.html` -- news 产品当日日报（HTML）
- `docs/feeds/news.xml` -- news 产品 RSS 全文 Feed

兼容别名：
- 若 `config/products/news/product.yaml` 中 `publish.legacy_aliases` 为 `true`，发布流程会同步维护根目录别名，如 `docs/daily/YYYY-MM-DD.html` 与 `docs/feed.xml`

## 部署

主发布链路由 Cloudflare Workers Cron 触发 GitHub Actions：

- 每天北京时间 07:30 触发 `Daily RSS Publish`，抓取并发布日报
- 每周一北京时间 07:35 触发 `Weekly Publish`，基于数据库生成并发布周报
- 每月 1 日北京时间 07:40 触发 `Monthly Publish`，基于数据库生成并发布月报

这些 workflow 会恢复已发布归档、更新产品 feed、重建首页，然后发布到 GitHub Pages。

Reader 订阅地址：

```
https://<username>.github.io/us_politics_news/feeds/news.xml
```

## Pipeline 流程

```
并发抓取 -> 跨源去重 -> AI 评分 -> 事件合并 -> AI 写作 -> 渲染日报 -> 生成 Feed
```

| 步骤 | 说明 |
|------|------|
| 并发抓取 | RSS / RSSHub / Google News / Custom 等抓取器异步并发，统一返回 ContentItem |
| 跨源 URL 去重 | 同一 URL 多源 -> 保留内容最丰富的，合并 metadata |
| AI 评分 | 来源权重 + 主题优先级 + 关键词命中 + AI 深度分析 |
| 事件合并 | 语义相似度识别同一事件的不同报道 |
| AI 写作 | 生成中文栏目正文、要点与周期性总览 |
| 渲染日报 | Markdown + HTML + Reader 友好 HTML 片段 |
| 生成 Feed | RSS 2.0 全文，Reader 订阅 |

## 输出文章结构

### 日报结构

日报最终输出按下面的顺序组织：

```text
今日要点
- 要点 1
- 要点 2
- ...

一、美国政局
### 重点解析
1. 事件标题
   事件单段正文
2. 事件标题
   事件单段正文

### 其他要闻
- 简短补充要闻
- 简短补充要闻

二、国际局势
三、科技前沿
四、经济走势
```

约束：
- 日报顶部不再输出一段总导语，首页和 Reader 都以 `今日要点` 开头
- `重点解析` 只放带单段正文的主事件
- `其他要闻` 只放简短补充条目；如果该栏没有补充条目，就不显示这一小节
- 面向读者的日报输出要求中文可读；明显未翻译的英文标题或英文正文不会进入最终日报

### 周报 / 月报结构

- 周报、月报会保留顶部总览
- 总览结构为：综述段落 + 核心主题列表 + 观察点列表
- 四个栏目仍按 `美国政局 / 国际局势 / 科技前沿 / 经济走势` 顺序展开

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
│   ├── run_product.py        # 多 product 统一入口（推荐）
│   ├── run_pipeline.py       # news/daily pipeline
│   ├── models.py             # Pydantic 数据模型
│   ├── database.py           # SQLite 存储层
│   ├── fetchers.py           # 异步并发抓取 + 去重
│   ├── scoring.py            # 规则评分 + 推荐理由
│   ├── ai_analyzer.py        # AI 深度分析
│   ├── topic_rules.py        # 主题分类规则
│   ├── report_renderer.py    # 日报渲染（Markdown + HTML）
│   └── feed_builder.py       # RSS Feed 生成
├── config/
│   ├── config.yaml           # 默认指向 news product 的兼容入口
│   ├── base.yaml             # 共享基础配置
│   └── products/news/sources.yaml  # news 产品新闻源配置
├── scripts/
│   ├── daily_run.sh          # 本地定时脚本（旧入口）
│   └── publish_daily.sh      # 仅用数据库补跑日报并可推送
├── docs/
│   ├── news/daily/           # news/daily 输出（运行后生成）
│   ├── feeds/news.xml        # news 产品 Feed（运行后生成）
│   └── ...                   # 兼容别名、weekly/monthly、algorithms 等
├── data/                     # SQLite 数据库 + 历史抓取数据
├── .env.example              # 环境变量模板
└── requirements.txt
```

## 配置说明

### config/config.yaml

兼容入口，默认指向 `news` product。

### config/products/news/product.yaml

news 产品主配置，控制发布路径、定时配置、数据库位置和四栏配额：

- `publish.site_root` -- 站点输出根目录，默认 `docs/news`
- `publish.feed_path` -- feed 输出路径，默认 `docs/feeds/news.xml`
- `publish.legacy_aliases` -- 是否同步维护根目录兼容别名
- `storage.db_path` -- 数据库路径，默认 `data/products/news/news.db`
- `digest.columns` -- 四个栏目各自的主事件/补充要闻配额

### config/base.yaml

基础配置文件，控制共享行为：

- `sources` -- 数据源开关和参数
- `scoring` -- 评分权重和阈值
- `ai` -- AI 写作 provider 和 prompt 配置
- `analysis` -- 历史上下文和分析相关配置
- `runtime` -- 并发和运行时参数

### config/products/news/sources.yaml

news 产品新闻源配置，按四大维度分类，每个源包含：

```yaml
- name: "源名称"
  url: "https://..."
  fetch_mode: rss | rsshub | google_news | custom | hacker_news
  fetcher_key: china_media_article_list   # 仅 custom 必填
  column: us_politics | global_affairs | technology | economy
  source_tier: 1 | 2 | 3 | 4    # 1=官方一线 2=主流 3=专业智库 4=聚合
  language: en | zh | multi
  tags: [cn_source, policy, macro]
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
# 本机 cron（每天 8:00 执行，作为本地备用方案）
0 8 * * * /path/to/scripts/daily_run.sh
```

脚本会在运行后自动校验：
- `docs/feeds/news.xml` 包含 `content:encoded`（确保全文 Feed）
- 当日日报文件存在（默认 `docs/news/daily/YYYY-MM-DD.md`）

校验失败会 `exit 1`，便于 CI 告警。

## Cloudflare 定时触发

1. 创建 GitHub fine-grained personal access token，仅授予本仓库 `Actions: write` 权限。不要复用本机 `gh auth` 登录 token。
2. 安装并登录 Wrangler。
3. 将该专用 token 写入 Worker Secret：

```bash
wrangler secret put GITHUB_TOKEN
```

建议补一条校验，确认线上 Worker 已持有独立 secret：

```bash
wrangler secret list
```

4. 部署 Worker：

```bash
wrangler deploy
```

`wrangler.toml` 中的 cron 使用 UTC：

- `30 23 * * *` 等价于北京时间每日 07:30，触发 `daily-rss-publish.yml`
- `35 23 * * 1` 等价于北京时间每周一 07:35，触发 `weekly-publish.yml`
- `40 23 28-31 * *` 在北京时间月初 07:40 命中时触发 `monthly-publish.yml`

Worker 只负责触发 workflow，不直接抓取、生成或发布内容。
