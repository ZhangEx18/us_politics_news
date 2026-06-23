#!/usr/bin/env python3
"""
统一发布元数据。

将标题、guid、link、pubDate、highlights_heading 等散落在各模块的推导逻辑
收敛到 ReportManifest 结构，由 build_manifest() 统一构造。
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class ReportManifest:
    """报告发布元数据，供渲染器和 Feed 生成器共用。"""
    product_key: str
    report_type: str
    report_key: str
    title: str
    highlights_heading: str
    pub_date: datetime
    link_path: str
    guid: str


_REPORT_TYPE_TITLE: dict[str, str] = {
    "daily": "日报",
    "weekly": "周报",
    "monthly": "月报",
}

_HIGHLIGHTS_HEADING: dict[str, str] = {
    "daily": "今日要点",
    "weekly": "本周要点",
    "monthly": "本月要点",
}


def build_manifest(
    product_key: str,
    report_type: str,
    report_key: str,
    title: str | None = None,
    pub_date: datetime | None = None,
    base_url: str = "",
) -> ReportManifest:
    """
    构造 ReportManifest。

    未提供 title 时根据 report_type + report_key 自动生成。
    未提供 pub_date 时使用当前北京时间。
    """
    if not title:
        type_label = _REPORT_TYPE_TITLE.get(report_type, "日报")
        title = f"{report_key} {type_label}"
    if not pub_date:
        pub_date = datetime.now(BEIJING_TZ)
    return ReportManifest(
        product_key=product_key,
        report_type=report_type,
        report_key=report_key,
        title=title,
        highlights_heading=_HIGHLIGHTS_HEADING.get(report_type, "今日要点"),
        pub_date=pub_date,
        link_path=f"{product_key}/{report_type}/{report_key}.html",
        guid=f"{product_key}/{report_type}/{report_key}",
    )
