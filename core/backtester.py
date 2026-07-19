# -*- coding: utf-8 -*-
"""A-share constrained replay/backtest helpers.

The first version is deliberately conservative and daily-bar based. It is used
to answer: "If this selection had been bought, what did T+0/T+1/T+2 risk look
like under A-share constraints?"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _format_code(code: str) -> str:
    code = str(code or "").strip()
    if not code:
        return ""
    if "." in code:
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


@dataclass
class ReplayConfig:
    horizon_days: int = 3
    slippage_bps: float = 5.0
    fee_bps: float = 3.0
    tax_bps: float = 5.0
    limit_up_pct: float = 9.8
    limit_down_pct: float = -9.8
    stop_loss_pct: float = -3.0
    use_today_minute: bool = True

    @property
    def round_trip_cost_pct(self) -> float:
        return (self.slippage_bps * 2 + self.fee_bps * 2 + self.tax_bps) / 100.0


class AShareReplayBacktester:
    """Daily-bar replay with T+1 and limit-up/down constraints."""

    def __init__(self, portfolio_manager, data_provider, config: ReplayConfig | None = None):
        self.portfolio = portfolio_manager
        self.provider = data_provider
        self.config = config or ReplayConfig()
        self._bars_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._minute_cache: dict[tuple[str, str], Any] = {}
        self._realtime_cache: dict[str, dict[str, Any]] = {}
        self._daily_map_cache: dict[str, dict[str, Any]] | None = None

    def _daily_map(self) -> dict[str, dict[str, Any]]:
        if self._daily_map_cache is not None:
            return self._daily_map_cache
        daily_map = {}
        try:
            daily_map = {
                row.get("ts_code"): row
                for row in (self.provider.get_daily_data() or [])
                if isinstance(row, dict) and row.get("ts_code")
            }
        except Exception:
            daily_map = {}
        self._daily_map_cache = daily_map
        return daily_map

    def _repair_realtime_quote(self, ts_code: str, quote: dict[str, Any]) -> dict[str, Any]:
        fixed = dict(quote or {})
        price = _safe_float(fixed.get("price") or fixed.get("close"), 0.0)
        pre_close = _safe_float(fixed.get("pre_close"), 0.0)
        if pre_close <= 0:
            daily_row = self._daily_map().get(ts_code) or {}
            pre_close = _safe_float(daily_row.get("pre_close") or daily_row.get("close"), 0.0)
            if pre_close > 0:
                fixed["pre_close"] = pre_close
        if price > 0 and pre_close > 0:
            fixed["pct_chg"] = (price - pre_close) / pre_close * 100
        return fixed

    def _normalize_date(self, date_value: str) -> str:
        value = str(date_value or "").strip()
        if len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return value

    def _compact_date(self, date_value: str) -> str:
        value = self._normalize_date(date_value)
        return value.replace("-", "")

    def _fetch_selections(self, date_value: str, strategy: str | None = None, code: str | None = None) -> list[dict[str, Any]]:
        trade_date = self._normalize_date(date_value)
        conn = self.portfolio._get_connection()
        if not conn:
            return []
        try:
            with conn.cursor() as cursor:
                sql = "SELECT * FROM strategy_selection WHERE date=%s"
                params: list[Any] = [trade_date]
                if strategy and strategy.lower() not in {"all", "*"}:
                    sql += " AND strategy=%s"
                    params.append(strategy)
                if code:
                    sql += " AND code=%s"
                    params.append(str(code).split(".")[0])
                sql += " ORDER BY strategy, score_total DESC, id DESC"
                cursor.execute(sql, params)
                return list(cursor.fetchall() or [])
        finally:
            conn.close()

    def _fetch_bars(self, ts_code: str, trade_date: str) -> list[dict[str, Any]]:
        cache_key = (ts_code, self._compact_date(trade_date))
        if cache_key in self._bars_cache:
            return self._bars_cache[cache_key]

        start_dt = datetime.strptime(self._compact_date(trade_date), "%Y%m%d") - timedelta(days=5)
        end_dt = datetime.strptime(self._compact_date(trade_date), "%Y%m%d") + timedelta(days=max(10, self.config.horizon_days * 3))
        df = self.provider.get_stock_hist(ts_code, start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"))
        records = []
        if df is not None and not df.empty:
            records = df.sort_values("trade_date").to_dict("records")
        records = [r for r in records if str(r.get("trade_date") or "") >= self._compact_date(trade_date)]

        # Same-day replay before daily is published: use realtime quote as a
        # pseudo daily bar so T0 drawdown/close risk can still be inspected.
        if not records and self._compact_date(trade_date) == datetime.now().strftime("%Y%m%d"):
            try:
                quote = self._realtime_cache.get(ts_code)
                if quote is None:
                    quotes = self.provider.get_realtime_quotes([ts_code])
                    quote = quotes.get(ts_code, {}) if isinstance(quotes, dict) else {}
                    quote = self._repair_realtime_quote(ts_code, quote)
                    self._realtime_cache[ts_code] = quote
                price = _safe_float(quote.get("price") or quote.get("close"), 0.0)
                if price > 0:
                    records = [{
                        "ts_code": ts_code,
                        "trade_date": self._compact_date(trade_date),
                        "open": _safe_float(quote.get("open"), price),
                        "high": _safe_float(quote.get("high"), price),
                        "low": _safe_float(quote.get("low"), price),
                        "close": price,
                        "pre_close": _safe_float(quote.get("pre_close"), 0.0),
                        "pct_chg": _safe_float(quote.get("pct_chg"), 0.0),
                        "vol": _safe_float(quote.get("vol"), 0.0),
                        "amount": _safe_float(quote.get("amount"), 0.0),
                        "source_api": quote.get("source_api") or "realtime",
                    }]
            except Exception:
                pass
        records = records[: max(1, self.config.horizon_days + 1)]
        self._bars_cache[cache_key] = records
        return records

    def _signal_time(self, selection: dict[str, Any]) -> str:
        created_at = selection.get("created_at")
        if created_at:
            try:
                if isinstance(created_at, datetime):
                    return created_at.strftime("%Y-%m-%d %H:%M:%S")
                text = str(created_at)
                if len(text) >= 16:
                    return text[:19]
            except Exception:
                pass

        strategy = str(selection.get("strategy") or "")
        date_str = self._normalize_date(str(selection.get("date") or datetime.now().strftime("%Y%m%d")))
        defaults = {
            "集合竞价": "09:26:00",
            "早盘竞价首选": "09:26:00",
            "冷启动": "09:26:00",
            "午盘精选": "14:30:00",
            "技术突破": "10:00:00",
            "龙头跟踪": "10:00:00",
            "盘后资金流": "14:55:00",
        }
        return f"{date_str} {defaults.get(strategy, '09:30:00')}"

    def _fetch_today_minute_after_signal(self, ts_code: str, signal_time: str):
        if not self.config.use_today_minute:
            return None
        try:
            signal_date = str(signal_time or "")[:10].replace("-", "")
            if signal_date != datetime.now().strftime("%Y%m%d"):
                return None
            cache_key = (ts_code, signal_date)
            if cache_key in self._minute_cache:
                df = self._minute_cache[cache_key]
            else:
                df = self.provider.get_stock_min_data(ts_code, signal_date, signal_date, freq="1min")
                self._minute_cache[cache_key] = df
            if df is None or df.empty or "trade_time" not in df.columns:
                return None
            df = df.copy()
            df["trade_time"] = pd.to_datetime(df["trade_time"])
            cutoff = pd.to_datetime(signal_time)
            df = df[df["trade_time"] >= cutoff]
            if df.empty:
                return None
            for col in ("open", "close", "high", "low", "vol"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception:
            return None

    def _entry_price(self, selection: dict[str, Any], first_bar: dict[str, Any]) -> float:
        sel_price = _safe_float(selection.get("sel_price"), 0.0)
        if sel_price > 0:
            return sel_price * (1 + self.config.slippage_bps / 10000.0)
        open_price = _safe_float(first_bar.get("open"), 0.0)
        return open_price * (1 + self.config.slippage_bps / 10000.0) if open_price > 0 else 0.0

    def replay_selection(self, selection: dict[str, Any]) -> dict[str, Any]:
        code = str(selection.get("code") or "")
        ts_code = _format_code(code)
        trade_date = self._compact_date(str(selection.get("date") or ""))
        bars = self._fetch_bars(ts_code, trade_date)

        result = {
            "date": self._normalize_date(str(selection.get("date") or "")),
            "strategy": selection.get("strategy"),
            "code": code,
            "ts_code": ts_code,
            "name": selection.get("name") or "",
            "status": "NO_DATA",
            "reasons": [],
            "entry_price": 0.0,
            "t0_max_drawdown_pct": None,
            "t0_close_ret_pct": None,
            "t1_open_ret_pct": None,
            "t1_close_ret_pct": None,
            "t2_close_ret_pct": None,
            "max_drawdown_pct": None,
            "best_return_pct": None,
            "t1_blocked_stop_loss": False,
            "limit_up_unfillable": False,
            "limit_down_unsellable": False,
            "data_quality": "daily_bar",
            "signal_time": self._signal_time(selection),
        }

        if not bars:
            result["reasons"].append("no_daily_bars")
            return result

        first = bars[0]
        pct_chg = _safe_float(first.get("pct_chg"), 0.0)
        if pct_chg >= self.config.limit_up_pct:
            result["status"] = "BLOCKED"
            result["limit_up_unfillable"] = True
            result["reasons"].append(f"limit_up_unfillable pct_chg={pct_chg:.2f}%")
            return result

        entry = self._entry_price(selection, first)
        if entry <= 0:
            result["status"] = "NO_DATA"
            result["reasons"].append("invalid_entry_price")
            return result

        result["entry_price"] = entry
        lows = [_safe_float(b.get("low"), entry) for b in bars if _safe_float(b.get("low"), 0) > 0]
        highs = [_safe_float(b.get("high"), entry) for b in bars if _safe_float(b.get("high"), 0) > 0]
        closes = [_safe_float(b.get("close"), entry) for b in bars if _safe_float(b.get("close"), 0) > 0]

        minute_df = self._fetch_today_minute_after_signal(ts_code, result["signal_time"])
        first_low = _safe_float(first.get("low"), entry)
        first_close = _safe_float(first.get("close"), entry)
        first_high = _safe_float(first.get("high"), entry)
        if minute_df is not None and not minute_df.empty:
            first_low = _safe_float(minute_df["low"].min(), first_low)
            first_high = _safe_float(minute_df["high"].max(), first_high)
            first_close = _safe_float(minute_df["close"].iloc[-1], first_close)
            result["data_quality"] = "rt_min_after_signal"
        elif self._compact_date(trade_date) == datetime.now().strftime("%Y%m%d"):
            result["data_quality"] = "realtime_or_daily_full_day_approx"
            if str(selection.get("strategy") or "") in {"午盘精选", "技术突破", "龙头跟踪", "盘后资金流"}:
                result["reasons"].append("daily_low_may_precede_signal")

        result["t0_max_drawdown_pct"] = (first_low - entry) / entry * 100.0
        result["t0_close_ret_pct"] = (first_close - entry) / entry * 100.0 - self.config.round_trip_cost_pct
        result["t1_blocked_stop_loss"] = result["t0_max_drawdown_pct"] <= self.config.stop_loss_pct
        if result["t1_blocked_stop_loss"]:
            result["reasons"].append(f"T+1 blocked stop loss: t0_dd={result['t0_max_drawdown_pct']:.2f}%")

        if len(bars) > 1:
            t1 = bars[1]
            t1_open = _safe_float(t1.get("open"), entry)
            t1_close = _safe_float(t1.get("close"), entry)
            result["t1_open_ret_pct"] = (t1_open - entry) / entry * 100.0 - self.config.round_trip_cost_pct
            result["t1_close_ret_pct"] = (t1_close - entry) / entry * 100.0 - self.config.round_trip_cost_pct
            if _safe_float(t1.get("pct_chg"), 0.0) <= self.config.limit_down_pct:
                result["limit_down_unsellable"] = True
                result["reasons"].append(f"limit_down_unsellable pct_chg={_safe_float(t1.get('pct_chg'), 0.0):.2f}%")

        if len(bars) > 2:
            t2_close = _safe_float(bars[2].get("close"), entry)
            result["t2_close_ret_pct"] = (t2_close - entry) / entry * 100.0 - self.config.round_trip_cost_pct

        if minute_df is not None and not minute_df.empty:
            result["max_drawdown_pct"] = result["t0_max_drawdown_pct"]
        elif lows:
            result["max_drawdown_pct"] = (min(lows) - entry) / entry * 100.0
        if minute_df is not None and not minute_df.empty:
            result["best_return_pct"] = (first_high - entry) / entry * 100.0 - self.config.round_trip_cost_pct
        elif highs:
            result["best_return_pct"] = (max(highs) - entry) / entry * 100.0 - self.config.round_trip_cost_pct

        result["status"] = "OK"
        if not result["reasons"]:
            result["reasons"].append("replay_ok")
        return result

    def replay_day(self, date_value: str, strategy: str | None = None, code: str | None = None) -> dict[str, Any]:
        selections = self._fetch_selections(date_value, strategy=strategy, code=code)
        rows = [self.replay_selection(sel) for sel in selections]

        ok_rows = [r for r in rows if r.get("status") == "OK"]
        blocked_rows = [r for r in rows if r.get("status") == "BLOCKED"]
        no_data_rows = [r for r in rows if r.get("status") == "NO_DATA"]

        def avg(key: str) -> float | None:
            values = [_safe_float(r.get(key), None) for r in ok_rows if r.get(key) is not None]
            values = [v for v in values if v is not None]
            return sum(values) / len(values) if values else None

        summary = {
            "date": self._normalize_date(date_value),
            "strategy": strategy or "all",
            "code": code or "",
            "total": len(rows),
            "ok": len(ok_rows),
            "blocked": len(blocked_rows),
            "no_data": len(no_data_rows),
            "limit_up_unfillable": sum(1 for r in rows if r.get("limit_up_unfillable")),
            "limit_down_unsellable": sum(1 for r in rows if r.get("limit_down_unsellable")),
            "t1_blocked_stop_loss": sum(1 for r in rows if r.get("t1_blocked_stop_loss")),
            "minute_quality_rows": sum(1 for r in rows if r.get("data_quality") == "rt_min_after_signal"),
            "approx_quality_rows": sum(1 for r in rows if r.get("data_quality") == "realtime_or_daily_full_day_approx"),
            "avg_t0_close_ret_pct": avg("t0_close_ret_pct"),
            "avg_t1_close_ret_pct": avg("t1_close_ret_pct"),
            "avg_t2_close_ret_pct": avg("t2_close_ret_pct"),
            "worst_max_drawdown_pct": min([r["max_drawdown_pct"] for r in ok_rows if r.get("max_drawdown_pct") is not None], default=None),
            "best_return_pct": max([r["best_return_pct"] for r in ok_rows if r.get("best_return_pct") is not None], default=None),
        }
        by_strategy = {}
        for row in rows:
            strategy_name = str(row.get("strategy") or "未知")
            stat = by_strategy.setdefault(strategy_name, {
                "total": 0,
                "ok": 0,
                "blocked": 0,
                "no_data": 0,
                "t1_blocked_stop_loss": 0,
                "limit_up_unfillable": 0,
                "t0_close_values": [],
                "drawdown_values": [],
            })
            stat["total"] += 1
            status = row.get("status")
            if status == "OK":
                stat["ok"] += 1
                if row.get("t0_close_ret_pct") is not None:
                    stat["t0_close_values"].append(_safe_float(row.get("t0_close_ret_pct")))
                if row.get("max_drawdown_pct") is not None:
                    stat["drawdown_values"].append(_safe_float(row.get("max_drawdown_pct")))
            elif status == "BLOCKED":
                stat["blocked"] += 1
            elif status == "NO_DATA":
                stat["no_data"] += 1
            if row.get("t1_blocked_stop_loss"):
                stat["t1_blocked_stop_loss"] += 1
            if row.get("limit_up_unfillable"):
                stat["limit_up_unfillable"] += 1

        strategy_summary = []
        for strategy_name, stat in by_strategy.items():
            t0_values = stat.pop("t0_close_values")
            dd_values = stat.pop("drawdown_values")
            stat["strategy"] = strategy_name
            stat["avg_t0_close_ret_pct"] = sum(t0_values) / len(t0_values) if t0_values else None
            stat["worst_drawdown_pct"] = min(dd_values) if dd_values else None
            strategy_summary.append(stat)
        strategy_summary.sort(key=lambda x: (
            x.get("avg_t0_close_ret_pct") if x.get("avg_t0_close_ret_pct") is not None else 999,
            -int(x.get("t1_blocked_stop_loss") or 0),
        ))

        return {"summary": summary, "strategy_summary": strategy_summary, "rows": rows}

    def format_report(self, replay_result: dict[str, Any], limit: int = 40) -> str:
        summary = replay_result.get("summary") or {}
        rows = replay_result.get("rows") or []

        def pct(value: Any) -> str:
            if value is None:
                return "-"
            return f"{_safe_float(value):+.2f}%"

        lines = []
        lines.append(f"📼【A股真实约束日线回放】{summary.get('date')} strategy={summary.get('strategy')}")
        if summary.get("code"):
            lines.append(f"代码过滤: {summary.get('code')}")
        lines.append(
            f"- 候选: {summary.get('total', 0)} | 可模拟: {summary.get('ok', 0)} | "
            f"涨停买不到: {summary.get('limit_up_unfillable', 0)} | 无数据: {summary.get('no_data', 0)}"
        )
        lines.append(
            f"- 数据质量: rt_min信号后分钟 {summary.get('minute_quality_rows', 0)} | "
            f"日线/实时整日近似 {summary.get('approx_quality_rows', 0)}"
        )
        lines.append(
            f"- T+1阻止止损: {summary.get('t1_blocked_stop_loss', 0)} | "
            f"跌停不可卖: {summary.get('limit_down_unsellable', 0)}"
        )
        lines.append(
            f"- 平均T0收盘: {pct(summary.get('avg_t0_close_ret_pct'))} | "
            f"平均T+1收盘: {pct(summary.get('avg_t1_close_ret_pct'))} | "
            f"平均T+2收盘: {pct(summary.get('avg_t2_close_ret_pct'))}"
        )
        lines.append(
            f"- 最差回撤: {pct(summary.get('worst_max_drawdown_pct'))} | "
            f"最好浮盈: {pct(summary.get('best_return_pct'))}"
        )

        strategy_summary = replay_result.get("strategy_summary") or []
        if strategy_summary:
            lines.append("\n按策略汇总:")
            lines.append("\n| 策略 | 候选 | 可模拟 | 涨停买不到 | T+1止损阻断 | 平均T0收盘 | 最差回撤 |")
            lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: |")
            for stat in strategy_summary:
                lines.append(
                    f"| {stat.get('strategy') or '-'} | {stat.get('total', 0)} | {stat.get('ok', 0)} | "
                    f"{stat.get('limit_up_unfillable', 0)} | {stat.get('t1_blocked_stop_loss', 0)} | "
                    f"{pct(stat.get('avg_t0_close_ret_pct'))} | {pct(stat.get('worst_drawdown_pct'))} |"
                )

        if not rows:
            lines.append("\n暂无候选。")
            return "\n".join(lines)

        lines.append("\n| 策略 | 代码 | 名称 | 状态 | 数据 | 入场价 | T0收盘 | T+1收盘 | T+2收盘 | 最大回撤 | 约束/原因 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |")
        for row in rows[:limit]:
            reason = "; ".join(row.get("reasons") or [])[:80]
            lines.append(
                f"| {row.get('strategy') or '-'} | {row.get('code') or '-'} | {row.get('name') or '-'} | "
                f"{row.get('status') or '-'} | {row.get('data_quality') or '-'} | {_safe_float(row.get('entry_price')):.2f} | "
                f"{pct(row.get('t0_close_ret_pct'))} | {pct(row.get('t1_close_ret_pct'))} | "
                f"{pct(row.get('t2_close_ret_pct'))} | {pct(row.get('max_drawdown_pct'))} | {reason} |"
            )
        return "\n".join(lines)
