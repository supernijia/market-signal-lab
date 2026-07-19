# -*- coding: utf-8 -*-
"""
Observe-mode adapter for first-limit overnight quality + next-day breakout confirmation.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from core.config import Config

logger = logging.getLogger("StockAnalyzer.ComboSignal")


class FirstLimitBreakoutComboAdapter:
    def __init__(self, provider):
        self.provider = provider
        self._ready = False
        self._load_attempted = False
        self._first_limit_model = None
        self._breakout_model = None
        self._breakout_false60_model = None
        self._breakout_model_variant = ""
        self._first_limit_module = None
        self._breakout_module = None
        self._minute_budget_used = 0

    def enabled(self) -> bool:
        cfg = self._cfg()
        return bool(cfg.get("enabled", False))

    def mode(self) -> str:
        return str(self._cfg().get("mode", "observe") or "observe")

    def annotate_watchlist_item(self, item: Dict[str, Any], qt: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled():
            return {}
        if str(item.get("board_context") or "") != "first_board":
            return {}
        if not self._ensure_loaded():
            return {}

        result: Dict[str, Any] = {}
        ts_code = str(item.get("ts_code") or "")
        if not ts_code:
            code = str(item.get("code") or "")
            if code:
                ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        if not ts_code:
            return {}

        sel_date = str(item.get("date") or "").replace("-", "")
        if len(sel_date) != 8:
            return {}

        first_limit_score = self._score_first_limit(ts_code, sel_date)
        if first_limit_score is not None:
            result["first_limit_score_model"] = round(first_limit_score, 4)

        today = None
        try:
            today = str(self.provider._get_latest_trade_date() or "")
        except Exception:
            today = ""
        if today and today > sel_date:
            breakout_scores = self._score_breakout_today(ts_code, today, qt)
            if breakout_scores:
                breakout_score = breakout_scores.get("confirm")
                false60_score = breakout_scores.get("false60")
                if breakout_score is not None:
                    result["breakout_confirm_score_model"] = round(float(breakout_score), 4)
                if false60_score is not None:
                    result["breakout_false60_score_model"] = round(float(false60_score), 4)
                if breakout_score is not None and false60_score is not None:
                    net60_score = float(breakout_score) * (1.0 - float(false60_score))
                    result["breakout_net60_score_model"] = round(net60_score, 4)

        first_thr = float(self._cfg().get("first_limit_score_min", 0.45) or 0.45)
        breakout_thr = float(self._cfg().get("breakout_score_min", 0.24) or 0.24)
        combo_thr = float(self._cfg().get("combo_score_product_min", 0.12) or 0.12)
        net60_thr = float(self._cfg().get("breakout_net60_score_min", 0.22943997075673855) or 0.22943997075673855)
        false60_max = float(self._cfg().get("breakout_false60_score_max", 0.4016039363675987) or 0.4016039363675987)

        first_score = float(result.get("first_limit_score_model") or 0.0)
        breakout_score = float(result.get("breakout_confirm_score_model") or 0.0)
        false60_score = float(result.get("breakout_false60_score_model") or 0.0)
        net60_score = float(result.get("breakout_net60_score_model") or 0.0)
        result["first_limit_gate_pass"] = bool(first_score >= first_thr) if "first_limit_score_model" in result else False
        result["breakout_gate_pass"] = bool(breakout_score >= breakout_thr) if "breakout_confirm_score_model" in result else False
        result["breakout_false60_veto_pass"] = bool(false60_score <= false60_max) if "breakout_false60_score_model" in result else False
        result["breakout_net60_gate_pass"] = bool(net60_score >= net60_thr) if "breakout_net60_score_model" in result else False
        if "breakout_net60_score_model" in result and "breakout_false60_score_model" in result:
            high_quality = bool(result["breakout_net60_gate_pass"] and result["breakout_false60_veto_pass"])
            result["breakout_high_quality_gate_pass"] = high_quality
            result["recommended_entry_delay_min"] = int(
                self._cfg().get("breakout_high_quality_entry_delay_min", 0) if high_quality
                else self._cfg().get("breakout_normal_entry_delay_min", 10)
            )
            if self._breakout_model_variant:
                result["breakout_model_variant"] = self._breakout_model_variant
            exec_gate = self._execution_gate(result, qt)
            if exec_gate:
                result.update(exec_gate)
        if "first_limit_score_model" in result and "breakout_confirm_score_model" in result:
            combo_product = first_score * breakout_score
            result["combo_score_product"] = round(combo_product, 4)
            result["combo_gate_pass"] = bool(combo_product >= combo_thr and result["first_limit_gate_pass"] and result["breakout_gate_pass"])

        result["combo_observe_mode"] = self.mode()
        return result

    def _execution_gate(self, result: Dict[str, Any], qt: Dict[str, Any] | None) -> Dict[str, Any]:
        cfg = self._cfg()
        if not bool(cfg.get("breakout_execution_gate_enabled", False)):
            return {}
        qt = qt or {}
        max_chase = float(cfg.get("breakout_max_entry_chase_pct", 0.08) or 0.08)
        min_amount = float(cfg.get("breakout_min_entry_minute_amount", 1000000.0) or 1000000.0)
        price = float(qt.get("price") or qt.get("close") or 0.0)
        open_price = float(qt.get("open") or 0.0)
        amount = float(qt.get("amount") or 0.0)
        pre_close = float(qt.get("pre_close") or 0.0)
        entry_chase = (price / open_price - 1.0) if price > 0 and open_price > 0 else 0.0
        day_change = (price / pre_close - 1.0) if price > 0 and pre_close > 0 else 0.0
        likely_limit_queue = bool(day_change >= 0.095 and entry_chase >= max_chase)
        execution_pass = bool(
            entry_chase <= max_chase
            and (amount <= 0 or amount >= min_amount)
            and not (bool(cfg.get("breakout_limit_queue_risk_veto_enabled", True)) and likely_limit_queue)
        )
        return {
            "breakout_execution_gate_pass": execution_pass,
            "breakout_entry_chase_pct": round(entry_chase, 4),
            "breakout_execution_amount": round(amount, 2),
            "breakout_limit_queue_risk": likely_limit_queue,
        }

    def annotate_candidate(self, item: Dict[str, Any], trade_date: str | None = None, qt: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Annotate a freshly produced first-board candidate when model inputs are available."""
        if not isinstance(item, dict):
            return {}
        board_context = str(item.get("board_context") or "")
        if board_context not in {"first_board", "首板候选"} and not bool(item.get("is_first_board_candidate", False)):
            return {}

        sel_date = str(trade_date or item.get("date") or "").replace("-", "")
        if len(sel_date) != 8:
            try:
                sel_date = str(self.provider._get_latest_trade_date() or "")
            except Exception:
                sel_date = ""
        if len(sel_date) != 8:
            return {}

        quote = dict(qt or {})
        if not quote:
            price = item.get("price") or item.get("close") or item.get("trade")
            pre_close = item.get("pre_close")
            open_price = item.get("open") or item.get("open_price") or price
            open_change = item.get("open_change")
            try:
                if pre_close is None and price and open_change is not None:
                    pre_close = float(price) / (1.0 + float(open_change) / 100.0)
            except Exception:
                pre_close = None
            quote = {
                "price": price,
                "open": open_price,
                "high": item.get("high") or price,
                "low": item.get("low") or price,
                "pre_close": pre_close,
                "amount": item.get("amount") or item.get("amount_yi", 0) * 100000000,
                "turnover": item.get("turnover") or item.get("turnover_rate"),
                "turnover_rate": item.get("turnover") or item.get("turnover_rate"),
            }

        payload = dict(item)
        payload["date"] = sel_date
        return self.annotate_watchlist_item(payload, quote)

    def _cfg(self) -> dict:
        try:
            cfg = Config.STRATEGY.get("first_limit_breakout_combo", {})
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _training_root(self) -> Path | None:
        base_candidates = [
            Path(__file__).resolve().parents[2] / "market-signal-training",
            Path(__file__).resolve().parents[1] / "market-signal-training",
        ]
        for path in base_candidates:
            if (path / "training").exists():
                return path
        return None

    def _load_module(self, module_name: str, path: Path):
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _load_model_json(self, path: Path):
        if not path.exists():
            return None
        model = json.loads(path.read_text(encoding="utf-8"))
        mean = np.array(model["mean"], dtype=float)
        std = np.array(model["std"], dtype=float)
        std[std == 0] = 1.0
        model["__mean_np__"] = mean
        model["__std_np__"] = std
        model["__weights_np__"] = np.array(model["weights"], dtype=float)
        model["__bias_float__"] = float(model["bias"])
        return model

    def _predict_probability(self, model: dict, feature_names: list[str], features: dict) -> float:
        x = np.array([float(features.get(name, 0.0) or 0.0) for name in feature_names], dtype=float)
        mean = model["__mean_np__"]
        std = model["__std_np__"]
        if len(x) != len(mean):
            if len(x) > len(mean):
                x = x[: len(mean)]
            else:
                x = np.pad(x, (0, len(mean) - len(x)))
        xs = (x - mean) / std
        z = xs @ model["__weights_np__"] + model["__bias_float__"]
        z = np.clip(z, -30, 30)
        return float(1.0 / (1.0 + np.exp(-z)))

    def _combo_minute_budget(self) -> int:
        cfg = self._cfg()
        try:
            return int(cfg.get("max_minute_fetches_per_run", 10) or 0)
        except Exception:
            return 10

    def _get_minute_df_budgeted(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        cached = None
        try:
            cached = self.provider._get_cached_minute(ts_code, start_date, end_date, freq="1min")
        except Exception:
            cached = None
        if cached is not None:
            return self._normalize_minute_df(cached)

        budget = self._combo_minute_budget()
        if budget <= 0 or self._minute_budget_used >= budget:
            logger.debug(f"combo minute fetch budget exhausted: {self._minute_budget_used}/{budget}")
            return pd.DataFrame()

        self._minute_budget_used += 1
        return self._normalize_minute_df(self.provider.get_stock_min_data(ts_code, start_date, end_date, freq="1min"))

    def _ensure_loaded(self) -> bool:
        if self._ready:
            return True
        if self._load_attempted:
            return False
        self._load_attempted = True

        root = self._training_root()
        if root is None:
            logger.info("combo adapter skipped: training repo not found")
            return False

        training_dir = root / "training"
        try:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            self._first_limit_module = self._load_module(
                "_combo_first_limit_quality_event",
                training_dir / "first_limit_quality_event.py",
            )
            self._breakout_module = self._load_module(
                "_combo_breakout_confirmation_event",
                training_dir / "breakout_confirmation_event.py",
            )
            self._first_limit_model = self._load_model_json(
                training_dir / "factor_models" / "first_limit_quality_event_nextday_open_strong.json"
            )
            self._breakout_model_variant = str(self._cfg().get("breakout_model_variant") or "").strip()
            if self._breakout_model_variant == "entry_quality_tplus1_execution_v1":
                breakout_model_path = training_dir / "factor_models" / "breakout_entry_quality_confirm_60.json"
                false60_model_path = training_dir / "factor_models" / "breakout_entry_quality_false_break_60.json"
            else:
                breakout_model_path = training_dir / "factor_models" / "breakout_confirmation_event.json"
                false60_model_path = training_dir / "factor_models" / "breakout_confirmation_event_false_break_60.json"
            self._breakout_model = self._load_model_json(
                breakout_model_path
            )
            self._breakout_false60_model = self._load_model_json(
                false60_model_path
            )
        except Exception as e:
            logger.warning(f"combo adapter load failed: {e}")
            return False

        self._ready = all(
            [
                self._first_limit_module,
                self._breakout_module,
                self._first_limit_model,
                self._breakout_model,
                self._breakout_false60_model,
            ]
        )
        return self._ready

    @staticmethod
    def _normalize_minute_df(df: pd.DataFrame | None) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        minute_df = df.copy()
        if "trade_time" not in minute_df.columns:
            return pd.DataFrame()
        minute_df["trade_time"] = pd.to_datetime(minute_df["trade_time"], errors="coerce")
        minute_df = minute_df.dropna(subset=["trade_time"]).sort_values("trade_time").reset_index(drop=True)
        for col in ("open", "high", "low", "close", "vol", "amount"):
            if col in minute_df.columns:
                minute_df[col] = pd.to_numeric(minute_df[col], errors="coerce").fillna(0.0)
        if "amount" not in minute_df.columns:
            minute_df["amount"] = minute_df["close"].fillna(0.0) * minute_df["vol"].fillna(0.0) * 100.0
        minute_df["cum_amount"] = minute_df["amount"].fillna(0.0).cumsum()
        minute_df["cum_vol"] = minute_df["vol"].fillna(0.0).replace(0, np.nan).cumsum()
        minute_df["vwap"] = (minute_df["cum_amount"] / minute_df["cum_vol"]).ffill().fillna(minute_df["close"])
        return minute_df

    def _daily_basic_map(self, trade_date: str) -> dict:
        try:
            basic = self.provider.get_daily_basic(trade_date)
            return basic if isinstance(basic, dict) else {}
        except Exception:
            return {}

    def _score_first_limit(self, ts_code: str, sel_date: str) -> float | None:
        history = self.provider.get_history_data(ts_code, end_date=sel_date, count=25) or []
        if len(history) < 21:
            return None
        day_record = history[-1]
        prior_rows = history[:-1][-20:]
        if str(day_record.get("trade_date") or "") != sel_date:
            return None

        minute_df = self._get_minute_df_budgeted(ts_code, sel_date, sel_date)
        if minute_df.empty:
            return None
        daily_basic = self._daily_basic_map(sel_date)
        basic_row = daily_basic.get(ts_code, {}) if isinstance(daily_basic, dict) else {}
        day_row = dict(day_record)
        day_row["name"] = day_row.get("name") or ""
        day_row["amount"] = float(minute_df["amount"].sum())
        day_row["turnover_rate"] = float(basic_row.get("turnover_rate") or 0.0)
        extracted = self._first_limit_module.extract_first_limit_quality_features(
            ts_code=ts_code,
            trade_date=sel_date,
            day_row=day_row,
            minute_df=minute_df,
            close_history=deque([float(r.get("close") or 0.0) for r in prior_rows], maxlen=20),
        )
        if extracted is None:
            return None
        return self._predict_probability(
            self._first_limit_model,
            list(self._first_limit_model.get("feature_names") or []),
            extracted.features,
        )

    def _score_breakout_today(self, ts_code: str, trade_date: str, qt: Dict[str, Any]) -> dict | None:
        history = self.provider.get_history_data(ts_code, end_date=trade_date, count=25) or []
        if len(history) < 20:
            return None
        if history and str(history[-1].get("trade_date") or "") == trade_date:
            history = history[:-1]
        if len(history) < 20:
            return None

        minute_df = self._get_minute_df_budgeted(ts_code, trade_date, trade_date)
        if minute_df.empty:
            return None

        pre_close = float(qt.get("pre_close") or 0.0)
        price = float(qt.get("price") or 0.0)
        open_price = float(qt.get("open") or 0.0)
        high = float(qt.get("high") or 0.0)
        low = float(qt.get("low") or 0.0)
        amount = float(qt.get("amount") or minute_df["amount"].sum() or 0.0)
        pct_chg = ((price / pre_close) - 1.0) * 100.0 if pre_close > 0 and price > 0 else 0.0
        turnover = float(qt.get("turnover") or qt.get("turnover_rate") or 0.0)
        day_row = {
            "pre_close": pre_close,
            "open": open_price,
            "high": high,
            "low": low,
            "close": price,
            "amount": amount,
            "pct_chg": pct_chg,
            "turnover_rate": turnover,
        }
        extracted = self._breakout_module.extract_breakout_event_features(
            ts_code=ts_code,
            trade_date=trade_date,
            day_row=day_row,
            minute_df=minute_df,
            close_history=deque([float(r.get("close") or 0.0) for r in history[-20:]], maxlen=20),
        )
        if extracted is None:
            return None
        confirm_score = self._predict_probability(
            self._breakout_model,
            list(self._breakout_model.get("feature_names") or []),
            extracted.features,
        )
        false60_score = self._predict_probability(
            self._breakout_false60_model,
            list(self._breakout_false60_model.get("feature_names") or []),
            extracted.features,
        )
        return {
            "confirm": confirm_score,
            "false60": false60_score,
            "net60": confirm_score * (1.0 - false60_score),
        }
