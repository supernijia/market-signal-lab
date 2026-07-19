# -*- coding: utf-8 -*-
"""
Focus monitor for selection candidates.

This module separates "what deserves attention today" from the stricter
auto-buy gates. It never updates strategy_selection or account state. When
configured, it can write audit-only SHADOW pending rows that are not executable.
"""
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

from core.config import Config

logger = logging.getLogger("StockAnalyzer.FocusMonitor")


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


class FocusMonitor:
    """Build all-day monitoring snapshots for yesterday and active selections."""

    def __init__(self, portfolio, provider, analyzer=None):
        self.portfolio = portfolio
        self.provider = provider
        self.analyzer = analyzer
        self._minute_budget_used = 0

    def _code_to_ts(self, code):
        code = str(code or "").strip()
        if not code:
            return ""
        if "." in code:
            return code
        if code.startswith("6"):
            return f"{code}.SH"
        if code.startswith(("4", "8", "9")):
            return f"{code}.BJ"
        return f"{code}.SZ"

    def _date_fmt(self, trade_date):
        trade_date = str(trade_date or "").replace("-", "")
        if len(trade_date) == 8:
            return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        return trade_date

    def _trade_offsets(self, trade_date=None):
        today = str(trade_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
        start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
        cal = self.provider.get_trade_cal(start_date=start, end_date=today)
        open_days = [d["cal_date"] for d in cal or [] if d.get("is_open") == 1]
        open_days.sort()
        if not open_days:
            return {"today": today, "t1": "", "t2": ""}
        if today in open_days:
            idx = open_days.index(today)
        else:
            idx = len(open_days)
        t1 = open_days[idx - 1] if idx >= 1 else open_days[0]
        t2 = open_days[idx - 2] if idx >= 2 else t1
        return {"today": today, "t1": t1, "t2": t2}

    def _parse_tags(self, item):
        tags = []
        raw = item.get("tags_json")
        if not raw:
            return tags
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, list):
                tags = [str(x) for x in data[:6]]
            elif isinstance(data, dict):
                tags = [str(k) for k, v in data.items() if v][:6]
        except Exception:
            return tags
        return tags

    def _selection_price(self, item):
        for key in ("sel_price", "price", "close", "trade"):
            price = _safe_float(item.get(key), 0.0)
            if price > 0:
                return price
        return 0.0

    def _load_targets(self, trade_date=None):
        offsets = self._trade_offsets(trade_date)
        t1_fmt = self._date_fmt(offsets.get("t1"))
        t2_fmt = self._date_fmt(offsets.get("t2"))

        raw_targets = []

        for s in self.portfolio.get_selections_by_cycle(t1_fmt, cycle="T+1") or []:
            s = dict(s)
            s["_focus_bucket"] = "昨日入库"
            s["_focus_cycle"] = "T+1"
            raw_targets.append(s)

        for s in self.portfolio.get_selections_by_cycle(t2_fmt, cycle="T+2") or []:
            s = dict(s)
            s["_focus_bucket"] = "盘后复盘"
            s["_focus_cycle"] = "T+2"
            raw_targets.append(s)

        for s in self.portfolio.get_watchlist(days=5) or []:
            s = dict(s)
            s["_focus_bucket"] = "重点观测池"
            s["_focus_cycle"] = s.get("analysis_cycle") or "-"
            raw_targets.append(s)

        targets = {}
        for item in raw_targets:
            code = str(item.get("code") or "").split(".")[0]
            strategy = str(item.get("strategy") or "-")
            date = str(item.get("date") or "")
            key = (date, strategy, code)
            if key not in targets:
                item["code"] = code
                item["ts_code"] = item.get("ts_code") or self._code_to_ts(code)
                targets[key] = item
                continue
            old = targets[key]
            buckets = set(str(old.get("_focus_bucket", "")).split("+"))
            buckets.add(str(item.get("_focus_bucket", "")))
            old["_focus_bucket"] = "+".join([b for b in buckets if b])

        return list(targets.values()), offsets

    def _calc_ma(self, history, price):
        if not history:
            return 0.0, 0.0
        try:
            df = pd.DataFrame(history)
            df["close"] = df["close"].astype(float)
            df = pd.concat([df, pd.DataFrame([{"close": price}])], ignore_index=True)
            ma5 = float(df["close"].rolling(window=5).mean().iloc[-1]) if len(df) >= 5 else 0.0
            ma20 = float(df["close"].rolling(window=20).mean().iloc[-1]) if len(df) >= 20 else 0.0
            return ma5, ma20
        except Exception:
            return 0.0, 0.0

    def _volume_ratio(self, ts_code, quote, history):
        if not self.analyzer:
            return 0.0
        try:
            return _safe_float(self.analyzer.calculate_volume_ratio(ts_code, _safe_float(quote.get("vol"), 0.0), history=history[-5:]), 0.0)
        except Exception:
            return 0.0

    def _shadow_cfg(self):
        try:
            cfg = Config.STRATEGY.get("shadow_pending", {}) if isinstance(getattr(Config, "STRATEGY", {}), dict) else {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _shadow_enabled(self):
        return bool(self._shadow_cfg().get("enabled", False))

    def _shadow_strategies(self):
        strategies = self._shadow_cfg().get("strategies")
        if not strategies:
            strategies = ["龙头跟踪", "冷启动", "技术突破"]
        return {str(x) for x in strategies}

    def _shadow_max_rows(self):
        try:
            return int(self._shadow_cfg().get("max_rows_per_run", 20) or 20)
        except Exception:
            return 20

    def _permission_action(self, regime, strategy):
        try:
            matrix = Config.STRATEGY.get("strategy_permission_matrix", {}) if isinstance(Config.STRATEGY, dict) else {}
            rules = matrix.get(regime) or {}
            if not isinstance(rules, dict):
                return ""
            return str(rules.get(strategy) or rules.get("*") or "")
        except Exception:
            return ""

    def _real_dynamic_windows(self, strategy):
        try:
            entry_policy = Config.STRATEGY.get("entry_policy", {}) if isinstance(Config.STRATEGY, dict) else {}
            models = entry_policy.get("models") or {}
            dyn = models.get("dynamic_window") or {}
            windows = dyn.get("strategy_windows") or {}
            return [str(x) for x in (windows.get(strategy) or [])]
        except Exception:
            return []

    def _shadow_windows(self, strategy):
        try:
            windows = self._shadow_cfg().get("windows") or {}
            return [str(x) for x in (windows.get(strategy) or [])]
        except Exception:
            return []

    def _shadow_regime(self, snapshot, market_env=None):
        market_env = market_env or {}
        regime = market_env.get("regime")
        if regime:
            return str(regime)
        try:
            today = str((snapshot.get("offsets") or {}).get("today") or "").replace("-", "")
            env = self.analyzer.check_market_environment(today) if self.analyzer else {}
            regime = (env or {}).get("regime")
            if regime:
                return str(regime)
        except Exception:
            pass
        return "weak_market"

    def _shadow_failure_reason(self, row, regime):
        strategy = str(row.get("strategy") or "")
        permission = self._permission_action(regime, strategy)
        real_windows = self._real_dynamic_windows(strategy)
        shadow_windows = self._shadow_windows(strategy)

        if permission in ("OBSERVE", "OBSERVE_ONLY"):
            primary = "PERMISSION_OBSERVE_ONLY"
            secondary = ["SOURCE_NOT_ROUTED"]
            audit_action = "keep_observe_no_real_pending"
        elif permission == "BLOCK":
            primary = "PERMISSION_BLOCK"
            secondary = ["SOURCE_NOT_ROUTED"]
            audit_action = "keep_block_no_real_pending"
        elif not real_windows:
            primary = "SCHEDULE_WINDOW_MISSING"
            secondary = ["SOURCE_NOT_ROUTED"]
            audit_action = "add_audit_reason_or_shadow_dry_run_window"
        else:
            primary = "SOURCE_NOT_ROUTED"
            secondary = [f"PERMISSION_{permission}"] if permission else []
            audit_action = "route_to_failure_reason_audit_not_buy"

        labels = row.get("minute_labels") or []
        if isinstance(labels, str):
            labels = [labels]
        label_set = {str(x) for x in labels if x}
        text = " ".join([
            str(row.get("level") or ""),
            str(row.get("action") or ""),
            str(row.get("reason") or ""),
            ",".join(sorted(label_set)),
        ])
        if any(x in text for x in ("不追", "冲高", "回落", "风险", "今日+")):
            secondary.append("PRICE_BAND_OR_FALSE_STRENGTH_REVIEW")
        if label_set & {"60m强势延续", "60m持续强势观察", "收盘持有闸门"}:
            secondary.append("STRONG_HOLD_OR_WATCH_LABEL")
        if row.get("leader_false_strength_risk_60m") or row.get("leader_hard_false_strength_risk_60m"):
            secondary.append("LEADER_FALSE_STRENGTH_RISK")

        deduped = []
        for item in secondary:
            if item and item != primary and item not in deduped:
                deduped.append(item)

        return {
            "failure_reason_primary": primary,
            "failure_reason_secondary": ",".join(deduped),
            "next_audit_action": audit_action,
            "permission_action": permission,
            "regime_assumption": regime,
            "real_dynamic_windows": ",".join(real_windows),
            "shadow_windows": ",".join(shadow_windows),
        }

    def _current_bucket(self, now=None):
        now = now or datetime.now()
        hour = now.hour
        minute = now.minute
        if hour == 9 and minute >= 30 or hour == 10 and minute < 1:
            return "B1"
        if hour == 10 or (hour == 11 and minute <= 30):
            return "B2"
        if hour == 13:
            return "B3"
        if hour == 14 and minute <= 40:
            return "B4"
        if (hour == 14 and minute > 40) or (hour == 15 and minute <= 1):
            return "B5"
        return ""

    def _bucket_end_dt(self, trade_date, bucket):
        try:
            trade_date = self._date_fmt(trade_date) or datetime.now().strftime("%Y-%m-%d")
            y, m, d = int(trade_date[:4]), int(trade_date[5:7]), int(trade_date[8:10])
        except Exception:
            now = datetime.now()
            y, m, d = now.year, now.month, now.day
        ends = {"B1": (10, 0), "B2": (11, 30), "B3": (14, 0), "B4": (14, 40), "B5": (15, 1)}
        hhmm = ends.get(bucket)
        if not hhmm:
            return datetime.now()
        return datetime(y, m, d, hhmm[0], hhmm[1], 0)

    def _paper_change_ceiling(self, row):
        cfg = Config.STRATEGY.get("paper_strong_entry_experiment", {}) if isinstance(getattr(Config, "STRATEGY", {}), dict) else {}
        limits = cfg.get("board_change_ceiling_pct", {}) if isinstance(cfg.get("board_change_ceiling_pct"), dict) else {}
        code = str((row or {}).get("code") or "").strip()
        name = str((row or {}).get("name") or "")
        if "ST" in name.upper():
            return float(limits.get("st", 5.2) or 5.2)
        if code.startswith(("300", "301", "688", "689", "4", "8", "9")):
            return float(limits.get("growth", 20.2) or 20.2)
        return float(limits.get("main", 10.2) or 10.2)

    def _minute_budget(self):
        try:
            return int(getattr(Config, "FOCUS_MONITOR_MINUTE_MAX_PER_RUN", 20) or 0)
        except Exception:
            return 0

    def _normalize_minute_df(self, minute_df):
        if minute_df is None or getattr(minute_df, "empty", True):
            return pd.DataFrame()
        try:
            df = minute_df.copy()
            if "trade_time" not in df.columns and "time" in df.columns:
                df = df.rename(columns={"time": "trade_time"})
            if "trade_time" not in df.columns:
                return pd.DataFrame()
            df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
            df = df.dropna(subset=["trade_time"]).sort_values("trade_time").reset_index(drop=True)
            for col in ("open", "high", "low", "close", "vol"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
            if "close" not in df.columns or "vol" not in df.columns:
                return pd.DataFrame()
            df["pv"] = df["close"].fillna(0.0) * df["vol"].fillna(0.0)
            df["cum_pv"] = df["pv"].cumsum()
            df["cum_vol"] = df["vol"].fillna(0.0).cumsum()
            denom = df["cum_vol"].where(df["cum_vol"] > 0)
            df["vwap"] = (df["cum_pv"] / denom).ffill().fillna(df["close"])
            return df
        except Exception:
            return pd.DataFrame()

    def _fetch_minute_structure(self, ts_code, trade_date, quote=None):
        budget = self._minute_budget()
        if budget <= 0 or self._minute_budget_used >= budget:
            return {}
        trade_date = str(trade_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
        if len(trade_date) != 8 or not ts_code:
            return {}
        try:
            self._minute_budget_used += 1
            raw = self.provider.get_stock_min_data(ts_code, trade_date, trade_date, freq="1min")
            df = self._normalize_minute_df(raw)
            if df.empty:
                return {}
            return self._calc_leader_like_minute_labels(df, quote or {})
        except Exception as e:
            logger.debug(f"focus minute structure failed for {ts_code}: {e}")
            return {}

    def _calc_leader_like_minute_labels(self, minute_df, quote):
        pre_close = _safe_float((quote or {}).get("pre_close"), 0.0)
        if pre_close <= 0:
            first_open = _safe_float(minute_df["open"].iloc[0] if "open" in minute_df.columns else 0.0, 0.0)
            pre_close = first_open
        if pre_close <= 0:
            return {}

        df = minute_df.copy()
        for col in ("high", "low", "close", "vwap"):
            if col not in df.columns:
                df[col] = df["close"]
        df["ret_pct"] = (df["close"] / pre_close - 1.0) * 100.0
        df["high_ret_pct"] = (df["high"] / pre_close - 1.0) * 100.0
        df["low_ret_pct"] = (df["low"] / pre_close - 1.0) * 100.0
        df["above_vwap"] = df["close"] >= df["vwap"]

        first30 = df.iloc[:30]
        first60 = df.iloc[:60]
        if first60.empty:
            return {}

        max_ret_30m = float(first30["high_ret_pct"].max()) if not first30.empty else float(first60["high_ret_pct"].max())
        close_ret_60m = float(first60["ret_pct"].iloc[-1])
        min_ret_60m = float(first60["low_ret_pct"].min())
        above_vwap_60m_ratio = float(first60["above_vwap"].mean()) if len(first60) else 0.0
        retrace_60m = max(0.0, max_ret_30m - close_ret_60m)

        day_high_ret = float(df["high_ret_pct"].max())
        day_low_ret = float(df["low_ret_pct"].min())
        current_ret = float(df["ret_pct"].iloc[-1])
        day_range = day_high_ret - day_low_ret
        close_position = ((current_ret - day_low_ret) / day_range) if day_range > 0 else 1.0
        last30 = df.iloc[-30:]
        last30_above_vwap_ratio = float(last30["above_vwap"].mean()) if len(last30) else 0.0

        false_risk = bool(max_ret_30m >= 3.0 and close_ret_60m < 0.0 and above_vwap_60m_ratio <= 0.30)
        hard_false_risk = bool(max_ret_30m >= 3.0 and close_ret_60m <= -3.0 and above_vwap_60m_ratio <= 0.30 and retrace_60m >= 2.0)
        strict_hard_false_risk = bool(hard_false_risk and above_vwap_60m_ratio <= 0.10)
        sustained = bool(close_ret_60m >= 2.0 and above_vwap_60m_ratio >= 0.45 and min_ret_60m >= -3.0)
        strong_sustained = bool(close_ret_60m >= 5.0 and above_vwap_60m_ratio >= 0.80 and min_ret_60m >= -1.0)
        close_hold_gate = bool(current_ret >= 2.0 and close_position >= 0.60 and last30_above_vwap_ratio >= 0.60 and len(df) >= 120)

        labels = []
        notes = []
        if strict_hard_false_risk:
            labels.append("60m极强冲高回落风险")
        elif hard_false_risk:
            labels.append("60m强冲高回落风险")
        elif false_risk:
            labels.append("60m冲高回落风险")
        if strong_sustained:
            labels.append("60m强势延续")
        elif sustained:
            labels.append("60m持续强势观察")
        if close_hold_gate:
            labels.append("收盘持有闸门")

        if false_risk:
            notes.append(f"30m最高+{max_ret_30m:.1f}%后60m收{close_ret_60m:+.1f}%")
        if sustained:
            notes.append(f"60m收{close_ret_60m:+.1f}%且VWAP承接{above_vwap_60m_ratio:.0%}")
        if close_hold_gate:
            notes.append(f"收盘位置{close_position:.0%}，尾盘VWAP{last30_above_vwap_ratio:.0%}")

        return {
            "max_ret_30m_pct": round(max_ret_30m, 2),
            "close_ret_60m_pct": round(close_ret_60m, 2),
            "min_ret_60m_pct": round(min_ret_60m, 2),
            "above_vwap_60m_ratio": round(above_vwap_60m_ratio, 4),
            "retrace_60m_pct": round(retrace_60m, 2),
            "close_position": round(close_position, 4),
            "last30_above_vwap_ratio": round(last30_above_vwap_ratio, 4),
            "leader_false_strength_risk_60m": false_risk,
            "leader_hard_false_strength_risk_60m": hard_false_risk,
            "leader_strict_hard_false_strength_risk_60m": strict_hard_false_risk,
            "leader_sustained_strength_watch_60m": sustained,
            "leader_strong_sustained_strength_60m": strong_sustained,
            "leader_close_hold_gate": close_hold_gate,
            "minute_labels": labels,
            "minute_note": "；".join(notes[:2]),
        }

    def _focus_level(self, score, price, ma20, drawdown_pct):
        if ma20 > 0 and price < ma20 * 0.995:
            return "风险"
        if drawdown_pct <= -5:
            return "降级"
        if score >= 45:
            return "强盯"
        if score >= 25:
            return "重点"
        return "观察"

    def _focus_action(self, level, intraday_pct, total_pct, high_from_sel_pct, price, ma5, ma20, minute_signal=None):
        minute_signal = minute_signal or {}
        if minute_signal.get("leader_hard_false_strength_risk_60m"):
            return "强冲高回落，隔夜降级"
        if minute_signal.get("leader_false_strength_risk_60m"):
            return "冲高回落风险，不追"
        if minute_signal.get("leader_strong_sustained_strength_60m"):
            return "强势延续，盯收盘质量"
        if minute_signal.get("leader_sustained_strength_watch_60m"):
            return "强势延续观察"
        if minute_signal.get("leader_close_hold_gate"):
            return "收盘质量好，隔夜增强"
        if level in ("风险", "降级"):
            return "先看风险，不追"
        if high_from_sel_pct >= 8:
            return "冲高很强，盯回落承接"
        if intraday_pct >= 2 and ma5 > 0 and price >= ma5:
            return "盘中强势，盯分时承接"
        if total_pct >= 2:
            return "相对入库转强，继续盯"
        if ma20 > 0 and price >= ma20:
            return "未破趋势，等确认"
        return "只观察，不主动追"

    def _evaluate_target(self, item, quote, history, money_flow, minute_signal=None):
        minute_signal = minute_signal or {}
        ts_code = item.get("ts_code") or self._code_to_ts(item.get("code"))
        code = str(item.get("code") or ts_code.split(".")[0])
        price = _safe_float(quote.get("price"), self._selection_price(item))
        pre_close = _safe_float(quote.get("pre_close"), price)
        high = _safe_float(quote.get("high"), price)
        sel_price = self._selection_price(item)
        if sel_price <= 0:
            sel_price = price

        intraday_pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
        total_pct = ((price - sel_price) / sel_price * 100) if sel_price > 0 else 0.0
        high_from_sel_pct = ((high - sel_price) / sel_price * 100) if sel_price > 0 else total_pct
        drawdown_pct = ((price - high) / high * 100) if high > 0 else 0.0
        ma5, ma20 = self._calc_ma(history, price)
        vol_ratio = self._volume_ratio(ts_code, quote, history)

        net_inflow = _safe_float((money_flow or {}).get("net_inflow"), 0.0)
        vol_shares = _safe_float(quote.get("vol_shares"), _safe_float(quote.get("vol"), 0.0) * 100)
        amount_yuan = _safe_float(quote.get("amount_yuan"), price * vol_shares)
        mf_intensity = (net_inflow / amount_yuan * 100) if amount_yuan > 0 else 0.0

        score = 0.0
        score += max(high_from_sel_pct, -10) * 1.8
        score += max(total_pct, -10) * 1.2
        score += max(intraday_pct, -8) * 2.0
        score += min(vol_ratio, 5.0) * 5.0
        score += min(max(mf_intensity, -10), 20) * 1.2
        if ma5 > 0 and price >= ma5:
            score += 8
        if ma20 > 0 and price < ma20 * 0.995:
            score -= 25
        if str(item.get("strategy") or "") in ("集合竞价", "冷启动", "龙头跟踪", "技术突破"):
            score += 4
        if item.get("zt_result") in ("涨停", "吃肉"):
            score += 6
        if item.get("zt_result") in ("已剔除", "吃面"):
            score -= 8
        if minute_signal.get("leader_hard_false_strength_risk_60m"):
            score -= 28
        elif minute_signal.get("leader_false_strength_risk_60m"):
            score -= 16
        if minute_signal.get("leader_strong_sustained_strength_60m"):
            score += 14
        elif minute_signal.get("leader_sustained_strength_watch_60m"):
            score += 8
        if minute_signal.get("leader_close_hold_gate"):
            score += 8

        level = self._focus_level(score, price, ma20, drawdown_pct)
        if minute_signal.get("leader_hard_false_strength_risk_60m"):
            level = "风险"
        elif minute_signal.get("leader_false_strength_risk_60m") and level not in ("风险", "降级"):
            level = "降级"
        action = self._focus_action(level, intraday_pct, total_pct, high_from_sel_pct, price, ma5, ma20, minute_signal)

        reasons = []
        if high_from_sel_pct >= 2:
            reasons.append(f"入库后最高+{high_from_sel_pct:.1f}%")
        if intraday_pct >= 1:
            reasons.append(f"今日+{intraday_pct:.1f}%")
        if vol_ratio >= 1.5:
            reasons.append(f"量比{vol_ratio:.1f}")
        if mf_intensity > 0:
            reasons.append(f"资金强度{mf_intensity:.1f}%")
        if ma5 > 0:
            reasons.append("站上MA5" if price >= ma5 else "未上MA5")
        if ma20 > 0 and price < ma20 * 0.995:
            reasons.append("跌破MA20")
        if minute_signal.get("minute_note"):
            reasons.append(minute_signal.get("minute_note"))
        if not reasons:
            reasons.append("暂无强触发，保留观察")

        row = {
            "bucket": item.get("_focus_bucket") or "重点观测",
            "cycle": item.get("_focus_cycle") or item.get("analysis_cycle") or "-",
            "date": str(item.get("date") or "")[:10],
            "strategy": item.get("strategy") or "-",
            "code": code,
            "ts_code": ts_code,
            "name": item.get("name") or quote.get("name") or "-",
            "sel_price": sel_price,
            "price": price,
            "high": high,
            "intraday_pct": intraday_pct,
            "total_pct": total_pct,
            "high_from_sel_pct": high_from_sel_pct,
            "drawdown_pct": drawdown_pct,
            "ma5": ma5,
            "ma20": ma20,
            "vol_ratio": vol_ratio,
            "mf_intensity": mf_intensity,
            "status": item.get("zt_result") or "待验证",
            "score": round(score, 1),
            "level": level,
            "action": action,
            "reason": "；".join(reasons[:4]),
            "tags": self._parse_tags(item),
        }
        row.update(minute_signal)
        return row

    def build_snapshot(self, trade_date=None, limit=12):
        self._minute_budget_used = 0
        targets, offsets = self._load_targets(trade_date)
        ts_codes = sorted({t.get("ts_code") or self._code_to_ts(t.get("code")) for t in targets if t.get("code")})
        ts_codes = [c for c in ts_codes if c]

        if not ts_codes:
            return {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "offsets": offsets,
                "targets": [],
                "top_focus": [],
                "yesterday": [],
                "active_watch": [],
                "summary": {"total": 0, "strong": 0, "risk": 0},
            }

        quotes = self.provider.get_realtime_quotes(ts_codes)
        history_batch = self.provider.get_batch_history_data(ts_codes, count=30) if ts_codes else {}
        try:
            money_flow = self.provider.get_individual_money_flow(ts_codes, days=Config.INDIVIDUAL_FLOW_DAYS)
        except Exception:
            money_flow = {}

        rows = []
        for item in targets:
            ts_code = item.get("ts_code") or self._code_to_ts(item.get("code"))
            quote = quotes.get(ts_code) or {}
            history = history_batch.get(ts_code) or []
            if not quote and self._selection_price(item) <= 0:
                continue
            minute_signal = self._fetch_minute_structure(ts_code, offsets.get("today"), quote)
            rows.append(self._evaluate_target(item, quote, history, money_flow.get(ts_code) or {}, minute_signal))

        rows.sort(key=lambda x: (0 if x.get("level") in ("风险", "降级") else 1, x.get("score", 0)), reverse=True)
        top_focus = [r for r in rows if r.get("level") not in ("风险", "降级")][:limit]
        risk_rows = [r for r in rows if r.get("level") in ("风险", "降级")]
        yday_rows = [r for r in rows if "昨日入库" in str(r.get("bucket") or "") or "盘后复盘" in str(r.get("bucket") or "")]
        active_rows = [r for r in rows if "重点观测池" in str(r.get("bucket") or "")]

        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "offsets": offsets,
            "targets": rows,
            "top_focus": top_focus,
            "risk": risk_rows[:limit],
            "yesterday": yday_rows[:limit],
            "active_watch": active_rows[:limit],
            "summary": {
                "total": len(rows),
                "strong": len([r for r in rows if r.get("level") in ("强盯", "重点")]),
                "risk": len(risk_rows),
                "t1_date": self._date_fmt(offsets.get("t1")),
                "t2_date": self._date_fmt(offsets.get("t2")),
            },
        }

    def record_shadow_pending(self, snapshot, weather=None, market_env=None):
        """Write audit-only shadow pending rows from a focus snapshot.

        These rows use status=SHADOW and source_strategy=*_SHADOW, so they are
        never loaded by executable pending-entry code.
        """
        if not self._shadow_enabled():
            return {"enabled": False, "written": 0, "strategies": []}
        snapshot = snapshot or {}
        today = self._date_fmt((snapshot.get("offsets") or {}).get("today")) or datetime.now().strftime("%Y-%m-%d")
        rows = list(snapshot.get("targets") or [])
        strategies = self._shadow_strategies()
        max_rows = self._shadow_max_rows()
        now = datetime.now()
        bucket = self._current_bucket(now)
        regime = self._shadow_regime(snapshot, market_env=market_env)

        candidates = [
            r for r in rows
            if str(r.get("strategy") or "") in strategies
            and str(r.get("cycle") or "").upper() != "T+2"
        ]
        candidates.sort(key=lambda r: (
            0 if r.get("level") in ("风险", "降级") else 1,
            _safe_float(r.get("score"), 0.0),
            _safe_float(r.get("high_from_sel_pct"), 0.0),
        ), reverse=True)

        written = 0
        touched = []
        for row in candidates[:max_rows]:
            strategy = str(row.get("strategy") or "")
            failure = self._shadow_failure_reason(row, regime)
            row.update(failure)
            labels = row.get("minute_labels") or []
            if isinstance(labels, str):
                labels = [labels]
            reason_parts = [
                f"audit_only_shadow",
                f"failure={failure.get('failure_reason_primary') or '-'}",
                f"level={row.get('level') or '-'}",
                f"action={row.get('action') or '-'}",
            ]
            if labels:
                reason_parts.append("labels=" + ",".join([str(x) for x in labels[:3]]))
            if row.get("minute_note"):
                reason_parts.append(str(row.get("minute_note")))
            elif row.get("reason"):
                reason_parts.append(str(row.get("reason")))
            reason = " | ".join(reason_parts)

            payload = {
                "shadow_mode": "audit_only",
                "not_for_trading": True,
                "training_plan": "shadow_pending_coverage_v1_2026-06-03",
                "source": "focus_monitor",
                "source_bucket": row.get("bucket"),
                "source_cycle": row.get("cycle"),
                "source_date": row.get("date"),
                "strategy": strategy,
                "failure_reason_primary": failure.get("failure_reason_primary"),
                "failure_reason_secondary": failure.get("failure_reason_secondary"),
                "next_audit_action": failure.get("next_audit_action"),
                "permission_action": failure.get("permission_action"),
                "regime_assumption": failure.get("regime_assumption"),
                "real_dynamic_windows": failure.get("real_dynamic_windows"),
                "shadow_windows": failure.get("shadow_windows"),
                "level": row.get("level"),
                "action": row.get("action"),
                "reason": row.get("reason"),
                "minute_labels": labels,
                "minute_note": row.get("minute_note"),
                "sel_price": row.get("sel_price"),
                "price": row.get("price"),
                "intraday_pct": row.get("intraday_pct"),
                "total_pct": row.get("total_pct"),
                "high_from_sel_pct": row.get("high_from_sel_pct"),
                "score": row.get("score"),
                "leader_false_strength_risk_60m": row.get("leader_false_strength_risk_60m"),
                "leader_hard_false_strength_risk_60m": row.get("leader_hard_false_strength_risk_60m"),
                "leader_sustained_strength_watch_60m": row.get("leader_sustained_strength_watch_60m"),
                "leader_strong_sustained_strength_60m": row.get("leader_strong_sustained_strength_60m"),
                "leader_close_hold_gate": row.get("leader_close_hold_gate"),
            }
            try:
                self.portfolio.upsert_shadow_pending_signal(
                    trade_date=today,
                    code=row.get("code"),
                    name=row.get("name"),
                    ts_code=row.get("ts_code"),
                    source_strategy=strategy,
                    signal_time=now,
                    expires_at=now,
                    weather=weather,
                    signal_bucket=bucket,
                    payload=payload,
                    reason=reason,
                )
                written += 1
                touched.append(strategy)
            except Exception as e:
                logger.debug(f"shadow pending write failed for {row.get('code')}: {e}")

        summary = {
            "enabled": True,
            "written": written,
            "strategies": sorted(set(touched)),
            "mode": "audit_only",
            "status": "SHADOW",
            "bucket": bucket,
            "regime": regime,
        }
        snapshot["shadow_pending"] = summary
        if "summary" in snapshot and isinstance(snapshot.get("summary"), dict):
            snapshot["summary"]["shadow_pending_written"] = written
        logger.info(f"shadow pending audit rows written: {written} strategies={summary['strategies']}")
        return summary

    def record_paper_pending(self, snapshot, weather=None, market_env=None):
        """Write executable paper pending rows from strong focus-monitor targets."""
        cfg = Config.STRATEGY.get("paper_all_pool_execution", {}) if isinstance(getattr(Config, "STRATEGY", {}), dict) else {}
        if not bool(cfg.get("focus_monitor_enabled", True)):
            return {"enabled": False, "written": 0, "strategies": []}

        snapshot = snapshot or {}
        now = datetime.now()
        bucket = self._current_bucket(now)
        if not bucket:
            return {"enabled": True, "written": 0, "strategies": [], "reason": "outside_bucket"}

        today = self._date_fmt((snapshot.get("offsets") or {}).get("today")) or now.strftime("%Y-%m-%d")
        rows = list(snapshot.get("targets") or [])
        strategies = self._shadow_strategies()
        max_rows = int(cfg.get("focus_monitor_max_rows", 8) or 8)
        expires_at = self._bucket_end_dt(today, bucket)

        candidates = [
            r for r in rows
            if str(r.get("strategy") or "") in strategies
            and str(r.get("level") or "") in {"强盯", "重点"}
            and str(r.get("cycle") or "").upper() != "T+2"
        ]
        candidates.sort(key=lambda r: (
            _safe_float(r.get("score"), 0.0),
            _safe_float(r.get("high_from_sel_pct"), 0.0),
            _safe_float(r.get("intraday_pct"), 0.0),
        ), reverse=True)

        written = 0
        touched = []
        for row in candidates[:max_rows]:
            strategy = str(row.get("strategy") or "")
            code = str(row.get("code") or "").split(".")[0]
            ts_code = row.get("ts_code") or self._code_to_ts(code)
            payload = {
                "target_account": "paper_watchlist",
                "strategy_key": strategy,
                "strategy_name": strategy,
                "paper_executable_pool": True,
                "paper_source_pool": f"focus_monitor:{strategy}",
                "paper_strong_entry": True,
                "paper_experiment": True,
                "paper_experiment_type": "focus_monitor_shadow_buyable",
                "paper_experiment_reason": "FOCUS_MONITOR_STRONG_TO_PAPER_PENDING",
                "paper_max_buy_change": self._paper_change_ceiling(row),
                "source": "focus_monitor",
                "source_bucket": row.get("bucket"),
                "source_cycle": row.get("cycle"),
                "level": row.get("level"),
                "action": row.get("action"),
                "reason": row.get("reason"),
                "score": row.get("score"),
                "price": row.get("price"),
                "change": row.get("intraday_pct"),
                "high_from_sel_pct": row.get("high_from_sel_pct"),
                "vol_ratio": row.get("vol_ratio"),
                "mf_intensity": row.get("mf_intensity"),
                "candidate_attempt": {
                    "schema": "candidate_attempt_v1",
                    "stage": "focus_monitor_paper_create",
                    "action": "QUEUED",
                    "reason": "focus monitor strong target routed to paper pending",
                    "strategy": strategy,
                    "code": code,
                    "name": row.get("name"),
                    "target_account": "paper_watchlist",
                    "paper_trade": True,
                    "mode": "focus_monitor",
                },
            }
            self.portfolio.upsert_pending_entry_signal(
                trade_date=today,
                code=code,
                name=row.get("name"),
                ts_code=ts_code,
                source_strategy=strategy,
                signal_time=now,
                expires_at=expires_at,
                weather=weather,
                signal_bucket=bucket,
                entry_model="dynamic_window",
                payload=payload,
                status="PENDING",
            )
            written += 1
            touched.append(strategy)
            logger.info(
                "PAPER_FOCUS_PENDING_CREATED account=paper_watchlist code=%s name=%s strategy=%s bucket=%s expires_at=%s level=%s score=%s",
                code,
                row.get("name"),
                strategy,
                bucket,
                expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                row.get("level"),
                row.get("score"),
            )

        summary = {"enabled": True, "written": written, "strategies": sorted(set(touched)), "bucket": bucket}
        snapshot["paper_pending"] = summary
        if "summary" in snapshot and isinstance(snapshot.get("summary"), dict):
            snapshot["summary"]["paper_pending_written"] = written
        return summary

    def build_today_focus(self, result, mode="pre_market", limit=8):
        """Rank candidates already produced by a strategy report."""
        self._minute_budget_used = 0
        result = result or {}
        sources = []

        def add_items(label, items, cycle="T+1"):
            for item in items or []:
                row = dict(item)
                row["_focus_source"] = label
                row["_focus_cycle"] = cycle
                sources.append(row)

        if mode == "pre_market":
            add_items("集合竞价", result.get("auction_picks"), "T+1")
            add_items("冷启动", result.get("cold_start_picks"), "T+1")
            add_items("龙头跟踪", result.get("leader_picks"), "T+1")
            add_items("技术突破", result.get("technical_picks") or result.get("hot_stocks"), "T+1")
        elif mode == "afternoon":
            add_items("午盘精选", result.get("hot_stocks"), "T+1")
        elif mode == "post_market":
            add_items("盘后资金流", result.get("hot_stocks"), "T+2")
        elif mode == "watchlist":
            wd = result.get("watchlist_data") or {}
            add_items("买入触发", wd.get("buy_candidates"), "-")
            add_items("潜伏观察", wd.get("observed"), "-")
        else:
            add_items("候选", result.get("hot_stocks"), "T+1")

        deduped = {}
        for item in sources:
            code = str(item.get("code") or item.get("ts_code") or "").split(".")[0]
            if not code:
                continue
            source = item.get("_focus_source") or "-"
            key = (source, code)
            if key not in deduped:
                deduped[key] = item

        rows = []
        today = datetime.now().strftime("%Y%m%d")
        try:
            latest = str(self.provider._get_latest_trade_date() or "")
            if len(latest) == 8:
                today = latest
        except Exception:
            pass
        for item in deduped.values():
            score = _safe_float(item.get("score"), 0.0)
            price = _safe_float(item.get("price"), 0.0)
            if price <= 0:
                price = self._selection_price(item)
            change = _safe_float(item.get("change"), _safe_float(item.get("open_change"), 0.0))
            turnover = _safe_float(item.get("turnover"), 0.0)
            vol_ratio = _safe_float(item.get("vol_ratio"), 0.0)
            if vol_ratio <= 0 and turnover > 0:
                vol_ratio = min(turnover / 3.0, 5.0)
            focus_score = score * 0.35 + change * 3.0 + vol_ratio * 5.0
            if item.get("lhb_present_today"):
                focus_score += 5
            if item.get("first_board_tag") or item.get("zt_tag"):
                focus_score += 4
            if item.get("_focus_source") in ("冷启动", "龙头跟踪", "买入触发"):
                focus_score += 4
            code = str(item.get("code") or item.get("ts_code") or "").split(".")[0]
            ts_code = item.get("ts_code") or self._code_to_ts(code)
            pre_close = _safe_float(item.get("pre_close"), 0.0)
            if pre_close <= 0 and price > 0 and change != -100:
                pre_close = price / (1.0 + change / 100.0) if (1.0 + change / 100.0) > 0 else 0.0
            minute_signal = self._fetch_minute_structure(ts_code, today, {"pre_close": pre_close})
            if minute_signal.get("leader_hard_false_strength_risk_60m"):
                focus_score -= 28
            elif minute_signal.get("leader_false_strength_risk_60m"):
                focus_score -= 16
            if minute_signal.get("leader_strong_sustained_strength_60m"):
                focus_score += 14
            elif minute_signal.get("leader_sustained_strength_watch_60m"):
                focus_score += 8
            if minute_signal.get("leader_close_hold_gate"):
                focus_score += 8
            level = "强盯" if focus_score >= 35 else ("重点" if focus_score >= 20 else "观察")
            if minute_signal.get("leader_hard_false_strength_risk_60m"):
                level = "风险"
                action = "强冲高回落，隔夜降级"
            elif minute_signal.get("leader_false_strength_risk_60m"):
                level = "降级"
                action = "冲高回落风险，不追"
            elif minute_signal.get("leader_strong_sustained_strength_60m"):
                action = "强势延续，盯收盘质量"
            elif minute_signal.get("leader_sustained_strength_watch_60m"):
                action = "强势延续观察"
            else:
                action = "优先盯分时承接" if level == "强盯" else ("放入重点观察" if level == "重点" else "保留观察")
            reason = item.get("reason") or item.get("zt_tag") or item.get("first_board_tag") or item.get("strategy") or "-"
            if minute_signal.get("minute_note"):
                reason = f"{minute_signal.get('minute_note')}；{reason}"
            row = {
                "source": item.get("_focus_source") or "-",
                "cycle": item.get("_focus_cycle") or "-",
                "code": code,
                "name": item.get("name") or "-",
                "price": price,
                "change": change,
                "turnover": turnover,
                "score": round(focus_score, 1),
                "level": level,
                "action": action,
                "reason": reason,
            }
            row.update(minute_signal)
            rows.append(row)

        rows.sort(key=lambda x: x.get("score", 0), reverse=True)
        return rows[:limit]
