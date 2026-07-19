# -*- coding: utf-8 -*-
"""Factor/tag snapshot engine.

This module provides a lightweight, additive "common language" for:
- selections (strategy_selection)
- factor snapshots (factor_snapshot)
- later attribution/audit/evolution stages

Design principles:
- Never require extra data to be present; degrade gracefully.
- Only add fields; callers can ignore them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class FactorSnapshot:
    trade_date: str
    strategy: str
    analysis_cycle: str
    code: str
    ts_code: str
    name: str
    snapshot_version: str
    score_total: float
    factors: Dict[str, Any]
    tags: List[Dict[str, Any]]
    data_quality: str = "unknown"


class FactorEngine:
    """Builds factor/tags/score snapshots from already-available candidate fields."""

    SNAPSHOT_VERSION = "v1"

    @staticmethod
    def _normalize_trade_date(trade_date: Optional[str]) -> str:
        if not trade_date:
            return datetime.now().strftime("%Y-%m-%d")
        s = str(trade_date)
        # support YYYYMMDD
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return s

    @staticmethod
    def _safe_float(x: Any, default: float = 0.0) -> float:
        try:
            if x is None:
                return default
            return float(x)
        except Exception:
            return default

    def build_snapshot(
        self,
        candidate: Dict[str, Any],
        *,
        trade_date: Optional[str],
        strategy: str,
        analysis_cycle: str,
        market_env: Optional[Dict[str, Any]] = None,
    ) -> FactorSnapshot:
        td = self._normalize_trade_date(trade_date)

        code = (candidate.get("code") or "")
        ts_code = (candidate.get("ts_code") or "")
        if not code and ts_code:
            code = ts_code.split(".")[0]
        if not ts_code and code:
            # best-effort; many call sites use the same convention
            ts_code = f"{code}.SH" if str(code).startswith("6") else f"{code}.SZ"

        name = candidate.get("name") or ""

        # core numeric fields
        change = self._safe_float(candidate.get("change") or candidate.get("pct_chg") or 0.0)
        open_change = self._safe_float(candidate.get("open_change") or 0.0)
        turnover = self._safe_float(candidate.get("turnover") or candidate.get("turnover_rate") or 0.0)
        amount = self._safe_float(candidate.get("amount") or 0.0)
        vol_ratio = self._safe_float(candidate.get("volume_ratio") or 0.0)
        sector_bonus = self._safe_float(candidate.get("sector_bonus") or 0.0)
        score = self._safe_float(candidate.get("score") or 0.0)

        weather = None
        risk_level = None
        if isinstance(market_env, dict):
            weather = market_env.get("weather")
            risk_level = market_env.get("risk_level")

        board_context = candidate.get("board_context") or "unknown"
        prev_limit_times = candidate.get("prev_limit_times")
        is_first_board_candidate = bool(candidate.get("is_first_board_candidate", False))
        is_continue_board_candidate = bool(candidate.get("is_continue_board_candidate", False))
        first_board_bonus = self._safe_float(candidate.get("first_board_bonus") or 0.0)
        first_limit_score_model = self._safe_float(candidate.get("first_limit_score_model") or 0.0)
        breakout_confirm_score_model = self._safe_float(candidate.get("breakout_confirm_score_model") or 0.0)
        breakout_false60_score_model = self._safe_float(candidate.get("breakout_false60_score_model") or 0.0)
        breakout_net60_score_model = self._safe_float(candidate.get("breakout_net60_score_model") or 0.0)
        cold_start_good_score = self._safe_float(candidate.get("cold_start_good_score") or 0.0)
        cold_start_profit_score = self._safe_float(candidate.get("cold_start_profit_score") or 0.0)
        cold_start_risk_score = self._safe_float(candidate.get("cold_start_risk_score") or 0.0)
        cold_start_score_10m = self._safe_float(candidate.get("cold_start_score_10m") or 0.0)
        cold_start_observe_score = self._safe_float(candidate.get("cold_start_observe_score") or 0.0)
        cold_start_score_60m = self._safe_float(candidate.get("cold_start_score_60m") or 0.0)
        recommended_entry_delay_min = candidate.get("recommended_entry_delay_min")
        combo_score_product = self._safe_float(candidate.get("combo_score_product") or 0.0)
        first_limit_gate_pass = bool(candidate.get("first_limit_gate_pass", False))
        breakout_gate_pass = bool(candidate.get("breakout_gate_pass", False))
        breakout_false60_veto_pass = bool(candidate.get("breakout_false60_veto_pass", False))
        breakout_net60_gate_pass = bool(candidate.get("breakout_net60_gate_pass", False))
        breakout_high_quality_gate_pass = bool(candidate.get("breakout_high_quality_gate_pass", False))
        combo_gate_pass = bool(candidate.get("combo_gate_pass", False))

        # score_total: keep compatible with any existing 'score' usage
        score_total = score + sector_bonus

        factors: Dict[str, Any] = {
            "change": change,
            "open_change": open_change,
            "turnover": turnover,
            "amount": amount,
            "volume_ratio": vol_ratio,
            "sector_bonus": sector_bonus,
            "weather": weather,
            "risk_level": risk_level,
            "board_context": board_context,
            "prev_limit_times": prev_limit_times,
            "is_first_board_candidate": is_first_board_candidate,
            "is_continue_board_candidate": is_continue_board_candidate,
            "first_board_bonus": first_board_bonus,
            "first_limit_score_model": first_limit_score_model,
            "breakout_confirm_score_model": breakout_confirm_score_model,
            "breakout_false60_score_model": breakout_false60_score_model,
            "breakout_net60_score_model": breakout_net60_score_model,
            "cold_start_good_score": cold_start_good_score,
            "cold_start_profit_score": cold_start_profit_score,
            "cold_start_risk_score": cold_start_risk_score,
            "cold_start_score_10m": cold_start_score_10m,
            "cold_start_observe_score": cold_start_observe_score,
            "cold_start_score_60m": cold_start_score_60m,
            "cold_start_early_absorb": bool(candidate.get("cold_start_early_absorb", False)),
            "cold_start_delayed_confirm": bool(candidate.get("cold_start_delayed_confirm", False)),
            "recommended_entry_delay_min": recommended_entry_delay_min,
            "combo_score_product": combo_score_product,
            "first_limit_gate_pass": first_limit_gate_pass,
            "breakout_gate_pass": breakout_gate_pass,
            "breakout_false60_veto_pass": breakout_false60_veto_pass,
            "breakout_net60_gate_pass": breakout_net60_gate_pass,
            "breakout_high_quality_gate_pass": breakout_high_quality_gate_pass,
            "combo_gate_pass": combo_gate_pass,
        }

        tags: List[Dict[str, Any]] = []

        # market-level tags
        if weather:
            tags.append({"tag": f"WEATHER_{weather}", "weight": 0, "reason": "market weather"})
        if risk_level:
            tags.append({"tag": f"RISK_{risk_level}", "weight": 0, "reason": "market risk level"})

        # candidate-level tags
        if open_change >= 4.0:
            tags.append({"tag": "GAP_UP_STRONG", "weight": 1, "reason": f"open_change={open_change:.1f}%"})
        if change >= 6.0:
            tags.append({"tag": "MOMENTUM_STRONG", "weight": 1, "reason": f"change={change:.1f}%"})
        if turnover >= 15.0:
            tags.append({"tag": "TURNOVER_HIGH", "weight": -1, "reason": f"turnover={turnover:.1f}%"})
        if vol_ratio >= 2.0:
            tags.append({"tag": "VOLUME_RATIO_OK", "weight": 1, "reason": f"volume_ratio={vol_ratio:.2f}"})
        if sector_bonus > 0:
            tags.append({"tag": "SECTOR_HOT_BONUS", "weight": 1, "reason": f"sector_bonus={sector_bonus:.0f}"})
        sector_state = candidate.get("sector_state")
        if sector_state:
            factors["sector_state"] = sector_state
            factors["sector_rank"] = candidate.get("sector_rank")
            tags.append({"tag": f"SECTOR_{sector_state}", "weight": 0, "reason": f"rank={candidate.get('sector_rank') or '-'}"})
        if candidate.get("is_sector_leader"):
            tags.append({"tag": "SECTOR_LEADER_CONFIRMED", "weight": 1, "reason": "行业内候选排名前列"})
        for tag in candidate.get("sector_rotation_tags") or []:
            if tag not in {"SECTOR_STRONG", "SECTOR_NEUTRAL", "SECTOR_WEAK", "SECTOR_UNKNOWN"}:
                tags.append({"tag": str(tag), "weight": 0, "reason": "sector_rotation"})
        if is_first_board_candidate:
            tags.append({"tag": "FIRST_BOARD_CANDIDATE", "weight": 1, "reason": f"prev_limit_times={prev_limit_times or 0}"})
        elif is_continue_board_candidate:
            tags.append({"tag": "CONTINUE_BOARD_CANDIDATE", "weight": 0, "reason": f"prev_limit_times={prev_limit_times or 0}"})
        first_board_tag = candidate.get("first_board_tag")
        if first_board_tag:
            tags.append({"tag": "FIRST_BOARD_OPEN_OK", "weight": 1, "reason": str(first_board_tag)[:120]})
        if first_limit_score_model > 0:
            tags.append({"tag": "FIRST_LIMIT_MODEL", "weight": 0, "reason": f"score={first_limit_score_model:.3f}"})
        if breakout_confirm_score_model > 0:
            tags.append({"tag": "BREAKOUT_CONFIRM_MODEL", "weight": 0, "reason": f"score={breakout_confirm_score_model:.3f}"})
        if breakout_false60_score_model > 0:
            tags.append({"tag": "BREAKOUT_FALSE60_MODEL", "weight": 0, "reason": f"risk={breakout_false60_score_model:.3f}"})
        if breakout_net60_score_model > 0:
            tags.append({"tag": "BREAKOUT_NET60_MODEL", "weight": 0, "reason": f"net={breakout_net60_score_model:.3f}"})
        if cold_start_good_score > 0:
            tags.append({"tag": "COLD_START_MODEL_GOOD_SCORE", "weight": 0, "reason": f"good={cold_start_good_score:.3f}"})
        if cold_start_profit_score > 0:
            tags.append({"tag": "COLD_START_PROFIT_SCORE", "weight": 0, "reason": f"profit={cold_start_profit_score:.3f}"})
        if cold_start_risk_score > 0:
            weight = -1 if cold_start_risk_score >= 0.65 else 0
            tags.append({"tag": "COLD_START_RISK_SCORE", "weight": weight, "reason": f"risk={cold_start_risk_score:.3f}"})
        if candidate.get("cold_start_early_absorb"):
            tags.append({"tag": "COLD_START_EARLY_ABSORB", "weight": 0, "reason": "observation-only absorb chain"})
        if candidate.get("cold_start_delayed_confirm"):
            tags.append({"tag": "COLD_START_DELAYED_CONFIRM", "weight": 0, "reason": "observation-only delayed confirmation"})
        existing_tag_names = {str(item.get("tag")) for item in tags if isinstance(item, dict)}
        for tag in candidate.get("cold_start_model_tags") or []:
            tag_name = str(tag)
            if tag_name not in existing_tag_names:
                tags.append({"tag": tag_name, "weight": 0, "reason": "cold-start observe model"})
                existing_tag_names.add(tag_name)
        if breakout_false60_veto_pass:
            tags.append({"tag": "BREAKOUT_FALSE60_VETO_PASS", "weight": 1, "reason": "false-break risk below threshold"})
        elif breakout_false60_score_model > 0:
            tags.append({"tag": "BREAKOUT_FALSE60_RISK_HIGH", "weight": -1, "reason": f"risk={breakout_false60_score_model:.3f}"})
        if breakout_net60_gate_pass:
            tags.append({"tag": "BREAKOUT_NET60_PASS", "weight": 1, "reason": f"net={breakout_net60_score_model:.3f}"})
        if breakout_high_quality_gate_pass:
            tags.append({"tag": "BREAKOUT_HIGH_QUALITY", "weight": 1, "reason": "net60 pass + false60 veto pass"})
        if recommended_entry_delay_min is not None:
            try:
                delay_i = int(recommended_entry_delay_min)
                tags.append({"tag": f"ENTRY_DELAY_{delay_i}M", "weight": 0, "reason": "model recommended entry delay"})
            except Exception:
                pass
        if combo_score_product > 0:
            tags.append({"tag": "FIRST_LIMIT_BREAKOUT_COMBO", "weight": 0, "reason": f"product={combo_score_product:.3f}"})
        if combo_gate_pass:
            tags.append({"tag": "FIRST_LIMIT_BREAKOUT_PASS", "weight": 1, "reason": "observe combo gate pass"})

        # 龙虎榜 tags (optional, only if caller already attached these fields)
        lhb_present = candidate.get('lhb_present_today')
        lhb_net = candidate.get('lhb_net_amount')
        if lhb_present:
            tags.append({"tag": "LHB_PRESENT", "weight": 1, "reason": "龙虎榜上榜"})
        if lhb_net is not None:
            try:
                lhb_net_f = float(lhb_net)
                if lhb_net_f > 0:
                    tags.append({"tag": "LHB_NET_BUY", "weight": 1, "reason": f"net_amount={lhb_net_f:.0f}"})
                elif lhb_net_f < 0:
                    tags.append({"tag": "LHB_NET_SELL", "weight": -1, "reason": f"net_amount={lhb_net_f:.0f}"})
            except Exception:
                pass

        # 概念/题材共振 tags (optional)
        concepts = candidate.get('concepts') or []
        if isinstance(concepts, str):
            concepts = [concepts]
        if isinstance(concepts, list) and concepts:
            # keep factors small; tags carry the human-readable meaning
            factors['concept_count'] = len(concepts)
            # record up to 3 head concepts for downstream audit/report readability
            factors['concept_head'] = concepts[:3]

            top_concept = concepts[0]
            tags.append({"tag": "CONCEPT_PRESENT", "weight": 0, "reason": f"概念={top_concept}"})

            # if upstream already computed heat, we can mark resonance
            concept_heat = candidate.get('concept_heat') or {}
            if isinstance(concept_heat, dict):
                heat_map = concept_heat.get('heat') if isinstance(concept_heat.get('heat'), dict) else concept_heat
                if isinstance(heat_map, dict):
                    cnt = heat_map.get(top_concept)
                    if cnt is not None:
                        try:
                            cnt_i = int(cnt)
                            factors['concept_heat_top'] = cnt_i
                            if cnt_i >= 3:
                                tags.append({"tag": "CONCEPT_RESONANCE", "weight": 1, "reason": f"{top_concept}当日出现{cnt_i}次"})
                        except Exception:
                            pass

        # 分时结构 tags (optional, computed from already-attached fields)
        vwap = candidate.get('vwap')
        price = candidate.get('price')
        if vwap is not None and price is not None:
            try:
                vwap_f = float(vwap)
                price_f = float(price)
                if vwap_f > 0:
                    ratio = price_f / vwap_f
                    factors['price_vwap_ratio'] = round(ratio, 4)

                    # Keep tag threshold aligned with strategy gate (if provided)
                    threshold = 1.03
                    try:
                        threshold = float(candidate.get('intraday_max_price_vwap_ratio') or threshold)
                    except Exception:
                        threshold = 1.03
                    factors['intraday_vwap_threshold'] = threshold

                    if ratio >= threshold:
                        tags.append({"tag": "INTRADAY_VWAP_EXTENDED", "weight": -1, "reason": f"price/vwap={ratio:.3f}>= {threshold:.3f}"})
                    elif ratio <= 1.005:
                        tags.append({"tag": "INTRADAY_NEAR_VWAP", "weight": 0, "reason": f"price/vwap={ratio:.3f}"})

                    # Observation-only structure tags (do not affect buy gate by default)
                    if ratio >= 1.02 and ratio < threshold:
                        tags.append({"tag": "INTRADAY_VWAP_STRETCHED", "weight": 0, "reason": f"price/vwap={ratio:.3f}"})
                    if ratio <= 0.99:
                        tags.append({"tag": "INTRADAY_BELOW_VWAP", "weight": 0, "reason": f"price/vwap={ratio:.3f}"})
            except Exception:
                pass

        vol_ratio2 = candidate.get('volume_ratio')
        if vol_ratio2 is not None:
            try:
                vr_f = float(vol_ratio2)
                if vr_f >= 3.0:
                    tags.append({"tag": "INTRADAY_VOLUME_BURST", "weight": 1, "reason": f"volume_ratio={vr_f:.2f}"})
            except Exception:
                pass

        if candidate.get("reason"):
            tags.append({"tag": "STRATEGY_REASON", "weight": 0, "reason": str(candidate.get("reason"))[:200]})

        # Data provider quality passthrough
        data_quality = candidate.get("data_quality") or "unknown"
        dq = candidate.get('_data_quality') if isinstance(candidate, dict) else None
        if data_quality == 'unknown' and isinstance(dq, dict):
            data_quality = dq.get('source') or 'unknown'

        return FactorSnapshot(
            trade_date=td,
            strategy=strategy,
            analysis_cycle=analysis_cycle,
            code=str(code),
            ts_code=str(ts_code),
            name=str(name),
            snapshot_version=self.SNAPSHOT_VERSION,
            score_total=float(score_total),
            factors=factors,
            tags=tags,
            data_quality=str(data_quality),
        )

    def build_snapshots(
        self,
        selections: List[Dict[str, Any]],
        *,
        trade_date: Optional[str],
        strategy: str,
        analysis_cycle: str,
        market_env: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        snapshots: List[Dict[str, Any]] = []
        for s in selections or []:
            snap = self.build_snapshot(
                s,
                trade_date=trade_date,
                strategy=strategy,
                analysis_cycle=analysis_cycle,
                market_env=market_env,
            )
            snapshots.append(
                {
                    "trade_date": snap.trade_date,
                    "strategy": snap.strategy,
                    "analysis_cycle": snap.analysis_cycle,
                    "code": snap.code,
                    "ts_code": snap.ts_code,
                    "name": snap.name,
                    "snapshot_version": snap.snapshot_version,
                    "score_total": snap.score_total,
                    "factors": snap.factors,
                    "tags": snap.tags,
                    "data_quality": snap.data_quality,
                }
            )
        return snapshots
