# -*- coding: utf-8 -*-
"""Shared pre-trade entry gate for automatic buy paths."""

from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any

from core.config import Config


DEFAULT_WEAK_GATE = {
    "enabled": True,
    "early_cutoff": "10:00",
    "early_block_strategies": ["集合竞价", "早盘竞价首选", "pre_market"],
    "weak_weather_blocklist": ["☁️多云", "⚠️暴雨"],
    "weak_risk_levels": ["medium", "high"],
    "weak_message_keywords": ["市场转弱", "跌破MA20", "MA20下方", "暴雨", "极端警报"],
    "min_win_rate_samples": 30,
    "block_insufficient_samples": True,
    "relay_keywords": ["昨日涨停", "接力", "高开", "涨停续涨", "CONTINUE_BOARD_CANDIDATE"],
    "weak_max_open_change": 2.5,
    "weak_max_change": 4.5,
    "weak_max_change_floor": 4.5,
    "allow_storm_pending_probe": True,
    "storm_pending_probe_max_per_run": 1,
    "force_pending_before_cutoff": True,
    "strong_regimes": ["strong_uptrend"],
}

DEFAULT_DATA_QUALITY_GATE = {
    "enabled": True,
    "bad_action": "BLOCK",
    "degraded_action": "CONFIRM_ONLY",
    "bad_missing_fields": ["price", "pre_close"],
    "degraded_missing_fields": ["vol", "amount"],
    "treat_realtime_fallback_as_degraded": True,
    "description": "L0 data quality gate before auto buy.",
}

ACTION_RANK = {
    "BLOCK": 0,
    "OBSERVE": 1,
    "PENDING": 2,
    "CONFIRM_ONLY": 2,
    "LOW_SIZE_CONFIRM": 2,
    "LOW_SIZE_AUTO": 3,
    "AUTO": 4,
    "ALLOW": 4,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged.get(key, {}), value)
        else:
            merged[key] = value
    return merged


def get_weak_market_gate_config() -> dict[str, Any]:
    cfg = {}
    try:
        if isinstance(Config.STRATEGY, dict):
            cfg = Config.STRATEGY.get("weak_market_entry_gate", {})
    except Exception:
        cfg = {}
    merged = _deep_merge(DEFAULT_WEAK_GATE, cfg if isinstance(cfg, dict) else {})
    return merged


def get_paper_weak_market_gate_experiment_config() -> dict[str, Any]:
    cfg = {}
    try:
        if isinstance(Config.STRATEGY, dict):
            cfg = Config.STRATEGY.get("paper_weak_market_gate_experiment", {})
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "allowed_accounts": cfg.get("allowed_accounts") or ["paper_main", "paper_watchlist"],
        "sample_floor_override": cfg.get("sample_floor_override", 25),
        "weak_chase_override_pct": cfg.get("weak_chase_override_pct", 5.0),
        "max_per_day": cfg.get("max_per_day"),
        "require_paper_experiment_tag": bool(cfg.get("require_paper_experiment_tag", False)),
    }


def get_data_quality_gate_config() -> dict[str, Any]:
    cfg = {}
    try:
        if isinstance(Config.STRATEGY, dict):
            cfg = Config.STRATEGY.get("data_quality_gate", {})
    except Exception:
        cfg = {}
    return _deep_merge(DEFAULT_DATA_QUALITY_GATE, cfg if isinstance(cfg, dict) else {})


def canonical_strategy_name(strategy: str | None, fallback: str | None = None) -> str:
    s = str(strategy or fallback or "").strip()
    if s in ("集合竞价", "早盘竞价首选", "pre_market"):
        return "集合竞价"
    if s in ("午盘精选",):
        return "午盘精选"
    if s in ("备选池买入触发",):
        return "备选池买入触发"
    if s in ("冷启动",):
        return "冷启动"
    if s in ("龙头跟踪",):
        return "龙头跟踪"
    if s in ("技术突破",):
        return "技术突破"
    return s


def canonical_strategy(strategy: str | None, fallback: str | None = None) -> str:
    """Backward-compatible alias used by older call sites."""
    return canonical_strategy_name(strategy, fallback)


def _normalize_action(action: Any, default: str = "AUTO") -> str:
    s = str(action or default or "AUTO").strip().upper()
    aliases = {
        "ALLOW": "AUTO",
        "DENY": "BLOCK",
        "CONFIRM": "CONFIRM_ONLY",
        "LOW_SIZE": "LOW_SIZE_AUTO",
    }
    return aliases.get(s, s)


def _merge_actions(current: str, proposed: str) -> str:
    cur = _normalize_action(current, "AUTO")
    prop = _normalize_action(proposed, "AUTO")
    return prop if ACTION_RANK.get(prop, 4) < ACTION_RANK.get(cur, 4) else cur


def get_strategy_permission_action(market_env: dict[str, Any] | None, strategy: str | None, *, mode: str | None = None) -> tuple[str, str]:
    """Return matrix action for regime × strategy.

    Actions: AUTO, LOW_SIZE_AUTO, LOW_SIZE_CONFIRM, CONFIRM_ONLY/PENDING, OBSERVE, BLOCK.
    """
    try:
        matrix = Config.STRATEGY.get("strategy_permission_matrix", {}) if isinstance(Config.STRATEGY, dict) else {}
    except Exception:
        matrix = {}
    if not isinstance(matrix, dict):
        return "AUTO", "matrix missing"

    regime = str((market_env or {}).get("regime") or "").strip() or "normal_uptrend"
    row = matrix.get(regime) or matrix.get("default") or {}
    if not isinstance(row, dict):
        return "AUTO", f"regime={regime} no row"

    canon = canonical_strategy_name(strategy, mode)
    action = row.get(canon)
    reason_key = canon
    if action is None and strategy:
        action = row.get(str(strategy))
        reason_key = str(strategy)
    if action is None:
        action = row.get("*")
        reason_key = "*"
    if action is None:
        return "AUTO", f"regime={regime} strategy={canon} default AUTO"
    action = _normalize_action(action, "AUTO")
    return action, f"permission_matrix regime={regime} key={reason_key} action={action}"


def is_immediate_execution_allowed(action: str) -> bool:
    return _normalize_action(action) in {"AUTO", "LOW_SIZE_AUTO"}


def is_pending_allowed(action: str) -> bool:
    return _normalize_action(action) in {"AUTO", "LOW_SIZE_AUTO", "PENDING", "CONFIRM_ONLY", "LOW_SIZE_CONFIRM"}


def is_confirm_action(action: str) -> bool:
    return _normalize_action(action) in {"PENDING", "CONFIRM_ONLY", "LOW_SIZE_CONFIRM"}


def position_multiplier_for_action(action: str) -> float:
    action = _normalize_action(action)
    if action in {"LOW_SIZE_AUTO", "LOW_SIZE_CONFIRM"}:
        return 0.3
    if action in {"BLOCK", "OBSERVE"}:
        return 0.0
    return 1.0


def _parse_hhmm(value: str, default: time = time(10, 0)) -> time:
    try:
        hh, mm = str(value or "").split(":", 1)
        return time(int(hh), int(mm))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _paper_weak_experiment_active(candidate: dict[str, Any], account: str | None) -> tuple[bool, dict[str, Any]]:
    cfg = get_paper_weak_market_gate_experiment_config()
    acct = str(account or candidate.get("target_account") or candidate.get("_target_account") or "").strip()
    allowed_accounts = {str(x).strip() for x in (cfg.get("allowed_accounts") or [])}
    has_tag = any(
        bool(candidate.get(key))
        for key in ("paper_experiment", "paper_strong_entry", "paper_executable_pool")
    )
    active = (
        bool(cfg.get("enabled"))
        and acct in allowed_accounts
        and (has_tag or not bool(cfg.get("require_paper_experiment_tag", True)))
    )
    return active, {
        "enabled": bool(cfg.get("enabled")),
        "account": acct,
        "allowed_accounts": sorted(allowed_accounts),
        "has_paper_experiment_tag": bool(has_tag),
        "sample_floor_override": cfg.get("sample_floor_override"),
        "weak_chase_override_pct": cfg.get("weak_chase_override_pct"),
        "max_per_day": cfg.get("max_per_day"),
        "active": bool(active),
    }


def evaluate_candidate_data_quality(
    candidate: dict[str, Any] | None,
    *,
    realtime_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = get_data_quality_gate_config()
    cand = candidate or {}
    reasons: list[str] = []
    warnings: list[str] = []

    if not cand:
        reasons.append("candidate_empty")

    values = {
        "price": cand.get("price") or cand.get("close"),
        "pre_close": cand.get("pre_close"),
        "vol": cand.get("vol") or cand.get("vol_lots") or cand.get("vol_shares"),
        "amount": cand.get("amount") or cand.get("amount_yuan"),
    }

    for field in cfg.get("bad_missing_fields") or []:
        if _safe_float(values.get(field), 0.0) <= 0:
            reasons.append(f"{field}_invalid")

    for field in cfg.get("degraded_missing_fields") or []:
        if _safe_float(values.get(field), 0.0) <= 0:
            warnings.append(f"{field}_missing")

    dq = {}
    raw_dq = cand.get("_data_quality")
    if isinstance(raw_dq, dict):
        dq = raw_dq
    elif isinstance(realtime_map, dict) and isinstance(realtime_map.get("_data_quality"), dict):
        dq = realtime_map.get("_data_quality") or {}

    if bool(cfg.get("treat_realtime_fallback_as_degraded", True)):
        if dq.get("fallback_used"):
            warnings.append("realtime_fallback_used")
        if str(dq.get("source") or "").lower() == "fallback":
            warnings.append("data_source_fallback")

    if reasons:
        quality = "BAD"
        action = _normalize_action(cfg.get("bad_action"), "BLOCK")
    elif warnings:
        quality = "DEGRADED"
        action = _normalize_action(cfg.get("degraded_action"), "CONFIRM_ONLY")
    else:
        quality = "GOOD"
        action = "AUTO"

    return {
        "quality": quality,
        "action": action,
        "reasons": reasons,
        "warnings": warnings,
        "metrics": {
            "price": _safe_float(values.get("price"), 0.0),
            "pre_close": _safe_float(values.get("pre_close"), 0.0),
            "vol": _safe_float(values.get("vol"), 0.0),
            "amount": _safe_float(values.get("amount"), 0.0),
            "source": dq.get("source"),
            "fallback_used": bool(dq.get("fallback_used", False)),
            "note": dq.get("note"),
        },
    }


def _normalize_weather(weather: Any) -> str:
    try:
        from core.utils import normalize_weather

        return normalize_weather(weather)
    except Exception:
        return str(weather or "")


def _extract_candidate_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("reason", "zt_tag", "first_board_tag", "board_context", "strategy", "tag"):
        value = candidate.get(key)
        if value:
            parts.append(str(value))

    for key in ("tags", "signal_tags", "tags_json", "entry_tags_json"):
        value = candidate.get(key)
        if not value:
            continue
        try:
            if isinstance(value, str):
                parsed = json.loads(value)
            else:
                parsed = value
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        parts.append(str(item.get("tag") or item.get("reason") or ""))
                    else:
                        parts.append(str(item))
            elif isinstance(parsed, dict):
                parts.append(json.dumps(parsed, ensure_ascii=False))
        except Exception:
            parts.append(str(value))
    return "|".join([p for p in parts if p])


def is_weak_market(market_env: dict[str, Any] | None, cfg: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    cfg = cfg or get_weak_market_gate_config()
    env = market_env or {}
    reasons: list[str] = []

    weather = _normalize_weather(env.get("weather"))
    weather_blocklist = set(cfg.get("weak_weather_blocklist") or [])
    if weather in weather_blocklist:
        reasons.append(f"weather={weather}")

    risk_level = str(env.get("risk_level") or "").lower()
    risk_levels = {str(x).lower() for x in (cfg.get("weak_risk_levels") or [])}
    if risk_level and risk_level in risk_levels:
        reasons.append(f"risk_level={risk_level}")

    if env.get("is_safe") is False:
        reasons.append("is_safe=False")

    message = str(env.get("message") or env.get("desc") or "")
    for keyword in cfg.get("weak_message_keywords") or []:
        if str(keyword) and str(keyword) in message:
            reasons.append(f"message_contains={keyword}")
            break

    return bool(reasons), reasons


def candidate_has_relay_risk(candidate: dict[str, Any], cfg: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    cfg = cfg or get_weak_market_gate_config()
    reasons: list[str] = []

    if bool(candidate.get("prev_limit_present")):
        reasons.append("prev_limit_present=True")
    if bool(candidate.get("is_continue_board_candidate")):
        reasons.append("is_continue_board_candidate=True")

    prev_limit_times = int(_safe_float(candidate.get("prev_limit_times"), 0))
    if prev_limit_times > 0:
        reasons.append(f"prev_limit_times={prev_limit_times}")

    board_context = str(candidate.get("board_context") or "")
    if board_context in ("continue_board", "接力候选", "接力观察", "接力规则"):
        reasons.append(f"board_context={board_context}")

    text = _extract_candidate_text(candidate)
    for keyword in cfg.get("relay_keywords") or []:
        if str(keyword) and str(keyword) in text:
            reasons.append(f"tag_contains={keyword}")
            break

    return bool(reasons), reasons


def build_pre_trade_risk_tags(gate_result: dict[str, Any]) -> list[dict[str, Any]]:
    tags: list[dict[str, Any]] = []
    if not isinstance(gate_result, dict):
        return tags

    for tag in gate_result.get("tags") or []:
        tags.append({"tag": str(tag), "weight": -1, "reason": "pre_trade_gate"})
    for reason in gate_result.get("reasons") or []:
        tags.append({"tag": "PRE_TRADE_GATE_REASON", "weight": -1, "reason": str(reason)})
    return tags


def _parse_tag_payload(payload: Any) -> list[Any]:
    tags: list[Any] = []
    if payload:
        try:
            parsed = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(parsed, list):
                tags.extend(parsed)
            elif parsed:
                tags.append(parsed)
        except Exception:
            tags.append({"tag": "LEGACY_TAGS_JSON", "weight": 0, "reason": str(payload)[:200]})
    return tags


def merge_signal_tags_json(existing_json: Any, gate_result: dict[str, Any] | None) -> str | None:
    tags = _parse_tag_payload(existing_json)

    tags.extend(build_pre_trade_risk_tags(gate_result or {}))
    if not tags:
        return existing_json
    return json.dumps(tags, ensure_ascii=False)


def merge_entry_confirm_tags_json(existing_json: Any, confirm_result: dict[str, Any] | None) -> str | None:
    tags = _parse_tag_payload(existing_json)

    confirm = confirm_result or {}
    scenario = confirm.get("scenario")
    if scenario:
        tags.append({"tag": f"ENTRY_SCENARIO_{scenario}", "weight": 1, "reason": confirm.get("reason") or ""})
    for item in confirm.get("confirmations") or []:
        tags.append({"tag": f"ENTRY_CONFIRM_{item}", "weight": 1, "reason": "entry_confirm"})
    for item in confirm.get("warnings") or []:
        tags.append({"tag": f"ENTRY_CONFIRM_WARN_{item}", "weight": -1, "reason": "entry_confirm"})
    dq = confirm.get("data_quality")
    if dq and dq != "GOOD":
        tags.append({"tag": f"DATA_QUALITY_{dq}", "weight": -1, "reason": "entry_confirm"})

    if not tags:
        return existing_json
    return json.dumps(tags, ensure_ascii=False)


def evaluate_pre_trade_gate(
    candidate: dict[str, Any],
    *,
    market_env: dict[str, Any] | None,
    strategy: str | None,
    account: str | None = None,
    now: datetime | None = None,
    mode: str | None = None,
    win_rate_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = get_weak_market_gate_config()
    now = now or datetime.now()
    canon_strategy = canonical_strategy_name(strategy or candidate.get("strategy"), mode)

    result = {
        "allow": True,
        "action": "AUTO",
        "reasons": [],
        "tags": [],
        "metrics": {},
        "strategy": canon_strategy,
        "position_multiplier": 1.0,
        "allow_pending": True,
        "allow_execute_buy": True,
        "required_confirmations": [],
    }
    if not bool(cfg.get("enabled", True)):
        return result

    dq_cfg = get_data_quality_gate_config()
    if bool(dq_cfg.get("enabled", True)):
        data_quality = evaluate_candidate_data_quality(candidate)
        result["data_quality"] = data_quality
        result["metrics"]["data_quality"] = data_quality
        dq_action = _normalize_action(data_quality.get("action"), "AUTO")
        if data_quality.get("quality") == "BAD":
            result["action"] = _merge_actions(result.get("action", "AUTO"), dq_action)
            result["allow"] = False
            result["allow_execute_buy"] = False
            result["allow_pending"] = False
            result["reasons"].append(f"数据质量BAD: {','.join(data_quality.get('reasons') or [])}")
            result["tags"].append("DATA_QUALITY_BAD")
            result["reason"] = "；".join(result["reasons"])
            result["risk_tags"] = result["tags"]
            result["market_weak"] = False
            result["weak_reasons"] = []
            return result
        elif data_quality.get("quality") == "DEGRADED":
            result["action"] = _merge_actions(result.get("action", "AUTO"), dq_action)
            result["allow_execute_buy"] = False
            result["allow_pending"] = True
            result["reasons"].append(f"数据质量DEGRADED: {','.join(data_quality.get('warnings') or [])}")
            result["tags"].append("DATA_QUALITY_DEGRADED")
            result["required_confirmations"].append("data_quality_recheck")

    weak, weak_reasons = is_weak_market(market_env, cfg)
    matrix_action, matrix_reason = get_strategy_permission_action(market_env, canon_strategy, mode=mode)
    weather = _normalize_weather((market_env or {}).get("weather"))
    open_change = _safe_float(candidate.get("open_change"), 0.0)
    change = _safe_float(candidate.get("change", candidate.get("pct_chg")), 0.0)
    win_samples = int(_safe_float((win_rate_stats or {}).get("cnt"), -1))
    win_rate = _safe_float((win_rate_stats or {}).get("win_rate"), 0.0)
    paper_weak_active, paper_weak_metrics = _paper_weak_experiment_active(candidate, account)

    relay_risk, relay_reasons = candidate_has_relay_risk(candidate, cfg)

    result["metrics"] = {
        "weak_market": bool(weak),
        "weather": weather,
        "risk_level": (market_env or {}).get("risk_level"),
        "message": (market_env or {}).get("message"),
        "open_change": open_change,
        "change": change,
        "win_rate_samples": win_samples if win_samples >= 0 else None,
        "win_rate": win_rate if win_samples >= 0 else None,
        "relay_risk": bool(relay_risk),
        "weak_reasons": weak_reasons,
        "relay_reasons": relay_reasons,
        "permission_action": matrix_action,
        "permission_reason": matrix_reason,
        "account": paper_weak_metrics.get("account"),
        "paper_weak_market_gate_experiment": paper_weak_metrics,
    }

    # Strategy permission matrix is evaluated for all regimes, not just weak markets.
    matrix_action = _normalize_action(matrix_action, "AUTO")
    if matrix_action != "AUTO":
        result["action"] = _merge_actions(result.get("action", "AUTO"), matrix_action)
        result["reasons"].append(matrix_reason)
        result["tags"].append(f"PERMISSION_{matrix_action}")
        if matrix_action == "BLOCK":
            result["allow"] = False
        elif matrix_action == "OBSERVE":
            result["allow_execute_buy"] = False
            result["required_confirmations"].append("observe_only")
        elif matrix_action in {"PENDING", "CONFIRM_ONLY", "LOW_SIZE_CONFIRM"}:
            result["allow_execute_buy"] = False
            result["allow_pending"] = True
            result["required_confirmations"].append("dynamic_entry_confirm")
        if matrix_action in {"LOW_SIZE_AUTO", "LOW_SIZE_CONFIRM"}:
            result["position_multiplier"] = min(result.get("position_multiplier", 1.0), position_multiplier_for_action(matrix_action))

    if not weak:
        result["position_multiplier"] = min(result.get("position_multiplier", 1.0), position_multiplier_for_action(result.get("action", "AUTO")))
        result["allow_pending"] = bool(result.get("allow_pending", True)) and result.get("action") not in {"BLOCK", "OBSERVE"}
        result["allow_execute_buy"] = bool(result.get("allow_execute_buy", True)) and result.get("action") in {"AUTO", "LOW_SIZE_AUTO"}
        if result.get("action") in {"BLOCK", "OBSERVE"}:
            result["allow"] = False
        result["reason"] = "；".join(result["reasons"])
        result["risk_tags"] = result["tags"]
        result["market_weak"] = False
        result["weak_reasons"] = []
        return result

    result["tags"].append("WEAK_MARKET_ENTRY")
    cutoff = _parse_hhmm(str(cfg.get("early_cutoff", "10:00")))
    early_strategies = {canonical_strategy_name(x) for x in (cfg.get("early_block_strategies") or [])}
    if now.time() < cutoff and canon_strategy in early_strategies:
        result["reasons"].append(
            f"弱市早盘禁止自动买入: strategy={canon_strategy} time={now.strftime('%H:%M')} < {cutoff.strftime('%H:%M')}"
        )
        result["tags"].append("WEAK_MARKET_MORNING_BLOCK")

    min_samples = int(_safe_float(cfg.get("min_win_rate_samples"), 30))
    effective_min_samples = min_samples
    if paper_weak_active:
        sample_override = int(_safe_float(paper_weak_metrics.get("sample_floor_override"), min_samples))
        if 0 < sample_override < min_samples:
            effective_min_samples = sample_override
            result["tags"].append("PAPER_WEAK_SAMPLE_FLOOR_EXPERIMENT")
            result["required_confirmations"].append("paper_weak_sample_floor_review")
    result["metrics"]["effective_min_win_rate_samples"] = effective_min_samples
    if bool(cfg.get("block_insufficient_samples", True)) and 0 <= win_samples < effective_min_samples:
        result["reasons"].append(f"弱市样本不足禁止自动买入: samples={win_samples} < {effective_min_samples}")
        result["tags"].append("WEAK_MARKET_INSUFFICIENT_SAMPLES")
    elif paper_weak_active and 0 <= win_samples < min_samples:
        result["reasons"].append(f"paper弱市样本门槛实验: samples={win_samples} < {min_samples} 且 >= {effective_min_samples}")
        result["tags"].append("PAPER_WEAK_SAMPLE_FLOOR_USED")

    if relay_risk:
        result["reasons"].append(f"弱市接力/高开标签禁止自动买入: {';'.join(relay_reasons[:4])}")
        result["tags"].append("WEAK_MARKET_RELAY_BLOCK")

    max_open_change = _safe_float(cfg.get("weak_max_open_change"), 3.0)
    max_change = _safe_float(cfg.get("weak_max_change"), 3.0)
    effective_max_change = max_change
    if paper_weak_active:
        chase_override = _safe_float(paper_weak_metrics.get("weak_chase_override_pct"), max_change)
        if chase_override > max_change:
            effective_max_change = chase_override
            result["tags"].append("PAPER_WEAK_CHASE_BAND_EXPERIMENT")
            result["required_confirmations"].append("paper_weak_chase_band_review")
    result["metrics"]["effective_weak_max_change"] = effective_max_change
    if open_change > max_open_change:
        result["reasons"].append(f"弱市高开过大禁止自动买入: open_change={open_change:.2f}% > {max_open_change:.2f}%")
        result["tags"].append("WEAK_MARKET_GAP_BLOCK")
    if change > effective_max_change:
        result["reasons"].append(f"弱市当前涨幅过大禁止自动买入: change={change:.2f}% > {effective_max_change:.2f}%")
        result["tags"].append("WEAK_MARKET_CHASE_BLOCK")
    elif paper_weak_active and change > max_change:
        result["reasons"].append(f"paper弱市追高线实验: change={change:.2f}% > {max_change:.2f}% 且 <= {effective_max_change:.2f}%")
        result["tags"].append("PAPER_WEAK_CHASE_BAND_USED")

    if result["reasons"]:
        if result.get("allow_execute_buy") is False and result.get("action") != "OBSERVE":
            result["action"] = _merge_actions(result.get("action", "AUTO"), "CONFIRM_ONLY")
        result["allow"] = bool(result.get("allow_execute_buy", True)) and result.get("action") not in {"BLOCK", "OBSERVE", "CONFIRM_ONLY", "PENDING", "LOW_SIZE_CONFIRM"}

    result["position_multiplier"] = min(result.get("position_multiplier", 1.0), position_multiplier_for_action(result.get("action", "AUTO")))
    action = _normalize_action(result.get("action"), "AUTO")
    result["allow_pending"] = bool(result.get("allow_pending", True)) and action not in {"BLOCK", "OBSERVE"}
    result["allow_execute_buy"] = bool(result.get("allow_execute_buy", True)) and action in {"AUTO", "LOW_SIZE_AUTO"}
    if not result["allow_execute_buy"] and not is_confirm_action(action):
        result["allow"] = False
    elif is_confirm_action(action):
        result["allow"] = True

    result["reason"] = "；".join(result["reasons"])
    result["risk_tags"] = result["tags"]
    result["market_weak"] = bool(weak)
    result["weak_reasons"] = weak_reasons
    return result


def build_gate_event_params(
    *,
    candidate: dict[str, Any] | None,
    gate: dict[str, Any],
    market_env: dict[str, Any] | None,
    win_rate_samples: int | None = None,
) -> dict[str, Any]:
    cand = candidate or {}
    metrics = gate.get("metrics") or {}
    return {
        "strategy": gate.get("strategy") or canonical_strategy_name(cand.get("strategy")),
        "account": cand.get("target_account") or cand.get("_target_account"),
        "action": gate.get("action"),
        "risk_tags": gate.get("risk_tags") or gate.get("tags") or [],
        "reason": gate.get("reason") or "；".join(gate.get("reasons") or []),
        "market_weak": bool(gate.get("market_weak") or metrics.get("weak_market")),
        "weak_reasons": gate.get("weak_reasons") or metrics.get("weak_reasons") or [],
        "weather": (market_env or {}).get("weather"),
        "risk_level": (market_env or {}).get("risk_level"),
        "market_message": (market_env or {}).get("message"),
        "win_rate_samples": win_rate_samples if win_rate_samples is not None else metrics.get("win_rate_samples"),
        "win_rate": metrics.get("win_rate"),
        "open_change": cand.get("open_change"),
        "change": cand.get("change") or cand.get("pct_chg"),
        "prev_limit_present": cand.get("prev_limit_present"),
        "is_continue_board_candidate": cand.get("is_continue_board_candidate"),
        "board_context": cand.get("board_context"),
        "zt_tag": cand.get("zt_tag"),
        "price": cand.get("price"),
        "vwap": cand.get("vwap"),
        "volume_ratio": cand.get("volume_ratio"),
        "data_quality": gate.get("data_quality"),
        "paper_weak_market_gate_experiment": metrics.get("paper_weak_market_gate_experiment"),
        "position_multiplier": gate.get("position_multiplier"),
        "allow_pending": gate.get("allow_pending"),
        "allow_execute_buy": gate.get("allow_execute_buy"),
        "required_confirmations": gate.get("required_confirmations"),
    }
