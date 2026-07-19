#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate and optionally email a clear simulated-trade ledger."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Optional


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data_provider import DataProvider  # noqa: E402
from core.display_labels import display_account  # noqa: E402
from core.portfolio import PortfolioManager  # noqa: E402
from core.reporter import Reporter, log_report_snapshot  # noqa: E402
from core.utils import setup_logger  # noqa: E402


logger = logging.getLogger("StockAnalyzer.SimTradeReport")


def _mark_data_warning(pm: PortfolioManager, message: str) -> None:
    warnings = getattr(pm, "_sim_report_warnings", None)
    if not isinstance(warnings, list):
        warnings = []
        setattr(pm, "_sim_report_warnings", warnings)
    if message not in warnings:
        warnings.append(message)


def _fetch_rows(pm: PortfolioManager, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = pm._get_connection()
    if not conn:
        _mark_data_warning(pm, "数据库连接失败，交易流水可能不完整。")
        return []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() or []
    except Exception as exc:
        _mark_data_warning(pm, f"交易流水查询失败: {str(exc)[:120]}")
        logger.warning("SIM_TRADE_DATA_WARNING query transactions failed: %s", exc)
        return []
    finally:
        conn.close()


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


def _fmt_money(value: Any) -> str:
    return f"{_safe_float(value):,.2f}"


def _fmt_pct(value: Any) -> str:
    return f"{_safe_float(value):+.2f}%"


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)[:19]


def _account_text(value: Any) -> str:
    return display_account(str(value or ""))


def _load_account_positions(pm: PortfolioManager, account: str) -> list[dict[str, Any]]:
    conn = pm._get_connection()
    if not conn:
        _mark_data_warning(pm, f"数据库连接失败，{_account_text(account)} 当前持仓可能不完整。")
        return []
    try:
        with conn.cursor() as cursor:
            sql = """SELECT p.*,
                     (SELECT MIN(date) FROM transactions t WHERE t.code = p.code AND t.account = p.account AND t.type = 'BUY') AS created_at
                     FROM positions p WHERE p.account=%s"""
            cursor.execute(sql, (account,))
            return cursor.fetchall() or []
    except Exception as exc:
        _mark_data_warning(pm, f"{_account_text(account)} 持仓查询失败: {str(exc)[:120]}")
        logger.warning("SIM_TRADE_DATA_WARNING query positions failed account=%s error=%s", account, exc)
        return []
    finally:
        conn.close()


def _load_realtime_prices(positions: list[dict[str, Any]]) -> dict[str, float]:
    codes = sorted({str(row.get("code") or "") for row in positions if row.get("code")})
    if not codes:
        return {}
    try:
        provider = DataProvider()
        basics = provider.get_stock_basic()
        code_ts = {key.split(".")[0]: key for key in basics}
        ts_codes = [code_ts[code] for code in codes if code in code_ts]
        quotes = provider.get_realtime_quotes(ts_codes) or {}
        prices = {}
        for ts_code, quote in quotes.items():
            code = ts_code.split(".")[0]
            price = _safe_float((quote or {}).get("price"), 0.0)
            if price > 0:
                prices[code] = price
        return prices
    except Exception:
        return {}


def _query_transactions(pm: PortfolioManager, accounts: list[str], start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(accounts))
    where = [f"account IN ({placeholders})"]
    params: list[Any] = list(accounts)
    if start_date:
        where.append("DATE(date) >= %s")
        params.append(start_date)
    if end_date:
        where.append("DATE(date) <= %s")
        params.append(end_date)
    sql = f"""
        SELECT id, date, account, type, code, name, price, quantity, amount, balance, reason,
               source_strategy, weather, signal_tags_json
        FROM transactions
        WHERE {" AND ".join(where)}
        ORDER BY account, code, date, id
    """
    return _fetch_rows(pm, sql, tuple(params))


def _query_pending_check_events(pm: PortfolioManager, accounts: list[str], start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(accounts))
    where = [f"account IN ({placeholders})"]
    params: list[Any] = list(accounts)
    if start_date:
        where.append("DATE(check_time) >= %s")
        params.append(start_date)
    if end_date:
        where.append("DATE(check_time) <= %s")
        params.append(end_date)
    if not start_date and not end_date:
        where.append("DATE(check_time) = CURDATE()")
    sql = f"""
        SELECT account, strategy, decision, reason, COUNT(1) AS cnt, MAX(check_time) AS last_check_time
        FROM pending_entry_check_events
        WHERE {" AND ".join(where)}
        GROUP BY account, strategy, decision, reason
        ORDER BY account, strategy, cnt DESC, last_check_time DESC
        LIMIT 80
    """
    return _fetch_rows(pm, sql, tuple(params))


def _closed_trades(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lots: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []
    for tx in transactions:
        side = str(tx.get("type") or "").upper()
        account = str(tx.get("account") or "")
        code = str(tx.get("code") or "")
        key = (account, code)
        qty = _safe_int(tx.get("quantity"), 0)
        price = _safe_float(tx.get("price"), 0.0)
        if qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            lots[key].append({
                "buy_time": tx.get("date"),
                "buy_price": price,
                "remaining": qty,
                "name": tx.get("name") or "",
                "strategy": tx.get("source_strategy") or "",
                "weather": tx.get("weather") or "",
            })
        elif side == "SELL":
            sell_qty = qty
            while sell_qty > 0 and lots[key]:
                lot = lots[key][0]
                matched = min(sell_qty, int(lot["remaining"]))
                buy_price = _safe_float(lot["buy_price"], 0.0)
                cost = buy_price * matched
                proceeds = price * matched
                pnl = proceeds - cost
                pnl_pct = pnl / cost * 100.0 if cost > 0 else 0.0
                closed.append({
                    "account": account,
                    "code": code,
                    "name": tx.get("name") or lot.get("name") or "",
                    "strategy": tx.get("source_strategy") or lot.get("strategy") or "",
                    "buy_time": lot.get("buy_time"),
                    "sell_time": tx.get("date"),
                    "quantity": matched,
                    "buy_price": buy_price,
                    "sell_price": price,
                    "cost": cost,
                    "proceeds": proceeds,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "sell_reason": tx.get("reason") or "",
                })
                lot["remaining"] -= matched
                sell_qty -= matched
                if lot["remaining"] <= 0:
                    lots[key].popleft()
    return closed


def _open_positions(pm: PortfolioManager, accounts: list[str], realtime: bool) -> list[dict[str, Any]]:
    positions_by_account = {
        account: _load_account_positions(pm, account)
        for account in accounts
    }
    prices = _load_realtime_prices([pos for rows in positions_by_account.values() for pos in rows]) if realtime else {}
    rows: list[dict[str, Any]] = []
    for account in accounts:
        for pos in positions_by_account.get(account) or []:
            qty = _safe_int(pos.get("quantity"), 0)
            buy_price = _safe_float(pos.get("avg_price") or pos.get("buy_price"), 0.0)
            current_price = prices.get(str(pos.get("code") or ""), _safe_float(pos.get("current_price") or buy_price, buy_price))
            cost = buy_price * qty
            market_value = current_price * qty
            pnl = market_value - cost
            pnl_pct = pnl / cost * 100.0 if cost > 0 else 0.0
            rows.append({
                "account": account,
                "code": pos.get("code") or "",
                "name": pos.get("name") or "",
                "strategy": pos.get("entry_strategy") or "",
                "buy_time": pos.get("created_at") or pos.get("update_time"),
                "quantity": qty,
                "buy_price": buy_price,
                "current_price": current_price,
                "cost": cost,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })
    return rows


def _cash_balances(pm: PortfolioManager, accounts: list[str]) -> dict[str, float]:
    balances: dict[str, float] = {}
    for account in accounts:
        try:
            balances[account] = _safe_float(pm.load_cash(account), 0.0)
        except Exception as exc:
            _mark_data_warning(pm, f"{_account_text(account)} 现金余额读取失败: {str(exc)[:120]}")
            balances[account] = 0.0
    return balances


def _build_report(
    accounts: list[str],
    closed: list[dict[str, Any]],
    open_rows: list[dict[str, Any]],
    realtime: bool,
    data_warnings: Optional[list[str]] = None,
    cash_balances: Optional[dict[str, float]] = None,
) -> str:
    closed_pnl = sum(_safe_float(row.get("pnl")) for row in closed)
    open_pnl = sum(_safe_float(row.get("pnl")) for row in open_rows)
    closed_cost = sum(_safe_float(row.get("cost")) for row in closed)
    open_cost = sum(_safe_float(row.get("cost")) for row in open_rows)
    cash_balances = cash_balances or {}
    total_cash = sum(_safe_float(cash_balances.get(account)) for account in accounts)
    total_cost = closed_cost + open_cost
    total_pnl = closed_pnl + open_pnl
    total_pnl_pct = total_pnl / total_cost * 100.0 if total_cost > 0 else 0.0
    cash_lines = [
        f"| {_account_text(account)}现金余额 | {_fmt_money(cash_balances.get(account, 0.0))} |"
        for account in accounts
    ]

    lines = [
        "📊【模拟仓交易流水日报】",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 统计账户: {', '.join(_account_text(account) for account in accounts)}",
        f"- 持仓估值: {'实时行情' if realtime else '持仓价/买入价兜底'}",
        "",
        "【汇总】",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 已平仓笔数 | {len(closed)} |",
        f"| 已平仓盈亏 | {_fmt_money(closed_pnl)} |",
        f"| 当前持仓数 | {len(open_rows)} |",
        f"| 当前浮动盈亏 | {_fmt_money(open_pnl)} |",
        *cash_lines,
        f"| 模拟现金合计 | {_fmt_money(total_cash)} |",
        f"| 总盈亏 | {_fmt_money(total_pnl)} |",
        f"| 总收益率 | {_fmt_pct(total_pnl_pct)} |",
        "",
        "",
        "【已平仓交易】",
        "",
        "| 账户 | 买入时间 | 卖出时间 | 代码 | 名称 | 策略 | 股数 | 买入价 | 卖出价 | 成本 | 盈亏 | 盈亏比例 | 卖出原因 |",
        "|---|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    if data_warnings:
        insert_at = 6
        lines[insert_at:insert_at] = [
            "",
            "【数据源状态】",
            *[f"- 警告: {warning}" for warning in data_warnings],
        ]
    return "\n".join(lines)


def _append_closed(lines: list[str], closed: list[dict[str, Any]]) -> None:
    if closed:
        for row in sorted(closed, key=lambda x: str(x.get("sell_time") or "")):
            lines.append(
                f"| {_account_text(row['account'])} | {_date_text(row['buy_time'])} | {_date_text(row['sell_time'])} | "
                f"{row['code']} | {row['name']} | {row['strategy'] or '-'} | {row['quantity']} | "
                f"{_fmt_money(row['buy_price'])} | {_fmt_money(row['sell_price'])} | {_fmt_money(row['cost'])} | "
                f"{_fmt_money(row['pnl'])} | {_fmt_pct(row['pnl_pct'])} | {str(row.get('sell_reason') or '')[:80]} |"
            )
    else:
        lines.append("| - | - | - | - | 暂无已平仓交易 | - | - | - | - | - | - | - | - |")


def _append_open(lines: list[str], open_rows: list[dict[str, Any]]) -> None:
    lines.extend([
        "",
        "【当前持仓】",
        "",
        "| 账户 | 买入时间 | 代码 | 名称 | 策略 | 股数 | 买入价 | 当前价 | 成本 | 市值 | 浮动盈亏 | 浮动盈亏比例 |",
        "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    if open_rows:
        for row in sorted(open_rows, key=lambda x: (str(x.get("account") or ""), str(x.get("buy_time") or ""))):
            lines.append(
                f"| {_account_text(row['account'])} | {_date_text(row['buy_time'])} | {row['code']} | {row['name']} | "
                f"{row['strategy'] or '-'} | {row['quantity']} | {_fmt_money(row['buy_price'])} | "
                f"{_fmt_money(row['current_price'])} | {_fmt_money(row['cost'])} | {_fmt_money(row['market_value'])} | "
                f"{_fmt_money(row['pnl'])} | {_fmt_pct(row['pnl_pct'])} |"
            )
    else:
        lines.append("| - | - | - | 暂无当前持仓 | - | - | - | - | - | - | - | - |")


def _append_pending_checks(lines: list[str], pending_checks: list[dict[str, Any]]) -> None:
    lines.extend([
        "",
        "【动态入场复核事件】",
        "",
        "| 账户 | 策略 | 决策 | 次数 | 最近时间 | 原因 |",
        "|---|---|---|---:|---|---|",
    ])
    if pending_checks:
        for row in pending_checks[:40]:
            lines.append(
                f"| {_account_text(row.get('account'))} | {row.get('strategy') or '-'} | {row.get('decision') or '-'} | "
                f"{_safe_int(row.get('cnt'), 0)} | {_date_text(row.get('last_check_time'))} | {str(row.get('reason') or '')[:120]} |"
            )
    else:
        lines.append("| - | - | - | 0 | - | 暂无动态入场复核事件 |")


def _report_content(
    accounts: list[str],
    closed: list[dict[str, Any]],
    open_rows: list[dict[str, Any]],
    realtime: bool,
    data_warnings: Optional[list[str]] = None,
    cash_balances: Optional[dict[str, float]] = None,
    pending_checks: Optional[list[dict[str, Any]]] = None,
) -> str:
    head = _build_report(accounts, closed, open_rows, realtime, data_warnings=data_warnings, cash_balances=cash_balances)
    lines = head.splitlines()
    _append_closed(lines, closed)
    _append_open(lines, open_rows)
    _append_pending_checks(lines, pending_checks or [])
    lines.extend([
        "",
        "【复盘口径】",
        "  • 已平仓盈亏按 FIFO 买入批次匹配卖出流水计算。",
        "  • 当前持仓浮盈亏按持仓均价和当前价估算。",
        "  • 动态入场复核事件来自 pending_entry_check_events，用于解释影子/模拟仓买入、跳过、不可成交和过期原因。",
        "  • 这是一份模拟仓复盘报告，不构成投资建议。",
    ])
    return "\n".join(lines) + "\n"


def _log_detail(accounts: list[str], closed: list[dict[str, Any]], open_rows: list[dict[str, Any]], pending_checks: list[dict[str, Any]], content: str) -> None:
    closed_pnl = sum(_safe_float(row.get("pnl")) for row in closed)
    open_pnl = sum(_safe_float(row.get("pnl")) for row in open_rows)
    logger.info(
        "SIM_TRADE_SUMMARY accounts=%s closed=%s closed_pnl=%.2f open_positions=%s open_pnl=%.2f",
        ",".join(accounts),
        len(closed),
        closed_pnl,
        len(open_rows),
        open_pnl,
    )
    for row in sorted(closed, key=lambda x: str(x.get("sell_time") or "")):
        logger.info(
            "SIM_TRADE_CLOSED account=%s code=%s name=%s strategy=%s buy_time=%s sell_time=%s qty=%s buy=%.3f sell=%.3f cost=%.2f pnl=%.2f pnl_pct=%.2f reason=%s",
            row.get("account"),
            row.get("code"),
            row.get("name"),
            row.get("strategy") or "-",
            _date_text(row.get("buy_time")),
            _date_text(row.get("sell_time")),
            row.get("quantity"),
            _safe_float(row.get("buy_price")),
            _safe_float(row.get("sell_price")),
            _safe_float(row.get("cost")),
            _safe_float(row.get("pnl")),
            _safe_float(row.get("pnl_pct")),
            str(row.get("sell_reason") or "")[:120],
        )
    for row in sorted(open_rows, key=lambda x: (str(x.get("account") or ""), str(x.get("buy_time") or ""))):
        logger.info(
            "SIM_TRADE_OPEN account=%s code=%s name=%s strategy=%s buy_time=%s qty=%s buy=%.3f current=%.3f cost=%.2f market_value=%.2f pnl=%.2f pnl_pct=%.2f",
            row.get("account"),
            row.get("code"),
            row.get("name"),
            row.get("strategy") or "-",
            _date_text(row.get("buy_time")),
            row.get("quantity"),
            _safe_float(row.get("buy_price")),
            _safe_float(row.get("current_price")),
            _safe_float(row.get("cost")),
            _safe_float(row.get("market_value")),
            _safe_float(row.get("pnl")),
            _safe_float(row.get("pnl_pct")),
        )
    for row in pending_checks[:40]:
        logger.info(
            "SIM_PENDING_CHECK_SUMMARY account=%s strategy=%s decision=%s count=%s reason=%s last=%s",
            row.get("account"),
            row.get("strategy") or "-",
            row.get("decision") or "-",
            _safe_int(row.get("cnt"), 0),
            str(row.get("reason") or "")[:160],
            _date_text(row.get("last_check_time")),
        )
    log_report_snapshot("模拟仓交易流水日报", content, source="sim_trade_report")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report simulated trade ledger")
    parser.add_argument("--accounts", default="paper_main,paper_watchlist", help="Comma-separated accounts")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--realtime", action="store_true", help="Use realtime quotes for open positions")
    parser.add_argument("--email", action="store_true", help="Send the report by email")
    parser.add_argument("--no-log-content", action="store_true", help="Do not log full report content")
    args = parser.parse_args()

    setup_logger("StockAnalyzer.SimTradeReport")
    accounts = [item.strip() for item in str(args.accounts or "").split(",") if item.strip()]
    if not accounts:
        raise SystemExit("--accounts cannot be empty")

    pm = PortfolioManager()
    transactions = _query_transactions(pm, accounts, args.start_date, args.end_date)
    pending_checks = _query_pending_check_events(pm, accounts, args.start_date, args.end_date)
    closed = _closed_trades(transactions)
    open_rows = _open_positions(pm, accounts, realtime=bool(args.realtime))
    cash = _cash_balances(pm, accounts)
    data_warnings = getattr(pm, "_sim_report_warnings", [])
    content = _report_content(accounts, closed, open_rows, realtime=bool(args.realtime), data_warnings=data_warnings, cash_balances=cash, pending_checks=pending_checks)

    if not args.no_log_content:
        _log_detail(accounts, closed, open_rows, pending_checks, content)
    else:
        logger.info("SIM_TRADE_SUMMARY_LOG_ONLY accounts=%s closed=%s open_positions=%s", ",".join(accounts), len(closed), len(open_rows))

    if args.email:
        subject_date = datetime.now().strftime("%Y-%m-%d")
        Reporter().send_email(f"📊【模拟仓日报】{subject_date}", content)

    print(content)


if __name__ == "__main__":
    main()
