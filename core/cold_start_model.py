# -*- coding: utf-8 -*-
"""Observation-only cold-start scoring helpers.

The model files may be trained in an optional companion workspace. Runtime use is
intentionally additive: scores/tags are attached for reports and logs, but do
not change selection ranking or auto-trade gates.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from core.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "data" / "models"

FEATURE_NAMES = [
    "market_SH",
    "market_SZ",
    "market_BJ",
    "time_early_0935_1000",
    "time_morning_1000_1030",
    "time_late_morning_1030_1130",
    "time_midday_1130_1330",
    "time_afternoon_1330_1430",
    "time_late_1430_1500",
    "trigger_ret_pct",
    "entry_vs_vwap_pct",
    "prev_day_ret_pct",
    "open_ret_pct",
    "pre_trigger_max_ret_pct",
    "pre_trigger_min_ret_pct",
    "max_ret_5m_pct",
    "min_ret_5m_pct",
    "close_ret_5m_pct",
    "above_vwap_5m_ratio",
    "max_ret_10m_pct",
    "min_ret_10m_pct",
    "close_ret_10m_pct",
    "above_vwap_10m_ratio",
]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _sigmoid(value: float) -> float:
    value = max(-30.0, min(30.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def _time_bucket(signal_time: str) -> str:
    text = str(signal_time or "")
    if text < "10:00:00":
        return "early_0935_1000"
    if text < "10:30:00":
        return "morning_1000_1030"
    if text < "11:30:00":
        return "late_morning_1030_1130"
    if text < "13:30:00":
        return "midday_1130_1330"
    if text < "14:30:00":
        return "afternoon_1330_1430"
    return "late_1430_1500"


def _load_model(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _predict(model: dict[str, Any] | None, features: dict[str, float]) -> float | None:
    if not model:
        return None
    names = list(model.get("feature_names") or FEATURE_NAMES)
    weights = list(model.get("weights") or [])
    mean = list(model.get("mean") or [])
    std = list(model.get("std") or [])
    if not weights or not mean or not std:
        return None
    total = _safe_float(model.get("bias"))
    for idx, name in enumerate(names):
        if idx >= len(weights) or idx >= len(mean) or idx >= len(std):
            break
        denom = _safe_float(std[idx], 1.0) or 1.0
        x = (_safe_float(features.get(name)) - _safe_float(mean[idx])) / denom
        total += x * _safe_float(weights[idx])
    return _sigmoid(total)


class ColdStartModelScorer:
    def __init__(self, model_dir: str | os.PathLike[str] | None = None):
        cfg = Config.STRATEGY.get("cold_start_observe_model", {}) if isinstance(Config.STRATEGY, dict) else {}
        configured = cfg.get("model_dir") if isinstance(cfg, dict) else None
        env_dir = os.environ.get("COLD_START_MODEL_DIR")
        self.enabled = bool((cfg or {}).get("enabled", True)) if isinstance(cfg, dict) else True
        self.model_dir = Path(model_dir or env_dir or configured or DEFAULT_MODEL_DIR)
        if not self.model_dir.is_absolute():
            self.model_dir = PROJECT_ROOT / self.model_dir
        self.good_model = _load_model(self.model_dir / "cold_start_quality_good_10m.json")
        self.profit_model = _load_model(self.model_dir / "cold_start_quality_profit_capture_10m.json")
        self.risk_model = _load_model(self.model_dir / "cold_start_quality_risk_10m.json")

    def available(self) -> bool:
        return bool(self.enabled and self.good_model and self.profit_model and self.risk_model)

    def score_candidate(self, candidate: dict[str, Any], *, signal_time: str = "09:35:00") -> dict[str, Any]:
        if not self.available():
            return {"cold_start_model_available": False}
        features = self._features(candidate, signal_time=signal_time)
        good = _predict(self.good_model, features)
        profit = _predict(self.profit_model, features)
        risk = _predict(self.risk_model, features)
        if good is None or profit is None or risk is None:
            return {"cold_start_model_available": False}
        early_absorb = self._early_high_trigger_absorb(candidate, signal_time=signal_time)
        delayed = self._delayed_confirm(candidate)
        pullback = self._pullback_entry_candidate(candidate, signal_time=signal_time)
        score_10m = profit * 0.55 + good * 0.35 - risk * 0.45
        observe_score = score_10m + (0.30 if early_absorb else 0.0)
        score_60m = observe_score + (0.35 if delayed else 0.0)
        tags = []
        if good >= 0.5:
            tags.append("COLD_START_MODEL_GOOD")
        if profit >= 0.45:
            tags.append("COLD_START_PROFIT_CAPTURE")
        if risk >= 0.65:
            tags.append("COLD_START_RISK_HIGH")
        if early_absorb:
            tags.append("COLD_START_EARLY_ABSORB")
        if delayed:
            tags.append("COLD_START_VWAP_SUPPORT_OBSERVE")
        if pullback["candidate"]:
            tags.append("COLD_START_PULLBACK_ENTRY_WATCH")
        return {
            "cold_start_model_available": True,
            "cold_start_good_score": good,
            "cold_start_profit_score": profit,
            "cold_start_risk_score": risk,
            "cold_start_score_10m": score_10m,
            "cold_start_observe_score": observe_score,
            "cold_start_score_60m": score_60m,
            "cold_start_early_absorb": early_absorb,
            "cold_start_delayed_confirm": delayed,
            "cold_start_vwap_support_observe": delayed,
            "cold_start_pullback_entry_candidate": pullback["candidate"],
            "cold_start_pullback_window_min": pullback["window_min"],
            "cold_start_pullback_entry_vs_signal": pullback["entry_vs_signal"],
            "cold_start_pullback_above_vwap_prefix": pullback["above_vwap_prefix"],
            "cold_start_entry_mode": pullback["entry_mode"],
            "cold_start_model_tags": tags,
        }

    def _features(self, candidate: dict[str, Any], *, signal_time: str) -> dict[str, float]:
        ts_code = str(candidate.get("ts_code") or "")
        market = ts_code.split(".")[-1] if "." in ts_code else ("SH" if str(candidate.get("code") or "").startswith("6") else "SZ")
        bucket = _time_bucket(signal_time)
        trigger_ret = _safe_float(candidate.get("trigger_ret_pct"), _safe_float(candidate.get("open_change")))
        price = _safe_float(candidate.get("price"))
        vwap = _safe_float(candidate.get("vwap"))
        entry_vs_vwap = _safe_float(candidate.get("entry_vs_vwap_pct"))
        if entry_vs_vwap == 0.0 and price > 0 and vwap > 0:
            entry_vs_vwap = (price / vwap - 1.0) * 100.0
        features = {
            "market_SH": 1.0 if market == "SH" else 0.0,
            "market_SZ": 1.0 if market == "SZ" else 0.0,
            "market_BJ": 1.0 if market == "BJ" else 0.0,
            "trigger_ret_pct": trigger_ret,
            "entry_vs_vwap_pct": entry_vs_vwap,
            "prev_day_ret_pct": _safe_float(candidate.get("prev_change")),
            "open_ret_pct": _safe_float(candidate.get("open_change")),
            "pre_trigger_max_ret_pct": _safe_float(candidate.get("pre_trigger_max_ret_pct"), trigger_ret),
            "pre_trigger_min_ret_pct": _safe_float(candidate.get("pre_trigger_min_ret_pct"), _safe_float(candidate.get("open_change"))),
            "max_ret_5m_pct": _safe_float(candidate.get("max_ret_5m_pct")),
            "min_ret_5m_pct": _safe_float(candidate.get("min_ret_5m_pct")),
            "close_ret_5m_pct": _safe_float(candidate.get("close_ret_5m_pct")),
            "above_vwap_5m_ratio": _safe_float(candidate.get("above_vwap_5m_ratio")),
            "max_ret_10m_pct": _safe_float(candidate.get("max_ret_10m_pct")),
            "min_ret_10m_pct": _safe_float(candidate.get("min_ret_10m_pct")),
            "close_ret_10m_pct": _safe_float(candidate.get("close_ret_10m_pct")),
            "above_vwap_10m_ratio": _safe_float(candidate.get("above_vwap_10m_ratio")),
        }
        for name in FEATURE_NAMES:
            if name.startswith("time_"):
                features[name] = 1.0 if name == f"time_{bucket}" else 0.0
            else:
                features.setdefault(name, 0.0)
        return features

    def _early_high_trigger_absorb(self, candidate: dict[str, Any], *, signal_time: str) -> bool:
        return (
            str(signal_time or "") < "10:00:00"
            and _safe_float(candidate.get("trigger_ret_pct"), _safe_float(candidate.get("open_change"))) >= 4.5
            and _safe_float(candidate.get("close_ret_5m_pct")) >= 1.0
            and _safe_float(candidate.get("above_vwap_5m_ratio")) >= 0.6
            and _safe_float(candidate.get("min_ret_10m_pct")) >= -1.5
            and _safe_float(candidate.get("close_ret_10m_pct")) >= -1.0
        )

    def _delayed_confirm(self, candidate: dict[str, Any]) -> bool:
        return (
            -1.0 <= _safe_float(candidate.get("close_ret_5m_pct")) <= 0.2
            and _safe_float(candidate.get("above_vwap_5m_ratio")) >= 0.8
            and _safe_float(candidate.get("close_ret_10m_pct")) >= -1.2
            and _safe_float(candidate.get("min_ret_10m_pct")) >= -1.5
            and _safe_float(candidate.get("max_ret_60m_pct")) >= 2.0
            and _safe_float(candidate.get("close_ret_60m_pct")) >= 1.0
            and _safe_float(candidate.get("min_ret_60m_pct")) >= -1.5
        )

    def _pullback_entry_candidate(self, candidate: dict[str, Any], *, signal_time: str) -> dict[str, Any]:
        """Observation-only label for early no-chase cold-start entry windows.

        Training found the useful window is close to the signal, not after a
        mechanical 5/10/60 minute chase. Runtime candidates often lack full
        post-signal minute paths; only mark this label when 5/10 minute fields
        are already present.
        """

        trigger_ret = _safe_float(candidate.get("trigger_ret_pct"), _safe_float(candidate.get("open_change")))
        price = _safe_float(candidate.get("price"))
        vwap = _safe_float(candidate.get("vwap"))
        entry_vs_signal = 0.0
        if trigger_ret > 0 and price > 0:
            # open_change is the signal proxy in live cold-start picks.
            open_change = _safe_float(candidate.get("open_change"), trigger_ret)
            entry_vs_signal = (open_change - trigger_ret) / 100.0
        if _safe_float(candidate.get("entry_vs_signal_ret")):
            entry_vs_signal = _safe_float(candidate.get("entry_vs_signal_ret"))

        close5 = _safe_float(candidate.get("close_ret_5m_pct"))
        min5 = _safe_float(candidate.get("min_ret_5m_pct"))
        above5 = _safe_float(candidate.get("above_vwap_5m_ratio"))
        close10 = _safe_float(candidate.get("close_ret_10m_pct"))
        min10 = _safe_float(candidate.get("min_ret_10m_pct"))
        above10 = _safe_float(candidate.get("above_vwap_10m_ratio"))
        has_5m = any(str(candidate.get(k, "")) not in ("", "None", "nan") for k in ("close_ret_5m_pct", "min_ret_5m_pct", "above_vwap_5m_ratio"))
        has_10m = any(str(candidate.get(k, "")) not in ("", "None", "nan") for k in ("close_ret_10m_pct", "min_ret_10m_pct", "above_vwap_10m_ratio"))

        candidate_5m = (
            has_5m
            and -1.2 <= close5 <= 0.3
            and min5 >= -1.8
            and above5 >= 0.8
        )
        candidate_10m = (
            has_10m
            and -1.2 <= close10 <= 0.3
            and min10 >= -1.8
            and above10 >= 0.8
        )
        near_vwap_now = bool(price > 0 and vwap > 0 and abs(price / vwap - 1.0) <= 0.004)
        signal_nearby = bool(entry_vs_signal <= 0.003)

        if candidate_5m:
            return {
                "candidate": True,
                "window_min": 5,
                "entry_vs_signal": entry_vs_signal,
                "above_vwap_prefix": above5,
                "entry_mode": "pullback_watch",
            }
        if candidate_10m:
            return {
                "candidate": True,
                "window_min": 10,
                "entry_vs_signal": entry_vs_signal,
                "above_vwap_prefix": above10,
                "entry_mode": "pullback_watch",
            }
        if near_vwap_now and signal_nearby and str(signal_time or "") < "10:00:00":
            return {
                "candidate": True,
                "window_min": 0,
                "entry_vs_signal": entry_vs_signal,
                "above_vwap_prefix": None,
                "entry_mode": "signal_nearby",
            }
        return {
            "candidate": False,
            "window_min": None,
            "entry_vs_signal": entry_vs_signal,
            "above_vwap_prefix": None,
            "entry_mode": "confirm_only",
        }
