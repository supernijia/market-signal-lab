# -*- coding: utf-8 -*-
"""Unified position sizing for automatic buy paths."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.config import Config
from core.pre_trade_gate import position_multiplier_for_action


DEFAULT_POSITION_SIZER = {
    "enabled": True,
    "min_order_amount": 5000,
    "min_order_position_pct_floor": 0.08,
    "absolute_max_position_pct": 0.40,
    "base_position_pct": 0.20,
    "low_size_multiplier": 0.30,
    "market_regime_multiplier": {
        "strong_uptrend": 1.20,
        "normal_uptrend": 1.00,
        "range_market": 0.60,
        "weak_market": 0.35,
        "storm_market": 0.0,
    },
    "volatility_multiplier": {
        "low": 1.25,
        "medium": 1.00,
        "high": 0.50,
        "unknown": 0.80,
    },
    "max_positions_multiplier": {
        "near_limit": 0.50,
        "at_or_over_limit": 0.0,
    },
    "daily_loss": {
        "enabled": True,
        "warn_pct": -0.02,
        "hard_stop_pct": -0.04,
        "warn_multiplier": 0.50,
    },
    "consecutive_loss": {
        "enabled": True,
        "lookback": 5,
        "warn_count": 2,
        "hard_stop_count": 3,
        "warn_multiplier": 0.50,
    },
    "ensure_round_lot_when_cash_available": False,
    "account_overrides": {},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged.get(key, {}), value)
        else:
            merged[key] = value
    return merged


def get_position_sizer_config() -> dict[str, Any]:
    cfg = {}
    try:
        if isinstance(Config.STRATEGY, dict):
            cfg = Config.STRATEGY.get("position_sizer", {})
    except Exception:
        cfg = {}
    merged = _deep_merge(DEFAULT_POSITION_SIZER, cfg if isinstance(cfg, dict) else {})
    try:
        base = Config.RISK_MANAGEMENT.get("MAX_POSITION_PER_STOCK", merged.get("base_position_pct", 0.20))
        merged["base_position_pct"] = float(merged.get("base_position_pct", base) or base)
    except Exception:
        pass
    return merged


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


class PositionSizer:
    """Compute executable position size with explainable risk adjustments."""

    def __init__(self, portfolio_manager, data_provider=None, config: dict[str, Any] | None = None):
        self.portfolio = portfolio_manager
        self.provider = data_provider
        self.config = _deep_merge(get_position_sizer_config(), config or {})

    def _recent_trade_loss_stats(self, account: str, lookback: int = 5) -> dict[str, Any]:
        conn = self.portfolio._get_connection()
        if not conn:
            return {"consecutive_losses": 0, "recent_closed": 0}
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT type, amount, code, date
                    FROM transactions
                    WHERE account=%s
                      AND type IN ('BUY', 'SELL')
                    ORDER BY date DESC, id DESC
                    LIMIT %s
                    """,
                    (account, max(10, int(lookback or 5) * 4)),
                )
                txs = cursor.fetchall() or []

            buys: dict[str, list[float]] = {}
            closed = []
            for tx in reversed(txs):
                code = str(tx.get("code") or "")
                tx_type = str(tx.get("type") or "").upper()
                amount = _safe_float(tx.get("amount"), 0.0)
                if tx_type == "BUY":
                    buys.setdefault(code, []).append(abs(amount))
                elif tx_type == "SELL":
                    cost = buys.get(code, []).pop(0) if buys.get(code) else 0.0
                    if cost > 0:
                        closed.append(amount - cost)

            closed = closed[-int(lookback or 5):]
            consecutive = 0
            for pnl in reversed(closed):
                if pnl < 0:
                    consecutive += 1
                else:
                    break
            return {"consecutive_losses": consecutive, "recent_closed": len(closed)}
        except Exception:
            return {"consecutive_losses": 0, "recent_closed": 0}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _today_realized_pnl_pct(self, account: str, total_asset: float) -> float:
        if total_asset <= 0:
            return 0.0
        conn = self.portfolio._get_connection()
        if not conn:
            return 0.0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT SUM(amount) AS amount_sum
                    FROM transactions
                    WHERE account=%s
                      AND type='SELL'
                      AND DATE(date)=%s
                    """,
                    (account, today),
                )
                row = cursor.fetchone() or {}
                pnl_proxy = _safe_float(row.get("amount_sum"), 0.0)
            return pnl_proxy / total_asset
        except Exception:
            return 0.0
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def calculate(
        self,
        *,
        account: str,
        price: float,
        cash_available: float,
        positions: list[dict[str, Any]] | None = None,
        market_env: dict[str, Any] | None = None,
        strategy: str | None = None,
        pre_gate: dict[str, Any] | None = None,
        candidate: dict[str, Any] | None = None,
        ts_code: str | None = None,
        max_position_mult: float = 1.0,
        market_status: str = "",
        analyzer: Any = None,
    ) -> dict[str, Any]:
        base_cfg = self.config
        account_key = str(account or "")
        overrides = base_cfg.get("account_overrides") if isinstance(base_cfg.get("account_overrides"), dict) else {}
        account_override = {}
        if isinstance(overrides, dict):
            account_override = overrides.get(account_key) or {}
            if not account_override and account_key.startswith("paper_"):
                account_override = overrides.get("paper_*") or {}
        cfg = _deep_merge(base_cfg, account_override if isinstance(account_override, dict) else {})
        reasons: list[str] = []
        multipliers: dict[str, float] = {}

        price = _safe_float(price, 0.0)
        cash_available = _safe_float(cash_available, 0.0)
        positions = positions or []
        candidate = candidate or {}
        pre_gate = pre_gate or {}
        market_env = market_env or {}

        if price <= 0 or cash_available <= 0:
            return {
                "quantity": 0,
                "budget": 0.0,
                "position_pct": 0.0,
                "reasons": ["invalid price or cash"],
                "multipliers": multipliers,
            }

        base_pct = _safe_float(cfg.get("base_position_pct"), Config.RISK_MANAGEMENT.get("MAX_POSITION_PER_STOCK", 0.20))
        position_pct = base_pct
        reasons.append(f"base={base_pct:.2%}")

        action = str(pre_gate.get("action") or "AUTO")
        action_mult = position_multiplier_for_action(action)
        if action in {"LOW_SIZE_AUTO", "LOW_SIZE_CONFIRM"}:
            action_mult = min(action_mult, _safe_float(cfg.get("low_size_multiplier"), 0.30))
        multipliers["action"] = action_mult
        position_pct *= action_mult
        reasons.append(f"action={action} x{action_mult:.2f}")

        regime = str(market_env.get("regime") or "normal_uptrend")
        regime_mult = _safe_float((cfg.get("market_regime_multiplier") or {}).get(regime), 1.0)
        multipliers["regime"] = regime_mult
        position_pct *= regime_mult
        reasons.append(f"regime={regime} x{regime_mult:.2f}")

        volatility = "unknown"
        try:
            provider = analyzer.provider if analyzer is not None and getattr(analyzer, "provider", None) is not None else self.provider
            if provider and ts_code:
                atr = provider.get_atr(ts_code)
                if atr:
                    volatility = str(atr.get("volatility") or "unknown")
        except Exception:
            volatility = "unknown"
        vol_mult = _safe_float((cfg.get("volatility_multiplier") or {}).get(volatility), 1.0)
        multipliers["volatility"] = vol_mult
        position_pct *= vol_mult
        reasons.append(f"volatility={volatility} x{vol_mult:.2f}")

        external_mult = _safe_float(max_position_mult, 1.0)
        multipliers["market_adjustment"] = external_mult
        position_pct *= external_mult
        if external_mult != 1.0:
            reasons.append(f"market_adjustment x{external_mult:.2f}")

        if candidate.get("_reduce_position"):
            position_pct *= 0.5
            multipliers["candidate_reduce"] = 0.5
            reasons.append("candidate_reduce x0.50")

        if "超级牛市" in str(market_status or ""):
            position_pct *= 1.5
            multipliers["legacy_super_bull"] = 1.5
            reasons.append("legacy_super_bull x1.50")
        elif "震荡市" in str(market_status or ""):
            position_pct = min(0.10, position_pct)
            reasons.append("legacy_range_cap<=10%")

        max_total = _safe_int(Config.RISK_MANAGEMENT.get("MAX_TOTAL_POSITIONS", 5), 5)
        current_count = len([p for p in positions if str(p.get("account") or account) == account])
        if max_total > 0:
            if current_count >= max_total:
                position_pct = 0.0
                multipliers["position_count"] = 0.0
                reasons.append(f"position_count {current_count}/{max_total} hard_stop")
            elif current_count >= max_total - 1:
                near_mult = _safe_float((cfg.get("max_positions_multiplier") or {}).get("near_limit"), 0.5)
                position_pct *= near_mult
                multipliers["position_count"] = near_mult
                reasons.append(f"position_count {current_count}/{max_total} x{near_mult:.2f}")

        current_pos_value = sum(_safe_float(p.get("market_value"), 0.0) for p in positions if str(p.get("account") or account) == account)
        total_asset = cash_available + current_pos_value
        daily_loss_cfg = cfg.get("daily_loss") or {}
        if bool(daily_loss_cfg.get("enabled", True)):
            pnl_pct = self._today_realized_pnl_pct(account, total_asset)
            if pnl_pct <= _safe_float(daily_loss_cfg.get("hard_stop_pct"), -0.04):
                position_pct = 0.0
                multipliers["daily_loss"] = 0.0
                reasons.append(f"daily_loss={pnl_pct:.2%} hard_stop")
            elif pnl_pct <= _safe_float(daily_loss_cfg.get("warn_pct"), -0.02):
                warn_mult = _safe_float(daily_loss_cfg.get("warn_multiplier"), 0.5)
                position_pct *= warn_mult
                multipliers["daily_loss"] = warn_mult
                reasons.append(f"daily_loss={pnl_pct:.2%} x{warn_mult:.2f}")

        loss_cfg = cfg.get("consecutive_loss") or {}
        if bool(loss_cfg.get("enabled", True)):
            loss_stats = self._recent_trade_loss_stats(account, _safe_int(loss_cfg.get("lookback"), 5))
            consecutive = _safe_int(loss_stats.get("consecutive_losses"), 0)
            if consecutive >= _safe_int(loss_cfg.get("hard_stop_count"), 3):
                position_pct = 0.0
                multipliers["consecutive_loss"] = 0.0
                reasons.append(f"consecutive_losses={consecutive} hard_stop")
            elif consecutive >= _safe_int(loss_cfg.get("warn_count"), 2):
                warn_mult = _safe_float(loss_cfg.get("warn_multiplier"), 0.5)
                position_pct *= warn_mult
                multipliers["consecutive_loss"] = warn_mult
                reasons.append(f"consecutive_losses={consecutive} x{warn_mult:.2f}")

        abs_cap = _safe_float(cfg.get("absolute_max_position_pct"), 0.40)
        position_pct = max(0.0, min(abs_cap, position_pct))
        budget = cash_available * position_pct

        min_order = _safe_float(cfg.get("min_order_amount"), 5000.0)
        min_order_floor = _safe_float(cfg.get("min_order_position_pct_floor"), 0.08)
        lot_cost = price * 100
        ensure_round_lot = bool(cfg.get("ensure_round_lot_when_cash_available", False))
        if 0 < budget < min_order and cash_available >= min_order and position_pct >= min_order_floor:
            budget = min_order
            reasons.append(f"min_order={min_order:.0f}")
        elif 0 < budget < min_order and ensure_round_lot and cash_available >= lot_cost and position_pct >= min_order_floor:
            budget = lot_cost
            reasons.append(f"round_lot_min={lot_cost:.0f}")
        elif 0 < budget < min_order:
            reasons.append(f"below_min_order={budget:.0f}<{min_order:.0f}")
            budget = 0.0

        if ensure_round_lot and 0 < budget < lot_cost and cash_available >= lot_cost and position_pct >= min_order_floor:
            budget = lot_cost
            reasons.append(f"round_lot_min={lot_cost:.0f}")

        quantity = int(budget // (price * 100)) * 100
        if quantity <= 0:
            reasons.append("quantity<=0")

        return {
            "quantity": quantity,
            "budget": budget,
            "position_pct": position_pct,
            "amount": quantity * price,
            "base_position_pct": base_pct,
            "total_asset": total_asset,
            "cash_available": cash_available,
            "volatility": volatility,
            "reasons": reasons,
            "multipliers": multipliers,
            "strategy": strategy,
            "action": action,
            "regime": regime,
        }
