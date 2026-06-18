#!/bin/bash
# 每日新闻获取与分析自动化脚本
# 运行时间：每天上午 8:00

set -e

WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$WORK_DIR/data"
OUTPUT_DIR="$WORK_DIR/docs"
VENV="$WORK_DIR/venv"

# 加载环境变量（从 .env 文件）
if [ -f "$WORK_DIR/.env" ]; then
    set -a
    source "$WORK_DIR/.env"
    set +a
else
    echo "[警告] 未找到 .env 文件，部分数据源可能无法使用"
    echo "请复制 .env.example 为 .env 并填入密钥"
fi

# 日期
TODAY=$(date +%Y-%m-%d)
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

cd "$WORK_DIR"

# 激活虚拟环境（如果存在）
if [ -d "$VENV" ]; then
    source "$VENV/bin/activate"
fi

echo "========================================"
echo "四维日报生成: $TODAY $(date +%H:%M:%S)"
echo "========================================"

# 运行统一 pipeline
python "$WORK_DIR/src/run_pipeline.py"

# ── 运行后校验 ──

# 1. 检查 feed.xml 包含 content:encoded
FEED_FILE="$OUTPUT_DIR/feed.xml"
if [ ! -f "$FEED_FILE" ] || ! grep -q "content:encoded" "$FEED_FILE"; then
    echo "[错误] feed.xml 缺少 content:encoded，Feed 不含全文"
    exit 1
fi

# 2. 检查日报字数 > 5000
DAILY_FILE=$(find "$OUTPUT_DIR/daily" -name "*.md" -newer "$WORK_DIR/scripts/daily_run.sh" -print -quit 2>/dev/null)
if [ -z "$DAILY_FILE" ]; then
    # 回退：找今天日期的文件
    DAILY_FILE="$OUTPUT_DIR/daily/${TODAY}.md"
fi
if [ ! -f "$DAILY_FILE" ]; then
    echo "[错误] 未找到今日日报文件"
    exit 1
fi
CHAR_COUNT=$(wc -m < "$DAILY_FILE")
if [ "$CHAR_COUNT" -lt 5000 ]; then
    echo "[错误] 日报字数 ${CHAR_COUNT} < 5000，内容不足"
    exit 1
fi

echo "校验通过: feed.xml 含全文，日报 ${CHAR_COUNT} 字"
echo ""
echo "完成时间: $(date +%H:%M:%S)"
echo "========================================"
