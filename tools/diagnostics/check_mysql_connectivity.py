#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Layered MySQL connectivity diagnostics for shadow training readiness."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pymysql


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config  # noqa: E402
from core.display_labels import display_account  # noqa: E402
from core.reporter import Reporter  # noqa: E402

DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "reports" / "mysql_preflight" / "latest.json"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _db_timeout(value: float | None) -> float:
    if value is not None and value > 0:
        return float(value)
    return _safe_float(getattr(Config, "DB_TIMEOUT", 5), 5.0)


def _accounts(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _paper_cash_floor() -> float:
    try:
        cfg = Config.STRATEGY.get("paper_account", {}) if isinstance(Config.STRATEGY, dict) else {}
        return _safe_float(cfg.get("training_cash_floor"), 100000.0)
    except Exception:
        return 100000.0


def _timed_step(name: str, func: Callable[[], Any]) -> dict[str, Any]:
    started = time.time()
    try:
        detail = func()
        return {
            "name": name,
            "status": "PASS",
            "elapsed_sec": round(time.time() - started, 3),
            "detail": detail,
            "error": "",
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "FAIL",
            "elapsed_sec": round(time.time() - started, 3),
            "detail": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _connect(timeout: float):
    return pymysql.connect(
        host=Config.DB_HOST,
        port=int(Config.DB_PORT),
        user=Config.DB_USER,
        password=Config.DB_PASS,
        database=Config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=int(max(1, timeout)),
        read_timeout=int(max(1, timeout)),
        write_timeout=int(max(1, timeout)),
        autocommit=False,
    )


def _tcp_probe(host: str, port: int, timeout: float) -> str:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        return f"{host}:{port} connected"
    finally:
        sock.close()


def _fetch_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone() or {}


def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() or []


def build_report(accounts: list[str], *, timeout: float, write_probe: bool = False) -> dict[str, Any]:
    host = Config.DB_HOST
    port = int(Config.DB_PORT)
    floor = _paper_cash_floor()
    steps: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []

    steps.append(
        _timed_step(
            "TCP 端口连通",
            lambda: _tcp_probe(host, port, timeout),
        )
    )

    conn_holder: dict[str, Any] = {}

    if steps[-1].get("status") == "PASS":
        def connect_step() -> str:
            conn_holder["conn"] = _connect(timeout)
            return f"{Config.DB_USER}@{host}:{port}/{Config.DB_NAME}"

        steps.append(_timed_step("MySQL 握手登录", connect_step))

    conn = conn_holder.get("conn")
    if conn:
        try:
            steps.append(_timed_step("基础查询 SELECT 1", lambda: _fetch_one(conn, "SELECT 1 AS ok")))
            steps.append(_timed_step("accounts 表检查", lambda: _fetch_one(conn, "SELECT COUNT(*) AS cnt FROM accounts")))
            steps.append(
                _timed_step(
                    "portfolio_value 表检查",
                    lambda: _fetch_one(conn, "SELECT COUNT(*) AS cnt FROM portfolio_value"),
                )
            )

            def cash_step() -> list[dict[str, Any]]:
                rows = _fetch_all(
                    conn,
                    """
                    SELECT pv.account, pv.cash, pv.date
                    FROM portfolio_value pv
                    JOIN (
                        SELECT account, MAX(date) AS max_date
                        FROM portfolio_value
                        WHERE account IN ({})
                        GROUP BY account
                    ) latest
                      ON pv.account = latest.account AND pv.date = latest.max_date
                    ORDER BY pv.account
                    """.format(",".join(["%s"] * len(accounts))),
                    tuple(accounts),
                ) if accounts else []
                by_account = {str(row.get("account")): row for row in rows}
                for account in accounts:
                    row = by_account.get(account) or {}
                    cash = row.get("cash")
                    status = "PASS" if cash is not None and float(cash) >= floor else "FAIL"
                    cash_rows.append(
                        {
                            "account": account,
                            "display": display_account(account),
                            "cash": None if cash is None else float(cash),
                            "floor": floor,
                            "date": row.get("date"),
                            "status": status,
                        }
                    )
                return cash_rows

            steps.append(_timed_step("paper 现金读取", cash_step))

            if write_probe:
                def write_step() -> str:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS diagnostics_write_probe (
                                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                                name VARCHAR(64),
                                created_at DATETIME
                            )
                            """
                        )
                        cursor.execute(
                            "INSERT INTO diagnostics_write_probe (name, created_at) VALUES (%s, %s)",
                            ("mysql_connectivity", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        )
                    conn.rollback()
                    return "write probe inserted then rolled back"

                steps.append(_timed_step("写入权限探针", write_step))
        finally:
            conn.close()

    failed = [step for step in steps if step.get("status") != "PASS"]
    cash_failed = [row for row in cash_rows if row.get("status") != "PASS"]
    overall = "FAIL" if failed or cash_failed else "PASS"
    recommendations: list[str] = []
    if any(step["name"] == "TCP 端口连通" and step["status"] != "PASS" for step in steps):
        recommendations.append("TCP 端口不通：优先检查服务器网络、安全组、防火墙和 DB_HOST/DB_PORT。")
    elif any(step["name"] == "MySQL 握手登录" and step["status"] != "PASS" for step in steps):
        recommendations.append("TCP 已通但 MySQL 握手失败：优先检查 MySQL 服务状态、连接数、账号权限、bind-address、SSL/认证插件和来源 IP 白名单。")
    elif any(step["name"] in {"基础查询 SELECT 1", "accounts 表检查", "portfolio_value 表检查"} and step["status"] != "PASS" for step in steps):
        recommendations.append("MySQL 可登录但基础查询失败：优先检查数据库负载、表锁、权限和 schema 状态。")
    elif cash_failed:
        recommendations.append("paper 现金未达训练下限：先执行 P10 现金校准，确认 paper_main/paper_watchlist 均不低于 100000。")
    else:
        recommendations.append("MySQL 训练前置检查通过：可以继续 P10/P11 和交易日影子采样。")

    return {
        "ok": overall == "PASS",
        "overall": overall,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runner": "run_openclaw_required",
        "env_file": ".env.openclaw",
        "host": host,
        "port": port,
        "database": Config.DB_NAME,
        "user": Config.DB_USER,
        "accounts": accounts,
        "timeout": timeout,
        "write_probe": write_probe,
        "steps": steps,
        "cash_rows": cash_rows,
        "recommendations": recommendations,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"【MySQL 训练前置诊断】{report.get('generated_at')}",
        "",
        f"- 总体状态: {report.get('overall')}",
        f"- 地址: {report.get('host')}:{report.get('port')}/{report.get('database')}",
        f"- 账号: {report.get('user')}",
        f"- 超时: {report.get('timeout')} 秒",
        "",
        "| 检查项 | 状态 | 耗时秒 | 说明/错误 |",
        "|---|---|---:|---|",
    ]
    for step in report.get("steps") or []:
        detail = step.get("detail") if step.get("status") == "PASS" else step.get("error")
        lines.append(f"| {step.get('name')} | {step.get('status')} | {step.get('elapsed_sec')} | {detail} |")

    if report.get("cash_rows"):
        lines.extend(["", "## paper 现金", "", "| 账户 | 当前现金 | 下限 | 数据时间 | 状态 |", "|---|---:|---:|---|---|"])
        for row in report.get("cash_rows") or []:
            cash = "-" if row.get("cash") is None else f"{_safe_float(row.get('cash')):,.2f}"
            floor = f"{_safe_float(row.get('floor')):,.2f}"
            lines.append(f"| {row.get('display') or row.get('account')} | {cash} | {floor} | {row.get('date') or '-'} | {row.get('status')} |")

    lines.extend(["", "## 下一步建议"])
    for item in report.get("recommendations") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def write_json_report(report: dict[str, Any], path: Path | None) -> str:
    if not path:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Layered MySQL connectivity diagnostics")
    parser.add_argument("--accounts", default="paper_main,paper_watchlist", help="Comma-separated paper accounts")
    parser.add_argument("--db-timeout", type=float, default=None, help="DB connect/read/write timeout in seconds")
    parser.add_argument("--write-probe", action="store_true", help="Run a rolled-back write permission probe")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON, help="Write machine-readable JSON report")
    parser.add_argument("--no-output-json", action="store_true", help="Do not write the JSON report file")
    parser.add_argument("--email", action="store_true", help="Send report by email")
    parser.add_argument("--assert-ready", action="store_true", help="Exit non-zero when diagnostics fail")
    args = parser.parse_args()

    report = build_report(_accounts(args.accounts), timeout=_db_timeout(args.db_timeout), write_probe=args.write_probe)
    if not args.no_output_json:
        report["output_json"] = write_json_report(report, args.output_json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))

    if args.email:
        Reporter().send_email(f"🧪【MySQL训练前置诊断】{report.get('overall')} {datetime.now().strftime('%Y-%m-%d')}", format_report(report))

    if args.assert_ready and report.get("overall") != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
