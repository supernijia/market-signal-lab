# -*- coding: utf-8 -*-
"""Register an externally executed holding for alert-only sentinel tracking."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.data_provider import DataProvider
from core.portfolio import PortfolioManager


def _normalize_code(code: str) -> str:
    return str(code or "").strip()[:6]


def main():
    parser = argparse.ArgumentParser(description="Register manual real holding for rescue-account alert tracking")
    parser.add_argument("--code", required=True, help="Stock code, e.g. 600579")
    parser.add_argument("--name", default="", help="Stock name. If omitted, stock_basic will be used.")
    parser.add_argument("--price", type=float, required=True, help="Actual holding cost / avg price")
    parser.add_argument("--quantity", type=int, required=True, help="Holding quantity")
    parser.add_argument("--account", default="rescue", help="Tracking account. Default: rescue")
    parser.add_argument("--strategy", default="手工实盘跟踪", help="Entry strategy label")
    parser.add_argument("--created-at", default="", help="Actual buy time, e.g. '2026-06-30 14:30:00'")
    args = parser.parse_args()

    code = _normalize_code(args.code)
    name = args.name.strip()
    if not name:
        try:
            provider = DataProvider()
            ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            basics = provider.get_stock_basic()
            name = (basics.get(ts_code) or {}).get("name") or code
        except Exception:
            name = code

    pm = PortfolioManager()
    pm.init_tables()
    ok, msg = pm.upsert_manual_position(
        code=code,
        name=name,
        price=args.price,
        quantity=args.quantity,
        account=args.account,
        source_strategy=args.strategy,
        created_at=args.created_at or None,
    )
    if not ok:
        print(f"ERROR: {msg}")
        raise SystemExit(1)
    print(msg)


if __name__ == "__main__":
    main()
