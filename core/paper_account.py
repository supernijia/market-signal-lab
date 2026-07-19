# -*- coding: utf-8 -*-
"""Paper-account helpers for shadow trading."""
from __future__ import annotations

import logging
from typing import Any

from core.config import Config

logger = logging.getLogger("StockAnalyzer.PaperAccount")


DEFAULT_PAPER_ACCOUNT = {
    "enabled": True,
    "shadow_buy_enabled": True,
    "main_account": "paper_main",
    "watchlist_account": "paper_watchlist",
    "mirror_quantity": True,
    "description": "影子仿真账户：真实/观察链路买入成功后，同步记录到 paper_main/paper_watchlist，用于1-2周规则观察。",
}


def get_paper_account_config() -> dict[str, Any]:
    cfg = {}
    try:
        cfg = Config.STRATEGY.get("paper_account", {}) if isinstance(Config.STRATEGY, dict) else {}
    except Exception:
        cfg = {}
    merged = dict(DEFAULT_PAPER_ACCOUNT)
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def paper_account_for(source_account: str | None) -> str | None:
    cfg = get_paper_account_config()
    if not bool(cfg.get("enabled", True)):
        return None

    src = str(source_account or "main").strip().lower()
    if not src or src.startswith("paper_") or src == "rescue":
        return None
    if src == "watchlist":
        return str(cfg.get("watchlist_account") or "paper_watchlist")
    return str(cfg.get("main_account") or "paper_main")


def account_position_count(positions: list[dict[str, Any]] | None, account: str) -> int:
    acct = str(account or "main")
    return len([p for p in (positions or []) if str(p.get("account") or "") == acct])


def mirror_paper_buy(
    portfolio,
    *,
    source_account: str,
    code: str,
    name: str,
    price: float,
    quantity: int,
    snapshot_id=None,
    source_strategy: str | None = None,
    weather: str | None = None,
    signal_tags_json: str | None = None,
    selection_id=None,
) -> tuple[bool, str]:
    """Mirror a successful buy into the configured paper account."""
    cfg = get_paper_account_config()
    if not bool(cfg.get("enabled", True)) or not bool(cfg.get("shadow_buy_enabled", True)):
        return False, "paper account disabled"

    paper_account = paper_account_for(source_account)
    if not paper_account:
        return False, "no mapped paper account"

    try:
        qty = int(quantity or 0)
        px = float(price or 0)
    except Exception:
        return False, "invalid paper order"
    if qty <= 0 or px <= 0:
        return False, "invalid paper order"

    ok, msg = portfolio.execute_buy(
        code,
        name,
        px,
        qty,
        account=paper_account,
        snapshot_id=snapshot_id,
        source_strategy=source_strategy,
        weather=weather,
        signal_tags_json=signal_tags_json,
        selection_id=selection_id,
    )
    if ok:
        logger.info(
            "📄 Paper buy mirrored: %s -> %s %s %s %s股 @ %.2f",
            source_account,
            paper_account,
            code,
            name,
            qty,
            px,
        )
        try:
            portfolio.log_risk_event(
                account=paper_account,
                code=code,
                event_type="PAPER_BUY_MIRRORED",
                weather=weather,
                reason=f"shadow buy from {source_account}",
                params={
                    "source_account": source_account,
                    "source_strategy": source_strategy,
                    "price": px,
                    "quantity": qty,
                    "snapshot_id": snapshot_id,
                    "selection_id": selection_id,
                },
            )
        except Exception:
            pass
    else:
        logger.warning("Paper buy mirror failed: %s -> %s %s reason=%s", source_account, paper_account, code, msg)
    return ok, msg
