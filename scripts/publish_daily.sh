#!/bin/bash
# 出刊任务 — 每天执行一次
# 做：score → merge → quota → digest → render → feed → git push

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
echo "出刊任务: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# 运行 digest 流程
python3 "$WORK_DIR/src/run_pipeline.py" --digest-only --hours 24

# 发布后校验
echo ""
echo "[校验] 检查输出..."

# 检查 feed.xml
if ! grep -q "content:encoded" "$WORK_DIR/docs/feed.xml"; then
    echo "[错误] feed.xml 缺少 content:encoded"
    exit 1
fi

# 检查日报文件
TODAY=$(python3 "$WORK_DIR/scripts/report_date.py")
if [ ! -f "$WORK_DIR/docs/daily/$TODAY.md" ]; then
    echo "[错误] 日报文件不存在: docs/daily/$TODAY.md"
    exit 1
fi

# 检查字数
CHAR_COUNT=$(wc -c < "$WORK_DIR/docs/daily/$TODAY.md")
if [ "$CHAR_COUNT" -lt 10000 ]; then
    echo "[警告] 日报字数偏少: $CHAR_COUNT 字节"
fi

echo "[校验] 通过"

# Git push
if [ -n "$GIT_PUSH_TOKEN" ] && [ -n "$GIT_PUSH_REPO" ]; then
    echo ""
    echo "[发布] 推送到 GitHub..."
    BRANCH="${GIT_PUSH_BRANCH:-main}"
    git add docs/
    git commit -m "daily: $TODAY" || echo "无新内容"
    git push "https://$GIT_PUSH_TOKEN@github.com/$GIT_PUSH_REPO.git" "$BRANCH"
    echo "[发布] 完成"
else
    echo "[发布] 未配置 GIT_PUSH_TOKEN，跳过"
fi

echo ""
echo "完成: $(date '+%H:%M:%S')"
echo "========================================"
