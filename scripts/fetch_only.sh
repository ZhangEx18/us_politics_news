#!/bin/bash
# 抓取任务 — 每 30 分钟执行一次
# 只做：fetch → dedupe → save_to_db
# 不做：AI 评分、日报生成

set -e

WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORK_DIR"

# 加载环境变量
if [ -f "$WORK_DIR/.env" ]; then
    set -a
    source "$WORK_DIR/.env"
    set +a
else
    echo "[错误] 未找到 .env 文件"
    exit 1
fi

# 激活虚拟环境
if [ -d "$WORK_DIR/venv" ]; then
    source "$WORK_DIR/venv/bin/activate"
fi

echo "========================================"
echo "抓取任务: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

python3 "$WORK_DIR/src/run_pipeline.py" --fetch-only --hours 1

echo ""
echo "完成: $(date '+%H:%M:%S')"
echo "========================================"
