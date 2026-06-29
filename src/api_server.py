#!/usr/bin/env python3
"""
观察日报 API 服务 — 提供 pipeline 状态和日报数据
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from database import NewsDatabase

app = FastAPI(title="观察日报 API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件：docs 目录
app.mount("/docs", StaticFiles(directory=str(PROJECT_ROOT / "docs"), html=True), name="docs")


def _get_db() -> NewsDatabase:
    db_path = PROJECT_ROOT / "data" / "products" / "news" / "news.db"
    return NewsDatabase(str(db_path))


# ── API 端点 ──


@app.get("/api/status")
async def get_status():
    """数据库和运行状态概览"""
    db = _get_db()

    # 文章统计
    total_articles = db.count_articles()

    # 今日新增
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = db.count_articles_since(today)

    # 最近运行记录
    recent_runs = db.get_recent_runs(limit=5)

    # 源覆盖统计
    source_stats = db.get_source_stats()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "articles": {
            "total": total_articles,
            "today_new": today_count,
        },
        "recent_runs": recent_runs,
        "sources": source_stats,
    }


@app.get("/api/stats")
async def get_stats():
    """详细统计信息"""
    db = _get_db()

    # 按日期统计
    daily_stats = db.get_daily_stats(days=30)

    # 按栏目统计
    column_stats = db.get_column_stats()

    # 按来源类型统计
    source_type_stats = db.get_source_type_stats()

    return {
        "daily": daily_stats,
        "columns": column_stats,
        "source_types": source_type_stats,
    }


@app.get("/api/pipeline")
async def get_pipeline_status():
    """当前 pipeline 运行状态"""
    # 检查是否有正在运行的进程
    is_running = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_product.py"],
            capture_output=True,
            text=True,
        )
        is_running = result.returncode == 0
    except Exception:
        pass

    # 读取最新日志
    log_lines = []
    log_file = PROJECT_ROOT / "pipeline.log"
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_lines = f.readlines()[-20:]  # 最后 20 行
        except Exception:
            pass

    return {
        "is_running": is_running,
        "log_tail": [line.strip() for line in log_lines],
    }


@app.get("/api/reports")
async def get_reports():
    """已发布的日报列表"""
    reports = []

    # 扫描 docs/news/daily/ 目录
    daily_dir = PROJECT_ROOT / "docs" / "news" / "daily"
    if daily_dir.exists():
        for file in sorted(daily_dir.glob("*.html"), reverse=True):
            date_str = file.stem  # 2026-06-29
            md_file = daily_dir / f"{date_str}.md"

            # 读取 md 文件获取 highlights
            highlights = []
            if md_file.exists():
                try:
                    content = md_file.read_text(encoding="utf-8")
                    # 解析 YAML frontmatter
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            import yaml
                            meta = yaml.safe_load(parts[1])
                            highlights = meta.get("highlights", [])
                except Exception:
                    pass

            reports.append({
                "date": date_str,
                "html_url": f"/docs/news/daily/{date_str}.html",
                "md_url": f"/docs/news/daily/{date_str}.md",
                "highlights": highlights[:3],  # 只返回前 3 条
            })

    return {"reports": reports}


@app.get("/api/report/{date}")
async def get_report_detail(date: str):
    """获取指定日期的日报详情"""
    daily_dir = PROJECT_ROOT / "docs" / "news" / "daily"

    html_file = daily_dir / f"{date}.html"
    md_file = daily_dir / f"{date}.md"

    if not html_file.exists():
        raise HTTPException(status_code=404, detail=f"日报 {date} 不存在")

    # 读取 HTML
    html_content = html_file.read_text(encoding="utf-8")

    # 读取 MD 并解析元数据
    meta = {}
    if md_file.exists():
        try:
            content = md_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    import yaml
                    meta = yaml.safe_load(parts[1])
        except Exception:
            pass

    return {
        "date": date,
        "html": html_content,
        "meta": meta,
    }


@app.post("/api/run")
async def trigger_run():
    """手动触发日报生成"""
    # 检查是否已在运行
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_product.py"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return {"status": "already_running", "message": "Pipeline 已在运行中"}
    except Exception:
        pass

    # 触发运行
    try:
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "src" / "run_product.py"),
             "--product", "news", "--report-type", "daily"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        return {"status": "started", "message": "Pipeline 已启动"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Dashboard 页面 ──


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """返回监控面板 HTML"""
    html_path = PROJECT_ROOT / "docs" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Dashboard not found</h1>")


@app.get("/", response_class=HTMLResponse)
async def root():
    """重定向到 dashboard"""
    return HTMLResponse(content="""
    <html>
    <head><meta http-equiv="refresh" content="0;url=/dashboard"></head>
    <body><p>Redirecting to <a href="/dashboard">dashboard</a>...</p></body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
