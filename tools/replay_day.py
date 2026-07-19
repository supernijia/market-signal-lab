# -*- coding: utf-8 -*-
"""CLI for daily A-share constrained replay."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.backtester import AShareReplayBacktester, ReplayConfig
from core.data_provider import DataProvider
from core.portfolio import PortfolioManager


def main():
    parser = argparse.ArgumentParser(description="Replay one day's selections with A-share constraints.")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y%m%d"), help="Replay date, YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--strategy", type=str, default="all", help="strategy_selection.strategy, or all")
    parser.add_argument("--code", type=str, default=None, help="Optional stock code, e.g. 002669")
    parser.add_argument("--horizon-days", type=int, default=3, help="Number of daily bars to inspect including entry day")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="One-side slippage in bps")
    parser.add_argument("--fee-bps", type=float, default=3.0, help="One-side fee in bps")
    parser.add_argument("--tax-bps", type=float, default=5.0, help="Sell-side stamp tax/cost in bps")
    parser.add_argument("--stop-loss-pct", type=float, default=-3.0, help="T0 stop-loss threshold for T+1 blocked flag")
    args = parser.parse_args()

    cfg = ReplayConfig(
        horizon_days=max(1, int(args.horizon_days or 3)),
        slippage_bps=float(args.slippage_bps),
        fee_bps=float(args.fee_bps),
        tax_bps=float(args.tax_bps),
        stop_loss_pct=float(args.stop_loss_pct),
    )
    replay = AShareReplayBacktester(PortfolioManager(), DataProvider(), cfg)
    result = replay.replay_day(args.date, strategy=args.strategy, code=args.code)
    print(replay.format_report(result))


if __name__ == "__main__":
    main()
