#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize paper/shadow training evidence from runtime logs.

This tool is intentionally log-only. It keeps offline training useful when
MySQL is temporarily unavailable, and it does not read or write trading state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.reporter import Reporter, log_report_snapshot  # noqa: E402

DEFAULT_THRESHOLDS = {
    "min_pending": 10,
    "min_checks": 10,
    "min_training_checks": 1,
    "min_buys": 1,
    "max_budget_too_small": 3,
    "max_window_not_allowed": 10,
    "max_retry_cooldown": 30,
}

PATTERNS = {
    "email_sent": "EMAIL_RESULT status=SENT",
    "paper_focus_pending": "PAPER_FOCUS_PENDING_CREATED",
    "paper_strong_pending": "PAPER_STRONG_PENDING_CREATED",
    "paper_training_check": "PAPER_TRAINING_PENDING_CHECK",
    "paper_sector_bypass": "PAPER_SECTOR_FILTER_BYPASS",
    "paper_board_bypass": "PAPER_BOARD_FILTER_BYPASS",
    "paper_training_bypass": "PAPER_TRAINING_FILTER_BYPASS",
    "pending_check": " - INFO - PENDING_CHECK id=",
    "unfillable_limit_up": "PAPER_STRONG_UNFILLABLE_LIMIT_UP",
    "sim_buy": "SIM_TRADE_BUY",
    "sim_sell": "SIM_TRADE_SELL",
    "storm_matrix_block": "permission_matrix regime=storm_market",
    "insufficient_samples": "弱市样本不足",
    "weak_chase_block": "弱市当前涨幅过大",
    "sector_reject": "行业轮动拒绝",
    "board_reject": "主板权限过滤拦截",
}

PAPER_TRAINING_ACCOUNTS = {"paper_main", "paper_watchlist"}

BUY_RE = re.compile(
    r"SIM_TRADE_BUY account=(?P<account>\S+) code=(?P<code>\S+) name=(?P<name>\S+) "
    r"strategy=(?P<strategy>\S+) .*?qty=(?P<qty>\d+) price=(?P<price>[-.\d]+)"
)
SELL_RE = re.compile(
    r"SIM_TRADE_SELL account=(?P<account>\S+) code=(?P<code>\S+) name=(?P<name>\S+) "
    r"strategy=(?P<strategy>\S+) .*?qty=(?P<qty>\d+) .*?sell=(?P<sell>[-.\d]+) "
    r"pnl=(?P<pnl>[-.\d]+) pnl_pct=(?P<pnl_pct>[-.\d]+) reason=(?P<reason>.*)$"
)
PENDING_RE = re.compile(
    r"PAPER_(?:FOCUS|STRONG)_PENDING_CREATED account=(?P<account>\S+) code=(?P<code>\S+) "
    r"name=(?P<name>\S+) strategy=(?P<strategy>\S+)"
)
SKIP_REASON_RE = re.compile(
    r"PENDING_SKIP .*?account=(?P<account>\S+).*?reason=(?P<reason>.*)$"
)
GATE_BLOCK_RE = re.compile(r"动态入场门禁拦截: .*?reason=(?P<reason>.*)$")
MODE_RE = re.compile(r"Starting Stock Analyzer in (?P<mode>[a-zA-Z0-9_]+) mode")
RUNTIME_FLAG_RE = re.compile(
    r"Runtime flags: .*?queue_entry=(?P<queue_entry>True|False).*?"
    r"paper_trade=(?P<paper_trade>True|False).*?monitor=(?P<monitor>True|False)"
)

TASK_LABELS = {
    "M01_MACRO": "M01 盘前宏观",
    "M02_RISK_DASHBOARD": "M02/M11 风控仪表盘",
    "M04_PRE_MARKET": "M04 主早盘竞价入队",
    "M05_WATCHLIST": "M05 主备选池入队",
    "M08_AFTERNOON": "M08 主午盘资金流入队",
    "M09_AUDIT": "M09 交易质量审计",
    "M10_TRACK": "M10 策略追踪",
    "M12_POST_MARKET": "M12 盘后资金复盘",
    "M14_SIM_REPORT": "M14 模拟仓日报",
    "P01_PAPER_SENTINEL": "P01 影子 paper-only 哨兵",
    "P02_PAPER_PRE_MARKET": "P02 影子早盘全池入队",
    "P03_P07_PAPER_WATCHLIST": "P03-P07 影子备选池巡航",
    "P08_PAPER_AFTERNOON": "P08 影子午盘资金流入队",
    "P09_PAPER_FOCUS": "P09 影子重点雷达入队",
    "P10_PAPER_CASH": "P10 影子训练现金校准",
    "P11_PAPER_PREFLIGHT": "P11 影子训练盘前预检",
    "R08_SHADOW_LOG_ANALYSIS": "R08 影子日志训练复盘",
}

REQUIRED_PAPER_TASKS = [
    "P01_PAPER_SENTINEL",
    "P02_PAPER_PRE_MARKET",
    "P03_P07_PAPER_WATCHLIST",
    "P08_PAPER_AFTERNOON",
    "P09_PAPER_FOCUS",
    "P10_PAPER_CASH",
    "P11_PAPER_PREFLIGHT",
]


def _task_key_for_runtime(mode: str, *, queue_entry: bool, paper_trade: bool, monitor: bool) -> str | None:
    mode = str(mode or "").strip()
    if mode == "macro":
        return "M01_MACRO"
    if mode == "risk_dashboard":
        return "M02_RISK_DASHBOARD"
    if mode == "audit":
        return "M09_AUDIT"
    if mode == "track":
        return "M10_TRACK"
    if mode == "post_market":
        return "M12_POST_MARKET"
    if not queue_entry:
        return None
    if mode == "pre_market":
        return "P02_PAPER_PRE_MARKET" if paper_trade else "M04_PRE_MARKET"
    if mode == "watchlist":
        return "P03_P07_PAPER_WATCHLIST" if paper_trade else "M05_WATCHLIST"
    if mode == "afternoon":
        return "P08_PAPER_AFTERNOON" if paper_trade else "M08_AFTERNOON"
    if mode == "focus_monitor" and paper_trade:
        return "P09_PAPER_FOCUS"
    return None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        return []


def _date_from_log_path(path: Path) -> str | None:
    match = re.search(r"stock_analyzer-(\d{8})\.log$", path.name)
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def _is_weekday(date_text: str | None) -> bool | None:
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").weekday() < 5
    except ValueError:
        return None


def _short_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return "UNKNOWN"
    text = re.sub(r"\s+", " ", text)
    replacements = [
        ("permission_matrix regime=storm_market key=* action=BLOCK", "暴雨矩阵 BLOCK"),
        ("弱市样本不足禁止自动买入", "弱市样本不足"),
        ("弱市当前涨幅过大禁止自动买入", "弱市涨幅过大"),
        ("window not allowed", "窗口不允许"),
        ("retry cooldown", "复核冷却"),
        ("近期过热拦截", "近期过热"),
        ("量比不足", "量比不足"),
        ("开盘区间突破/回踩确认不足", "开盘突破/回踩不足"),
        ("一字板涨停不可成交", "一字板不可成交"),
        ("paper weak daily cap reached", "paper弱市每日上限"),
    ]
    for needle, label in replacements:
        if needle in text:
            return label
    return text[:80]


def analyze_log(path: Path) -> dict[str, Any]:
    lines = _read_lines(path)
    log_date = _date_from_log_path(path)
    weekday_open = _is_weekday(log_date)
    counters = Counter()
    task_counts = Counter()
    skip_reasons = Counter()
    block_reasons = Counter()
    pending_by_strategy = Counter()
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    current_mode: str | None = None

    for line in lines:
        for key, needle in PATTERNS.items():
            if needle in line:
                counters[key] += 1

        mode_match = MODE_RE.search(line)
        if mode_match:
            current_mode = mode_match.group("mode")

        runtime_match = RUNTIME_FLAG_RE.search(line)
        if runtime_match and current_mode:
            task_key = _task_key_for_runtime(
                current_mode,
                queue_entry=runtime_match.group("queue_entry") == "True",
                paper_trade=runtime_match.group("paper_trade") == "True",
                monitor=runtime_match.group("monitor") == "True",
            )
            if task_key:
                task_counts[task_key] += 1
            current_mode = None

        if "Monitor paper-only enabled" in line or "PAPER_ONLY_PENDING_SCAN" in line:
            task_counts["P01_PAPER_SENTINEL"] += 1
        if "Subject: 🧪【影子账户训练现金校准】" in line:
            task_counts["P10_PAPER_CASH"] += 1
        if "【影子训练盘前预检】" in line or "check_shadow_training_preflight.py" in line:
            task_counts["P11_PAPER_PREFLIGHT"] += 1
        if "SIM_TRADE_SUMMARY" in line or "【模拟仓日报】" in line:
            task_counts["M14_SIM_REPORT"] += 1
        if "shadow_training_log_analysis" in line or "【影子训练验收】" in line:
            task_counts["R08_SHADOW_LOG_ANALYSIS"] += 1

        pending_match = PENDING_RE.search(line)
        if pending_match:
            pending_by_strategy[pending_match.group("strategy")] += 1

        skip_match = SKIP_REASON_RE.search(line)
        if skip_match and skip_match.group("account") in PAPER_TRAINING_ACCOUNTS:
            counters["pending_skip"] += 1
            skip_reasons[_short_reason(skip_match.group("reason"))] += 1

        block_match = GATE_BLOCK_RE.search(line)
        if block_match:
            block_reasons[_short_reason(block_match.group("reason"))] += 1

        buy_match = BUY_RE.search(line)
        if buy_match:
            item = buy_match.groupdict()
            item["qty"] = int(item["qty"])
            item["price"] = float(item["price"])
            buys.append(item)

        sell_match = SELL_RE.search(line)
        if sell_match:
            item = sell_match.groupdict()
            item["qty"] = int(item["qty"])
            item["sell"] = float(item["sell"])
            item["pnl"] = float(item["pnl"])
            item["pnl_pct"] = float(item["pnl_pct"])
            sells.append(item)

    return {
        "file": str(path),
        "exists": path.exists(),
        "log_date": log_date,
        "weekday_open": weekday_open,
        "lines": len(lines),
        "counts": dict(counters),
        "task_counts": dict(task_counts),
        "pending_by_strategy": dict(pending_by_strategy),
        "top_skip_reasons": dict(skip_reasons.most_common(10)),
        "top_gate_block_reasons": dict(block_reasons.most_common(10)),
        "buys": buys,
        "sells": sells,
        "paper_buy_count": len(buys),
        "paper_sell_count": len(sells),
    }


def build_report(paths: list[Path]) -> dict[str, Any]:
    reports = [analyze_log(path) for path in paths]
    total_counts = Counter()
    total_task_counts = Counter()
    total_pending_by_strategy = Counter()
    total_skip_reasons = Counter()
    total_block_reasons = Counter()
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    weekdays: list[bool] = []

    for report in reports:
        total_counts.update(report.get("counts") or {})
        total_task_counts.update(report.get("task_counts") or {})
        total_pending_by_strategy.update(report.get("pending_by_strategy") or {})
        total_skip_reasons.update(report.get("top_skip_reasons") or {})
        total_block_reasons.update(report.get("top_gate_block_reasons") or {})
        buys.extend(report.get("buys") or [])
        sells.extend(report.get("sells") or [])
        if report.get("weekday_open") is not None:
            weekdays.append(bool(report.get("weekday_open")))

    contains_trade_day = any(weekdays) if weekdays else None
    contains_only_non_trade_days = bool(weekdays) and not contains_trade_day

    report = {
        "ok": True,
        "files": reports,
        "totals": {
            "contains_trade_day": contains_trade_day,
            "contains_only_non_trade_days": contains_only_non_trade_days,
            "counts": dict(total_counts),
            "task_counts": dict(total_task_counts),
            "task_coverage": build_task_coverage(total_task_counts, enabled=not contains_only_non_trade_days),
            "pending_by_strategy": dict(total_pending_by_strategy),
            "top_skip_reasons": dict(total_skip_reasons.most_common(10)),
            "top_gate_block_reasons": dict(total_block_reasons.most_common(10)),
            "paper_buy_count": len(buys),
            "paper_sell_count": len(sells),
            "buys": buys,
            "sells": sells,
            "new_bypass_seen": bool(
                total_counts.get("paper_sector_bypass")
                or total_counts.get("paper_board_bypass")
                or total_counts.get("paper_training_bypass")
            ),
        },
    }
    report["health"] = evaluate_health(report)
    return report


def build_task_coverage(task_counts: Counter | dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    counts = Counter(task_counts or {})
    seen = [
        {"key": key, "name": TASK_LABELS.get(key, key), "count": int(count)}
        for key, count in sorted(counts.items())
        if int(count or 0) > 0
    ]
    if not enabled:
        return {
            "seen": seen,
            "missing_paper_tasks": [],
            "paper_task_status": "N/A",
            "note": "非交易日日志，不要求 P01-P11 影子任务覆盖",
        }

    missing_paper = [
        {"key": key, "name": TASK_LABELS.get(key, key)}
        for key in REQUIRED_PAPER_TASKS
        if int(counts.get(key, 0) or 0) <= 0
    ]
    return {
        "seen": seen,
        "missing_paper_tasks": missing_paper,
        "paper_task_status": "PASS" if not missing_paper else "FAIL",
    }


def evaluate_health(report: dict[str, Any], thresholds: dict[str, Any] | None = None, *, expect_bypass: bool = False) -> dict[str, Any]:
    """Evaluate whether shadow training produced enough usable evidence."""
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    totals = report.get("totals") or {}
    counts = totals.get("counts") or {}
    task_coverage = totals.get("task_coverage") or {}
    skip_reasons = totals.get("top_skip_reasons") or {}
    non_trade_day_only = bool(totals.get("contains_only_non_trade_days"))

    pending_total = int(counts.get("paper_focus_pending", 0) or 0) + int(counts.get("paper_strong_pending", 0) or 0)
    check_total = int(counts.get("pending_check", 0) or 0)
    training_check_total = int(counts.get("paper_training_check", 0) or 0)
    buy_total = int(totals.get("paper_buy_count", 0) or 0)
    bypass_total = (
        int(counts.get("paper_sector_bypass", 0) or 0)
        + int(counts.get("paper_board_bypass", 0) or 0)
        + int(counts.get("paper_training_bypass", 0) or 0)
    )
    budget_too_small = int(skip_reasons.get("budget too small", 0) or 0)
    window_not_allowed = int(skip_reasons.get("窗口不允许", 0) or 0)
    retry_cooldown = int(skip_reasons.get("复核冷却", 0) or 0)
    has_task_coverage = "task_coverage" in totals
    missing_paper_tasks = len(task_coverage.get("missing_paper_tasks") or []) if has_task_coverage else 0

    checks = []

    def add_check(name: str, status: str, actual: int, threshold: int, message: str) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "actual": actual,
                "threshold": threshold,
                "message": message,
            }
        )

    min_pending = int(thresholds.get("min_pending", 10) or 0)
    if non_trade_day_only:
        checks.append(
            {
                "name": "non_trade_day",
                "status": "N/A",
                "actual": 0,
                "threshold": 0,
                "message": "非交易日日志，仅用于进化/研究邮件检查，不做影子交易验收",
            }
        )
        return {
            "overall": "N/A",
            "thresholds": thresholds,
            "metrics": {
                "paper_pending": pending_total,
                "pending_checks": check_total,
                "paper_training_checks": training_check_total,
                "paper_buys": buy_total,
                "paper_filter_bypass": bypass_total,
                "budget_too_small": budget_too_small,
                "window_not_allowed": window_not_allowed,
                "retry_cooldown": retry_cooldown,
                "missing_paper_tasks": 0,
                "non_trade_day_only": 1,
            },
            "checks": checks,
            "recommendations": ["当前日志日期为非交易日：不据此调整影子买入、预算、冷却或任务课表；等待下一份真实交易日日志做运行验收。"],
        }

    add_check(
        "paper_pending",
        "PASS" if pending_total >= min_pending else "FAIL",
        pending_total,
        min_pending,
        f"paper 入队 {pending_total}，最低要求 {min_pending}",
    )

    min_checks = int(thresholds.get("min_checks", 10) or 0)
    add_check(
        "pending_checks",
        "PASS" if check_total >= min_checks else "FAIL",
        check_total,
        min_checks,
        f"哨兵复核 {check_total}，最低要求 {min_checks}",
    )

    min_training_checks = int(thresholds.get("min_training_checks", 1) or 0)
    training_status = "PASS"
    if check_total > 0 and training_check_total < min_training_checks:
        training_status = "WARN"
    add_check(
        "paper_training_checks",
        training_status,
        training_check_total,
        min_training_checks,
        f"paper 训练阈值复核 {training_check_total}，目标至少 {min_training_checks}",
    )

    min_buys = int(thresholds.get("min_buys", 1) or 0)
    add_check(
        "paper_buys",
        "PASS" if buy_total >= min_buys else "WARN",
        buy_total,
        min_buys,
        f"影子买入 {buy_total}，目标至少 {min_buys}",
    )

    max_budget = int(thresholds.get("max_budget_too_small", 3) or 0)
    add_check(
        "budget_too_small",
        "PASS" if budget_too_small <= max_budget else "WARN",
        budget_too_small,
        max_budget,
        f"预算不足跳过 {budget_too_small}，期望不超过 {max_budget}",
    )

    max_window = int(thresholds.get("max_window_not_allowed", 10) or 0)
    add_check(
        "window_not_allowed",
        "PASS" if window_not_allowed <= max_window else "WARN",
        window_not_allowed,
        max_window,
        f"窗口不允许跳过 {window_not_allowed}，期望不超过 {max_window}",
    )

    max_cooldown = int(thresholds.get("max_retry_cooldown", 30) or 0)
    add_check(
        "retry_cooldown",
        "PASS" if retry_cooldown <= max_cooldown else "WARN",
        retry_cooldown,
        max_cooldown,
        f"复核冷却跳过 {retry_cooldown}，期望不超过 {max_cooldown}",
    )

    if expect_bypass:
        add_check(
            "paper_filter_bypass",
            "PASS" if bypass_total > 0 else "WARN",
            bypass_total,
            1,
            f"paper 过滤绕过标签 {bypass_total}，期望至少出现 1 次",
        )

    if has_task_coverage:
        add_check(
            "paper_task_coverage",
            str(task_coverage.get("paper_task_status") or ("PASS" if missing_paper_tasks <= 0 else "FAIL")),
            len(REQUIRED_PAPER_TASKS) - missing_paper_tasks,
            len(REQUIRED_PAPER_TASKS),
            f"影子任务覆盖 {len(REQUIRED_PAPER_TASKS) - missing_paper_tasks}/{len(REQUIRED_PAPER_TASKS)}",
        )

    statuses = [c.get("status") for c in checks]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    recommendations = build_recommendations(
        {
            "paper_pending": pending_total,
            "pending_checks": check_total,
            "paper_training_checks": training_check_total,
            "paper_buys": buy_total,
            "paper_filter_bypass": bypass_total,
            "budget_too_small": budget_too_small,
            "window_not_allowed": window_not_allowed,
            "retry_cooldown": retry_cooldown,
            "missing_paper_tasks": missing_paper_tasks,
        },
        thresholds,
        expect_bypass=expect_bypass,
    )
    return {
        "overall": overall,
        "thresholds": thresholds,
        "metrics": {
            "paper_pending": pending_total,
            "pending_checks": check_total,
            "paper_training_checks": training_check_total,
            "paper_buys": buy_total,
            "paper_filter_bypass": bypass_total,
            "budget_too_small": budget_too_small,
            "window_not_allowed": window_not_allowed,
            "retry_cooldown": retry_cooldown,
            "missing_paper_tasks": missing_paper_tasks,
        },
        "checks": checks,
        "recommendations": recommendations,
    }


def build_recommendations(metrics: dict[str, int], thresholds: dict[str, Any], *, expect_bypass: bool = False) -> list[str]:
    """Build concrete next actions from health metrics."""
    recs: list[str] = []
    if int(metrics.get("missing_paper_tasks", 0) or 0) > 0:
        recs.append("影子任务覆盖不足：先核对 OpenClaw 是否执行 P01-P11，尤其 P01 paper-only 哨兵、P02 早盘全池、P09 重点雷达、P10 现金校准和 P11 盘前预检。")
    if int(metrics.get("paper_pending", 0) or 0) < int(thresholds.get("min_pending", 0) or 0):
        recs.append("paper 入队不足：检查 P02-P09 是否按课表启用，重点看 P09 重点雷达和 P02 早盘全池。")
    if int(metrics.get("pending_checks", 0) or 0) < int(thresholds.get("min_checks", 0) or 0):
        recs.append("哨兵复核不足：检查 P01 paper-only 哨兵是否 09:26-15:56 每 10 分钟运行。")
    if int(metrics.get("pending_checks", 0) or 0) > 0 and int(metrics.get("paper_training_checks", 0) or 0) < int(thresholds.get("min_training_checks", 1) or 0):
        recs.append("未看到 paper 训练阈值复核：确认新版 monitor.py 已部署，普通 paper_* pending 应出现 PAPER_TRAINING_PENDING_CHECK。")
    if int(metrics.get("paper_buys", 0) or 0) < int(thresholds.get("min_buys", 0) or 0):
        recs.append("影子买入不足：优先复核量比/VWAP/近期过热/一字板不可成交原因，不要直接放宽主账户。")
    if int(metrics.get("budget_too_small", 0) or 0) > int(thresholds.get("max_budget_too_small", 0) or 0):
        recs.append("预算不足偏高：确认 P10 影子现金校准、paper_* 一手兜底、paper 连续亏损降仓是否生效；不要让 paper 连续亏损硬停归零。")
    if int(metrics.get("window_not_allowed", 0) or 0) > int(thresholds.get("max_window_not_allowed", 0) or 0):
        recs.append("窗口不允许偏高：检查 paper_all_pool_execution.windows 是否覆盖对应策略，尤其午盘精选 B2-B5。")
    if int(metrics.get("retry_cooldown", 0) or 0) > int(thresholds.get("max_retry_cooldown", 0) or 0):
        recs.append("复核冷却偏高：确认 paper_all_pool_execution.scan_interval_sec 不低于 retry_cooldown_sec，且线上运行新版 monitor.py；如果仍偏高，再评估 P01 哨兵频率。")
    if expect_bypass and int(metrics.get("paper_filter_bypass", 0) or 0) <= 0:
        recs.append("未看到 paper 过滤绕过标签：检查是否出现行业/板块权限被主账户挡住的候选，或确认新版 main.py 已部署。")
    if not recs:
        recs.append("影子训练链路达标：下一步观察买卖盈亏、策略来源分布和 T+1/T+2 表现。")
    return recs


def _fmt_counter(data: dict[str, Any]) -> str:
    if not data:
        return "无"
    return "；".join(f"{k}: {v}" for k, v in data.items())


def format_text_report(report: dict[str, Any]) -> str:
    totals = report.get("totals") or {}
    counts = totals.get("counts") or {}
    lines = [
        "【影子训练日志复盘】",
        f"- 文件数: {len(report.get('files') or [])}",
        f"- 邮件发送: {counts.get('email_sent', 0)}",
        f"- paper 重点雷达入队: {counts.get('paper_focus_pending', 0)}",
        f"- paper 强票实验入队: {counts.get('paper_strong_pending', 0)}",
        f"- paper 复核: {counts.get('pending_check', 0)}",
        f"- paper 训练阈值复核: {counts.get('paper_training_check', 0)}",
        f"- paper 跳过: {counts.get('pending_skip', 0)}",
        f"- 一字板不可成交: {counts.get('unfillable_limit_up', 0)}",
        f"- 影子买入: {totals.get('paper_buy_count', 0)}",
        f"- 影子卖出: {totals.get('paper_sell_count', 0)}",
        f"- 新版 paper 过滤绕过标签出现: {'是' if totals.get('new_bypass_seen') else '否'}",
        "",
        f"入队策略分布: {_fmt_counter(totals.get('pending_by_strategy') or {})}",
        f"主要跳过原因: {_fmt_counter(totals.get('top_skip_reasons') or {})}",
        f"主要门禁拦截: {_fmt_counter(totals.get('top_gate_block_reasons') or {})}",
    ]
    task_coverage = totals.get("task_coverage") or {}
    seen_tasks = task_coverage.get("seen") or []
    missing_tasks = task_coverage.get("missing_paper_tasks") or []
    if seen_tasks or missing_tasks:
        lines.append("")
        lines.append(f"任务覆盖: {task_coverage.get('paper_task_status') or '-'}")
        lines.append(f"- 已见任务: {_fmt_counter({item.get('name'): item.get('count') for item in seen_tasks})}")
        lines.append(f"- 缺失影子任务: {'、'.join(item.get('name') for item in missing_tasks) if missing_tasks else '无'}")
    health = report.get("health") or {}
    if health:
        lines.append("")
        lines.append(f"训练验收: {health.get('overall')}")
        for item in health.get("checks") or []:
            lines.append(f"- {item.get('status')} {item.get('message')}")
        recommendations = health.get("recommendations") or []
        if recommendations:
            lines.append("")
            lines.append("下一步建议:")
            for item in recommendations:
                lines.append(f"- {item}")

    buys = totals.get("buys") or []
    if buys:
        lines.extend(["", "| 买入账户 | 代码 | 名称 | 策略 | 股数 | 价格 |", "|---|---|---|---|---:|---:|"])
        for item in buys:
            lines.append(
                f"| {item.get('account')} | {item.get('code')} | {item.get('name')} | "
                f"{item.get('strategy')} | {item.get('qty')} | {item.get('price'):.3f} |"
            )

    sells = totals.get("sells") or []
    if sells:
        lines.extend(
            [
                "",
                "| 卖出账户 | 代码 | 名称 | 策略 | 股数 | 卖价 | 盈亏 | 盈亏比例 | 原因 |",
                "|---|---|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for item in sells:
            lines.append(
                f"| {item.get('account')} | {item.get('code')} | {item.get('name')} | "
                f"{item.get('strategy')} | {item.get('qty')} | {item.get('sell'):.3f} | "
                f"{item.get('pnl'):.2f} | {item.get('pnl_pct'):.2f}% | {item.get('reason')} |"
            )

    return "\n".join(lines)


def print_text(report: dict[str, Any]) -> None:
    print(format_text_report(report))


def send_email_report(report: dict[str, Any], *, dates: list[str] | None = None) -> bool:
    health = report.get("health") or {}
    status = str(health.get("overall") or "UNKNOWN")
    date_text = ",".join(dates or []) or datetime.now().strftime("%Y-%m-%d")
    subject = f"🧪【影子训练验收】{status} {date_text}"
    content = format_text_report(report)
    log_report_snapshot(subject, content, source="shadow_training_log_analysis")
    return bool(Reporter().send_email(subject, content))


def _default_log_path(date: str) -> Path:
    compact = date.replace("-", "")
    return ROOT / "logs" / f"stock_analyzer-{compact}.log"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze paper/shadow training evidence from logs.")
    parser.add_argument("--date", action="append", default=[], help="trade date, YYYY-MM-DD or YYYYMMDD; can repeat")
    parser.add_argument("--log", action="append", default=[], help="explicit log file path; can repeat")
    parser.add_argument("--min-pending", type=int, default=DEFAULT_THRESHOLDS["min_pending"])
    parser.add_argument("--min-checks", type=int, default=DEFAULT_THRESHOLDS["min_checks"])
    parser.add_argument("--min-buys", type=int, default=DEFAULT_THRESHOLDS["min_buys"])
    parser.add_argument("--max-budget-too-small", type=int, default=DEFAULT_THRESHOLDS["max_budget_too_small"])
    parser.add_argument("--max-window-not-allowed", type=int, default=DEFAULT_THRESHOLDS["max_window_not_allowed"])
    parser.add_argument("--max-retry-cooldown", type=int, default=DEFAULT_THRESHOLDS["max_retry_cooldown"])
    parser.add_argument("--expect-bypass", action="store_true", help="warn if paper bypass tags did not appear")
    parser.add_argument("--assert-healthy", action="store_true", help="exit non-zero when health is FAIL")
    parser.add_argument("--strict", action="store_true", help="with --assert-healthy, also fail on WARN")
    parser.add_argument("--email", action="store_true", help="Send the shadow training health report by email")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = [Path(p) for p in args.log]
    paths.extend(_default_log_path(d) for d in args.date)
    if not paths:
        parser.error("provide --date or --log")

    report = build_report(paths)
    thresholds = {
        "min_pending": args.min_pending,
        "min_checks": args.min_checks,
        "min_buys": args.min_buys,
        "max_budget_too_small": args.max_budget_too_small,
        "max_window_not_allowed": args.max_window_not_allowed,
        "max_retry_cooldown": args.max_retry_cooldown,
    }
    report["health"] = evaluate_health(report, thresholds, expect_bypass=bool(args.expect_bypass))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print_text(report)
    if args.email:
        send_email_report(report, dates=args.date)
    if args.assert_healthy:
        overall = str((report.get("health") or {}).get("overall") or "FAIL")
        if overall == "FAIL" or (args.strict and overall == "WARN"):
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
