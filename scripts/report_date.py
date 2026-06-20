#!/usr/bin/env python3
"""输出当前调度配置对应的日报日期。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import load_config
from run_pipeline import _get_report_window


def main() -> None:
    _, _, report_date = _get_report_window(config=load_config())
    print(report_date)


if __name__ == "__main__":
    main()
