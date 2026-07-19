import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.pre_trade_gate import evaluate_pre_trade_gate  # noqa: E402
from core.sector_rotation import SectorRotation  # noqa: E402
from main import is_paper_training_route, mark_paper_filter_bypass  # noqa: E402


class ConservativeProbeGateTest(unittest.TestCase):
    def test_paper_training_route_detection(self):
        class Args:
            paper_trade = True

        self.assertTrue(is_paper_training_route(Args(), "paper_watchlist"))
        self.assertTrue(is_paper_training_route(Args(), "paper_main"))
        self.assertFalse(is_paper_training_route(Args(), "watchlist"))

        class RealArgs:
            paper_trade = False

        self.assertFalse(is_paper_training_route(RealArgs(), "paper_watchlist"))

    def test_paper_filter_bypass_keeps_original_reason(self):
        row = mark_paper_filter_bypass(
            {"code": "300139", "name": "晓程科技", "sector_rotation_tags": ["SECTOR_WEAK"]},
            filter_name="board_filter",
            reason="主板权限过滤拦截: 创业板未启用",
            tag="PAPER_BOARD_FILTER_BYPASS",
        )

        self.assertTrue(row["paper_filter_bypass"])
        self.assertEqual(row["paper_filter_bypass_name"], "board_filter")
        self.assertIn("PAPER_BOARD_FILTER_BYPASS", row["sector_rotation_tags"])
        self.assertIn("PAPER_TRAINING_FILTER_BYPASS", row["sector_rotation_tags"])
        self.assertIn("创业板未启用", row["paper_original_filter_reason"])

    def test_unknown_sector_is_penalized_not_rejected_in_weak_market(self):
        rotation = SectorRotation(
            [],
            config={
                "enabled": True,
                "weak_market_require_strong_sector": True,
                "weak_market_unknown_sector_action": "PENALTY",
                "unknown_sector_penalty": -4,
            },
        )

        accepted, rejected = rotation.annotate_candidates(
            [{"code": "600001", "name": "行业缺失样本", "industry": "", "score": 60}],
            market_env={"regime": "weak_market"},
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])
        self.assertIn("SECTOR_UNKNOWN_CONFIRM", accepted[0]["sector_rotation_tags"])
        self.assertEqual(accepted[0]["sector_bonus"], -4)

    def test_storm_market_strong_strategy_uses_low_size_confirm(self):
        strategy = {
            "data_quality_gate": {"enabled": False},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["☁️多云", "⚠️暴雨"],
                "weak_risk_levels": ["medium", "high"],
                "min_win_rate_samples": 30,
                "block_insufficient_samples": True,
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "strategy_permission_matrix": {
                "storm_market": {
                    "午盘精选": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            gate = evaluate_pre_trade_gate(
                {
                    "code": "600001",
                    "name": "暴雨强票样本",
                    "strategy": "午盘精选",
                    "open_change": 1.2,
                    "change": 4.0,
                    "price": 10.0,
                    "pre_close": 9.62,
                },
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="午盘精选",
                win_rate_stats={"cnt": 40, "win_rate": 0.6},
                now=datetime(2026, 7, 3, 14, 10),
                mode="afternoon",
            )

        self.assertTrue(gate["allow"])
        self.assertEqual(gate["action"], "LOW_SIZE_CONFIRM")
        self.assertTrue(gate["allow_pending"])
        self.assertFalse(gate["allow_execute_buy"])
        self.assertNotIn("WEAK_MARKET_CHASE_BLOCK", gate["tags"])

    def test_weak_market_chase_threshold_is_four_point_five(self):
        strategy = {
            "data_quality_gate": {"enabled": False},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["☁️多云", "⚠️暴雨"],
                "weak_risk_levels": ["medium", "high"],
                "min_win_rate_samples": 30,
                "block_insufficient_samples": False,
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "strategy_permission_matrix": {
                "weak_market": {
                    "午盘精选": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            gate = evaluate_pre_trade_gate(
                {
                    "code": "600002",
                    "name": "弱市强票样本",
                    "strategy": "午盘精选",
                    "open_change": 1.0,
                    "change": 4.2,
                    "price": 10.0,
                    "pre_close": 9.6,
                },
                market_env={"weather": "☁️多云", "risk_level": "medium", "regime": "weak_market"},
                strategy="午盘精选",
                win_rate_stats={"cnt": 5, "win_rate": 0.4},
                now=datetime(2026, 7, 3, 14, 10),
                mode="afternoon",
            )

        self.assertTrue(gate["allow"])
        self.assertNotIn("WEAK_MARKET_CHASE_BLOCK", gate["tags"])

    def test_paper_weak_gate_sample_floor_does_not_affect_main_account(self):
        strategy = {
            "data_quality_gate": {"enabled": False},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["⚠️暴雨"],
                "weak_risk_levels": ["high"],
                "min_win_rate_samples": 30,
                "block_insufficient_samples": True,
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "paper_weak_market_gate_experiment": {
                "enabled": True,
                "allowed_accounts": ["paper_main", "paper_watchlist"],
                "require_paper_experiment_tag": True,
                "sample_floor_override": 25,
                "weak_chase_override_pct": 5.0,
            },
            "strategy_permission_matrix": {
                "storm_market": {
                    "冷启动": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }
        candidate = {
            "code": "000620",
            "name": "盈新发展",
            "strategy": "冷启动",
            "open_change": 1.0,
            "change": 3.0,
            "paper_experiment": True,
            "paper_experiment_type": "weak_sample_floor",
            "price": 10.0,
            "pre_close": 9.7,
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            main_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="watchlist",
                win_rate_stats={"cnt": 26, "win_rate": 0.5},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )
            paper_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="paper_watchlist",
                win_rate_stats={"cnt": 26, "win_rate": 0.5},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )

        self.assertIn("WEAK_MARKET_INSUFFICIENT_SAMPLES", main_gate["tags"])
        self.assertNotIn("PAPER_WEAK_SAMPLE_FLOOR_USED", main_gate["tags"])
        self.assertNotIn("WEAK_MARKET_INSUFFICIENT_SAMPLES", paper_gate["tags"])
        self.assertIn("PAPER_WEAK_SAMPLE_FLOOR_USED", paper_gate["tags"])
        self.assertEqual(paper_gate["metrics"]["effective_min_win_rate_samples"], 25)

    def test_paper_weak_gate_covers_plain_paper_pending_when_tag_not_required(self):
        strategy = {
            "data_quality_gate": {"enabled": False},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["⚠️暴雨"],
                "weak_risk_levels": ["high"],
                "min_win_rate_samples": 30,
                "block_insufficient_samples": True,
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "paper_weak_market_gate_experiment": {
                "enabled": True,
                "allowed_accounts": ["paper_main", "paper_watchlist"],
                "require_paper_experiment_tag": False,
                "sample_floor_override": 25,
                "weak_chase_override_pct": 5.0,
            },
            "strategy_permission_matrix": {
                "storm_market": {
                    "冷启动": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }
        candidate = {
            "code": "600001",
            "name": "普通paper样本",
            "strategy": "冷启动",
            "open_change": 1.0,
            "change": 3.0,
            "price": 10.0,
            "pre_close": 9.7,
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            main_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="watchlist",
                win_rate_stats={"cnt": 26, "win_rate": 0.5},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )
            paper_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="paper_watchlist",
                win_rate_stats={"cnt": 26, "win_rate": 0.5},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )

        self.assertIn("WEAK_MARKET_INSUFFICIENT_SAMPLES", main_gate["tags"])
        self.assertNotIn("PAPER_WEAK_SAMPLE_FLOOR_USED", main_gate["tags"])
        self.assertNotIn("WEAK_MARKET_INSUFFICIENT_SAMPLES", paper_gate["tags"])
        self.assertIn("PAPER_WEAK_SAMPLE_FLOOR_USED", paper_gate["tags"])
        self.assertFalse(paper_gate["metrics"]["paper_weak_market_gate_experiment"]["has_paper_experiment_tag"])

    def test_paper_weak_gate_chase_band_does_not_affect_main_account(self):
        strategy = {
            "data_quality_gate": {"enabled": False},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["⚠️暴雨"],
                "weak_risk_levels": ["high"],
                "min_win_rate_samples": 30,
                "block_insufficient_samples": False,
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "paper_weak_market_gate_experiment": {
                "enabled": True,
                "allowed_accounts": ["paper_main", "paper_watchlist"],
                "require_paper_experiment_tag": True,
                "sample_floor_override": 25,
                "weak_chase_override_pct": 5.0,
            },
            "strategy_permission_matrix": {
                "storm_market": {
                    "冷启动": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }
        candidate = {
            "code": "002202",
            "name": "金风科技",
            "strategy": "冷启动",
            "open_change": 1.0,
            "change": 4.8,
            "paper_experiment": True,
            "paper_experiment_type": "weak_chase_band",
            "price": 10.0,
            "pre_close": 9.54,
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            main_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="watchlist",
                win_rate_stats={"cnt": 40, "win_rate": 0.6},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )
            paper_gate = evaluate_pre_trade_gate(
                candidate,
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                account="paper_watchlist",
                win_rate_stats={"cnt": 40, "win_rate": 0.6},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )

        self.assertIn("WEAK_MARKET_CHASE_BLOCK", main_gate["tags"])
        self.assertNotIn("PAPER_WEAK_CHASE_BAND_USED", main_gate["tags"])
        self.assertNotIn("WEAK_MARKET_CHASE_BLOCK", paper_gate["tags"])
        self.assertIn("PAPER_WEAK_CHASE_BAND_USED", paper_gate["tags"])
        self.assertEqual(paper_gate["metrics"]["effective_weak_max_change"], 5.0)

    def test_bad_data_quality_stays_blocked_even_when_matrix_confirms(self):
        strategy = {
            "data_quality_gate": {"enabled": True},
            "weak_market_entry_gate": {
                "enabled": True,
                "weak_weather_blocklist": ["⚠️暴雨"],
                "weak_risk_levels": ["high"],
                "weak_max_open_change": 2.5,
                "weak_max_change": 4.5,
            },
            "strategy_permission_matrix": {
                "storm_market": {
                    "冷启动": "LOW_SIZE_CONFIRM",
                    "*": "BLOCK",
                }
            },
        }

        with patch.dict(Config.STRATEGY, strategy, clear=False):
            gate = evaluate_pre_trade_gate(
                {
                    "code": "600003",
                    "name": "缺昨收样本",
                    "strategy": "冷启动",
                    "price": 10.0,
                },
                market_env={"weather": "⚠️暴雨", "risk_level": "high", "regime": "storm_market"},
                strategy="冷启动",
                win_rate_stats={"cnt": 30, "win_rate": 0.6},
                now=datetime(2026, 7, 3, 14, 10),
                mode="monitor",
            )

        self.assertFalse(gate["allow"])
        self.assertFalse(gate["allow_pending"])
        self.assertFalse(gate["allow_execute_buy"])
        self.assertEqual(gate["action"], "BLOCK")
        self.assertIn("DATA_QUALITY_BAD", gate["tags"])


if __name__ == "__main__":
    unittest.main()
