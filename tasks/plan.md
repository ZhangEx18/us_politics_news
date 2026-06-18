# 海外 VPS 部署方案

## 架构

```
VPS (新加坡/东京, 2C4G, Ubuntu 24.04)
  ├─ cron: */30 * * * * fetch_only.sh
  ├─ cron: 0 8 * * * publish_daily.sh
  └─ git push → GitHub Pages → Reader 订阅 feed.xml
```

## 职责分工

| 节点 | 职责 |
|------|------|
| VPS | 抓取、入库、AI 评分、日报生成、Feed 生成、git push |
| GitHub Pages | 对外分发 feed.xml + 日报 HTML |
| 本机 | 开发、调试、查看结果，不参与正式抓取 |
| GitHub Actions | CI 语法检查、手动补跑，不做主抓取 |

## 依赖图

```
Task 1: VPS 初始化
  └─ Task 2: 代码部署 + 环境配置
       ├─ Task 3: fetch 定时任务
       ├─ Task 4: publish 定时任务
       └─ Task 5: git push 自动发布
            └─ Task 6: 验收测试
```

## 环境变量

```bash
# AI / 新闻 API
AI_API_KEY=xxx
AI_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
AI_MODEL=mimo-v2.5-pro
NEWSAPI_KEY=xxx
TIANAPI_KEY=xxx

# 发布凭据
GIT_PUSH_TOKEN=xxx
GIT_PUSH_REPO=username/us_politics_news
GIT_PUSH_BRANCH=main

# 可选代理兜底
# HTTP_PROXY=
# HTTPS_PROXY=
```

## 定时任务

### 抓取任务（每 30 分钟）
```
*/30 * * * * /opt/us_politics_news/scripts/fetch_only.sh >> /var/log/fetch.log 2>&1
```
只做：fetch → dedupe → save_to_db

### 出刊任务（每天 8:00）
```
0 8 * * * /opt/us_politics_news/scripts/publish_daily.sh >> /var/log/publish.log 2>&1
```
做：score → merge → quota → digest → render → feed → git push

## 发布流程

```bash
# publish_daily.sh 末尾
cd /opt/us_politics_news
git add docs/
git commit -m "daily: $(date +%Y-%m-%d)"
git push origin main
# GitHub Pages 自动更新
```

## 监控

- 抓取日志：每轮抓取总数、成功源数、失败源数
- 出刊日志：候选条数、入选条数、总字数、四栏条数
- 发布后检查：feed.xml 含 content:encoded、四栏目齐全、字数 20k-40k

## 验收标准

- [ ] VPS 上 fetch 可达大部分源
- [ ] 数据库 24 小时持续积累
- [ ] publish 生成四栏目日报 20k-40k 字
- [ ] feed.xml 含 content:encoded
- [ ] Reader 订阅可读全文
- [ ] 连续 3-7 天稳定运行
