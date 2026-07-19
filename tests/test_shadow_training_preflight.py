import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tools.diagnostics.check_shadow_training_preflight import build_report, format_report, main, write_json_report


class ShadowTrainingPreflightTest(unittest.TestCase):
    def test_current_config_passes_without_db_cash_check(self):
        report = build_report(["paper_main", "paper_watchlist"], check_db_cash=False)

        self.assertEqual(report["overall"], "PASS")
        by_name = {row["name"]: row for row in report["checks"]}
        self.assertEqual(by_name["新版 paper 训练复核埋点"]["status"], "PASS")
        self.assertEqual(by_name["paper 训练现金配置"]["status"], "PASS")
        self.assertEqual(by_name["paper-only 独立扫描节奏"]["status"], "PASS")
        self.assertEqual(by_name["paper 一手兜底"]["status"], "PASS")
        self.assertEqual(by_name["严格日志验收支持"]["status"], "PASS")

        text = format_report(report)
        self.assertIn("总体状态: PASS", text)
        self.assertIn("盘前预检通过", text)

    def test_fails_when_db_cash_cannot_be_read(self):
        with patch(
            "tools.diagnostics.check_shadow_training_preflight._fetch_cash",
            return_value=({}, "database unavailable"),
        ):
            report = build_report(["paper_main"], check_db_cash=True)

        self.assertEqual(report["overall"], "FAIL")
        by_name = {row["name"]: row for row in report["checks"]}
        self.assertEqual(by_name["paper 数据库现金读取"]["status"], "FAIL")
        self.assertIn("不要先放宽主账户门禁", "\n".join(report["recommendations"]))

    def test_fails_when_runtime_markers_are_missing(self):
        def fake_read_text(rel_path):
            if rel_path == "monitor.py":
                return "old monitor without paper marker"
            if rel_path == "tools/maintenance/ensure_paper_training_cash.py":
                return "old cash script"
            if rel_path == "tools/diagnostics/analyze_shadow_training_logs.py":
                return "old log analyzer"
            return ""

        with patch("tools.diagnostics.check_shadow_training_preflight._read_text", side_effect=fake_read_text):
            report = build_report(["paper_main"], check_db_cash=False)

        self.assertEqual(report["overall"], "FAIL")
        by_name = {row["name"]: row for row in report["checks"]}
        self.assertEqual(by_name["新版 paper 训练复核埋点"]["status"], "FAIL")
        self.assertEqual(by_name["P10 现金校准工具"]["status"], "FAIL")
        self.assertEqual(by_name["严格日志验收支持"]["status"], "FAIL")
        self.assertTrue(any("PAPER_TRAINING_PENDING_CHECK" in item for item in report["recommendations"]))

    def test_write_json_report_creates_parent_directory(self):
        with tempfile.TemporaryDirectory(prefix="shadow_preflight_json_") as tmp:
            path = Path(tmp) / "nested" / "latest.json"
            written = write_json_report({"overall": "PASS"}, path)

            self.assertEqual(written, str(path))
            self.assertTrue(path.exists())
            self.assertIn('"overall": "PASS"', path.read_text(encoding="utf-8"))

    def test_main_can_skip_json_file_output(self):
        with tempfile.TemporaryDirectory(prefix="shadow_preflight_no_json_") as tmp:
            path = Path(tmp) / "latest.json"
            argv = [
                "check_shadow_training_preflight.py",
                "--output-json",
                str(path),
                "--no-output-json",
            ]
            with patch("tools.diagnostics.check_shadow_training_preflight.sys.argv", argv), patch(
                "tools.diagnostics.check_shadow_training_preflight.build_report",
                return_value={"overall": "PASS", "accounts": [], "checks": [], "cash_rows": [], "recommendations": []},
            ), patch("tools.diagnostics.check_shadow_training_preflight.print"):
                code = main()

            self.assertEqual(code, 0)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
