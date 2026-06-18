# VPS 部署任务清单

## Phase 1: VPS 基础设施
- [ ] 购买海外 VPS（新加坡/东京，2C4G，Ubuntu 24.04）
- [ ] SSH 连接验证
- [ ] 安装 Python 3.12+、git、pip
- [ ] 创建项目目录 /opt/us_politics_news/

## Phase 2: 代码部署
- [ ] git clone 仓库到 VPS
- [ ] python3 -m venv venv && pip install -r requirements.txt
- [ ] 创建 .env 文件（AI_API_KEY 等）
- [ ] 配置 git push 凭据
- [ ] python3 -m py_compile src/*.py 验证

## Phase 3: 定时抓取
- [ ] 创建 scripts/fetch_only.sh
- [ ] chmod +x scripts/fetch_only.sh
- [ ] crontab: */30 * * * *
- [ ] 手动运行验证数据库有新增
- [ ] 运行 24 小时验证持续积累

## Phase 4: 定时出刊
- [ ] 创建 scripts/publish_daily.sh
- [ ] chmod +x scripts/publish_daily.sh
- [ ] crontab: 0 8 * * *
- [ ] 手动运行验证四栏目日报
- [ ] 验证字数 20k-40k

## Phase 5: 自动发布
- [ ] publish_daily.sh 集成 git push
- [ ] GitHub Pages 配置为 docs/ 目录
- [ ] 验证 feed.xml 可通过 URL 访问
- [ ] Reader 订阅验证

## Phase 6: 验收
- [ ] 连续运行 3-7 天
- [ ] Feed 有历史积累
- [ ] GitHub Pages 稳定更新
- [ ] 本机不参与正式抓取
