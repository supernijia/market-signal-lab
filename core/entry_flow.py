# -*- coding: utf-8 -*-
"""Shared entry-flow orchestration for auto-buy and pending retries."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any

from core.config import Config
from core.entry_confirm import confirm_entry


DEFAULT_RECENT_OVERHEAT_GATE = {
    "enabled": True,
    "apply_to_pending": True,
    "apply_to_immediate": False,
    "history_count": 30,
    "min_history_count": 10,
    "fail_open": True,
    "default": {
        "max_5d_gain": 20,
        "max_10d_gain": 30,
        "max_ma20_deviation": 25,
        "max_consecutive_limit_up": 3,
    },
    "strategies": {
        "集合竞价": {
            "max_5d_gain": 10,
            "max_10d_gain": 15,
            "max_ma20_deviation": 20,
            "max_consecutive_limit_up": 3,
        },
        "早盘竞价首选": {
            "max_5d_gain": 10,
            "max_10d_gain": 15,
            "max_ma20_deviation": 20,
            "max_consecutive_limit_up": 3,
        },
        "冷启动": {
            "max_5d_gain": 10,
            "max_10d_gain": 15,
            "max_ma20_deviation": 20,
            "max_consecutive_limit_up": 3,
        },
        "午盘精选": {
            "max_5d_gain": 20,
            "max_10d_gain": 30,
            "max_ma20_deviation": 25,
            "max_consecutive_limit_up": 2,
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged.get(key, {}), value)
        else:
            merged[key] = value
    return merged


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _strategy_key(strategy: str | None) -> str:
    s = str(strategy or "").strip()
    if s in ("集合竞价", "早盘竞价首选", "pre_market"):
        return "集合竞价"
    if s in ("午盘精选",):
        return "午盘精选"
    if s in ("冷启动",):
        return "冷启动"
    return s


def _candidate_ts_code(candidate: dict[str, Any]) -> str:
    ts_code = str((candidate or {}).get("ts_code") or "").strip()
    if ts_code.endswith((".SH", ".SZ", ".BJ")):
        return ts_code
    code = str((candidate or {}).get("code") or "").strip()
    if not code:
        return ""
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _recent_overheat_gate_config() -> dict[str, Any]:
    try:
        entry_confirm_cfg = Config.STRATEGY.get("entry_confirm", {}) if isinstance(Config.STRATEGY, dict) else {}
        cfg = entry_confirm_cfg.get("recent_overheat_gate", {}) if isinstance(entry_confirm_cfg, dict) else {}
    except Exception:
        cfg = {}
    return _deep_merge(DEFAULT_RECENT_OVERHEAT_GATE, cfg if isinstance(cfg, dict) else {})


def _paper_strong_policy_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        cfg = Config.STRATEGY.get("paper_strong_entry_experiment", {}) if isinstance(Config.STRATEGY, dict) else {}
    except Exception:
        cfg = {}
    default = {
        "enabled": False,
        "max_buy_change": 20.2,
        "min_volume_ratio": 0.9,
        "max_price_vwap_ratio": 1.05,
        "min_price_vwap_ratio": 0.99,
        "max_distance_from_intraday_high_pct": 3.0,
        "max_upper_retrace_pct": 4.0,
        "disable_limit_up_gate": True,
        "recent_overheat_gate": {
            "max_5d_gain": 35,
            "max_10d_gain": 55,
            "max_ma20_deviation": 45,
            "max_consecutive_limit_up": 4,
        },
    }
    merged = _deep_merge(default, cfg if isinstance(cfg, dict) else {})
    if isinstance(override, dict):
        merged = _deep_merge(merged, override)
    return merged


@contextmanager
def _temporary_paper_strong_policy(policy: dict[str, Any] | None):
    """Temporarily relax confirmation gates for paper-only strong-ticket experiments."""

    if not policy or not bool(policy.get("enabled", False)):
        yield
        return

    strategy_cfg = Config.STRATEGY if isinstance(getattr(Config, "STRATEGY", {}), dict) else {}
    risk_cfg = Config.RISK_MANAGEMENT if isinstance(getattr(Config, "RISK_MANAGEMENT", {}), dict) else {}
    restore_strategy = deepcopy(strategy_cfg)
    restore_risk = deepcopy(risk_cfg)

    try:
        if isinstance(risk_cfg, dict):
            risk_cfg["MAX_BUY_CHANGE"] = float(policy.get("max_buy_change", risk_cfg.get("MAX_BUY_CHANGE", 7.0)) or 7.0)
            risk_cfg["MIN_VOLUME_RATIO"] = float(policy.get("min_volume_ratio", risk_cfg.get("MIN_VOLUME_RATIO", 2.0)) or 2.0)

        if isinstance(strategy_cfg, dict):
            entry_confirm = strategy_cfg.setdefault("entry_confirm", {})
            if isinstance(entry_confirm, dict):
                for key in (
                    "min_volume_ratio",
                    "max_price_vwap_ratio",
                    "min_price_vwap_ratio",
                    "max_distance_from_intraday_high_pct",
                    "max_upper_retrace_pct",
                ):
                    if policy.get(key) is not None:
                        entry_confirm[key] = policy.get(key)

                overheat = entry_confirm.setdefault("recent_overheat_gate", {})
                if isinstance(overheat, dict):
                    thresholds = policy.get("recent_overheat_gate", {})
                    if isinstance(thresholds, dict):
                        default_thresholds = overheat.setdefault("default", {})
                        if isinstance(default_thresholds, dict):
                            default_thresholds.update(thresholds)
                        strategies = overheat.setdefault("strategies", {})
                        if isinstance(strategies, dict):
                            for name in ("备选池买入触发", "冷启动", "集合竞价", "午盘精选"):
                                current = strategies.setdefault(name, {})
                                if isinstance(current, dict):
                                    current.update(thresholds)

            if bool(policy.get("disable_limit_up_gate", True)):
                limit_gate = strategy_cfg.setdefault("limit_up_gate", {})
                if isinstance(limit_gate, dict):
                    limit_gate["enabled"] = False

        yield
    finally:
        if isinstance(getattr(Config, "STRATEGY", None), dict):
            Config.STRATEGY.clear()
            Config.STRATEGY.update(restore_strategy)
        if isinstance(getattr(Config, "RISK_MANAGEMENT", None), dict):
            Config.RISK_MANAGEMENT.clear()
            Config.RISK_MANAGEMENT.update(restore_risk)


def _check_recent_overheat(
    candidate: dict[str, Any],
    *,
    analyzer: Any,
    strategy: str | None,
    pending_retry: bool,
) -> dict[str, Any] | None:
    """Return a rejection result when recent price history is overheated."""

    cfg = _recent_overheat_gate_config()
    if not bool(cfg.get("enabled", True)):
        return None
    if pending_retry and not bool(cfg.get("apply_to_pending", True)):
        return None
    if (not pending_retry) and not bool(cfg.get("apply_to_immediate", False)):
        return None

    ts_code = _candidate_ts_code(candidate)
    if not ts_code:
        return None

    strat = _strategy_key(strategy or candidate.get("strategy"))
    default_thresholds = cfg.get("default", {}) if isinstance(cfg.get("default"), dict) else {}
    strategy_thresholds = {}
    strategies = cfg.get("strategies", {}) if isinstance(cfg.get("strategies"), dict) else {}
    if strat in strategies and isinstance(strategies.get(strat), dict):
        strategy_thresholds = strategies.get(strat) or {}
    thresholds = _deep_merge(default_thresholds, strategy_thresholds)

    try:
        history_count = int(cfg.get("history_count", 30) or 30)
        min_history_count = int(cfg.get("min_history_count", 10) or 10)
        history = analyzer.provider.get_history_data(ts_code, count=history_count)
        if not history or len(history) < min_history_count:
            return None

        is_hot, reason, gain_10d, ma20_dev = analyzer._is_overheated(
            history,
            max_10d_gain=_safe_float(thresholds.get("max_10d_gain"), 30.0),
            max_5d_gain=_safe_float(thresholds.get("max_5d_gain"), 20.0),
            max_ma20_dev=_safe_float(thresholds.get("max_ma20_deviation"), 25.0),
            max_consec_zt=int(thresholds.get("max_consecutive_limit_up", 3) or 3),
        )
        if not is_hot:
            return None

        candidate["recent_overheat_reason"] = reason
        candidate["recent_overheat_gain_10d"] = gain_10d
        candidate["recent_overheat_ma20_dev"] = ma20_dev
        return {
            "ok": False,
            "reason": f"近期过热拦截: {reason}",
            "verification": "recent_overheat_gate",
            "confirm": None,
            "metrics": {
                "ts_code": ts_code,
                "strategy": strat,
                "gain_10d": gain_10d,
                "ma20_dev": ma20_dev,
                "thresholds": thresholds,
            },
        }
    except Exception as exc:
        if bool(cfg.get("fail_open", True)):
            candidate["recent_overheat_check_error"] = str(exc)[:200]
            return None
        return {
            "ok": False,
            "reason": f"近期过热检查失败: {exc}",
            "verification": "recent_overheat_gate_error",
            "confirm": None,
            "metrics": {"ts_code": ts_code, "strategy": strat},
        }


@contextmanager
def _temporary_intraday_vwap_ratio(max_price_vwap_ratio: float | None):
    """Temporarily tighten intraday VWAP ratio during entry verification."""

    if max_price_vwap_ratio is None:
        yield
        return

    strategy_cfg = Config.STRATEGY if isinstance(getattr(Config, "STRATEGY", {}), dict) else {}
    intraday_cfg = strategy_cfg.get("intraday_structure", {}) if isinstance(strategy_cfg, dict) else {}
    restore = {
        "enabled": bool(intraday_cfg.get("enabled", False)) if isinstance(intraday_cfg, dict) else False,
        "max_price_vwap_ratio": float(intraday_cfg.get("max_price_vwap_ratio", 1.03) or 1.03),
    }

    try:
        if not isinstance(strategy_cfg, dict):
            yield
            return
        if "intraday_structure" not in strategy_cfg or not isinstance(strategy_cfg.get("intraday_structure"), dict):
            strategy_cfg["intraday_structure"] = {}
        strategy_cfg["intraday_structure"]["enabled"] = True
        strategy_cfg["intraday_structure"]["max_price_vwap_ratio"] = float(max_price_vwap_ratio)
        yield
    finally:
        try:
            if isinstance(strategy_cfg, dict) and isinstance(strategy_cfg.get("intraday_structure"), dict):
                strategy_cfg["intraday_structure"]["enabled"] = restore["enabled"]
                strategy_cfg["intraday_structure"]["max_price_vwap_ratio"] = restore["max_price_vwap_ratio"]
        except Exception:
            pass


def verify_entry_flow(
    candidate: dict[str, Any],
    *,
    analyzer: Any,
    market_env: dict[str, Any] | None,
    weather: str,
    strategy: str | None,
    now: datetime | None = None,
    realtime_map: dict[str, Any] | None = None,
    pending_retry: bool = False,
    watchlist_flow: bool = False,
    policy_max_vwap_ratio: float | None = None,
    paper_entry_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a candidate through the shared entry flow.

    Immediate auction/watchlist-style checks still use the legacy money-flow path.
    Pending retries use the structured confirm_entry() path.
    """

    now = now or datetime.now()
    result: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "verification": "pending_entry_confirm" if pending_retry else "legacy_money_flow",
        "confirm": None,
        "metrics": {},
    }

    if paper_entry_policy:
        paper_entry_policy = _paper_strong_policy_config(paper_entry_policy)
        if not paper_entry_policy.get("enabled"):
            paper_entry_policy = None

    with _temporary_paper_strong_policy(paper_entry_policy):
        if paper_entry_policy:
            result["verification"] = "paper_strong_entry_confirm"
            candidate["paper_strong_policy"] = {
                "max_buy_change": paper_entry_policy.get("max_buy_change"),
                "min_volume_ratio": paper_entry_policy.get("min_volume_ratio"),
                "max_price_vwap_ratio": paper_entry_policy.get("max_price_vwap_ratio"),
            }

        overheat_result = _check_recent_overheat(
            candidate,
            analyzer=analyzer,
            strategy=strategy,
            pending_retry=pending_retry,
        )
        if overheat_result is not None:
            if paper_entry_policy:
                overheat_result["verification"] = "paper_strong_recent_overheat_gate"
            return overheat_result

        effective_vwap_ratio = policy_max_vwap_ratio
        if paper_entry_policy and paper_entry_policy.get("max_price_vwap_ratio") is not None:
            effective_vwap_ratio = float(paper_entry_policy.get("max_price_vwap_ratio"))

        with _temporary_intraday_vwap_ratio(effective_vwap_ratio):
            if pending_retry:
                confirm = confirm_entry(
                    candidate,
                    analyzer=analyzer,
                    market_env=market_env,
                    strategy=strategy,
                    now=now,
                    realtime_map=realtime_map,
                )
                result["confirm"] = confirm
                result["ok"] = bool(confirm.get("ok"))
                result["reason"] = confirm.get("reason") or "entry_confirm rejected"
                result["verification"] = "paper_strong_entry_confirm" if paper_entry_policy else "pending_entry_confirm"
                metrics = confirm.get("metrics") or {}
                result["metrics"] = metrics
                if result["ok"]:
                    candidate["entry_confirm"] = confirm
                    candidate["entry_scenario"] = confirm.get("scenario")
                    candidate["entry_confirmations"] = confirm.get("confirmations")
                    if metrics.get("vwap"):
                        candidate["vwap"] = metrics.get("vwap")
                    if metrics.get("volume_ratio"):
                        candidate["volume_ratio"] = metrics.get("volume_ratio")
            else:
                is_window_gated = strategy in ("集合竞价", "早盘竞价首选") or watchlist_flow
                if is_window_gated:
                    is_morning_rush = now.hour == 9 and 30 <= now.minute <= 59
                    is_10am_check = now.hour == 10 and 0 <= now.minute <= 15
                    if not (is_morning_rush or is_10am_check):
                        result["ok"] = False
                        result["reason"] = "outside immediate entry verification window"
                        result["verification"] = "legacy_window_gate"
                        return result

                ok, reason = analyzer.verify_money_flow(candidate, weather)
                result["ok"] = bool(ok)
                result["reason"] = reason or "legacy money-flow rejected"
                result["verification"] = "legacy_window_gate" if is_window_gated else "legacy_money_flow"

    return result
