"""report_date 脚本回归测试"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


def _load_report_date_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "report_date.py"
    spec = importlib.util.spec_from_file_location("report_date_script", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_date_uses_beijing_cutoff_when_runner_date_is_previous_utc_day(monkeypatch, capsys):
    report_date = _load_report_date_script()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            utc_now = datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc)
            if tz is None:
                return utc_now.replace(tzinfo=None)
            return utc_now.astimezone(tz)

    monkeypatch.setattr("run_pipeline.datetime", FixedDatetime)
    monkeypatch.setattr(
        report_date,
        "load_config",
        lambda: {"schedule": {"timezone": "Asia/Shanghai", "cutoff_hour": 7}},
    )

    report_date.main()

    assert capsys.readouterr().out.strip() == "2026-06-20"
