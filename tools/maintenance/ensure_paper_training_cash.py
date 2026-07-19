#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ensure paper accounts have enough simulated cash for shadow training."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import Config  # noqa: E402
from core.display_labels import display_account  # noqa: E402
from core.reporter import Reporter  # noqa: E402
from core.utils import setup_logger  # noqa: E402


logger = setup_logger("StockAnalyzer.PaperCash")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "reports" / "paper_training_cash" / "latest.json"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _accounts(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _paper_cfg() -> dict[str, Any]:
    try:
        cfg = Config.STRATEGY.get("paper_account", {}) if isinstance(Config.STRATEGY, dict) else {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _target_cash(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    cfg = _paper_cfg()
    target = cfg.get("training_cash_target")
    if target is not None:
        return _safe_float(target, 50000.0)
    return 50000.0


def _cash_floor(explicit: float | None, target: float) -> float:
    if explicit is not None:
        return float(explicit)
    cfg = _paper_cfg()
    floor = cfg.get("training_cash_floor")
    if floor is not None:
        return _safe_float(floor, min(8000.0, target))
    return min(8000.0, target)


def _db_timeout(explicit: float | None) -> float:
    if explicit is not None:
        return max(1.0, float(explicit))
    return max(1.0, _safe_float(os.environ.get("PAPER_TRAINING_CASH_DB_TIMEOUT"), 5.0))


def _get_direct_connection(timeout: float):
    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASS,
        database=Config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=int(max(1, timeout)),
        read_timeout=int(max(1, timeout)),
        write_timeout=int(max(1, timeout)),
    )


def _ensure_cash_tables(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account VARCHAR(20) PRIMARY KEY,
                initial_capital FLOAT NOT NULL DEFAULT 20000,
                created_at DATETIME
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_value (
                date VARCHAR(20),
                account VARCHAR(20) DEFAULT 'main',
                total_value FLOAT,
                cash FLOAT,
                market_value FLOAT,
                return_rate FLOAT,
                PRIMARY KEY (date, account)
            )
            """
        )
    conn.commit()


def _read_current_cash(account: str, *, db_timeout: float) -> float:
    """Read current cash; raise when DB cannot provide an authoritative value."""
    conn = _get_direct_connection(db_timeout)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1", (account,))
            row = cursor.fetchone()
            if row and row.get("cash") is not None:
                return float(row["cash"])
            cursor.execute("SELECT initial_capital FROM accounts WHERE account=%s", (account,))
            row = cursor.fetchone()
            if row and row.get("initial_capital") is not None:
                return float(row["initial_capital"])
        return float(Config.RISK_MANAGEMENT.get("INITIAL_CAPITAL", {}).get(account, 50000.0))
    finally:
        conn.close()


def _set_training_cash(account: str, target: float, dry_run: bool, *, db_timeout: float) -> None:
    if dry_run:
        return
    conn = _get_direct_connection(db_timeout)
    try:
        _ensure_cash_tables(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO accounts (account, initial_capital, created_at)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE initial_capital=VALUES(initial_capital)
                """,
                (account, float(target), now),
            )
            cursor.execute(
                """
                INSERT INTO portfolio_value (date, account, cash, total_value, market_value, return_rate)
                VALUES (%s, %s, %s, %s, 0, 0)
                ON DUPLICATE KEY UPDATE
                    cash=VALUES(cash),
                    total_value=VALUES(total_value),
                    market_value=VALUES(market_value),
                    return_rate=VALUES(return_rate)
                """,
                (now, account, float(target), float(target)),
            )
        conn.commit()
    finally:
        conn.close()


def _ensure_one_account(
    account: str,
    target: float,
    floor: float,
    *,
    force: bool,
    dry_run: bool,
    db_retries: int,
    retry_delay: float,
    db_timeout: float = 5.0,
) -> dict[str, Any]:
    last_error = ""
    before = None
    read_failed = False
    for attempt in range(1, max(1, int(db_retries)) + 1):
        try:
            try:
                before = _read_current_cash(account, db_timeout=db_timeout)
                read_failed = False
            except Exception as exc:
                # P10 is a paper-only safety operation: if DB reads are flaky,
                # still try to set the training cash target instead of failing
                # before the write path has a chance to recover.
                before = None
                read_failed = True
                logger.warning(
                    "PAPER_TRAINING_CASH_READ_FALLBACK account=%s attempt=%s/%s error=%s",
                    account,
                    attempt,
                    db_retries,
                    exc,
                )
            should_update = True if read_failed else force or before < floor
            after = target if should_update else before
            reason = (
                "读取现金失败，按训练目标校准"
                if read_failed
                else "强制校准"
                if force and should_update
                else "低于训练现金下限"
                if should_update
                else "现金充足"
            )
            if should_update:
                _set_training_cash(account, target, dry_run, db_timeout=db_timeout)
                logger.info(
                    "PAPER_TRAINING_CASH account=%s before=%s after=%.2f floor=%.2f target=%.2f dry_run=%s attempt=%s",
                    account,
                    "-" if before is None else f"{before:.2f}",
                    after,
                    floor,
                    target,
                    dry_run,
                    attempt,
                )
                if read_failed and not dry_run:
                    try:
                        after = _read_current_cash(account, db_timeout=db_timeout)
                    except Exception:
                        after = target
            return {
                "account": account,
                "display": display_account(account),
                "before": before,
                "after": after,
                "updated": should_update,
                "reason": reason,
                "status": "OK",
                "error": "",
                "attempts": attempt,
            }
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "PAPER_TRAINING_CASH_RETRY account=%s attempt=%s/%s error=%s",
                account,
                attempt,
                db_retries,
                last_error,
            )
            if attempt < max(1, int(db_retries)) and retry_delay > 0:
                time.sleep(float(retry_delay))

    return {
        "account": account,
        "display": display_account(account),
        "before": before,
        "after": before,
        "updated": False,
        "reason": "数据库连接失败，未完成校准",
        "status": "ERROR",
        "error": last_error or "unknown database error",
        "attempts": max(1, int(db_retries)),
    }


def ensure_cash(
    accounts: list[str],
    target: float,
    floor: float,
    *,
    force: bool = False,
    dry_run: bool = False,
    db_retries: int = 5,
    retry_delay: float = 2.0,
    db_timeout: float = 5.0,
) -> list[dict[str, Any]]:
    rows = []
    for account in accounts:
        if not account.startswith("paper_"):
            rows.append(
                {
                    "account": account,
                    "display": display_account(account),
                    "before": None,
                    "after": None,
                    "updated": False,
                    "reason": "非 paper 账户，已跳过",
                    "status": "SKIP",
                    "error": "",
                    "attempts": 0,
                }
            )
            continue

        rows.append(
            _ensure_one_account(
                account,
                target,
                floor,
                force=force,
                dry_run=dry_run,
                db_retries=db_retries,
                retry_delay=retry_delay,
                db_timeout=db_timeout,
            )
        )
    return rows


def build_report_payload(
    rows: list[dict[str, Any]],
    target: float,
    floor: float,
    dry_run: bool,
    *,
    accounts: list[str] | None = None,
    force: bool = False,
    db_retries: int = 5,
    retry_delay: float = 2.0,
    db_timeout: float = 5.0,
) -> dict[str, Any]:
    has_error = any(row.get("status") == "ERROR" for row in rows)
    has_skip = any(row.get("status") == "SKIP" for row in rows)
    overall = "ERROR" if has_error else "WARN" if has_skip else "OK"
    recommendations: list[str] = []
    if has_error:
        recommendations.append("P10 现金校准失败：先修复数据库读写或账号权限，不要把当天影子训练失败解释成策略失败。")
    elif overall == "WARN":
        recommendations.append("P10 已跳过非 paper 账户：OpenClaw 任务只应传入 paper_main,paper_watchlist。")
    else:
        recommendations.append("P10 现金校准完成：可以继续执行 P11 影子训练盘前预检。")
    if any(row.get("updated") for row in rows):
        recommendations.append("已校准 paper_* 模拟现金；该操作不影响 main/watchlist。")

    return {
        "ok": overall == "OK",
        "overall": overall,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runner": "run_openclaw_required",
        "env_file": ".env.openclaw",
        "accounts": accounts if accounts is not None else [str(row.get("account")) for row in rows],
        "target_cash": target,
        "floor": floor,
        "dry_run": dry_run,
        "force": force,
        "db_retries": db_retries,
        "retry_delay": retry_delay,
        "db_timeout": db_timeout,
        "rows": rows,
        "recommendations": recommendations,
    }


def write_json_report(report: dict[str, Any], path: Path | None) -> str:
    if not path:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return str(path)


def _suppress_console_logs_for_json() -> None:
    root_logger = logging.getLogger("StockAnalyzer")
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.CRITICAL + 1)


def format_report(rows: list[dict[str, Any]], target: float, floor: float, dry_run: bool) -> str:
    lines = [
        f"【影子账户训练现金校准】{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 目标现金: {target:,.2f}",
        f"- 触发下限: {floor:,.2f}",
        f"- 演练模式: {'是' if dry_run else '否'}",
        "",
        "| 账户 | 校准前现金 | 校准后现金 | 是否更新 | 状态 | 尝试 | 原因 |",
        "|---|---:|---:|---|---|---:|---|",
    ]
    for row in rows:
        before = "-" if row.get("before") is None else f"{_safe_float(row.get('before')):,.2f}"
        after = "-" if row.get("after") is None else f"{_safe_float(row.get('after')):,.2f}"
        reason = row.get("reason") or "-"
        if row.get("error"):
            reason = f"{reason}: {row.get('error')}"
        lines.append(
            f"| {row.get('display') or row.get('account')} | {before} | {after} | "
            f"{'是' if row.get('updated') else '否'} | {row.get('status') or '-'} | "
            f"{int(row.get('attempts') or 0)} | {reason} |"
        )
    lines.append("")
    lines.append("说明: 该工具只校准 paper_* 模拟现金，用于扩大影子买卖训练样本，不影响 main/watchlist。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure paper accounts have enough simulated cash for shadow training.")
    cfg = _paper_cfg()
    default_accounts = ",".join(
        [
            str(cfg.get("main_account") or "paper_main"),
            str(cfg.get("watchlist_account") or "paper_watchlist"),
        ]
    )
    parser.add_argument("--accounts", default=default_accounts, help="Comma-separated paper accounts")
    parser.add_argument("--target-cash", type=float, default=None, help="Target simulated cash after top-up")
    parser.add_argument("--floor", type=float, default=None, help="Only top up when cash is below this floor")
    parser.add_argument("--force", action="store_true", help="Set target cash even when current cash is above the floor")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without updating database")
    parser.add_argument("--email", action="store_true", help="Send the calibration report by email")
    parser.add_argument("--db-retries", type=int, default=5, help="Per-account DB read/write retries")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between DB retries")
    parser.add_argument("--db-timeout", type=float, default=None, help="Direct DB connect/read/write timeout for this maintenance task")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON, help="Write machine-readable JSON report")
    parser.add_argument("--no-output-json", action="store_true", help="Do not write the JSON report file")
    args = parser.parse_args()

    target = _target_cash(args.target_cash)
    floor = _cash_floor(args.floor, target)
    accounts = _accounts(args.accounts)
    db_timeout = _db_timeout(args.db_timeout)
    if args.json:
        _suppress_console_logs_for_json()
    rows = ensure_cash(
        accounts,
        target,
        floor,
        force=args.force,
        dry_run=args.dry_run,
        db_retries=args.db_retries,
        retry_delay=args.retry_delay,
        db_timeout=db_timeout,
    )
    payload = build_report_payload(
        rows,
        target,
        floor,
        args.dry_run,
        accounts=accounts,
        force=args.force,
        db_retries=args.db_retries,
        retry_delay=args.retry_delay,
        db_timeout=db_timeout,
    )
    if not args.no_output_json:
        payload["output_json"] = write_json_report(payload, args.output_json)
    text_report = format_report(rows, target, floor, args.dry_run)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(text_report)

    if args.email:
        Reporter().send_email(f"🧪【影子账户训练现金校准】{payload.get('overall')} {datetime.now().strftime('%Y-%m-%d')}", text_report)
    return 2 if any(row.get("status") == "ERROR" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
