import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from tools.diagnostics.check_mysql_connectivity import build_report, format_report, main, write_json_report


class FakeCursor:
    def __init__(self, rows_by_sql=None):
        self.rows_by_sql = rows_by_sql or {}
        self.sql = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.sql = " ".join(str(sql).split())
        self.params = params

    def fetchone(self):
        if "SELECT 1 AS ok" in self.sql:
            return {"ok": 1}
        if "COUNT(*) AS cnt FROM accounts" in self.sql:
            return {"cnt": 4}
        if "COUNT(*) AS cnt FROM portfolio_value" in self.sql:
            return {"cnt": 4}
        return {}

    def fetchall(self):
        if "FROM portfolio_value pv" in self.sql:
            return self.rows_by_sql.get("cash_rows", [])
        return []


class FakeConnection:
    def __init__(self, rows_by_sql=None):
        self.rows_by_sql = rows_by_sql or {}
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows_by_sql)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class MysqlConnectivityDiagnosticsTest(unittest.TestCase):
    def test_tcp_failure_stops_before_mysql_login(self):
        with patch(
            "tools.diagnostics.check_mysql_connectivity.socket.create_connection",
            side_effect=OSError("network unreachable"),
        ), patch("tools.diagnostics.check_mysql_connectivity._connect") as connect:
            report = build_report(["paper_main"], timeout=1)

        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["steps"][0]["name"], "TCP 端口连通")
        self.assertEqual(report["steps"][0]["status"], "FAIL")
        connect.assert_not_called()
        self.assertIn("TCP 端口不通", "\n".join(report["recommendations"]))

    def test_mysql_handshake_failure_after_tcp_pass(self):
        sock = Mock()
        with patch(
            "tools.diagnostics.check_mysql_connectivity.socket.create_connection",
            return_value=sock,
        ), patch(
            "tools.diagnostics.check_mysql_connectivity._connect",
            side_effect=RuntimeError("lost connection during handshake"),
        ):
            report = build_report(["paper_main"], timeout=1)

        self.assertEqual(report["overall"], "FAIL")
        by_name = {row["name"]: row for row in report["steps"]}
        self.assertEqual(by_name["TCP 端口连通"]["status"], "PASS")
        self.assertEqual(by_name["MySQL 握手登录"]["status"], "FAIL")
        self.assertIn("MySQL 握手失败", "\n".join(report["recommendations"]))
        sock.close.assert_called_once()

    def test_success_when_cash_meets_training_floor(self):
        rows = {
            "cash_rows": [
                {"account": "paper_main", "cash": 100000.0, "date": "2026-07-10"},
                {"account": "paper_watchlist", "cash": 120000.0, "date": "2026-07-10"},
            ]
        }
        sock = Mock()
        conn = FakeConnection(rows)
        with patch(
            "tools.diagnostics.check_mysql_connectivity.socket.create_connection",
            return_value=sock,
        ), patch("tools.diagnostics.check_mysql_connectivity._connect", return_value=conn):
            report = build_report(["paper_main", "paper_watchlist"], timeout=1)

        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(len(report["cash_rows"]), 2)
        self.assertTrue(all(row["status"] == "PASS" for row in report["cash_rows"]))
        self.assertIn("检查通过", "\n".join(report["recommendations"]))
        self.assertIn("总体状态: PASS", format_report(report))
        self.assertTrue(conn.closed)

    def test_cash_below_floor_fails_with_p10_recommendation(self):
        rows = {"cash_rows": [{"account": "paper_main", "cash": 9999.0, "date": "2026-07-10"}]}
        with patch(
            "tools.diagnostics.check_mysql_connectivity.socket.create_connection",
            return_value=Mock(),
        ), patch("tools.diagnostics.check_mysql_connectivity._connect", return_value=FakeConnection(rows)):
            report = build_report(["paper_main"], timeout=1)

        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["cash_rows"][0]["status"], "FAIL")
        self.assertIn("P10 现金校准", "\n".join(report["recommendations"]))
        self.assertIn("paper 现金", format_report(report))

    def test_write_json_report_creates_parent_directory(self):
        with tempfile.TemporaryDirectory(prefix="mysql_diag_json_") as tmp:
            path = Path(tmp) / "nested" / "latest.json"
            written = write_json_report({"overall": "PASS"}, path)

            self.assertEqual(written, str(path))
            self.assertTrue(path.exists())
            self.assertIn('"overall": "PASS"', path.read_text(encoding="utf-8"))

    def test_main_can_skip_json_file_output(self):
        with tempfile.TemporaryDirectory(prefix="mysql_diag_no_json_") as tmp:
            path = Path(tmp) / "latest.json"
            argv = [
                "check_mysql_connectivity.py",
                "--db-timeout",
                "1",
                "--output-json",
                str(path),
                "--no-output-json",
            ]
            with patch("tools.diagnostics.check_mysql_connectivity.sys.argv", argv), patch(
                "tools.diagnostics.check_mysql_connectivity.build_report",
                return_value={"overall": "PASS", "steps": [], "cash_rows": [], "recommendations": []},
            ), patch("tools.diagnostics.check_mysql_connectivity.print"):
                code = main()

            self.assertEqual(code, 0)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
