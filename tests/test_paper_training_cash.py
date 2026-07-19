import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tools.maintenance.ensure_paper_training_cash import (
    _ensure_one_account,
    build_report_payload,
    ensure_cash,
    format_report,
    main,
    write_json_report,
)


class PaperTrainingCashTest(unittest.TestCase):
    def test_retries_then_updates_paper_cash_after_write_failure(self):
        with patch(
            "tools.maintenance.ensure_paper_training_cash._read_current_cash",
            side_effect=[RuntimeError("db read lost"), 50000.0],
        ) as read_cash, patch(
            "tools.maintenance.ensure_paper_training_cash._set_training_cash",
            side_effect=[RuntimeError("db write lost"), None],
        ) as set_cash, patch("tools.maintenance.ensure_paper_training_cash.time.sleep") as sleep:
            row = _ensure_one_account(
                "paper_main",
                100000.0,
                100000.0,
                force=False,
                dry_run=False,
                db_retries=3,
                retry_delay=0.01,
                db_timeout=1,
            )

        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["before"], 50000.0)
        self.assertEqual(row["after"], 100000.0)
        self.assertTrue(row["updated"])
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(read_cash.call_count, 2)
        self.assertEqual(set_cash.call_count, 2)
        sleep.assert_called_once()

    def test_returns_error_row_after_retries_exhausted(self):
        with patch(
            "tools.maintenance.ensure_paper_training_cash._read_current_cash",
            side_effect=RuntimeError("db unavailable"),
        ), patch(
            "tools.maintenance.ensure_paper_training_cash._set_training_cash",
            side_effect=RuntimeError("db write unavailable"),
        ) as set_cash, patch(
            "tools.maintenance.ensure_paper_training_cash.time.sleep"
        ):
            row = _ensure_one_account(
                "paper_watchlist",
                100000.0,
                100000.0,
                force=False,
                dry_run=False,
                db_retries=2,
                retry_delay=0.01,
                db_timeout=1,
            )

        self.assertEqual(row["status"], "ERROR")
        self.assertFalse(row["updated"])
        self.assertIn("db write unavailable", row["error"])
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(set_cash.call_count, 2)

    def test_writes_target_when_cash_read_fails_but_write_recovers(self):
        with patch(
            "tools.maintenance.ensure_paper_training_cash._read_current_cash",
            side_effect=[RuntimeError("db read lost"), 100000.0],
        ) as read_cash, patch(
            "tools.maintenance.ensure_paper_training_cash._set_training_cash"
        ) as set_cash:
            row = _ensure_one_account(
                "paper_main",
                100000.0,
                100000.0,
                force=False,
                dry_run=False,
                db_retries=2,
                retry_delay=0,
                db_timeout=1,
            )

        self.assertEqual(row["status"], "OK")
        self.assertIsNone(row["before"])
        self.assertEqual(row["after"], 100000.0)
        self.assertTrue(row["updated"])
        self.assertEqual(row["reason"], "读取现金失败，按训练目标校准")
        self.assertEqual(read_cash.call_count, 2)
        set_cash.assert_called_once_with("paper_main", 100000.0, False, db_timeout=1)

    def test_ensure_cash_skips_non_paper_accounts(self):
        rows = ensure_cash(["main"], 100000.0, 100000.0, db_timeout=1)

        self.assertEqual(rows[0]["status"], "SKIP")
        self.assertEqual(rows[0]["reason"], "非 paper 账户，已跳过")

    def test_report_includes_error_status(self):
        text = format_report(
            [
                {
                    "account": "paper_main",
                    "display": "仿真主账户",
                    "before": None,
                    "after": None,
                    "updated": False,
                    "status": "ERROR",
                    "attempts": 5,
                    "reason": "数据库连接失败，未完成校准",
                    "error": "database connection failed while updating cash",
                }
            ],
            100000.0,
            100000.0,
            False,
        )

        self.assertIn("状态", text)
        self.assertIn("ERROR", text)
        self.assertIn("database connection failed while updating cash", text)

    def test_build_report_payload_marks_error_as_not_ok(self):
        payload = build_report_payload(
            [
                {
                    "account": "paper_main",
                    "display": "仿真主账户",
                    "before": None,
                    "after": None,
                    "updated": False,
                    "status": "ERROR",
                    "attempts": 1,
                    "reason": "数据库连接失败，未完成校准",
                    "error": "handshake lost",
                }
            ],
            100000.0,
            100000.0,
            False,
            accounts=["paper_main"],
            db_timeout=1,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["overall"], "ERROR")
        self.assertIn("P10 现金校准失败", "\n".join(payload["recommendations"]))

    def test_write_json_report_creates_parent_directory(self):
        with tempfile.TemporaryDirectory(prefix="paper_cash_json_") as tmp:
            path = Path(tmp) / "nested" / "latest.json"
            written = write_json_report({"overall": "OK"}, path)

            self.assertEqual(written, str(path))
            self.assertTrue(path.exists())
            self.assertIn('"overall": "OK"', path.read_text(encoding="utf-8"))

    def test_main_can_skip_json_file_output(self):
        with tempfile.TemporaryDirectory(prefix="paper_cash_no_json_") as tmp:
            path = Path(tmp) / "latest.json"
            argv = [
                "ensure_paper_training_cash.py",
                "--accounts",
                "paper_main",
                "--output-json",
                str(path),
                "--no-output-json",
            ]
            row = {
                "account": "paper_main",
                "display": "仿真主账户",
                "before": 100000.0,
                "after": 100000.0,
                "updated": False,
                "status": "OK",
                "attempts": 1,
                "reason": "现金充足",
                "error": "",
            }
            with patch("tools.maintenance.ensure_paper_training_cash.sys.argv", argv), patch(
                "tools.maintenance.ensure_paper_training_cash.ensure_cash",
                return_value=[row],
            ), patch("tools.maintenance.ensure_paper_training_cash.print"):
                code = main()

            self.assertEqual(code, 0)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
