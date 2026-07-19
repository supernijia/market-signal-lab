import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from tools.diagnostics.analyze_shadow_training_logs import (
    analyze_log,
    build_recommendations,
    build_report,
    evaluate_health,
    format_text_report,
    send_email_report,
)


class ShadowTrainingLogAnalysisTest(unittest.TestCase):
    def test_analyze_shadow_training_log_counts_core_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260703.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-07-03 10:35:33 - StockAnalyzer.FocusMonitor - INFO - PAPER_FOCUS_PENDING_CREATED account=paper_watchlist code=301379 name=天山电子 strategy=冷启动 bucket=B2 expires_at=2026-07-03 11:30:00 level=强盯 score=124.7",
                        "2026-07-03 10:35:40 - StockAnalyzer.Monitor - INFO - Monitor paper-only enabled; only paper_* pending entries and positions will be processed.",
                        "2026-07-03 10:35:41 - StockAnalyzer.Monitor - INFO - PAPER_ONLY_PENDING_SCAN trade_date=2026-07-03 bucket=B2 loaded=1 accounts=paper_main,paper_watchlist",
                        "2026-07-03 10:35:55 - StockAnalyzer.Monitor - INFO - PENDING_CHECK id=1 account=paper_watchlist code=301379 name=天山电子 strategy=冷启动 bucket=B2 expires_at=2026-07-03 11:30:00 check_count=0",
                        "2026-07-03 10:35:55 - StockAnalyzer.Monitor - INFO - PAPER_TRAINING_PENDING_CHECK id=1 account=paper_watchlist code=301379 name=天山电子 strategy=冷启动 reason= max_buy_change=",
                        "2026-07-03 10:35:56 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=1 account=paper_watchlist code=301379 name=天山电子 strategy=冷启动 reason=量比不足: 当前量比 0.80 < 门槛 0.9 (无量拉升)",
                        "2026-07-03 10:35:56 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=2 account=paper_watchlist code=600001 name=日限额样本 strategy=冷启动 reason=paper weak daily cap reached: 2/2",
                        "2026-07-03 10:35:57 - StockAnalyzer.Monitor - WARNING - ⛔ 动态入场门禁拦截: 安洁科技(002635) strategy=集合竞价 action=BLOCK tags=PERMISSION_BLOCK reason=permission_matrix regime=storm_market key=* action=BLOCK | 弱市样本不足禁止自动买入: samples=5 < 30 | 弱市当前涨幅过大禁止自动买入: change=9.98% > 2.50%",
                        "2026-07-03 13:45:27 - StockAnalyzer.Monitor - INFO - SIM_TRADE_BUY account=paper_watchlist code=002714 name=牧原股份 strategy=冷启动 time=2026-07-03 13:45:09 qty=100 price=37.040 cost=3705.11 cash_before=10000.00 pending_id=6696 reason=dynamic_window_confirm",
                        "2026-07-03 14:25:20 - StockAnalyzer.Monitor - INFO - SIM_TRADE_SELL account=paper_watchlist code=002714 name=牧原股份 strategy=冷启动 time=2026-07-03 14:25:08 qty=100 buy=37.040 sell=37.990 pnl=95.00 pnl_pct=2.56 reason=paper短线止盈",
                    ]
                ),
                encoding="utf-8",
            )

            report = analyze_log(path)

        self.assertEqual(report["counts"]["paper_focus_pending"], 1)
        self.assertEqual(report["counts"]["paper_training_check"], 1)
        self.assertEqual(report["counts"]["pending_check"], 1)
        self.assertEqual(report["counts"]["pending_skip"], 2)
        self.assertEqual(report["paper_buy_count"], 1)
        self.assertEqual(report["paper_sell_count"], 1)
        self.assertEqual(report["pending_by_strategy"]["冷启动"], 1)
        self.assertEqual(report["top_skip_reasons"]["量比不足"], 1)
        self.assertEqual(report["top_skip_reasons"]["paper弱市每日上限"], 1)
        self.assertEqual(report["top_gate_block_reasons"]["暴雨矩阵 BLOCK"], 1)
        self.assertEqual(report["task_counts"]["P01_PAPER_SENTINEL"], 2)

    def test_build_report_marks_new_bypass_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260704.log"
            path.write_text(
                "2026-07-04 10:00:00 - StockAnalyzer.Executor - INFO - PAPER_BOARD_FILTER_BYPASS account=paper_watchlist code=300139 name=晓程科技 board=创业板 allowed=['main'] reason=主板权限过滤拦截\n",
                encoding="utf-8",
            )

            report = build_report([path])

        self.assertTrue(report["totals"]["new_bypass_seen"])
        self.assertEqual(report["totals"]["counts"]["paper_board_bypass"], 1)

    def test_shadow_loss_metrics_only_count_paper_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260710.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-07-10 10:00:00 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=1 account=main code=600001 name=主账户 strategy=冷启动 reason=retry cooldown: last_checked=2026-07-10 09:59:00 cooldown=300s",
                        "2026-07-10 10:00:01 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=2 account=watchlist code=600002 name=观察账户 strategy=冷启动 reason=retry cooldown: last_checked=2026-07-10 09:59:01 cooldown=300s",
                        "2026-07-10 10:00:02 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=3 account=paper_main code=600003 name=影子主账户 strategy=冷启动 reason=retry cooldown: last_checked=2026-07-10 09:59:02 cooldown=120s",
                        "2026-07-10 10:00:03 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=4 account=paper_watchlist code=600004 name=影子观察账户 strategy=冷启动 reason=retry cooldown: last_checked=2026-07-10 09:59:03 cooldown=120s",
                        "2026-07-10 10:00:04 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=5 account=main code=600005 name=主账户预算 strategy=冷启动 reason=budget too small",
                        "2026-07-10 10:00:05 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=6 account=paper_main code=600006 name=影子预算 strategy=冷启动 reason=budget too small",
                        "2026-07-10 10:00:06 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=7 account=watchlist code=600007 name=观察窗口 strategy=冷启动 reason=window not allowed: strategy=冷启动 bucket=B5 allowed=B1,B2",
                        "2026-07-10 10:00:07 - StockAnalyzer.Monitor - INFO - PENDING_SKIP id=8 account=paper_watchlist code=600008 name=影子窗口 strategy=冷启动 reason=window not allowed: strategy=冷启动 bucket=B5 allowed=B1,B2",
                    ]
                ),
                encoding="utf-8",
            )

            report = build_report([path])

        self.assertEqual(report["totals"]["counts"]["pending_skip"], 4)
        self.assertEqual(report["totals"]["top_skip_reasons"]["复核冷却"], 2)
        self.assertEqual(report["totals"]["top_skip_reasons"]["budget too small"], 1)
        self.assertEqual(report["totals"]["top_skip_reasons"]["窗口不允许"], 1)
        self.assertEqual(report["health"]["metrics"]["retry_cooldown"], 2)
        self.assertEqual(report["health"]["metrics"]["budget_too_small"], 1)
        self.assertEqual(report["health"]["metrics"]["window_not_allowed"], 1)

    def test_task_coverage_detects_missing_openclaw_shadow_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260710.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-07-10 09:00:56 - StockAnalyzer - INFO - Starting Stock Analyzer in macro mode...",
                        "2026-07-10 09:00:56 - StockAnalyzer - INFO - Runtime flags: queue_entry=False auto_trade=False paper_trade=False monitor=False no_email=False date= strategy=all",
                        "Subject: 🧪【影子账户训练现金校准】2026-07-10",
                    ]
                ),
                encoding="utf-8",
            )

            report = build_report([path])

        coverage = report["totals"]["task_coverage"]
        self.assertEqual(coverage["paper_task_status"], "FAIL")
        missing_names = [item["name"] for item in coverage["missing_paper_tasks"]]
        self.assertIn("P01 影子 paper-only 哨兵", missing_names)
        self.assertIn("P09 影子重点雷达入队", missing_names)
        self.assertIn("任务覆盖: FAIL", format_text_report(report))
        self.assertTrue(any("影子任务覆盖不足" in item for item in report["health"]["recommendations"]))

    def test_task_coverage_passes_when_core_shadow_tasks_are_seen_on_trade_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260710.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-07-10 09:05:00 - StockAnalyzer.Reporter - INFO - Subject: 🧪【影子账户训练现金校准】2026-07-10",
                        "2026-07-10 09:06:00 - StockAnalyzer.Reporter - INFO - Subject: 🧪【影子训练盘前预检】PASS 2026-07-10",
                        "2026-07-10 09:26:00 - StockAnalyzer.Monitor - INFO - Monitor paper-only enabled; only paper_* pending entries and positions will be processed.",
                        "2026-07-10 09:26:05 - StockAnalyzer.Monitor - INFO - PAPER_ONLY_PENDING_SCAN trade_date=2026-07-10 bucket=- loaded=0 accounts=paper_main,paper_watchlist",
                        "2026-07-10 09:27:00 - StockAnalyzer - INFO - Starting Stock Analyzer in pre_market mode...",
                        "2026-07-10 09:27:00 - StockAnalyzer - INFO - Runtime flags: queue_entry=True auto_trade=False paper_trade=True monitor=False no_email=False date= strategy=all",
                        "2026-07-10 10:01:00 - StockAnalyzer - INFO - Starting Stock Analyzer in watchlist mode...",
                        "2026-07-10 10:01:00 - StockAnalyzer - INFO - Runtime flags: queue_entry=True auto_trade=False paper_trade=True monitor=False no_email=False date= strategy=all",
                        "2026-07-10 14:31:00 - StockAnalyzer - INFO - Starting Stock Analyzer in afternoon mode...",
                        "2026-07-10 14:31:00 - StockAnalyzer - INFO - Runtime flags: queue_entry=True auto_trade=False paper_trade=True monitor=False no_email=False date= strategy=all",
                        "2026-07-10 10:35:00 - StockAnalyzer - INFO - Starting Stock Analyzer in focus_monitor mode...",
                        "2026-07-10 10:35:00 - StockAnalyzer - INFO - Runtime flags: queue_entry=True auto_trade=False paper_trade=True monitor=False no_email=False date= strategy=all",
                    ]
                ),
                encoding="utf-8",
            )

            report = build_report([path])

        coverage = report["totals"]["task_coverage"]
        self.assertEqual(coverage["paper_task_status"], "PASS")
        self.assertEqual(coverage["missing_paper_tasks"], [])

    def test_non_trade_day_log_is_not_treated_as_missing_shadow_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_analyzer-20260711.log"
            path.write_text(
                "\n".join(
                    [
                        "2026-07-11 10:00:06 - StockAnalyzer - INFO - Runtime flags: queue_entry=False auto_trade=False paper_trade=False monitor=False no_email=False date= strategy=all",
                        "2026-07-11 10:00:09 - StockAnalyzer.Reporter - INFO - EMAIL_RESULT status=SENT subject=✨【策略进化】自学习引擎周报 2026-07-11",
                    ]
                ),
                encoding="utf-8",
            )

            report = build_report([path])

        coverage = report["totals"]["task_coverage"]
        self.assertEqual(coverage["paper_task_status"], "N/A")
        self.assertEqual(coverage["missing_paper_tasks"], [])
        self.assertTrue(report["totals"]["contains_only_non_trade_days"])
        self.assertEqual(report["health"]["overall"], "N/A")
        self.assertTrue(any("非交易日" in item for item in report["health"]["recommendations"]))

    def test_health_warns_on_training_friction_but_passes_core_pipeline(self):
        report = {
            "totals": {
                "counts": {
                    "paper_focus_pending": 20,
                    "paper_strong_pending": 2,
                    "pending_check": 30,
                    "paper_training_check": 30,
                },
                "paper_buy_count": 0,
                "top_skip_reasons": {
                    "budget too small": 7,
                    "窗口不允许": 15,
                    "复核冷却": 40,
                },
            }
        }

        health = evaluate_health(report)

        self.assertEqual(health["overall"], "WARN")
        by_name = {row["name"]: row for row in health["checks"]}
        self.assertEqual(by_name["paper_pending"]["status"], "PASS")
        self.assertEqual(by_name["pending_checks"]["status"], "PASS")
        self.assertEqual(by_name["paper_training_checks"]["status"], "PASS")
        self.assertEqual(by_name["paper_buys"]["status"], "WARN")
        self.assertEqual(by_name["budget_too_small"]["status"], "WARN")
        self.assertEqual(by_name["window_not_allowed"]["status"], "WARN")
        self.assertEqual(by_name["retry_cooldown"]["status"], "WARN")
        self.assertTrue(any("影子买入不足" in item for item in health["recommendations"]))
        self.assertTrue(any("预算不足偏高" in item for item in health["recommendations"]))
        self.assertTrue(any("窗口不允许偏高" in item for item in health["recommendations"]))
        self.assertTrue(any("复核冷却偏高" in item for item in health["recommendations"]))
        self.assertTrue(any("scan_interval_sec" in item for item in health["recommendations"]))

    def test_health_fails_when_pipeline_did_not_run(self):
        report = {
            "totals": {
                "counts": {
                    "paper_focus_pending": 0,
                    "pending_check": 0,
                },
                "paper_buy_count": 0,
                "top_skip_reasons": {},
            }
        }

        health = evaluate_health(report)

        self.assertEqual(health["overall"], "FAIL")
        by_name = {row["name"]: row for row in health["checks"]}
        self.assertEqual(by_name["paper_pending"]["status"], "FAIL")
        self.assertEqual(by_name["pending_checks"]["status"], "FAIL")
        self.assertTrue(any("paper 入队不足" in item for item in health["recommendations"]))
        self.assertTrue(any("哨兵复核不足" in item for item in health["recommendations"]))

    def test_format_text_report_includes_health(self):
        report = {
            "files": [],
            "totals": {
                "counts": {"paper_focus_pending": 12, "pending_check": 14, "paper_training_check": 14, "paper_board_bypass": 1},
                "paper_buy_count": 1,
                "paper_sell_count": 0,
                "pending_by_strategy": {"冷启动": 12},
                "top_skip_reasons": {},
                "top_gate_block_reasons": {},
                "new_bypass_seen": True,
                "buys": [],
                "sells": [],
            },
        }
        report["health"] = evaluate_health(report, expect_bypass=True)

        text = format_text_report(report)

        self.assertIn("训练验收: PASS", text)
        self.assertIn("paper 入队 12", text)
        self.assertIn("新版 paper 过滤绕过标签出现: 是", text)
        self.assertIn("下一步建议:", text)
        self.assertIn("影子训练链路达标", text)

    def test_recommendations_call_out_missing_bypass_when_expected(self):
        recommendations = build_recommendations(
            {
                "paper_pending": 20,
                "pending_checks": 30,
                "paper_training_checks": 30,
                "paper_buys": 2,
                "paper_filter_bypass": 0,
                "budget_too_small": 0,
                "window_not_allowed": 0,
                "retry_cooldown": 0,
            },
            {
                "min_pending": 10,
                "min_checks": 10,
                "min_training_checks": 1,
                "min_buys": 1,
                "max_budget_too_small": 3,
                "max_window_not_allowed": 10,
                "max_retry_cooldown": 30,
            },
            expect_bypass=True,
        )

        self.assertTrue(any("未看到 paper 过滤绕过标签" in item for item in recommendations))

    def test_recommendations_call_out_missing_paper_training_check(self):
        recommendations = build_recommendations(
            {
                "paper_pending": 20,
                "pending_checks": 30,
                "paper_training_checks": 0,
                "paper_buys": 2,
                "paper_filter_bypass": 1,
                "budget_too_small": 0,
                "window_not_allowed": 0,
                "retry_cooldown": 0,
            },
            {
                "min_pending": 10,
                "min_checks": 10,
                "min_training_checks": 1,
                "min_buys": 1,
                "max_budget_too_small": 3,
                "max_window_not_allowed": 10,
                "max_retry_cooldown": 30,
            },
        )

        self.assertTrue(any("PAPER_TRAINING_PENDING_CHECK" in item for item in recommendations))

    @patch("tools.diagnostics.analyze_shadow_training_logs.log_report_snapshot")
    @patch("tools.diagnostics.analyze_shadow_training_logs.Reporter")
    def test_send_email_report_uses_health_status_subject(self, reporter_cls, snapshot):
        reporter_cls.return_value.send_email.return_value = True
        report = {
            "files": [],
            "totals": {
                "counts": {"paper_focus_pending": 12, "pending_check": 14, "paper_training_check": 14, "paper_board_bypass": 1},
                "paper_buy_count": 1,
                "paper_sell_count": 0,
                "pending_by_strategy": {},
                "top_skip_reasons": {},
                "top_gate_block_reasons": {},
                "new_bypass_seen": False,
                "buys": [],
                "sells": [],
            },
        }
        report["health"] = evaluate_health(report)

        sent = send_email_report(report, dates=["2026-07-06"])

        self.assertTrue(sent)
        subject, content = reporter_cls.return_value.send_email.call_args.args
        self.assertTrue(subject.startswith("🧪【影子训练验收】PASS 2026-07-06"))
        self.assertIn("训练验收: PASS", content)
        snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
