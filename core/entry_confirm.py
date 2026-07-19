# -*- coding: utf-8 -*-
"""Intraday entry confirmation for pending buy signals."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.config import Config


DEFAULT_ENTRY_CONFIRM = {
    "enabled": True,
    "data_quality_bad_action": "BLOCK",
    "data_quality_degraded_action": "CONFIRM_ONLY",
    "min_price": 0.01,
    "min_volume_ratio": 1.8,
    "max_price_vwap_ratio": 1.025,
    "min_price_vwap_ratio": 0.995,
    "max_distance_from_intraday_high_pct": 1.2,
    "max_upper_retrace_pct": 1.8,
    "opening_range": {
        "enabled": True,
        "end_time": "10:00",
        "min_breakout_pct": 0.15,
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


def get_entry_confirm_config() -> dict[str, Any]:
    try:
        cfg = Config.STRATEGY.get("entry_confirm", {}) if isinstance(Config.STRATEGY, dict) else {}
    except Exception:
        cfg = {}
    return _deep_merge(DEFAULT_ENTRY_CONFIRM, cfg if isinstance(cfg, dict) else {})


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return default


def _now_after_hhmm(now: datetime, hhmm: str) -> bool:
    hh, mm = _parse_hhmm(hhmm, (10, 0))
    return (now.hour > hh) or (now.hour == hh and now.minute >= mm)


def _quote_quality(candidate: dict[str, Any], realtime_map: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not candidate:
        return "BAD", ["candidate_empty"]

    price = _safe_float(candidate.get("price"), 0.0)
    if price <= 0:
        reasons.append("price_invalid")

    pre_close = _safe_float(candidate.get("pre_close"), 0.0)
    if pre_close <= 0:
        reasons.append("pre_close_missing")

    amount = _safe_float(candidate.get("amount_yuan", candidate.get("amount")), 0.0)
    vol = _safe_float(candidate.get("vol_shares", candidate.get("vol")), 0.0)
    if amount <= 0 or vol <= 0:
        reasons.append("volume_or_amount_missing")

    dq = {}
    if isinstance(realtime_map, dict):
        raw_dq = realtime_map.get("_data_quality")
        if isinstance(raw_dq, dict):
            dq = raw_dq
    if dq.get("fallback_used"):
        reasons.append("realtime_fallback_used")
    if str(dq.get("source") or "").lower() in {"fallback"}:
        reasons.append("realtime_source_fallback")

    if any(reason in reasons for reason in ("price_invalid", "pre_close_missing")):
        return "BAD", reasons
    if reasons:
        return "DEGRADED", reasons
    return "GOOD", []


def _compute_volume_ratio(analyzer: Any, candidate: dict[str, Any], ts_code: str) -> float:
    existing = _safe_float(candidate.get("volume_ratio"), 0.0)
    if existing > 0:
        return existing
    vol_lots = _safe_float(candidate.get("vol_lots", candidate.get("vol")), 0.0)
    if vol_lots <= 0:
        return 0.0
    try:
        return float(analyzer.calculate_volume_ratio(ts_code, vol_lots) or 0.0)
    except Exception:
        return 0.0


def _compute_vwap(candidate: dict[str, Any]) -> float:
    vwap = _safe_float(candidate.get("vwap"), 0.0)
    if vwap > 0:
        return vwap
    amount = _safe_float(candidate.get("amount_yuan", candidate.get("amount")), 0.0)
    vol_shares = _safe_float(candidate.get("vol_shares"), 0.0)
    if vol_shares <= 0:
        vol_lots = _safe_float(candidate.get("vol_lots", candidate.get("vol")), 0.0)
        vol_shares = vol_lots * 100.0
    if amount > 0 and vol_shares > 0:
        return amount / vol_shares
    return 0.0


def confirm_entry(
    candidate: dict[str, Any],
    *,
    analyzer: Any,
    market_env: dict[str, Any] | None = None,
    strategy: str | None = None,
    now: datetime | None = None,
    realtime_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured pending-entry confirmation result.

    This is stricter than the legacy verify_money_flow path and is intended for
    delayed/pending entries where we can wait for better intraday structure.
    """

    cfg = get_entry_confirm_config()
    now = now or datetime.now()
    if not bool(cfg.get("enabled", True)):
        return {
            "ok": True,
            "scenario": "disabled",
            "confirmations": ["entry_confirm_disabled"],
            "reason": "entry_confirm disabled",
            "data_quality": "GOOD",
        }

    candidate = candidate or {}
    ts_code = str(candidate.get("ts_code") or "")
    if not ts_code and candidate.get("code"):
        code = str(candidate.get("code"))
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    quality, quality_reasons = _quote_quality(candidate, realtime_map)
    if quality == "BAD":
        return {
            "ok": False,
            "scenario": "data_quality_bad",
            "confirmations": [],
            "data_quality": quality,
            "quality_reasons": quality_reasons,
            "reason": f"数据质量BAD: {','.join(quality_reasons)}",
        }

    price = _safe_float(candidate.get("price"), 0.0)
    pre_close = _safe_float(candidate.get("pre_close"), 0.0)
    high = _safe_float(candidate.get("high"), 0.0)
    open_price = _safe_float(candidate.get("open"), 0.0)
    change = _safe_float(candidate.get("change", candidate.get("pct_chg")), 0.0)

    confirmations: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "price": price,
        "pre_close": pre_close,
        "high": high,
        "change": change,
        "strategy": strategy or candidate.get("strategy"),
        "market_regime": (market_env or {}).get("regime"),
    }

    legacy_ok, legacy_reason = True, "legacy skipped"
    try:
        legacy_ok, legacy_reason = analyzer.verify_money_flow(candidate, (market_env or {}).get("weather", "☀️晴天"))
    except Exception as exc:
        legacy_ok, legacy_reason = False, f"legacy verify error: {exc}"
    if not legacy_ok:
        return {
            "ok": False,
            "scenario": "legacy_money_flow_reject",
            "confirmations": confirmations,
            "data_quality": quality,
            "quality_reasons": quality_reasons,
            "metrics": metrics,
            "reason": legacy_reason,
        }
    confirmations.append("legacy_money_flow_ok")

    volume_ratio = _compute_volume_ratio(analyzer, candidate, ts_code)
    metrics["volume_ratio"] = volume_ratio
    min_vr = _safe_float(cfg.get("min_volume_ratio"), 1.8)
    if volume_ratio > 0:
        if volume_ratio < min_vr:
            return {
                "ok": False,
                "scenario": "volume_not_confirmed",
                "confirmations": confirmations,
                "data_quality": quality,
                "quality_reasons": quality_reasons,
                "metrics": metrics,
                "reason": f"二次放量不足: volume_ratio={volume_ratio:.2f} < {min_vr:.2f}",
            }
        confirmations.append("volume_confirmed")
    else:
        warnings.append("volume_ratio_unavailable")

    vwap = _compute_vwap(candidate)
    candidate["vwap"] = vwap
    vwap_ratio = price / vwap if vwap > 0 else 0.0
    metrics["vwap"] = vwap
    metrics["vwap_ratio"] = vwap_ratio
    max_vwap_ratio = _safe_float(cfg.get("max_price_vwap_ratio"), 1.025)
    min_vwap_ratio = _safe_float(cfg.get("min_price_vwap_ratio"), 0.995)
    if vwap <= 0:
        warnings.append("vwap_unavailable")
    elif vwap_ratio > max_vwap_ratio:
        return {
            "ok": False,
            "scenario": "vwap_overextended",
            "confirmations": confirmations,
            "data_quality": quality,
            "quality_reasons": quality_reasons,
            "metrics": metrics,
            "reason": f"VWAP偏离过高: price/vwap={vwap_ratio:.3f} > {max_vwap_ratio:.3f}",
        }
    elif vwap_ratio < min_vwap_ratio:
        return {
            "ok": False,
            "scenario": "below_vwap",
            "confirmations": confirmations,
            "data_quality": quality,
            "quality_reasons": quality_reasons,
            "metrics": metrics,
            "reason": f"尚未站回VWAP: price/vwap={vwap_ratio:.3f} < {min_vwap_ratio:.3f}",
        }
    else:
        confirmations.append("above_vwap")

    if high > 0 and price > 0:
        distance_from_high = (price - high) / high * 100.0
        metrics["distance_from_intraday_high_pct"] = distance_from_high
        max_dist = _safe_float(cfg.get("max_distance_from_intraday_high_pct"), 1.2)
        max_retrace = _safe_float(cfg.get("max_upper_retrace_pct"), 1.8)
        if abs(distance_from_high) <= max_dist:
            confirmations.append("near_intraday_high")
        elif distance_from_high < -max_retrace:
            return {
                "ok": False,
                "scenario": "intraday_retrace",
                "confirmations": confirmations,
                "data_quality": quality,
                "quality_reasons": quality_reasons,
                "metrics": metrics,
                "reason": f"冲高回落过滤: 距日内高点 {distance_from_high:.2f}% < -{max_retrace:.2f}%",
            }
        else:
            confirmations.append("pullback_not_excessive")

    opening_cfg = cfg.get("opening_range") or {}
    if bool(opening_cfg.get("enabled", True)) and _now_after_hhmm(now, str(opening_cfg.get("end_time", "10:00"))):
        if high > 0 and pre_close > 0:
            high_chg = (high - pre_close) / pre_close * 100.0
            min_breakout = _safe_float(opening_cfg.get("min_breakout_pct"), 0.15)
            metrics["intraday_high_change_pct"] = high_chg
            if price >= high * (1.0 - min_breakout / 100.0):
                confirmations.append("opening_range_breakout")
            elif vwap > 0 and vwap_ratio >= 1.0:
                confirmations.append("vwap_pullback_hold")
            else:
                return {
                    "ok": False,
                    "scenario": "opening_range_not_confirmed",
                    "confirmations": confirmations,
                    "data_quality": quality,
                    "quality_reasons": quality_reasons,
                    "metrics": metrics,
                    "reason": "开盘区间突破/回踩确认不足",
                }

    scenario = "vwap_pullback_rebreak"
    if "opening_range_breakout" in confirmations:
        scenario = "opening_range_breakout"
    elif "above_vwap" in confirmations and "volume_confirmed" in confirmations:
        scenario = "above_vwap_volume_confirmed"

    if quality == "DEGRADED":
        warnings.extend(quality_reasons)

    return {
        "ok": True,
        "scenario": scenario,
        "confirmations": confirmations,
        "warnings": warnings,
        "data_quality": quality,
        "quality_reasons": quality_reasons,
        "vwap_ratio": vwap_ratio,
        "distance_from_intraday_high": metrics.get("distance_from_intraday_high_pct"),
        "metrics": metrics,
        "reason": "；".join(confirmations) if confirmations else "entry confirmed",
    }
