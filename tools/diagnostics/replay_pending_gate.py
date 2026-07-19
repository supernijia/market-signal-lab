#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay latest pending-entry checks through the current pre-trade gate."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.portfolio import PortfolioManager  # noqa: E402
from core.pre_trade_gate import evaluate_pre_trade_gate  # noqa: E402
from core.utils import normalize_weather  # noqa: E402


def _fetch(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() or []


def _accounts(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _account_filter(accounts: list[str]) -> tuple[str, list[Any]]:
    if not accounts:
        return "", []
    return " AND e.account IN (" + ",".join(["%s"] * len(accounts)) + ")", list(accounts)


def _build_candidate(row: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_json(row.get("signal_payload"))
    payload.update(_safe_json(row.get("event_payload")))
    candidate = dict(payload)
    candidate.update(
        {
            "code": row.get("code"),
            "name": row.get("name"),
            "strategy": row.get("strategy") or row.get("source_strategy"),
            "price": row.get("price"),
            "pre_close": row.get("pre_close"),
            "change": row.get("change_pct"),
            "pct_chg": row.get("change_pct"),
            "volume_ratio": row.get("volume_ratio"),
            "price_vwap_ratio": row.get("price_vwap_ratio"),
        }
    )
    # The check event already proves the monitor had quote data. Keep the
    # replay focused on gate logic instead of payload persistence gaps.
    if candidate.get("price") and candidate.get("pre_close"):
        candidate.setdefault("vol", 1)
        candidate.setdefault("amount", 1)
    return candidate


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    accounts = _accounts(args.accounts)
    account_sql, account_params = _account_filter(accounts)
    pm = PortfolioManager()
    conn = pm._get_connection()
    if not conn:
        return {"ok": False, "error": "database connection failed"}
    try:
        raw_rows = _fetch(
            conn,
            f"""
            SELECT e.*,
                   s.name,
                   s.source_strategy,
                   s.payload_json AS signal_payload
            FROM pending_entry_check_events e
            LEFT JOIN pending_entry_signals s ON s.id=e.pending_id
            WHERE e.trade_date=%s {account_sql}
            ORDER BY e.pending_id, e.check_time DESC, e.id DESC
            """,
            tuple([args.date] + account_params),
        )
    finally:
        conn.close()

    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in raw_rows:
        grouped.setdefault(row.get("pending_id"), []).append(row)

    rows = []
    for pending_id, items in grouped.items():
        priced = [r for r in items if r.get("price") and r.get("pre_close")]
        rows.append((priced or items)[0])

    weather = normalize_weather(args.weather)
    market_env = {
        "weather": weather,
        "risk_level": args.risk_level,
        "regime": args.regime,
        "message": args.message or f"offline gate replay {weather}",
    }
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S") if args.now else datetime.now()
    win_stats = {"cnt": int(args.win_samples), "win_rate": float(args.win_rate)}

    details = []
    summary = Counter()
    changed = Counter()
    for row in rows:
        candidate = _build_candidate(row)
        gate = evaluate_pre_trade_gate(
            candidate,
            market_env=market_env,
            strategy=candidate.get("strategy"),
            win_rate_stats=win_stats,
            now=now,
            mode="monitor",
        )
        old_decision = str(row.get("decision") or "")
        new_action = str(gate.get("action") or "")
        new_pending = bool(gate.get("allow_pending"))
        new_execute = bool(gate.get("allow_execute_buy"))
        old_blocked = old_decision in {"CANCELLED", "SKIP", "EXPIRED", "UNFILLABLE"}
        now_confirm = new_action in {"LOW_SIZE_CONFIRM", "CONFIRM_ONLY", "PENDING"} and new_pending
        replay_change = "BLOCK_TO_CONFIRM" if old_blocked and now_confirm else "UNCHANGED_OR_STILL_BLOCKED"
        if replay_change == "BLOCK_TO_CONFIRM":
            changed[candidate.get("strategy") or "未知"] += 1
        summary[new_action or "UNKNOWN"] += 1
        details.append(
            {
                "pending_id": row.get("pending_id"),
                "account": row.get("account"),
                "code": row.get("code"),
                "name": row.get("name"),
                "strategy": candidate.get("strategy"),
                "old_decision": old_decision,
                "old_reason": row.get("reason"),
                "change_pct": row.get("change_pct"),
                "new_action": new_action,
                "new_allow": bool(gate.get("allow")),
                "new_allow_pending": new_pending,
                "new_allow_execute_buy": new_execute,
                "new_tags": gate.get("tags") or [],
                "new_reason": gate.get("reason") or "",
                "replay_change": replay_change,
            }
        )

    return {
        "ok": True,
        "date": args.date,
        "accounts": accounts,
        "market_env": market_env,
        "rows": len(rows),
        "summary": dict(summary),
        "block_to_confirm_by_strategy": dict(changed),
        "details": details,
    }


def print_text(report: dict[str, Any]) -> None:
    if not report.get("ok"):
        print(f"离线门禁复盘失败: {report.get('error')}")
        return
    env = report.get("market_env") or {}
    print(f"【Pending 门禁离线复盘】{report.get('date')} rows={report.get('rows')}")
    print(f"- 账户: {','.join(report.get('accounts') or []) or '全部'}")
    print(f"- 环境: weather={env.get('weather')} risk={env.get('risk_level')} regime={env.get('regime')}")
    print(f"- 新动作统计: {report.get('summary')}")
    print(f"- 旧阻断 -> 新确认: {report.get('block_to_confirm_by_strategy')}")
    print("")
    print("| id | 账户 | 代码 | 名称 | 策略 | 旧决策 | 涨幅 | 新动作 | 新状态 | 结论 |")
    print("|---:|---|---|---|---|---|---:|---|---|---|")
    for row in report.get("details") or []:
        new_state = "可确认" if row.get("new_allow_pending") else "仍拦截"
        conclusion = "旧阻断转确认" if row.get("replay_change") == "BLOCK_TO_CONFIRM" else "不放行或无变化"
        print(
            f"| {row.get('pending_id')} | {row.get('account')} | {row.get('code')} | {row.get('name')} | "
            f"{row.get('strategy')} | {row.get('old_decision')} | {float(row.get('change_pct') or 0):.2f}% | "
            f"{row.get('new_action')} | {new_state} | {conclusion} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay pending-entry checks through current gate config.")
    parser.add_argument("--date", required=True, help="trade date, YYYY-MM-DD")
    parser.add_argument("--accounts", default="main,watchlist,paper_main,paper_watchlist")
    parser.add_argument("--weather", default="⚠️暴雨")
    parser.add_argument("--risk-level", default="high")
    parser.add_argument("--regime", default="storm_market")
    parser.add_argument("--message", default="")
    parser.add_argument("--win-samples", type=int, default=30)
    parser.add_argument("--win-rate", type=float, default=0.5)
    parser.add_argument("--now", default="", help="optional replay time, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    else:
        print_text(report)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
