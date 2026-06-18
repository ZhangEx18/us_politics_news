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
echo "美国政治新闻日报生成: $TODAY $(date +%H:%M:%S)"
echo "========================================"

# 运行统一 pipeline
python "$WORK_DIR/src/run_pipeline.py"

echo ""
echo "完成时间: $(date +%H:%M:%S)"
echo "========================================"
