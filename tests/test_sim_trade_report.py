import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import report_sim_trades as report  # noqa: E402
from tools.diagnostics import check_pending_entry_events as pending_diag  # noqa: E402
import main as stock_main  # noqa: E402
import monitor as stock_monitor  # noqa: E402
from core.reporter import Reporter  # noqa: E402
from core.portfolio import PortfolioManager  # noqa: E402
from core.entry_flow import verify_entry_flow  # noqa: E402
from core.position_sizer import PositionSizer  # noqa: E402
from core.risk_dashboard import RiskDashboard  # noqa: E402
from core.trade_auditor import TradeAuditor  # noqa: E402
from core.utils import get_dynamic_stop_loss  # noqa: E402


class SimTradeReportTest(unittest.TestCase):
    def test_closed_trades_fifo_pnl(self):
        transactions = [
            {
                "date": "2026-06-22 09:40:00",
                "account": "paper_main",
                "type": "BUY",
                "code": "600000",
                "name": "浦发银行",
                "price": 10.0,
                "quantity": 200,
                "source_strategy": "pre_market",
            },
            {
                "date": "2026-06-22 10:00:00",
                "account": "paper_main",
                "type": "BUY",
                "code": "600000",
                "name": "浦发银行",
                "price": 11.0,
                "quantity": 100,
                "source_strategy": "pre_market",
            },
            {
                "date": "2026-06-22 14:30:00",
                "account": "paper_main",
                "type": "SELL",
                "code": "600000",
                "name": "浦发银行",
                "price": 12.0,
                "quantity": 250,
                "reason": "止盈",
            },
        ]

        closed = report._closed_trades(transactions)

        self.assertEqual(len(closed), 2)
        self.assertEqual(closed[0]["quantity"], 200)
        self.assertEqual(closed[0]["pnl"], 400.0)
        self.assertEqual(closed[1]["quantity"], 50)
        self.assertEqual(closed[1]["pnl"], 50.0)

    def test_main_logs_snapshot_and_sends_email_without_markdown_output(self):
        transactions = [
            {
                "date": "2026-06-22 09:40:00",
                "account": "paper_main",
                "type": "BUY",
                "code": "600000",
                "name": "浦发银行",
                "price": 10.0,
                "quantity": 100,
                "source_strategy": "pre_market",
            },
            {
                "date": "2026-06-22 14:30:00",
                "account": "paper_main",
                "type": "SELL",
                "code": "600000",
                "name": "浦发银行",
                "price": 11.0,
                "quantity": 100,
                "reason": "止盈",
            },
        ]
        sent = MagicMock()
        pm = MagicMock()
        pm.load_cash.return_value = 20000.0

        with patch.object(report, "PortfolioManager", return_value=pm), \
             patch.object(report, "_query_transactions", return_value=transactions), \
             patch.object(report, "_query_pending_check_events", return_value=[]), \
             patch.object(report, "_open_positions", return_value=[]), \
             patch.object(report, "log_report_snapshot") as snapshot, \
             patch.object(report, "Reporter") as reporter_cls, \
             patch.object(sys, "argv", ["report_sim_trades.py", "--accounts", "paper_main", "--email"]):
            reporter_cls.return_value.send_email = sent
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                report.main()

        content = stdout.getvalue()
        self.assertIn("【模拟仓交易流水日报】", content)
        self.assertIn("- 生成时间:", content)
        self.assertIn("- 统计账户: 仿真主账户", content)
        self.assertIn("| 账户 | 买入时间 | 卖出时间 | 代码 | 名称 | 策略 | 股数 | 买入价 | 卖出价 | 成本 | 盈亏 | 盈亏比例 | 卖出原因 |", content)
        self.assertIn("| 账户 | 买入时间 | 代码 | 名称 | 策略 | 股数 | 买入价 | 当前价 | 成本 | 市值 | 浮动盈亏 | 浮动盈亏比例 |", content)
        self.assertIn("【动态入场复核事件】", content)
        self.assertIn("| 仿真主账户现金余额 |", content)
        self.assertIn("| 模拟现金合计 |", content)
        self.assertIn("| 仿真主账户 |", content)
        self.assertIn("600000", content)
        self.assertIn("100.00", content)
        self.assertNotIn("Generated at", content)
        self.assertNotIn("- 统计账户: paper_main", content)
        self.assertNotIn("| Account |", content)
        snapshot.assert_called_once()
        sent.assert_called_once()
        subject, body = sent.call_args.args
        self.assertTrue(subject.startswith("📊【模拟仓日报】"))
        self.assertIn("【已平仓交易】", body)

        report_dir = ROOT / "reports" / "sim_trades"
        self.assertFalse((report_dir / "latest_paper_trade_ledger.md").exists())

    def test_monitor_contains_paper_trade_log_and_email_markers(self):
        monitor_source = (ROOT / "monitor.py").read_text(encoding="utf-8")

        self.assertIn("SIM_TRADE_BUY account=%s", monitor_source)
        self.assertIn("SIM_TRADE_SELL account=%s", monitor_source)
        self.assertIn("【模拟仓动态入场】", monitor_source)
        self.assertIn("【模拟仓{paper_sell_tag}】", monitor_source)
        self.assertIn("paper_only", monitor_source)

    def test_monitor_uses_paper_windows_for_all_paper_training_pending(self):
        strategy_windows = {"午盘精选": ["B4", "B5"]}
        paper_windows = {"*": ["B1", "B2", "B3", "B4"], "午盘精选": ["B2", "B3", "B4", "B5"]}

        main_allowed = stock_monitor._resolve_pending_windows(
            "午盘精选",
            "main",
            {},
            strategy_windows,
            paper_windows,
        )
        paper_allowed = stock_monitor._resolve_pending_windows(
            "午盘精选",
            "paper_watchlist",
            {"target_account": "paper_watchlist"},
            strategy_windows,
            paper_windows,
        )

        self.assertEqual(main_allowed, ["B4", "B5"])
        self.assertEqual(paper_allowed, ["B2", "B3", "B4", "B5"])
        self.assertIn("B2", paper_allowed)
        self.assertNotIn("B2", main_allowed)

    def test_monitor_uses_paper_entry_policy_for_all_paper_training_pending(self):
        main_policy = stock_monitor._paper_entry_policy_for_pending(
            "main",
            {"target_account": "main"},
        )
        paper_policy = stock_monitor._paper_entry_policy_for_pending(
            "paper_watchlist",
            {"target_account": "paper_watchlist"},
        )
        strong_policy = stock_monitor._paper_entry_policy_for_pending(
            "paper_main",
            {"target_account": "paper_main", "paper_strong_entry": True, "paper_max_buy_change": 20.2},
        )

        self.assertIsNone(main_policy)
        self.assertEqual(paper_policy, {"enabled": True})
        self.assertEqual(strong_policy["enabled"], True)
        self.assertEqual(strong_policy["max_buy_change"], 20.2)

    def test_monitor_scopes_main_and_paper_pending_accounts(self):
        self.assertEqual(
            stock_monitor._monitor_pending_target_accounts(paper_only=False),
            ("main", "watchlist"),
        )
        self.assertEqual(
            stock_monitor._monitor_pending_target_accounts(paper_only=True),
            ("paper_main", "paper_watchlist"),
        )

    def test_monitor_scopes_main_and_paper_positions(self):
        self.assertTrue(stock_monitor._position_visible_to_monitor({"account": "main"}, paper_only=False))
        self.assertTrue(stock_monitor._position_visible_to_monitor({"account": "watchlist"}, paper_only=False))
        self.assertFalse(stock_monitor._position_visible_to_monitor({"account": "paper_main"}, paper_only=False))
        self.assertFalse(stock_monitor._position_visible_to_monitor({"account": "rescue"}, paper_only=False))
        self.assertTrue(stock_monitor._position_visible_to_monitor({"account": "paper_watchlist"}, paper_only=True))
        self.assertFalse(stock_monitor._position_visible_to_monitor({"account": "main"}, paper_only=True))

    def test_monitor_caps_paper_weak_gate_buys_per_day(self):
        class FakePortfolio:
            def __init__(self, count):
                self.count = count

            def count_paper_weak_buys(self, account, trade_date=None):
                self.args = {"account": account, "trade_date": trade_date}
                return self.count

        pre_gate = {
            "tags": ["PAPER_WEAK_SAMPLE_FLOOR_USED"],
            "metrics": {
                "paper_weak_market_gate_experiment": {
                    "max_per_day": 2,
                }
            },
        }

        capped = stock_monitor._paper_weak_daily_cap_reason(
            FakePortfolio(2),
            "paper_watchlist",
            pre_gate,
            "2026-07-09",
        )
        available = stock_monitor._paper_weak_daily_cap_reason(
            FakePortfolio(1),
            "paper_watchlist",
            pre_gate,
            "2026-07-09",
        )
        main = stock_monitor._paper_weak_daily_cap_reason(
            FakePortfolio(9),
            "watchlist",
            pre_gate,
            "2026-07-09",
        )
        not_relaxed = stock_monitor._paper_weak_daily_cap_reason(
            FakePortfolio(9),
            "paper_watchlist",
            {"tags": ["PAPER_WEAK_SAMPLE_FLOOR_EXPERIMENT"], "metrics": pre_gate["metrics"]},
            "2026-07-09",
        )

        self.assertEqual(capped, "paper weak daily cap reached: 2/2")
        self.assertIsNone(available)
        self.assertIsNone(main)
        self.assertIsNone(not_relaxed)

    def test_same_day_position_high_water_ignores_pre_entry_day_high(self):
        now = datetime(2026, 7, 2, 14, 55, 0)
        position = {
            "created_at": "2026-07-02 13:45:48",
            "highest_price": 44.51,
        }
        quote = {"high": 45.69}

        high = stock_monitor._effective_position_high(
            position,
            quote,
            curr_price=44.11,
            buy_price=44.51,
            now=now,
        )

        self.assertEqual(high, 44.51)

    def test_overnight_position_high_water_can_use_day_high(self):
        now = datetime(2026, 7, 2, 14, 55, 0)
        position = {
            "created_at": "2026-07-01 13:45:48",
            "highest_price": 44.51,
        }
        quote = {"high": 45.69}

        high = stock_monitor._effective_position_high(
            position,
            quote,
            curr_price=44.11,
            buy_price=44.51,
            now=now,
        )

        self.assertEqual(high, 45.69)

    def test_risk_dashboard_uses_same_day_position_high_water(self):
        now = datetime(2026, 7, 2, 14, 55, 0)
        position = {
            "created_at": "2026-07-02 13:45:48",
            "highest_price": 44.51,
        }
        quote = {"high": 45.69}

        high = RiskDashboard._effective_position_high(
            position,
            quote,
            curr_price=44.11,
            buy_price=44.51,
            now=now,
        )

        self.assertEqual(high, 44.51)

    def test_position_sizer_applies_paper_account_override_only(self):
        class FakePortfolio:
            def _get_connection(self):
                return None

        config = {
            "base_position_pct": 0.20,
            "absolute_max_position_pct": 0.40,
            "min_order_amount": 5000,
            "min_order_position_pct_floor": 0.08,
            "market_regime_multiplier": {"normal_uptrend": 1.0},
            "volatility_multiplier": {"unknown": 1.0},
            "daily_loss": {"enabled": False},
            "consecutive_loss": {"enabled": False},
            "account_overrides": {
                "paper_*": {
                    "base_position_pct": 0.10,
                    "absolute_max_position_pct": 0.20,
                    "min_order_amount": 3000,
                    "min_order_position_pct_floor": 0.04,
                }
            },
        }
        sizer = PositionSizer(FakePortfolio(), config=config)

        main = sizer.calculate(account="main", price=10.0, cash_available=50000.0, market_env={"regime": "normal_uptrend"})
        paper = sizer.calculate(account="paper_watchlist", price=10.0, cash_available=50000.0, market_env={"regime": "normal_uptrend"})

        self.assertEqual(main["quantity"], 1000)
        self.assertEqual(paper["quantity"], 500)
        self.assertAlmostEqual(main["position_pct"], 0.20)
        self.assertAlmostEqual(paper["position_pct"], 0.10)

    def test_position_sizer_paper_round_lot_floor_keeps_training_sample(self):
        class FakePortfolio:
            def _get_connection(self):
                return None

        config = {
            "base_position_pct": 0.20,
            "absolute_max_position_pct": 0.40,
            "min_order_amount": 5000,
            "min_order_position_pct_floor": 0.08,
            "market_regime_multiplier": {"storm_market": 0.0},
            "volatility_multiplier": {"unknown": 0.8},
            "daily_loss": {"enabled": False},
            "consecutive_loss": {"enabled": False},
            "account_overrides": {
                "paper_*": {
                    "base_position_pct": 0.10,
                    "absolute_max_position_pct": 0.20,
                    "min_order_amount": 5000,
                    "min_order_position_pct_floor": 0.005,
                    "ensure_round_lot_when_cash_available": True,
                    "market_regime_multiplier": {"storm_market": 0.6},
                }
            },
        }
        sizer = PositionSizer(FakePortfolio(), config=config)

        main = sizer.calculate(
            account="main",
            price=68.91,
            cash_available=100000.0,
            market_env={"regime": "storm_market"},
            pre_gate={"action": "LOW_SIZE_CONFIRM"},
        )
        paper = sizer.calculate(
            account="paper_watchlist",
            price=68.91,
            cash_available=100000.0,
            market_env={"regime": "storm_market"},
            pre_gate={"action": "LOW_SIZE_CONFIRM"},
        )

        self.assertEqual(main["quantity"], 0)
        self.assertEqual(paper["quantity"], 100)
        self.assertIn("round_lot_min=6891", paper["reasons"])

    def test_position_sizer_paper_consecutive_losses_reduce_but_do_not_hard_stop(self):
        class FakePortfolio:
            def _get_connection(self):
                return None

        class LossySizer(PositionSizer):
            def _recent_trade_loss_stats(self, account, lookback=5):
                return {"consecutive_losses": 3, "recent_closed": 3}

        config = {
            "base_position_pct": 0.20,
            "absolute_max_position_pct": 0.40,
            "min_order_amount": 5000,
            "min_order_position_pct_floor": 0.08,
            "low_size_multiplier": 0.30,
            "market_regime_multiplier": {"weak_market": 0.35},
            "volatility_multiplier": {"unknown": 0.8},
            "daily_loss": {"enabled": False},
            "consecutive_loss": {
                "enabled": True,
                "lookback": 5,
                "warn_count": 2,
                "hard_stop_count": 3,
                "warn_multiplier": 0.5,
            },
            "account_overrides": {
                "paper_*": {
                    "base_position_pct": 0.10,
                    "absolute_max_position_pct": 0.20,
                    "min_order_amount": 5000,
                    "min_order_position_pct_floor": 0.001,
                    "ensure_round_lot_when_cash_available": True,
                    "market_regime_multiplier": {"weak_market": 0.5},
                    "consecutive_loss": {
                        "enabled": True,
                        "lookback": 5,
                        "warn_count": 2,
                        "hard_stop_count": 999,
                        "warn_multiplier": 0.7,
                    },
                }
            },
        }
        sizer = LossySizer(FakePortfolio(), config=config)

        main = sizer.calculate(
            account="main",
            price=52.13,
            cash_available=100000.0,
            market_env={"regime": "weak_market"},
            pre_gate={"action": "LOW_SIZE_CONFIRM"},
            max_position_mult=0.5,
        )
        paper = sizer.calculate(
            account="paper_watchlist",
            price=52.13,
            cash_available=100000.0,
            market_env={"regime": "weak_market"},
            pre_gate={"action": "LOW_SIZE_CONFIRM"},
            max_position_mult=0.5,
        )

        self.assertEqual(main["quantity"], 0)
        self.assertIn("consecutive_losses=3 hard_stop", main["reasons"])
        self.assertEqual(paper["quantity"], 100)
        self.assertIn("consecutive_losses=3 x0.70", paper["reasons"])
        self.assertIn("round_lot_min=5213", paper["reasons"])

    def test_current_config_paper_low_size_still_keeps_one_lot_sample(self):
        class FakePortfolio:
            def _get_connection(self):
                return None

        stock_monitor.Config.load_strategy_config()
        sizer = PositionSizer(FakePortfolio())

        paper = sizer.calculate(
            account="paper_watchlist",
            price=52.13,
            cash_available=100000.0,
            market_env={"regime": "weak_market"},
            pre_gate={"action": "LOW_SIZE_CONFIRM"},
            max_position_mult=0.5,
        )

        self.assertGreaterEqual(paper["quantity"], 100)
        self.assertIn("round_lot_min=5213", paper["reasons"])

    def test_monitor_paper_scan_interval_tracks_training_cooldown(self):
        with patch.object(
            stock_monitor.Config,
            "STRATEGY",
            {"paper_all_pool_execution": {"retry_cooldown_sec": 120}},
        ):
            self.assertEqual(stock_monitor._pending_entry_scan_interval(paper_only=True), 120)
            self.assertEqual(stock_monitor._pending_entry_scan_interval(paper_only=False), 60)

        with patch.object(
            stock_monitor.Config,
            "STRATEGY",
            {"paper_all_pool_execution": {"scan_interval_sec": 180, "retry_cooldown_sec": 120}},
        ):
            self.assertEqual(stock_monitor._pending_entry_scan_interval(paper_only=True), 180)

    def test_monitor_exit_settings_apply_paper_policy_only(self):
        default_ladder = [(0.03, 0.3), (0.05, 0.3)]
        trailing = {"☀️晴天": 0.03}

        main = stock_monitor._exit_settings_for_account(
            "main",
            base_stop_loss=-0.03,
            max_hold_days=5,
            min_hold_ret=0.01,
            ladder=default_ladder,
            trailing_config=trailing,
        )
        paper = stock_monitor._exit_settings_for_account(
            "paper_watchlist",
            base_stop_loss=-0.03,
            max_hold_days=5,
            min_hold_ret=0.01,
            ladder=default_ladder,
            trailing_config=trailing,
        )

        self.assertEqual(main["policy_name"], "default")
        self.assertEqual(main["ladder"], default_ladder)
        self.assertAlmostEqual(main["stop_loss"], -0.03)
        self.assertEqual(paper["policy_name"], "paper_short_cycle_exit_v1")
        self.assertEqual(paper["ladder"], [(0.025, 1.0)])
        self.assertAlmostEqual(paper["stop_loss"], -0.025)
        self.assertEqual(paper["max_hold_days"], 2)
        self.assertAlmostEqual(paper["min_hold_return"], 0.015)

    def test_risk_dashboard_uses_paper_exit_stop_loss(self):
        main = RiskDashboard._exit_stop_settings("main")
        paper = RiskDashboard._exit_stop_settings("paper_watchlist")

        self.assertAlmostEqual(main["stop_loss"], -0.03)
        self.assertAlmostEqual(paper["stop_loss"], -0.025)
        self.assertAlmostEqual(max(get_dynamic_stop_loss(0.0, paper["stop_loss"], "☀️晴天"), paper["stop_loss"]), -0.025)

    def test_reporter_uses_simulated_account_language(self):
        reporter = Reporter()

        buy = reporter.format_buy_alert(
            "600000",
            "浦发银行",
            10.23,
            1000,
            10233.07,
            "pre_market(动态时间窗)",
            100000.0,
            89766.93,
            account="paper_main",
        )
        sell = reporter.format_sell_alert(
            "600000",
            "浦发银行",
            10.88,
            1000,
            646.74,
            6.32,
            "测试止盈",
            100646.74,
            account="paper_main",
        )

        self.assertIn("模拟仓买入执行通知", buy)
        self.assertIn("注: 模拟仓执行，不构成投资建议", buy)
        self.assertIn("模拟仓卖出执行通知", sell)
        self.assertIn("注: 模拟仓执行，不构成投资建议", sell)

    def test_pending_upsert_isolated_by_target_account(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def close(self):
                pass

        conn = FakeConn()
        pm = PortfolioManager()
        pm._get_connection = lambda: conn

        pm.upsert_pending_entry_signal(
            trade_date="2026-06-22",
            code="600000",
            source_strategy="pre_market",
            payload={"target_account": "main"},
        )
        pm.upsert_pending_entry_signal(
            trade_date="2026-06-22",
            code="600000",
            source_strategy="pre_market",
            payload={"target_account": "paper_main"},
        )

        sql_1, params_1 = conn.cursor_obj.calls[0]
        sql_2, params_2 = conn.cursor_obj.calls[1]
        self.assertIn("target_account", sql_1)
        self.assertIn("target_account", sql_2)
        self.assertEqual(params_1[5], "main")
        self.assertEqual(params_2[5], "paper_main")

    def test_pending_loader_can_filter_target_accounts(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchall(self):
                return []

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                pass

        conn = FakeConn()
        pm = PortfolioManager()
        pm._get_connection = lambda: conn

        rows = pm.load_pending_entry_signals(
            trade_date="2026-06-23",
            now_dt=datetime(2026, 6, 23, 9, 45),
            limit=80,
            target_accounts=("paper_main", "paper_watchlist"),
        )

        sql, params = conn.cursor_obj.calls[0]
        self.assertEqual(rows, [])
        self.assertIn("target_account IN (%s,%s)", sql)
        self.assertEqual(params, ("2026-06-23", "2026-06-23 09:45:00", "paper_main", "paper_watchlist", 80))

    def test_pending_expiry_can_filter_target_accounts(self):
        class FakeCursor:
            rowcount = 2

            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def close(self):
                pass

        conn = FakeConn()
        pm = PortfolioManager()
        pm._get_connection = lambda: conn

        count = pm.expire_old_pending_entries(
            trade_date="2026-06-23",
            now_dt=datetime(2026, 6, 23, 10, 1),
            target_accounts=("paper_main", "paper_watchlist"),
        )

        sql, params = conn.cursor_obj.calls[0]
        self.assertEqual(count, 2)
        self.assertIn("target_account IN (%s,%s)", sql)
        self.assertEqual(params, ("2026-06-23 10:01:00", "2026-06-23", "2026-06-23 10:01:00", "paper_main", "paper_watchlist"))

    def test_pending_check_event_schema_and_writer(self):
        portfolio_source = (ROOT / "core" / "portfolio.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS pending_entry_check_events", portfolio_source)
        self.assertIn("INDEX idx_trade_account_strategy", portfolio_source)
        self.assertIn("def log_pending_entry_check_event", portfolio_source)

        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def close(self):
                pass

        conn = FakeConn()
        pm = PortfolioManager()
        pm._get_connection = lambda: conn

        pm.log_pending_entry_check_event(
            pending_id=123,
            trade_date="2026-07-02",
            code="600000",
            account="paper_watchlist",
            strategy="龙头跟踪",
            check_time=datetime(2026, 7, 2, 10, 15),
            bucket="B2",
            price=10.5,
            pre_close=10.0,
            change_pct=5.0,
            volume_ratio=1.6,
            price_vwap_ratio=1.02,
            decision="SKIP",
            reason="量比不足",
            status_before="PENDING",
            status_after="PENDING",
            check_count=2,
            payload={"paper_executable_pool": True},
        )

        sql, params = conn.cursor_obj.calls[0]
        self.assertIn("INSERT INTO pending_entry_check_events", sql)
        self.assertEqual(params[0], 123)
        self.assertEqual(params[1], "2026-07-02")
        self.assertEqual(params[3], "paper_watchlist")
        self.assertEqual(params[5], "2026-07-02 10:15:00")
        self.assertEqual(params[12], "SKIP")
        self.assertEqual(params[13], "量比不足")
        self.assertIn("paper_executable_pool", params[17])

    def test_pending_entry_event_diagnostics_queries(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchall(self):
                sql = self.calls[-1][0]
                if "SHOW TABLES" in sql:
                    return [{"Tables_in_test": "ok"}]
                if "COUNT(DISTINCT pending_id)" in sql:
                    return [{"cnt": 3, "pending_cnt": 2}]
                if "COUNT(1) AS cnt FROM pending_entry_signals" in sql:
                    return [{"cnt": 2}]
                if "FROM transactions" in sql:
                    return [{"account": "paper_watchlist", "strategy": "龙头跟踪", "cnt": 1, "quantity": 100, "amount": 1000.0}]
                if "GROUP BY account, strategy, decision, reason" in sql:
                    return [{"account": "paper_watchlist", "strategy": "龙头跟踪", "decision": "SKIP", "reason": "量比不足", "cnt": 2}]
                if "GROUP BY account, strategy, decision" in sql:
                    return [{"account": "paper_watchlist", "strategy": "龙头跟踪", "decision": "SKIP", "cnt": 2}]
                if "GROUP BY target_account" in sql:
                    return [{"account": "paper_watchlist", "strategy": "龙头跟踪", "status": "PENDING", "cnt": 2}]
                return []

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                pass

        pm = MagicMock()
        pm.init_tables = MagicMock()
        pm._get_connection.return_value = FakeConn()

        with patch.object(pending_diag, "PortfolioManager", return_value=pm):
            result = pending_diag.build_report("2026-07-02", ["paper_watchlist"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["coverage"]["pending_total"], 2)
        self.assertEqual(result["coverage"]["event_total"], 3)
        self.assertEqual(result["coverage"]["event_pending_total"], 2)
        self.assertEqual(result["coverage"]["checked_pending_ratio"], 1.0)
        self.assertEqual(result["events_by_decision"][0]["decision"], "SKIP")
        self.assertEqual(result["paper_buys"][0]["cnt"], 1)

    def test_trade_auditor_reports_pending_entry_check_events(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchall(self):
                sql = self.calls[-1][0]
                if "GROUP BY account, strategy, decision, reason" in sql:
                    return [{
                        "account": "paper_watchlist",
                        "strategy": "龙头跟踪",
                        "decision": "SKIP",
                        "reason": "VWAP偏离过高",
                        "cnt": 2,
                        "last_time": "2026-07-02 10:20:00",
                    }]
                return [{
                    "account": "paper_watchlist",
                    "strategy": "龙头跟踪",
                    "decision": "BOUGHT",
                    "cnt": 1,
                    "last_time": "2026-07-02 10:25:00",
                }]

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self, *args, **kwargs):
                return self.cursor_obj

            def close(self):
                pass

        pm = MagicMock()
        pm._get_connection.return_value = FakeConn()
        auditor = TradeAuditor(pm, data_provider=MagicMock())

        lines = auditor.audit_pending_entry_check_events()
        content = "\n".join(lines)

        self.assertIn("最近7天复核事件", content)
        self.assertIn("仿真巡航账户", content)
        self.assertIn("BOUGHT", content)
        self.assertIn("VWAP偏离过高", content)

    def test_pending_entry_rejects_recent_overheated_auction_candidate(self):
        class FakeProvider:
            def get_history_data(self, ts_code, count=30):
                self.last_request = (ts_code, count)
                closes = [5.70, 5.80, 5.85, 5.90, 5.95, 6.00, 6.05, 6.10, 6.15, 6.20,
                          6.25, 6.30, 6.35, 6.40, 6.45, 6.50, 6.55, 6.60, 6.65, 6.70,
                          6.55, 6.93, 6.77, 7.07, 7.52]
                return [
                    {"close": close, "pct_chg": 0.0}
                    for close in closes
                ]

        class FakeAnalyzer:
            def __init__(self):
                self.provider = FakeProvider()

            def _is_overheated(self, history, max_10d_gain=30, max_5d_gain=20, max_ma20_dev=25, max_consec_zt=3):
                closes = [float(r["close"]) for r in history]
                gain_5d = (closes[-1] - closes[-5]) / closes[-5] * 100
                gain_10d = (closes[-1] - closes[-10]) / closes[-10] * 100
                ma20 = sum(closes[-20:]) / 20
                ma20_dev = (closes[-1] - ma20) / ma20 * 100
                if gain_5d > max_5d_gain:
                    return True, f"5日涨幅{gain_5d:.1f}% > {max_5d_gain}%", gain_10d, ma20_dev
                if gain_10d > max_10d_gain:
                    return True, f"10日涨幅{gain_10d:.1f}% > {max_10d_gain}%", gain_10d, ma20_dev
                if ma20_dev > max_ma20_dev:
                    return True, f"偏离MA20 {ma20_dev:.1f}% > {max_ma20_dev}%", gain_10d, ma20_dev
                return False, "", gain_10d, ma20_dev

            def verify_money_flow(self, candidate, weather):
                return True, "验证通过"

        candidate = {
            "code": "000725",
            "ts_code": "000725.SZ",
            "name": "京东方Ａ",
            "strategy": "集合竞价",
            "price": 7.49,
            "pre_close": 7.07,
            "change": 5.9,
        }

        result = verify_entry_flow(
            candidate,
            analyzer=FakeAnalyzer(),
            market_env={"weather": "☀️晴天"},
            weather="☀️晴天",
            strategy="集合竞价",
            pending_retry=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["verification"], "recent_overheat_gate")
        self.assertIn("近期过热拦截", result["reason"])
        self.assertIn("5日涨幅", result["reason"])

    def test_queue_entry_writes_candidate_attempt_payload(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 23, 10, 5, 0)

        class FakeAnalyzer:
            def check_market_environment(self, date):
                return {
                    "weather": "晴天",
                    "is_safe": True,
                    "message": "normal",
                    "adjustments": {},
                    "regime": "normal_uptrend",
                    "risk_level": "normal",
                }

            def apply_negative_filters(self, candidates, weather):
                return candidates, []

            def apply_board_filter(self, candidates, allowed_boards):
                return candidates, []

        class FakeProvider:
            def get_industry_stats(self, date):
                return []

            def get_realtime_quotes(self, codes):
                return {
                    "600000.SH": {
                        "price": 10.0,
                        "pre_close": 9.8,
                        "open": 9.9,
                        "high": 10.2,
                        "low": 9.8,
                        "vol": 100000,
                        "amount": 1000000,
                    }
                }

        class FakePortfolio:
            def __init__(self):
                self.payloads = []

            def log_risk_event(self, **kwargs):
                pass

            def upsert_pending_entry_signal(self, **kwargs):
                self.payloads.append(kwargs["payload"])

            def expire_old_pending_entries(self, **kwargs):
                return 0

        args = type("Args", (), {
            "queue_entry": True,
            "auto_trade": False,
            "paper_trade": True,
            "mode": "watchlist",
            "date": "20260623",
            "monitor": False,
        })()
        result = {
            "watchlist_data": {
                "buy_candidates": [{
                    "code": "600000",
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "strategy": "备选池买入触发",
                    "score": 88,
                    "change": 2.0,
                    "open_change": 1.0,
                    "high": 10.2,
                    "pre_close": 9.8,
                }]
            }
        }
        fake_portfolio = FakePortfolio()

        with patch.object(stock_main, "datetime", FixedDatetime), \
             patch("core.utils.is_trading_hours", return_value=True), \
             patch.object(stock_main.Config, "get_allowed_boards", return_value=[]), \
             patch.dict(stock_main.Config.STRATEGY, {
                 "entry_policy": {
                     "enabled": True,
                     "default_model": "dynamic_window",
                     "models": {
                         "dynamic_window": {
                             "strategy_windows": {"备选池买入触发": ["B2"]},
                             "expire_at_bucket_end": True,
                         }
                     },
                 },
                 "attack_window_gate": {"enabled": False},
             }, clear=False):
            stock_main.execute_automated_strategies(
                FakeAnalyzer(),
                fake_portfolio,
                FakeProvider(),
                MagicMock(),
                result,
                {"positions": []},
                args,
            )

        self.assertEqual(result["entry_queue"]["created"], 1)
        self.assertEqual(len(fake_portfolio.payloads), 1)
        attempt = fake_portfolio.payloads[0]["candidate_attempt"]
        self.assertEqual(attempt["schema"], "candidate_attempt_v1")
        self.assertEqual(attempt["stage"], "pending_create")
        self.assertEqual(attempt["action"], "QUEUED")
        self.assertEqual(attempt["strategy"], "备选池买入触发")
        self.assertEqual(attempt["target_account"], "paper_watchlist")
        queued_audit = next(row for row in result["execution_audit"] if row.get("status") == "QUEUED")
        self.assertTrue(queued_audit["candidate_attempt"]["paper_trade"])

    def test_pre_pending_filters_write_candidate_attempt_payloads(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 23, 10, 5, 0)

        class FakeAnalyzer:
            def __init__(self, negative_rejected=None, board_rejected=None):
                self.negative_rejected = negative_rejected or []
                self.board_rejected = board_rejected or []

            def check_market_environment(self, date):
                return {
                    "weather": "晴天",
                    "is_safe": True,
                    "message": "normal",
                    "adjustments": {},
                    "regime": "normal_uptrend",
                    "risk_level": "normal",
                }

            def apply_negative_filters(self, candidates, weather):
                if self.negative_rejected:
                    return [], self.negative_rejected
                return candidates, []

            def apply_board_filter(self, candidates, allowed_boards):
                if self.board_rejected:
                    return [], self.board_rejected
                return candidates, []

        class FakeProvider:
            def get_industry_stats(self, date):
                return []

        class FakePortfolio:
            def __init__(self):
                self.events = []

            def log_risk_event(self, **kwargs):
                self.events.append(kwargs)

            def expire_old_pending_entries(self, **kwargs):
                return 0

        def run_case(analyzer, result, allowed_boards=None, paper_trade=True):
            args = type("Args", (), {
                "queue_entry": True,
                "auto_trade": False,
                "paper_trade": paper_trade,
                "mode": "watchlist",
                "date": "20260623",
                "monitor": False,
            })()
            fake_portfolio = FakePortfolio()
            with patch.object(stock_main, "datetime", FixedDatetime), \
                 patch("core.utils.is_trading_hours", return_value=True), \
                 patch.object(stock_main.Config, "get_allowed_boards", return_value=allowed_boards or []), \
                 patch.dict(stock_main.Config.RISK_MANAGEMENT, {"MAX_BUY_CHANGE": 7.0}, clear=False), \
                 patch.dict(stock_main.Config.STRATEGY, {
                     "entry_policy": {
                         "enabled": True,
                         "default_model": "dynamic_window",
                         "models": {"dynamic_window": {"strategy_windows": {"备选池买入触发": ["B2"]}}},
                     },
                     "attack_window_gate": {"enabled": False},
                 }, clear=False):
                stock_main.execute_automated_strategies(
                    analyzer,
                    fake_portfolio,
                    FakeProvider(),
                    MagicMock(),
                    result,
                    {"positions": []},
                    args,
                )
            return fake_portfolio.events, result["execution_audit"]

        cases = [
            (
                "WATCHLIST_LIMIT_TOUCH_FILTER",
                FakeAnalyzer(),
                {"watchlist_data": {"buy_candidates": [{
                    "code": "600001",
                    "ts_code": "600001.SH",
                    "name": "曾涨停样本",
                    "strategy": "备选池买入触发",
                    "score": 80,
                    "change": 4.0,
                    "open_change": 2.0,
                    "high": 11.0,
                    "pre_close": 10.0,
                }]}},
                None,
            ),
            (
                "WATCHLIST_OPEN_FADE_FILTER",
                FakeAnalyzer(),
                {"watchlist_data": {"buy_candidates": [{
                    "code": "600002",
                    "ts_code": "600002.SH",
                    "name": "高开低走样本",
                    "strategy": "备选池买入触发",
                    "score": 80,
                    "change": -1.0,
                    "open_change": 5.5,
                    "high": 10.5,
                    "pre_close": 10.0,
                }]}},
                None,
            ),
            (
                "NEGATIVE_FILTER",
                FakeAnalyzer(negative_rejected=[{
                    "code": "600003",
                    "ts_code": "600003.SH",
                    "name": "负面过滤样本",
                    "strategy": "备选池买入触发",
                    "reason": "测试负面原因",
                    "score": 80,
                    "change": 2.0,
                    "open_change": 1.0,
                    "high": 10.2,
                    "pre_close": 10.0,
                }]),
                {"watchlist_data": {"buy_candidates": [{
                    "code": "600003",
                    "ts_code": "600003.SH",
                    "name": "负面过滤样本",
                    "strategy": "备选池买入触发",
                    "score": 80,
                    "change": 2.0,
                    "open_change": 1.0,
                    "high": 10.2,
                    "pre_close": 10.0,
                }]}},
                None,
            ),
            (
                "BOARD_PERMISSION_FILTER",
                FakeAnalyzer(board_rejected=[{
                    "code": "300003",
                    "ts_code": "300003.SZ",
                    "name": "权限过滤样本",
                    "strategy": "备选池买入触发",
                    "reason": "创业板未开通",
                    "board": "创业板",
                    "score": 80,
                    "change": 2.0,
                    "open_change": 1.0,
                    "high": 10.2,
                    "pre_close": 10.0,
                }]),
                {"watchlist_data": {"buy_candidates": [{
                    "code": "300003",
                    "ts_code": "300003.SZ",
                    "name": "权限过滤样本",
                    "strategy": "备选池买入触发",
                    "score": 80,
                    "change": 2.0,
                    "open_change": 1.0,
                    "high": 10.2,
                    "pre_close": 10.0,
                }]}},
                ["主板"],
                False,
            ),
        ]

        for case in cases:
            expected_reason, analyzer, result, allowed_boards, *rest = case
            paper_trade = rest[0] if rest else True
            with self.subTest(expected_reason=expected_reason):
                events, audit_rows = run_case(analyzer, result, allowed_boards, paper_trade=paper_trade)
                self.assertEqual(len(events), 1)
                params = events[0]["params"]
                attempt = params["candidate_attempt"]
                self.assertEqual(attempt["schema"], "candidate_attempt_v1")
                self.assertEqual(attempt["stage"], "pre_pending_filter")
                self.assertEqual(attempt["action"], "SKIPPED")
                self.assertEqual(attempt["no_attempt_reason"], expected_reason)
                self.assertIn(expected_reason, attempt["filter_tags"])
                self.assertEqual(params["metrics"]["candidate_attempt"]["no_attempt_reason"], expected_reason)
                matched_audit = [row for row in audit_rows if row.get("candidate_attempt", {}).get("no_attempt_reason") == expected_reason]
                self.assertEqual(len(matched_audit), 1)

    def test_score_threshold_filter_writes_candidate_attempt_payload(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 23, 14, 35, 0)

        class FakeAnalyzer:
            def check_market_environment(self, date):
                return {
                    "weather": "多云",
                    "is_safe": False,
                    "message": "市场转弱",
                    "adjustments": {"score_threshold_mult": 1.5},
                    "regime": "weak_downtrend",
                    "risk_level": "medium",
                }

            def apply_negative_filters(self, candidates, weather):
                return candidates, []

            def apply_board_filter(self, candidates, allowed_boards):
                return candidates, []

        class FakeProvider:
            def get_industry_stats(self, date):
                return []

        class FakePortfolio:
            def __init__(self):
                self.events = []

            def log_risk_event(self, **kwargs):
                self.events.append(kwargs)

            def load_all_positions(self):
                return []

        args = type("Args", (), {
            "queue_entry": False,
            "auto_trade": True,
            "paper_trade": True,
            "mode": "afternoon",
            "date": "20260623",
            "monitor": False,
        })()
        result = {
            "hot_stocks": [{
                "code": "600004",
                "ts_code": "600004.SH",
                "name": "分数过滤样本",
                "strategy": "午盘精选",
                "score": 12,
                "change": 2.0,
                "open_change": 1.0,
                "price": 10.0,
                "pre_close": 9.8,
            }]
        }
        fake_portfolio = FakePortfolio()

        with patch.object(stock_main, "datetime", FixedDatetime), \
             patch("core.utils.is_trading_hours", return_value=True), \
             patch.object(stock_main.Config, "load_strategy_config", return_value=None), \
             patch.object(stock_main.Config, "get_allowed_boards", return_value=[]), \
             patch.dict(stock_main.Config.RISK_MANAGEMENT, {"MAX_TOTAL_POSITIONS": 5}, clear=False), \
             patch.dict(stock_main.Config.STRATEGY, {
                 "entry_policy": {"enabled": False, "default_model": "immediate"},
                 "attack_window_gate": {"enabled": False},
                 "win_rate_gate": {"enabled": False},
             }, clear=False):
            stock_main.execute_automated_strategies(
                FakeAnalyzer(),
                fake_portfolio,
                FakeProvider(),
                MagicMock(),
                result,
                {"positions": []},
                args,
            )

        self.assertEqual(len(fake_portfolio.events), 1)
        params = fake_portfolio.events[0]["params"]
        attempt = params["candidate_attempt"]
        self.assertEqual(attempt["schema"], "candidate_attempt_v1")
        self.assertEqual(attempt["stage"], "pre_pending_filter")
        self.assertEqual(attempt["action"], "SKIPPED")
        self.assertEqual(attempt["no_attempt_reason"], "SCORE_THRESHOLD_FILTER")
        self.assertEqual(params["metrics"]["candidate_attempt"]["no_attempt_reason"], "SCORE_THRESHOLD_FILTER")
        matched_audit = [
            row for row in result["execution_audit"]
            if row.get("candidate_attempt", {}).get("no_attempt_reason") == "SCORE_THRESHOLD_FILTER"
        ]
        self.assertEqual(len(matched_audit), 1)


if __name__ == "__main__":
    unittest.main()
