# -*- coding: utf-8 -*-
"""Sector rotation and sector-leader confirmation helpers."""

from __future__ import annotations

from typing import Any

from core.config import Config


DEFAULT_SECTOR_ROTATION = {
    "enabled": True,
    "strong_rank": 5,
    "weak_avg_change": -1.0,
    "strong_min_avg_change": 0.2,
    "strong_require_positive_flow": False,
    "weak_market_require_strong_sector": True,
    "weak_market_unknown_sector_action": "PENALTY",
    "leader_top_n": 3,
    "leader_score_bonus": 8,
    "weak_sector_penalty": -12,
    "unknown_sector_penalty": -4,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged.get(key, {}), value)
        else:
            merged[key] = value
    return merged


def get_sector_rotation_config() -> dict[str, Any]:
    cfg = {}
    try:
        if isinstance(Config.STRATEGY, dict):
            cfg = Config.STRATEGY.get("sector_rotation", {})
    except Exception:
        cfg = {}
    return _deep_merge(DEFAULT_SECTOR_ROTATION, cfg if isinstance(cfg, dict) else {})


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


class SectorRotation:
    """Classify sector strength and annotate candidates."""

    def __init__(self, industry_stats: list[dict[str, Any]] | None, config: dict[str, Any] | None = None):
        self.config = _deep_merge(get_sector_rotation_config(), config or {})
        self.industry_stats = [row for row in (industry_stats or []) if isinstance(row, dict)]
        self.by_industry = {str(row.get("industry") or ""): row for row in self.industry_stats}
        self.rank_by_industry = {
            str(row.get("industry") or ""): idx
            for idx, row in enumerate(self.industry_stats, 1)
            if row.get("industry")
        }

    def classify_sector(self, industry: str | None) -> dict[str, Any]:
        industry = str(industry or "").strip()
        row = self.by_industry.get(industry) or {}
        rank = self.rank_by_industry.get(industry, 0)
        avg_change = _safe_float(row.get("avg_change"), 0.0)
        net_flow = _safe_float(row.get("net_money_flow"), 0.0)
        strong_rank = int(self.config.get("strong_rank", 5) or 5)
        strong_min_avg = _safe_float(self.config.get("strong_min_avg_change"), 0.2)
        weak_avg = _safe_float(self.config.get("weak_avg_change"), -1.0)
        require_flow = bool(self.config.get("strong_require_positive_flow", False))

        if not industry or not row:
            state = "UNKNOWN"
        elif avg_change <= weak_avg:
            state = "WEAK"
        elif rank > 0 and rank <= strong_rank and avg_change >= strong_min_avg and (net_flow > 0 or not require_flow):
            state = "STRONG"
        else:
            state = "NEUTRAL"

        return {
            "industry": industry,
            "state": state,
            "rank": rank,
            "avg_change": avg_change,
            "net_money_flow": net_flow,
            "stock_count": row.get("stock_count"),
            "total_amount": row.get("total_amount"),
        }

    def annotate_candidates(
        self,
        candidates: list[dict[str, Any]] | None,
        *,
        market_env: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not candidates:
            return [], []
        if not bool(self.config.get("enabled", True)):
            return candidates, []

        market_env = market_env or {}
        regime = str(market_env.get("regime") or "")
        require_strong = bool(self.config.get("weak_market_require_strong_sector", True)) and regime in {"weak_market", "storm_market"}
        leader_top_n = int(self.config.get("leader_top_n", 3) or 3)
        leader_bonus = _safe_float(self.config.get("leader_score_bonus"), 8.0)
        weak_penalty = _safe_float(self.config.get("weak_sector_penalty"), -12.0)
        unknown_action = str(self.config.get("weak_market_unknown_sector_action") or "PENALTY").strip().upper()
        unknown_penalty = _safe_float(self.config.get("unknown_sector_penalty"), -4.0)

        by_sector: dict[str, list[dict[str, Any]]] = {}
        for c in candidates:
            industry = str(c.get("industry") or c.get("sector") or "其他")
            by_sector.setdefault(industry, []).append(c)

        leader_codes: set[str] = set()
        for items in by_sector.values():
            ranked = sorted(
                items,
                key=lambda x: (
                    _safe_float(x.get("score"), 0.0),
                    _safe_float(x.get("change") or x.get("pct_chg"), 0.0),
                    _safe_float(x.get("turnover"), 0.0),
                    _safe_float(x.get("amount") or x.get("amount_yi"), 0.0),
                ),
                reverse=True,
            )
            for item in ranked[:leader_top_n]:
                if item.get("code"):
                    leader_codes.add(str(item.get("code")))

        accepted = []
        rejected = []
        for c in candidates:
            industry = str(c.get("industry") or c.get("sector") or "其他")
            ctx = self.classify_sector(industry)
            tags = list(c.get("sector_rotation_tags") or [])
            reasons = list(c.get("sector_rotation_reasons") or [])
            code = str(c.get("code") or "")
            is_leader = bool(code and code in leader_codes)

            if ctx["state"] == "STRONG":
                tags.append("SECTOR_STRONG")
                reasons.append(f"行业强势 rank={ctx['rank']} avg={ctx['avg_change']:.2f}%")
                c["sector_bonus"] = _safe_float(c.get("sector_bonus"), 0.0) + leader_bonus
                if is_leader:
                    tags.append("SECTOR_LEADER_CONFIRMED")
                    reasons.append("行业内候选排名前列")
            elif ctx["state"] == "WEAK":
                tags.append("SECTOR_WEAK")
                reasons.append(f"行业弱势 avg={ctx['avg_change']:.2f}%")
                c["sector_bonus"] = _safe_float(c.get("sector_bonus"), 0.0) + weak_penalty
            elif ctx["state"] == "UNKNOWN":
                tags.append("SECTOR_UNKNOWN")
                reasons.append("行业数据缺失")
                if require_strong and unknown_action == "PENALTY":
                    tags.append("SECTOR_UNKNOWN_CONFIRM")
                    reasons.append(f"弱市行业未知降权确认 penalty={unknown_penalty:.0f}")
                    c["sector_bonus"] = _safe_float(c.get("sector_bonus"), 0.0) + unknown_penalty
            else:
                tags.append("SECTOR_NEUTRAL")

            if ctx.get("net_money_flow", 0) > 0:
                tags.append("SECTOR_MONEYFLOW_CONFIRM")

            c["sector_rotation"] = ctx
            c["sector_rotation_tags"] = sorted(set(tags))
            c["sector_rotation_reasons"] = reasons[:6]
            c["sector_rank"] = ctx.get("rank") or c.get("sector_rank", 0)
            c["sector_state"] = ctx.get("state")
            c["is_sector_leader"] = is_leader

            if require_strong and ctx["state"] not in {"STRONG"}:
                if ctx["state"] == "UNKNOWN" and unknown_action in {"ALLOW", "PENALTY", "CONFIRM_ONLY"}:
                    accepted.append(c)
                    continue
                reject = dict(c)
                reject["reject_reason"] = f"weak regime requires strong sector: {ctx['state']}"
                reject["sector_rotation_tags"] = sorted(set(tags + ["SECTOR_ROTATION_RISK"]))
                rejected.append(reject)
                continue

            accepted.append(c)

        return accepted, rejected
