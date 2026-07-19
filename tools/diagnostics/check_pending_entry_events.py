#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnose pending-entry execution evidence for one trade date."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.portfolio import PortfolioManager  # noqa: E402


def _fetch(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() or []


def _table_exists(conn, table: str) -> bool:
    rows = _fetch(conn, "SHOW TABLES LIKE %s", (table,))
    return bool(rows)


def _accounts(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _account_filter(accounts: list[str]) -> tuple[str, list[Any]]:
    if not accounts:
        return "", []
    return " AND target_account IN (" + ",".join(["%s"] * len(accounts)) + ")", list(accounts)


def _event_account_filter(accounts: list[str]) -> tuple[str, list[Any]]:
    if not accounts:
        return "", []
    return " AND account IN (" + ",".join(["%s"] * len(accounts)) + ")", list(accounts)


def _tx_account_filter(accounts: list[str]) -> tuple[str, list[Any]]:
    if not accounts:
        return "", []
    return " AND account IN (" + ",".join(["%s"] * len(accounts)) + ")", list(accounts)


def build_report(trade_date: str, accounts: list[str]) -> dict[str, Any]:
    pm = PortfolioManager()
    pm.init_tables()
    conn = pm._get_connection()
    if not conn:
        return {"ok": False, "error": "database connection failed", "trade_date": trade_date, "accounts": accounts}
    try:
        tables = {
            "pending_entry_signals": _table_exists(conn, "pending_entry_signals"),
            "pending_entry_check_events": _table_exists(conn, "pending_entry_check_events"),
            "transactions": _table_exists(conn, "transactions"),
        }
        result: dict[str, Any] = {
            "ok": all(tables.values()),
            "trade_date": trade_date,
            "accounts": accounts,
            "tables": tables,
            "pending_by_status": [],
            "events_by_decision": [],
            "top_reasons": [],
            "paper_buys": [],
            "coverage": {},
        }
        if not tables["pending_entry_signals"]:
            return result

        account_sql, account_params = _account_filter(accounts)
        result["pending_by_status"] = _fetch(
            conn,
            f"""
            SELECT target_account AS account, source_strategy AS strategy, status, COUNT(1) AS cnt
            FROM pending_entry_signals
            WHERE trade_date=%s {account_sql}
            GROUP BY target_account, source_strategy, status
            ORDER BY target_account, source_strategy, status
            """,
            tuple([trade_date] + account_params),
        )

        pending_total_rows = _fetch(
            conn,
            f"SELECT COUNT(1) AS cnt FROM pending_entry_signals WHERE trade_date=%s {account_sql}",
            tuple([trade_date] + account_params),
        )
        pending_total = int((pending_total_rows[0] or {}).get("cnt", 0) or 0) if pending_total_rows else 0

        if tables["pending_entry_check_events"]:
            event_sql, event_params = _event_account_filter(accounts)
            result["events_by_decision"] = _fetch(
                conn,
                f"""
                SELECT account, strategy, decision, COUNT(1) AS cnt
                FROM pending_entry_check_events
                WHERE trade_date=%s {event_sql}
                GROUP BY account, strategy, decision
                ORDER BY account, strategy, cnt DESC
                """,
                tuple([trade_date] + event_params),
            )
            result["top_reasons"] = _fetch(
                conn,
                f"""
                SELECT account, strategy, decision, reason, COUNT(1) AS cnt, MAX(check_time) AS last_check_time
                FROM pending_entry_check_events
                WHERE trade_date=%s {event_sql}
                GROUP BY account, strategy, decision, reason
                ORDER BY cnt DESC, last_check_time DESC
                LIMIT 20
                """,
                tuple([trade_date] + event_params),
            )
            event_total_rows = _fetch(
                conn,
                f"SELECT COUNT(1) AS cnt, COUNT(DISTINCT pending_id) AS pending_cnt FROM pending_entry_check_events WHERE trade_date=%s {event_sql}",
                tuple([trade_date] + event_params),
            )
            event_row = event_total_rows[0] if event_total_rows else {}
            event_total = int(event_row.get("cnt", 0) or 0)
            event_pending_total = int(event_row.get("pending_cnt", 0) or 0)
        else:
            event_total = 0
            event_pending_total = 0

        if tables["transactions"]:
            tx_sql, tx_params = _tx_account_filter(accounts)
            result["paper_buys"] = _fetch(
                conn,
                f"""
                SELECT account, source_strategy AS strategy, COUNT(1) AS cnt, SUM(quantity) AS quantity, SUM(amount) AS amount
                FROM transactions
                WHERE DATE(date)=%s AND type='BUY' {tx_sql}
                GROUP BY account, source_strategy
                ORDER BY account, cnt DESC
                """,
                tuple([trade_date] + tx_params),
            )

        result["coverage"] = {
            "pending_total": pending_total,
            "event_total": event_total,
            "event_pending_total": event_pending_total,
            "has_pending": pending_total > 0,
            "has_check_events": event_total > 0,
            "checked_pending_ratio": (event_pending_total / pending_total) if pending_total > 0 else 0.0,
        }
        return result
    finally:
        conn.close()


def print_text(result: dict[str, Any]) -> None:
    print(f"Pending entry diagnostics for {result.get('trade_date')} accounts={','.join(result.get('accounts') or [])}")
    print(f"ok={result.get('ok')} tables={result.get('tables')}")
    if result.get("error"):
        print(f"error={result['error']}")
        return
    coverage = result.get("coverage") or {}
    print(
        "coverage: "
        f"pending_total={coverage.get('pending_total', 0)} "
        f"event_total={coverage.get('event_total', 0)} "
        f"event_pending_total={coverage.get('event_pending_total', 0)} "
        f"checked_pending_ratio={float(coverage.get('checked_pending_ratio', 0.0)):.2%}"
    )

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        print(f"\n[{title}]")
        if not rows:
            print("- none")
            return
        for row in rows:
            print("- " + " | ".join(f"{k}={v}" for k, v in row.items()))

    section("pending_by_status", result.get("pending_by_status") or [])
    section("events_by_decision", result.get("events_by_decision") or [])
    section("top_reasons", result.get("top_reasons") or [])
    section("paper_buys", result.get("paper_buys") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Check pending-entry execution evidence")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Trade date YYYY-MM-DD")
    parser.add_argument("--accounts", default="paper_main,paper_watchlist", help="Comma-separated accounts")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--fail-on-missing-events", action="store_true", help="Exit 2 when pending exists but no check events exist")
    args = parser.parse_args()

    result = build_report(args.date, _accounts(args.accounts))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_text(result)

    coverage = result.get("coverage") or {}
    if args.fail_on_missing_events and coverage.get("has_pending") and not coverage.get("has_check_events"):
        raise SystemExit(2)
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
