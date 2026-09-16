# RSSHub 自托管（中文源基建）

中文新闻源（FT中文网 / 联合早报 / 观察者网 / 量子位 / 36氪 / 华尔街见闻 / 财新可选）
依赖 RSSHub 路由抓取，公共实例 rsshub.app 对数据中心 IP 返回 403，因此需要自托管。

## 两种运行方式

### 1. GitHub Actions（默认，无需部署）

`.github/workflows/publish-product.yml` 已配置 `services.rsshub` 服务容器，
CI 内 `RSSHUB_BASE_URL=http://localhost:1200` 自动生效，无需任何操作。

### 2. 本地 / VPS（手动跑 pipeline 时）

```bash
cd deploy/rsshub
docker compose up -d
curl -s "http://localhost:1200/zaobao/realtime/world" | head -5   # 验证
export RSSHUB_BASE_URL=http://localhost:1200
```

如需 CI 使用 VPS 实例（替代默认的本地服务容器），在仓库 Secrets 中设置
`RSSHUB_BASE_URL`，并修改 workflow 环境变量指向该 secret。

## 已验证路由

| 路由 | 用途 |
|---|---|
| `/ftchinese/simplified/news` | FT 中文网（FT 官方对高频访问会 429，CI 侧可能不稳定） |
| `/zaobao/realtime/world` | 联合早报国际 |
| `/guancha/home` | 观察者网首页 |
| `/qbitai/category/资讯` | 量子位资讯 |
| `/36kr/hot-list` | 36氪 24 小时热榜 |
| `/wallstreetcn/news/global` | 华尔街见闻宏观/全球 |

财新不使用 RSSHub：列表页直连 + URL 日期过滤（`article_url_pattern`），
只收 `https://.../YYYY-MM-DD/xxxx.html` 形式的文章链接。

## 验收

`docs/news/metrics/latest.json` 中 `cn_source_selected >= 3`。
