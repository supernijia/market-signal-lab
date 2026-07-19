#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks for the paper-only shadow training runtime."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config  # noqa: E402
from core.display_labels import display_account  # noqa: E402
from core.reporter import Reporter  # noqa: E402
from tools.maintenance.ensure_paper_training_cash import _db_timeout, _read_current_cash  # noqa: E402

DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "reports" / "shadow_training_preflight" / "latest.json"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _accounts(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _read_text(rel_path: str) -> str:
    path = PROJECT_ROOT / rel_path
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _check(name: str, ok: bool, detail: str, *, severity: str = "FAIL") -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if ok else severity,
        "ok": bool(ok),
        "detail": detail,
    }


def _cfg(name: str) -> dict[str, Any]:
    value = Config.STRATEGY.get(name, {}) if isinstance(Config.STRATEGY, dict) else {}
    return value if isinstance(value, dict) else {}


def _position_sizer_paper_override() -> dict[str, Any]:
    sizer = _cfg("position_sizer")
    overrides = sizer.get("account_overrides", {}) if isinstance(sizer.get("account_overrides"), dict) else {}
    value = overrides.get("paper_*", {})
    return value if isinstance(value, dict) else {}


def _fetch_cash(accounts: list[str], *, db_timeout: float) -> tuple[dict[str, float], str | None]:
    values: dict[str, float] = {}
    errors: list[str] = []
    for account in accounts:
        try:
            values[account] = float(_read_current_cash(account, db_timeout=db_timeout))
        except Exception as exc:
            errors.append(f"{account}: {exc}")
    return values, "; ".join(errors) if errors else None


def build_report(accounts: list[str], *, check_db_cash: bool = False, db_timeout: float = 5.0) -> dict[str, Any]:
    strategy = Config.STRATEGY if isinstance(Config.STRATEGY, dict) else {}
    paper_all = _cfg("paper_all_pool_execution")
    paper_account = _cfg("paper_account")
    paper_exit = _cfg("paper_exit_policy")
    paper_strong = _cfg("paper_strong_entry_experiment")
    paper_sizer = _position_sizer_paper_override()

    monitor_text = _read_text("monitor.py")
    cash_text = _read_text("tools/maintenance/ensure_paper_training_cash.py")
    log_diag_text = _read_text("tools/diagnostics/analyze_shadow_training_logs.py")

    scan_interval = _safe_int(paper_all.get("scan_interval_sec"), 0)
    retry_cooldown = _safe_int(paper_all.get("retry_cooldown_sec"), 0)
    training_target = _safe_float(paper_account.get("training_cash_target"), 0.0)
    training_floor = _safe_float(paper_account.get("training_cash_floor"), 0.0)
    windows = paper_all.get("windows", {}) if isinstance(paper_all.get("windows"), dict) else {}
    default_windows = set(windows.get("*") or [])
    afternoon_windows = set(windows.get("午盘精选") or [])

    checks = [
        _check(
            "新版 paper 训练复核埋点",
            "PAPER_TRAINING_PENDING_CHECK" in monitor_text,
            "monitor.py 必须包含 PAPER_TRAINING_PENDING_CHECK；否则下个交易日仍无法证明线上走统一 paper 阈值。",
        ),
        _check(
            "paper-only 独立扫描节奏",
            "paper_all_pool_execution" in monitor_text and scan_interval >= retry_cooldown > 0,
            f"scan_interval_sec={scan_interval}, retry_cooldown_sec={retry_cooldown}；扫描间隔必须不低于冷却时间，避免复核冷却噪声。",
        ),
        _check(
            "paper 全池入队已启用",
            bool(paper_all.get("enabled")) and bool(paper_all.get("focus_monitor_enabled")),
            f"paper_all_pool_execution.enabled={paper_all.get('enabled')} focus_monitor_enabled={paper_all.get('focus_monitor_enabled')}",
        ),
        _check(
            "paper 执行窗口覆盖",
            {"B1", "B2", "B3", "B4"}.issubset(default_windows) and {"B2", "B3", "B4", "B5"}.issubset(afternoon_windows),
            f"default_windows={sorted(default_windows)} 午盘精选={sorted(afternoon_windows)}",
        ),
        _check(
            "paper 强票实验已启用",
            bool(paper_strong.get("enabled")) and _safe_float(paper_strong.get("min_volume_ratio"), 99.0) <= 0.9,
            f"enabled={paper_strong.get('enabled')} min_volume_ratio={paper_strong.get('min_volume_ratio')} min_price_vwap_ratio={paper_strong.get('min_price_vwap_ratio')}",
        ),
        _check(
            "paper 训练现金配置",
            bool(paper_account.get("enabled")) and training_target >= 100000 and training_floor >= 100000,
            f"enabled={paper_account.get('enabled')} training_cash_target={training_target:.0f} training_cash_floor={training_floor:.0f}",
        ),
        _check(
            "P10 现金校准工具",
            "PAPER_TRAINING_CASH" in cash_text and "training_cash_target" in cash_text,
            "tools/maintenance/ensure_paper_training_cash.py 必须可校准 paper_* 现金并写日志。",
        ),
        _check(
            "paper 一手兜底",
            bool(paper_sizer.get("ensure_round_lot_when_cash_available"))
            and _safe_float(paper_sizer.get("min_order_position_pct_floor"), 1.0) <= 0.001,
            f"ensure_round_lot_when_cash_available={paper_sizer.get('ensure_round_lot_when_cash_available')} min_order_position_pct_floor={paper_sizer.get('min_order_position_pct_floor')}",
        ),
        _check(
            "paper 连续亏损不硬停",
            _safe_int((paper_sizer.get("consecutive_loss") or {}).get("hard_stop_count"), 0) >= 999
            and _safe_float((paper_sizer.get("daily_loss") or {}).get("hard_stop_pct"), 0.0) <= -1.0,
            f"daily_loss={paper_sizer.get('daily_loss')} consecutive_loss={paper_sizer.get('consecutive_loss')}",
        ),
        _check(
            "paper 短线卖出策略",
            bool(paper_exit.get("enabled")) and _safe_float(paper_exit.get("stop_loss"), 0.0) < 0 and _safe_float(paper_exit.get("min_profit_lock_trigger"), 0.0) > 0,
            f"enabled={paper_exit.get('enabled')} stop_loss={paper_exit.get('stop_loss')} min_profit_lock_trigger={paper_exit.get('min_profit_lock_trigger')}",
        ),
        _check(
            "严格日志验收支持",
            "PAPER_TRAINING_PENDING_CHECK" in log_diag_text and "assert-healthy" in log_diag_text and "strict" in log_diag_text,
            "analyze_shadow_training_logs.py 必须支持严格验收，WARN 不能被当成 PASS。",
        ),
    ]

    cash_rows: list[dict[str, Any]] = []
    cash_error = None
    if check_db_cash:
        cash_values, cash_error = _fetch_cash(accounts, db_timeout=db_timeout)
        for account in accounts:
            cash = cash_values.get(account)
            ok = cash is not None and cash >= training_floor
            cash_rows.append(
                {
                    "account": account,
                    "display": display_account(account),
                    "cash": cash,
                    "floor": training_floor,
                    "status": "PASS" if ok else "FAIL",
                }
            )
        if cash_error:
            checks.append(_check("paper 数据库现金读取", False, cash_error))
        elif cash_rows:
            checks.append(
                _check(
                    "paper 数据库现金下限",
                    all(row["status"] == "PASS" for row in cash_rows),
                    "; ".join(f"{row['account']}={row['cash']:.2f}" for row in cash_rows if row.get("cash") is not None),
                )
            )

    failed = [row for row in checks if row["status"] == "FAIL"]
    warnings = [row for row in checks if row["status"] == "WARN"]
    overall = "FAIL" if failed else "WARN" if warnings else "PASS"
    recommendations = []
    if overall == "PASS":
        recommendations.append("盘前预检通过：下个交易日可以按 P01-P11 继续采集 paper-only 买卖样本。")
    else:
        recommendations.append("盘前预检未完全通过：先修部署/配置/现金校准，再等待交易日日志验收；不要先放宽主账户门禁。")
    if any(row["name"] == "新版 paper 训练复核埋点" and row["status"] != "PASS" for row in checks):
        recommendations.append("优先同步新版 monitor.py，直到日志能出现 PAPER_TRAINING_PENDING_CHECK。")
    if any(row["name"] in {"paper 训练现金配置", "paper 数据库现金下限", "P10 现金校准工具"} and row["status"] != "PASS" for row in checks):
        recommendations.append("优先执行 P10 影子训练现金校准，并确认 paper_main/paper_watchlist 现金不低于训练下限。")
    if any(row["name"] == "paper-only 独立扫描节奏" and row["status"] != "PASS" for row in checks):
        recommendations.append("调整 paper_all_pool_execution.scan_interval_sec，使其不低于 retry_cooldown_sec。")

    return {
        "ok": overall == "PASS",
        "overall": overall,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runner": "run_openclaw_required",
        "env_file": ".env.openclaw",
        "accounts": accounts,
        "config": {
            "paper_all_pool_execution": paper_all,
            "paper_account": paper_account,
            "paper_exit_policy": paper_exit,
            "paper_strong_entry_experiment": paper_strong,
            "paper_position_sizer_override": paper_sizer,
            "strategy_keys": sorted(strategy.keys()),
        },
        "checks": checks,
        "cash_rows": cash_rows,
        "cash_error": cash_error,
        "recommendations": recommendations,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"【影子训练盘前预检】{report.get('generated_at')}",
        "",
        f"- 总体状态: {report.get('overall')}",
        f"- 账户: {', '.join(report.get('accounts') or [])}",
        "",
        "| 检查项 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for row in report.get("checks") or []:
        lines.append(f"| {row.get('name')} | {row.get('status')} | {row.get('detail')} |")
    if report.get("cash_rows"):
        lines.extend(["", "## paper 现金", "", "| 账户 | 当前现金 | 下限 | 状态 |", "|---|---:|---:|---|"])
        for row in report.get("cash_rows") or []:
            cash = "-" if row.get("cash") is None else f"{_safe_float(row.get('cash')):,.2f}"
            floor = f"{_safe_float(row.get('floor')):,.2f}"
            lines.append(f"| {row.get('display') or row.get('account')} | {cash} | {floor} | {row.get('status')} |")
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
    parser = argparse.ArgumentParser(description="Check paper-only shadow training preflight readiness.")
    parser.add_argument("--accounts", default="paper_main,paper_watchlist", help="Comma-separated paper accounts")
    parser.add_argument("--check-db-cash", action="store_true", help="Read current paper cash from database")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON, help="Write machine-readable JSON report")
    parser.add_argument("--no-output-json", action="store_true", help="Do not write the JSON report file")
    parser.add_argument("--email", action="store_true", help="Send preflight report by email")
    parser.add_argument("--assert-ready", action="store_true", help="Exit non-zero when preflight is not PASS")
    parser.add_argument("--db-timeout", type=float, default=None, help="Direct DB connect/read timeout for the cash check")
    args = parser.parse_args()

    report = build_report(_accounts(args.accounts), check_db_cash=args.check_db_cash, db_timeout=_db_timeout(args.db_timeout))
    if not args.no_output_json:
        report["output_json"] = write_json_report(report, args.output_json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))

    if args.email:
        Reporter().send_email(f"🧪【影子训练盘前预检】{report.get('overall')} {datetime.now().strftime('%Y-%m-%d')}", format_report(report))

    if args.assert_ready and report.get("overall") != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
