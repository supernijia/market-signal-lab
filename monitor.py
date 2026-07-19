# -*- coding: utf-8 -*-
"""
Real-time Position Monitor
"""
from __future__ import annotations

import time
import logging
import os
import sys
import json
import argparse
from datetime import datetime
from core.config import Config
from core.utils import setup_logger, get_dynamic_stop_loss, get_weather_risk_params, is_trading_hours
from core.data_provider import DataProvider
from core.portfolio import PortfolioManager, is_paper_account, is_virtual_account
from core.reporter import Reporter
from core.analyzer import StockAnalyzer
from core.entry_flow import verify_entry_flow
from core.paper_account import account_position_count, mirror_paper_buy
from core.pre_trade_gate import canonical_strategy_name, evaluate_pre_trade_gate, merge_entry_confirm_tags_json, merge_signal_tags_json
from core.position_sizer import PositionSizer
from core.display_labels import display_account, humanize_text
import pandas as pd

MAIN_LOCK_FILE = "monitor.lock"
PAPER_LOCK_FILE = "monitor.paper.lock"
_ACTIVE_LOCK_FILE = None


def _monitor_lock_file(paper_only=False):
    return PAPER_LOCK_FILE if paper_only else MAIN_LOCK_FILE


def _pending_entry_scan_interval(paper_only=False):
    """Return pending-entry scan cadence; paper-only follows its training cooldown."""
    base_interval = 60
    if not paper_only:
        return base_interval
    try:
        cfg = Config.STRATEGY.get('paper_all_pool_execution', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
        value = cfg.get('scan_interval_sec') or cfg.get('retry_cooldown_sec') or base_interval
        return max(base_interval, int(value))
    except Exception:
        return base_interval


def _json_dumps_or_none(value):
    if value in (None, "", []):
        return None
    try:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        return None


def _payload_base_tags_json(payload):
    """Return observe/training tags saved on pending signals."""
    payload = payload or {}
    direct = payload.get("base_tags_json") or payload.get("tags_json") or payload.get("signal_tags_json")
    if direct:
        return _json_dumps_or_none(direct)
    tags = []
    for tag in payload.get("cold_start_model_tags") or []:
        tags.append({"tag": str(tag), "weight": 0, "reason": "cold-start observe model"})
    if payload.get("cold_start_delayed_confirm"):
        tags.append({"tag": "COLD_START_CHAIN_DELAYED_CONFIRM", "weight": 0, "reason": "cold-start delayed confirm"})
    if payload.get("cold_start_early_absorb"):
        tags.append({"tag": "COLD_START_CHAIN_EARLY_ABSORB", "weight": 0, "reason": "cold-start early absorb"})
    if payload.get("cold_start_pullback_entry_candidate"):
        tags.append({"tag": "COLD_START_PULLBACK_ENTRY_WATCH", "weight": 0, "reason": "cold-start pullback watch"})
    if str(payload.get("strategy_key") or payload.get("strategy_name") or "") == "冷启动":
        tags.append({"tag": "COLD_START_OBSERVE_SIGNAL", "weight": 0, "reason": "strategy=冷启动"})
    return _json_dumps_or_none(tags)


def _is_paper_training_pending(account, payload):
    """Whether a pending signal belongs to the paper training execution lane."""
    payload = payload or {}
    return is_paper_account(account) and (
        bool(payload.get("paper_executable_pool"))
        or bool(payload.get("paper_strong_entry"))
        or str(account or "").startswith("paper_")
    )


def _resolve_pending_windows(strategy, account, payload, strategy_windows, paper_windows):
    """Choose dynamic entry windows; paper training uses paper-specific windows."""
    allowed = (strategy_windows or {}).get(strategy)
    if _is_paper_training_pending(account, payload):
        allowed = (paper_windows or {}).get(strategy) or (paper_windows or {}).get("*") or allowed
    return allowed


def _paper_entry_policy_for_pending(account, candidate):
    """Return paper-only entry policy for any paper training pending signal."""
    if not _is_paper_training_pending(account, candidate or {}):
        return None
    policy = {"enabled": True}
    if (candidate or {}).get("paper_max_buy_change") is not None:
        policy["max_buy_change"] = (candidate or {}).get("paper_max_buy_change")
    return policy


def _paper_weak_daily_cap_reason(portfolio, account, pre_gate, trade_date):
    """Return a skip reason when paper weak-gate relaxed buys hit the daily cap."""
    if not is_paper_account(account):
        return None
    tags = set(str(x) for x in ((pre_gate or {}).get("tags") or []))
    used_relaxed_gate = bool(tags & {"PAPER_WEAK_SAMPLE_FLOOR_USED", "PAPER_WEAK_CHASE_BAND_USED"})
    if not used_relaxed_gate:
        return None
    metrics = ((pre_gate or {}).get("metrics") or {}).get("paper_weak_market_gate_experiment") or {}
    try:
        max_per_day = int(metrics.get("max_per_day") or 0)
    except Exception:
        max_per_day = 0
    if max_per_day <= 0:
        return None
    try:
        used_today = int(portfolio.count_paper_weak_buys(account=account, trade_date=trade_date) or 0)
    except Exception:
        used_today = 0
    if used_today >= max_per_day:
        return f"paper weak daily cap reached: {used_today}/{max_per_day}"
    return None


def _monitor_pending_target_accounts(paper_only=False):
    """Keep main and paper sentinels from consuming each other's pending rows."""
    return ("paper_main", "paper_watchlist") if paper_only else ("main", "watchlist")


def _position_visible_to_monitor(position, paper_only=False):
    """Return whether a position should be managed by this monitor instance."""
    account = (position or {}).get("account")
    return is_paper_account(account) if paper_only else not is_virtual_account(account)


def _repair_realtime_quote(ts_code, qt, daily_map=None):
    """Fill missing realtime fields from daily data before gate checks."""
    quote = dict(qt or {})
    if float(quote.get('price', 0) or 0) > 0 and float(quote.get('pre_close', 0) or 0) > 0:
        return quote
    try:
        daily_row = (daily_map or {}).get(ts_code)
        if daily_row:
            if float(quote.get('pre_close', 0) or 0) <= 0:
                quote['pre_close'] = float(daily_row.get('close') or daily_row.get('pre_close') or 0)
            if not quote.get('name'):
                quote['name'] = daily_row.get('name') or quote.get('name')
    except Exception:
        pass
    return quote


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _board_limit_pct(code, name=''):
    cfg = Config.STRATEGY.get('limit_up_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    board_limit_pct = cfg.get('board_limit_pct', {}) if isinstance(cfg.get('board_limit_pct', {}), dict) else {}
    code = str(code or '').strip()
    name = str(name or '')
    if 'ST' in name.upper():
        return float(board_limit_pct.get('st', 5) or 5)
    if code.startswith(('300', '301')):
        return float(board_limit_pct.get('gem', 20) or 20)
    if code.startswith(('688', '689')):
        return float(board_limit_pct.get('star', 20) or 20)
    if code.startswith(('4', '8', '9')):
        return float(board_limit_pct.get('bj', 30) or 30)
    return float(board_limit_pct.get('main', cfg.get('fallback_limit_pct', 10)) or 10)


def _paper_strong_unfillable_limit_up(candidate, quote):
    """Return a reason when a paper strong ticket is limit-up but realistically unfillable."""
    if not _is_paper_training_pending((candidate or {}).get('target_account'), candidate or {}):
        return None

    code = str((candidate or {}).get('code') or '').strip()
    name = str((candidate or {}).get('name') or (quote or {}).get('name') or '')
    price = _safe_float((candidate or {}).get('price') or (quote or {}).get('price'))
    pre_close = _safe_float((candidate or {}).get('pre_close') or (quote or {}).get('pre_close'))
    open_price = _safe_float((quote or {}).get('open') or (candidate or {}).get('open'))
    high = _safe_float((quote or {}).get('high') or (candidate or {}).get('high'))
    low = _safe_float((quote or {}).get('low') or (candidate or {}).get('low'))
    if min(price, pre_close, open_price, high, low) <= 0:
        return None

    limit_pct = _board_limit_pct(code, name)
    policy = Config.STRATEGY.get('paper_strong_entry_experiment', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    one_price_range_pct = _safe_float(policy.get('max_one_price_range_pct'), 0.12)
    near_limit_buffer_pct = _safe_float(policy.get('unfillable_near_limit_buffer_pct'), 0.12)

    change = (price - pre_close) / pre_close * 100.0
    high_change = (high - pre_close) / pre_close * 100.0
    intraday_range_pct = (high - low) / pre_close * 100.0
    open_gap_to_limit = abs(((open_price - pre_close) / pre_close * 100.0) - limit_pct)
    price_gap_to_limit = abs(change - limit_pct)

    at_limit = change >= (limit_pct - near_limit_buffer_pct) and high_change >= (limit_pct - near_limit_buffer_pct)
    one_price_board = (
        intraday_range_pct <= one_price_range_pct
        and open_gap_to_limit <= near_limit_buffer_pct
        and price_gap_to_limit <= near_limit_buffer_pct
    )
    if at_limit and one_price_board:
        return (
            f"一字板涨停不可成交: chg={change:.2f}% open/high/low={open_price:.2f}/{high:.2f}/{low:.2f} "
            f"range={intraday_range_pct:.2f}% limit={limit_pct:.0f}%"
        )
    return None


def check_t1_limit(p):
    """Check A-share T+1 limit: Cannot sell positions bought today"""
    try:
        entry_dt = _position_entry_datetime(p)
        if entry_dt and entry_dt.date() == datetime.now().date():
            return True
    except Exception as e:
        logging.warning(f"T+1 check error: {e}")
    return False


def _position_entry_datetime(p):
    """Return the position entry datetime from persisted position metadata."""
    entry_time_str = str(p.get('created_at', '') or p.get('update_time', '') or '').strip()
    if not entry_time_str:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(entry_time_str.split('.')[0], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(entry_time_str.split('.')[0])
    except Exception:
        return None


def _position_opened_today(p, now=None):
    """Whether the position was opened today."""
    entry_dt = _position_entry_datetime(p)
    if not entry_dt:
        return False
    return entry_dt.date() == (now or datetime.now()).date()


def _effective_position_high(p, qt, curr_price, buy_price, now=None):
    """
    Return the high-water price that is valid for this position.

    For positions opened today, realtime quote high is the full-day high and can
    include prices before the buy. Use only recorded high-water and current
    price until we can observe newer ticks after entry.
    """
    highest_price = float(p.get('highest_price', 0) or 0)
    if highest_price <= 0:
        highest_price = buy_price
    if _position_opened_today(p, now=now):
        return max(highest_price, buy_price, curr_price)
    curr_high = float((qt or {}).get('high', curr_price) or curr_price)
    return max(highest_price, curr_high)


def check_time_exit(p, pct_change, max_hold_days, min_hold_ret):
    """Check if the position has been held too long without enough return"""
    try:
        entry_time_str = str(p.get('created_at', '') or p.get('update_time', ''))
        if entry_time_str:
            entry_date = datetime.strptime(entry_time_str.split()[0], '%Y-%m-%d')
            current_date = datetime.now()
            hold_days = (current_date - entry_date).days
            
            if hold_days >= max_hold_days and pct_change < min_hold_ret:
                return True, f"时间止损: 持仓{hold_days}天且涨幅{pct_change*100:.2f}% (< {min_hold_ret*100}%)"
    except Exception as e:
        logging.warning(f"Time-exit check error: {e}")
    return False, ""


def _parse_laddered_take_profit(value, fallback):
    ladder = []
    for item in value or []:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                tp_pct = float(item[0])
                sell_pct = float(item[1])
                if tp_pct > 0 and sell_pct > 0:
                    ladder.append((tp_pct, min(sell_pct, 1.0)))
        except Exception:
            continue
    return ladder or list(fallback or [])


def _paper_exit_policy(account):
    if not str(account or '').lower().startswith('paper_'):
        return {}
    try:
        cfg = Config.STRATEGY.get('paper_exit_policy', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict) or not bool(cfg.get('enabled', True)):
        return {}
    return cfg


def _exit_settings_for_account(account, *, base_stop_loss, max_hold_days, min_hold_ret, ladder, trailing_config):
    """Return sell settings, with paper-only overrides for training exits."""
    policy = _paper_exit_policy(account)
    settings = {
        'stop_loss': float(base_stop_loss),
        'max_hold_days': int(max_hold_days),
        'min_hold_return': float(min_hold_ret),
        'ladder': list(ladder or []),
        'trailing_config': dict(trailing_config or {}),
        'min_profit_lock_trigger': 0.02,
        'min_profit_lock_pct': 0.01,
        'policy_name': 'default',
    }
    if not policy:
        return settings

    settings['stop_loss'] = _safe_float(policy.get('stop_loss'), settings['stop_loss'])
    settings['max_hold_days'] = int(_safe_float(policy.get('max_hold_days'), settings['max_hold_days']))
    settings['min_hold_return'] = _safe_float(policy.get('min_hold_return'), settings['min_hold_return'])
    settings['ladder'] = _parse_laddered_take_profit(policy.get('laddered_take_profit'), settings['ladder'])
    if isinstance(policy.get('trailing_retrace'), dict):
        settings['trailing_config'] = {
            str(k): _safe_float(v, settings['trailing_config'].get(str(k), 0.03))
            for k, v in policy.get('trailing_retrace', {}).items()
        }
    settings['min_profit_lock_trigger'] = _safe_float(policy.get('min_profit_lock_trigger'), settings['min_profit_lock_trigger'])
    settings['min_profit_lock_pct'] = _safe_float(policy.get('min_profit_lock_pct'), settings['min_profit_lock_pct'])
    settings['policy_name'] = str(policy.get('name') or 'paper_exit_policy')
    return settings


def _manual_watch_levels(provider, ts_code, buy_price, source_price=None):
    """Build lightweight key levels for alert-only manual holdings."""
    levels = {
        '系统入库价/信号修复线': source_price,
        '成本/回本线': buy_price,
    }
    try:
        history = provider.get_history_data(ts_code, count=25)
        if history and len(history) >= 5:
            df = pd.DataFrame(history)
            for col in ('close', 'high', 'low'):
                df[col] = pd.to_numeric(df[col], errors='coerce')
            if 'trade_date' in df.columns:
                df = df.sort_values('trade_date').reset_index(drop=True)
            ma5 = float(df['close'].tail(5).mean() or 0)
            ma20 = float(df['close'].tail(20).mean() or 0) if len(df) >= 20 else 0
            recent_high = float(df['high'].tail(15).max() or 0)
            recent_low = float(df['low'].tail(15).min() or 0)
            if recent_high > 0 and ma5 > 0:
                levels['突破确认线'] = round(min(recent_high, ma5 * 1.05), 2)
            if recent_low > 0 and ma20 > 0:
                levels['日线结构支撑'] = round(max(recent_low, ma20), 2)
            if len(df) >= 1:
                prev = df.iloc[-1]
                p_high = float(prev.get('high', 0) or 0)
                p_low = float(prev.get('low', 0) or 0)
                p_close = float(prev.get('close', 0) or 0)
                if p_high > 0 and p_low > 0 and p_close > 0:
                    levels['日内多空枢轴'] = round((p_high + p_low + p_close) / 3, 2)
    except Exception:
        pass
    return levels


def _manual_volume_ratio(analyzer, ts_code, qt, provider):
    try:
        current_vol = float(qt.get('vol_lots', qt.get('vol', qt.get('volume', 0))) or 0)
        history = provider.get_history_data(ts_code, count=8)
        return float(analyzer.calculate_volume_ratio(ts_code, current_vol, history=history[-5:] if history else None))
    except Exception:
        return None


def _send_manual_watch_alerts(
    *,
    p,
    ts_code,
    qt,
    curr_price,
    buy_price,
    pct_change,
    weather,
    provider,
    portfolio,
    reporter,
    analyzer,
    alerted_today,
    dry_run,
    no_email,
):
    """Alert-only sentinel for externally held manual positions.

    This is intentionally scoped to the rescue account so live manual holdings
    can be watched without mutating/selling the simulated main account.
    """
    account = str(p.get('account') or '').lower()
    if account != 'rescue':
        return

    code = str(p.get('code') or '')[:6]
    if not code:
        return
    name = qt.get('name') or p.get('name') or code
    qty = int(p.get('quantity') or 0)
    pnl_pct = pct_change * 100
    pnl_amount = (curr_price - buy_price) * qty

    selection = portfolio.get_latest_selection_for_code(code, days=15) or {}
    source_price = None
    try:
        source_price = float(selection.get('sel_price') or 0) or None
    except Exception:
        source_price = None

    levels = _manual_watch_levels(provider, ts_code, buy_price, source_price=source_price)
    volume_ratio = _manual_volume_ratio(analyzer, ts_code, qt, provider)

    events = []
    if source_price and curr_price >= source_price:
        events.append(("SIGNAL_REPAIR", "信号修复", f"现价 {curr_price:.2f} 已重新站上系统入库价 {source_price:.2f}"))
    if source_price and curr_price < source_price * 0.995 and pnl_pct <= -3.0:
        events.append(("SIGNAL_LOST", "信号失守", f"现价 {curr_price:.2f} 跌在入库价 {source_price:.2f} 下方，且浮亏 {pnl_pct:.2f}%"))
    if curr_price >= buy_price:
        events.append(("BREAK_EVEN", "回本线触达", f"现价 {curr_price:.2f} 已触达/站上成本 {buy_price:.2f}"))

    breakout = levels.get('突破确认线')
    try:
        if breakout and curr_price >= float(breakout):
            events.append(("BREAKOUT", "突破确认", f"现价 {curr_price:.2f} 已触达突破确认线 {float(breakout):.2f}"))
    except Exception:
        pass

    pivot = levels.get('日内多空枢轴')
    try:
        if pivot and curr_price <= float(pivot) and pnl_pct < 0:
            events.append(("PIVOT_LOST", "日内枢轴失守", f"现价 {curr_price:.2f} 跌破日内多空枢轴 {float(pivot):.2f}"))
    except Exception:
        pass

    for level in (-3.0, -5.0, -8.0):
        if pnl_pct <= level:
            events.append((f"LOSS_{abs(int(level))}", f"浮亏{abs(int(level))}%警戒", f"当前浮亏 {pnl_pct:.2f}% 已达到 {level:.0f}% 警戒线"))

    try:
        if volume_ratio is not None and float(volume_ratio) >= 2.0 and abs(pnl_pct) >= 1.0:
            direction = "上涨" if curr_price >= buy_price else "下跌"
            events.append(("VOLUME_SPIKE", "放量异动", f"{direction}过程中量能达到近5日均量 {float(volume_ratio):.2f} 倍"))
    except Exception:
        pass

    for event_key, event_name, reason in events:
        key = f"MANUAL_WATCH_{code}_{account}_{event_key}"
        if key in alerted_today:
            continue
        alerted_today.add(key)
        subject = f"🔔【手工持仓提醒】{name}({code}) {event_name}"
        content = reporter.format_manual_watch_alert(
            code=code,
            name=name,
            event=event_name,
            price=curr_price,
            buy_price=buy_price,
            quantity=qty,
            pnl_pct=pnl_pct,
            pnl_amount=pnl_amount,
            reason=reason,
            account=account,
            weather=weather,
            levels=levels,
            volume_ratio=volume_ratio,
        )
        logging.getLogger("StockAnalyzer.Monitor").warning(f"MANUAL_WATCH_ALERT {code}/{account}: {event_name} - {reason}")
        try:
            if not dry_run:
                portfolio.log_risk_event(
                    account=account,
                    code=code,
                    event_type=f"MANUAL_WATCH_{event_key}",
                    weather=weather,
                    reason=reason[:255],
                    params={
                        'price': curr_price,
                        'buy_price': buy_price,
                        'quantity': qty,
                        'pnl_pct': pnl_pct,
                        'pnl_amount': pnl_amount,
                        'source_price': source_price,
                        'volume_ratio': volume_ratio,
                        'levels': levels,
                    },
                )
        except Exception:
            pass
        if not (dry_run or no_email):
            reporter.send_email(subject, content)

def run_monitor(*, once=False, dry_run=False, no_email=False, force=False, paper_only=False):
    """Main monitor loop.

    Responsibilities:
    1) Sell sentinel: trailing stop, ladder take profit, weather-aware risk.
    2) Buy entry sentinel (opt-in): when entry_policy.enabled && default_model=dynamic_window,
       it polls pending_entry_signals and attempts entry within configured buckets.
    """

    global _ACTIVE_LOCK_FILE

    logger = setup_logger("StockAnalyzer.Monitor")
    logger.info("Starting Real-time Position Monitor (Looping every 60s during trading hours)...")
    if once:
        logger.info("Monitor once mode enabled; will run one safe iteration and exit.")
    if dry_run:
        logger.info("Monitor dry-run enabled; buy/sell/update/email side effects are skipped.")
    if paper_only:
        logger.info("Monitor paper-only enabled; only paper_* pending entries and positions will be processed.")
    lock_file = _monitor_lock_file(paper_only)
    logger.info(
        "Monitor runtime flags: once=%s dry_run=%s no_email=%s force=%s paper_only=%s lock_file=%s",
        once,
        dry_run,
        no_email,
        force,
        paper_only,
        lock_file,
    )

    # === Environment sanity checks (do NOT print secret values) ===
    missing = []
    if not Config.DB_PASS:
        missing.append('DB_PASS (MySQL password)')
    if Config.EMAIL_ENABLED and not Config.EMAIL_PWD:
        missing.append('EMAIL_PWD (SMTP auth code)')
    if not Config.TUSHARE_TOKEN:
        missing.append('TUSHARE_TOKEN (optional)')
    if missing:
        logger.warning("Environment variables missing: %s", ", ".join(missing))
        logger.warning("Recommended: copy .env.openclaw.example -> .env.openclaw and fill it, then run via ./run_openclaw.sh")

    provider = DataProvider()
    portfolio = PortfolioManager()
    try:
        portfolio.init_tables()
    except Exception as e:
        logger.warning("Monitor table init skipped/failed: %s", str(e)[:160])
    reporter = Reporter()

    # Load settings
    base_stop_loss = Config.RISK_MANAGEMENT.get('STOP_LOSS', -0.05)
    take_profit_max = Config.RISK_MANAGEMENT.get('TAKE_PROFIT', 0.20) # Raised to 20% for trailing run
    max_hold_days = Config.RISK_MANAGEMENT.get('MAX_HOLD_DAYS', 5)
    min_hold_ret = Config.RISK_MANAGEMENT.get('MIN_HOLD_RETURN', 0.01)
    t1_alert_cfg = Config.STRATEGY.get('t1_blocked_alert', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    t1_alert_enabled = bool(t1_alert_cfg.get('enabled', True))
    t1_alert_thresholds = t1_alert_cfg.get('thresholds_pct') or [-5.0, -8.0]
    try:
        t1_alert_thresholds = sorted([float(x) for x in t1_alert_thresholds], reverse=True)
    except Exception:
        t1_alert_thresholds = [-5.0, -8.0]

    logger.info(f"Target settings: Base SL {base_stop_loss*100}%, Max TP {take_profit_max*100}%")

    # Pending-entry buy sentinel cadence (only meaningful when enabled)
    last_entry_scan = 0
    ENTRY_SCAN_INTERVAL = 60
    paper_accounts = _monitor_pending_target_accounts(paper_only=True)
    pending_target_accounts = _monitor_pending_target_accounts(paper_only=paper_only)
    
    # Pre-fetch basic info for code mapping
    basics = provider.get_stock_basic()
    code_ts_map = {}
    for k in basics:
        code_ts_map[k.split('.')[0]] = k
        
    # Check singleton lock
    if os.path.exists(lock_file) and not force:
        try:
            with open(lock_file, 'r') as f:
                last_heartbeat_timestamp = float(f.read().strip())
                if time.time() - last_heartbeat_timestamp < 180: # 3 minutes
                    logger.warning(f"Another monitor instance is already running for lock_file={lock_file} (heartbeat < 3 mins ago). Exiting.")
                    sys.exit(0)
        except Exception as e:
            logger.warning(f"Failed to read lock file, proceeding... Error: {e}")

    last_run_date = None
    last_watchlist_check = 0  # Unix timestamp of last watchlist pruning
    WATCHLIST_CHECK_INTERVAL = 600  # 10 minutes in seconds
    
    # [BUG FIX #4] 卖出记忆: 防止同一只股票在同一天被重复触发卖出
    sold_today = set()  # {"code_account", ...}
    sold_today_date = None  # Reset at midnight
    
    # [Phase 20] T+0 做T信号推送去重记忆
    t0_alerted_today = set()
    manual_alerted_today = set()

    while True:
        try:
            now = datetime.now()
            
            # [BUG FIX #4] 每日重置卖出记忆
            if sold_today_date != now.date():
                sold_today = set()
                t0_alerted_today = set()
                manual_alerted_today = set()
                sold_today_date = now.date()
            
            # Update heartbeat
            try:
                with open(lock_file, 'w') as f:
                    f.write(str(time.time()))
                _ACTIVE_LOCK_FILE = lock_file
            except Exception as e:
                logger.warning(f"Failed to update lock file: {e}")
            
            # [V9] Auto-exit at 15:05 to free resources and prevent zombie loops overnight
            if not force and ((now.hour == 15 and now.minute >= 5) or now.hour >= 16):
                logger.info("Market closed (15:05+). Monitor auto-exiting for the day. Will be restarted by openclaw tomorrow.")
                break
            
            # Print heartbeat at the start of every hour to know it's alive
            if not once and now.minute == 0 and now.second < 60:
                logger.info("Monitor heartbeat: System is running...")
                time.sleep(60) # Prevent multiple log entries within same minute
                continue
            
            # [BUG FIX #1] 哨兵从 09:25 开始监控，而非 09:30，抓住开盘第一秒
            if not force and not is_trading_hours(allow_auction=True):
                time.sleep(60) # Sleep 1min outside trading hours
                continue
            
            # [V10] Fetch market weather every minute (real-time risk assessment)
            try:
                analyzer = StockAnalyzer(provider)
                market_env = analyzer.check_market_environment()
                try:
                    from core.utils import normalize_weather
                    weather = normalize_weather(market_env.get('weather', '☀️晴天'))
                except Exception:
                    weather = market_env.get('weather', '☀️晴天')
                risk_params = get_weather_risk_params(weather, Config.STRATEGY.get('weather_risk', None))
                
                # Update stop loss and take profit based on weather
                base_stop_loss = risk_params.get('stop_loss', -0.05)
                take_profit_max = risk_params.get('take_profit', 0.20)
                trailing_retrace = risk_params.get('trailing_retrace', 0.10)
                
                # Log weather change (only when it changes)
                if now.minute % 5 == 0 and now.second < 10:  # Log every 5 minutes
                    logger.info(f"🌤️ Market Weather: {weather} | SL: {base_stop_loss*100:.0f}% | TP: {take_profit_max*100:.0f}%")
            except Exception as e:
                logger.warning(f"Weather fetch failed, using defaults: {e}")
                market_env = {}
                weather = '☀️晴天'
                base_stop_loss = -0.05
                take_profit_max = 0.20
                trailing_retrace = 0.10
                
            # --- Pending-entry buy sentinel (opt-in) ---
            try:
                # Refresh config each loop so OpenClaw can toggle without restart
                Config.load_strategy_config()

                entry_policy = Config.STRATEGY.get('entry_policy', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
                entry_enabled = bool((entry_policy or {}).get('enabled', False))
                default_model = (entry_policy or {}).get('default_model') or 'immediate'

                entry_scan_interval = _pending_entry_scan_interval(paper_only=paper_only)
                if entry_enabled and default_model == 'dynamic_window' and (time.time() - last_entry_scan) >= entry_scan_interval:
                    last_entry_scan = time.time()

                    # Use canonical buckets (same as attack_window / tracker)
                    def _get_time_bucket_now() -> str | None:
                        hh, mm = now.hour, now.minute
                        buckets = [
                            ("B1", (9, 30), (10, 0)),
                            ("B2", (10, 0), (11, 30)),
                            ("B3", (13, 0), (14, 0)),
                            ("B4", (14, 0), (14, 40)),
                            ("B5", (14, 40), (15, 1)),
                        ]
                        for b, (sh, sm), (eh, em) in buckets:
                            start_ok = (hh > sh) or (hh == sh and mm >= sm)
                            end_ok = (hh < eh) or (hh == eh and mm < em)
                            if start_ok and end_ok:
                                return b
                        return None

                    curr_bucket = _get_time_bucket_now()
                    trade_date = now.strftime('%Y-%m-%d')

                    dyn = ((entry_policy or {}).get('models') or {}).get('dynamic_window', {})
                    strategy_windows = (dyn.get('strategy_windows') or {}) if isinstance(dyn.get('strategy_windows'), dict) else {}
                    retry_cooldown_sec = int(dyn.get('retry_cooldown_sec', 300) or 300)
                    max_retries = int(dyn.get('max_retries', 6) or 6)
                    attack_gate_cfg = Config.STRATEGY.get('attack_window_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
                    attack_gate_enabled = bool((attack_gate_cfg or {}).get('enabled', False))
                    attack_gate_rules = (attack_gate_cfg or {}).get('rules') or {}
                    market_adjustments = (market_env or {}).get('adjustments') or {}
                    try:
                        max_position_mult = float(market_adjustments.get('max_position_mult', 1.0) or 1.0)
                    except Exception:
                        max_position_mult = 1.0
                    market_status = str((market_env or {}).get('message') or (market_env or {}).get('desc') or '')

                    # Optional extra strict VWAP ratio (tightens verify_money_flow when set)
                    try:
                        policy_max_vwap_ratio = dyn.get('max_price_vwap_ratio', None)
                        policy_max_vwap_ratio = None if policy_max_vwap_ratio in (None, '', 'null') else float(policy_max_vwap_ratio)
                    except Exception:
                        policy_max_vwap_ratio = None

                    # Expire then load. In paper-only mode, keep the DB query scoped
                    # to paper accounts so main/watchlist rows cannot crowd out short
                    # simulation entry windows.
                    if dry_run:
                        logger.info("DRY-RUN pending expiry skipped.")
                    else:
                        try:
                            expired = portfolio.expire_old_pending_entries(
                                trade_date=trade_date,
                                now_dt=now,
                                target_accounts=pending_target_accounts,
                            )
                            if paper_only and expired:
                                logger.info("PAPER_ONLY_PENDING_EXPIRED count=%s accounts=%s", expired, ",".join(paper_accounts))
                        except Exception:
                            pass

                    pending = portfolio.load_pending_entry_signals(
                        trade_date=trade_date,
                        now_dt=now,
                        limit=80,
                        target_accounts=pending_target_accounts,
                    )
                    if paper_only:
                        logger.info(
                            "PAPER_ONLY_PENDING_SCAN trade_date=%s bucket=%s loaded=%s accounts=%s",
                            trade_date,
                            curr_bucket or "-",
                            len(pending or []),
                            ",".join(paper_accounts),
                        )
                    if pending:
                        scoped_pending = []
                        for pending_row in pending:
                            try:
                                payload_for_account = json.loads(pending_row.get('payload_json') or '{}')
                                if not isinstance(payload_for_account, dict):
                                    payload_for_account = {}
                            except Exception:
                                payload_for_account = {}
                            acct_for_row = str(payload_for_account.get('target_account') or pending_row.get('target_account') or 'main')
                            if (paper_only and is_paper_account(acct_for_row)) or ((not paper_only) and not is_virtual_account(acct_for_row)):
                                scoped_pending.append(pending_row)
                        pending = scoped_pending

                    if pending:
                        # Avoid buying if global positions already at cap
                        max_total = Config.RISK_MANAGEMENT.get("MAX_TOTAL_POSITIONS", 5)
                        all_positions = portfolio.load_all_positions()
                        active_accounts = set()
                        for pending_row in pending:
                            payload_for_account = {}
                            try:
                                payload_for_account = json.loads(pending_row.get('payload_json') or '{}')
                                if not isinstance(payload_for_account, dict):
                                    payload_for_account = {}
                            except Exception:
                                payload_for_account = {}
                            active_accounts.add(str(payload_for_account.get('target_account') or pending_row.get('target_account') or 'main'))
                        has_capacity = any(account_position_count(all_positions, acct) < max_total for acct in active_accounts)
                        if has_capacity:
                            analyzer = StockAnalyzer(provider)
                            daily_map = {}
                            try:
                                daily_map = {
                                    row.get('ts_code'): row
                                    for row in (provider.get_daily_data() or [])
                                    if isinstance(row, dict) and row.get('ts_code')
                                }
                            except Exception:
                                daily_map = {}

                            for r in pending:
                                _log_pending_event = None
                                try:
                                    pid = int(r.get('id'))
                                    code = r.get('code')
                                    name = r.get('name') or ''
                                    ts_code = r.get('ts_code') or (f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ")
                                    strat = r.get('source_strategy') or ''
                                    row_account = r.get('target_account') or 'main'
                                    payload = {}
                                    try:
                                        payload = json.loads(r.get('payload_json') or '{}')
                                        if not isinstance(payload, dict):
                                            payload = {}
                                    except Exception:
                                        payload = {}
                                    acct = payload.get('target_account') or r.get('target_account') or 'main'
                                    paper_all_cfg = Config.STRATEGY.get('paper_all_pool_execution', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
                                    paper_windows = paper_all_cfg.get('windows', {}) if isinstance((paper_all_cfg or {}).get('windows'), dict) else {}
                                    paper_training_pending = _is_paper_training_pending(acct, payload)
                                    effective_retry_cooldown_sec = retry_cooldown_sec
                                    effective_max_retries = max_retries
                                    if paper_training_pending:
                                        try:
                                            effective_retry_cooldown_sec = int(paper_all_cfg.get('retry_cooldown_sec', retry_cooldown_sec) or retry_cooldown_sec)
                                        except Exception:
                                            effective_retry_cooldown_sec = retry_cooldown_sec
                                        try:
                                            effective_max_retries = int(paper_all_cfg.get('max_retries', max_retries) or max_retries)
                                        except Exception:
                                            effective_max_retries = max_retries
                                    status_before = str(r.get('status') or 'PENDING')

                                    def _log_pending_event(decision, reason, *, price_value=None, pre_close_value=None, change_value=None, volume_ratio=None, price_vwap_ratio=None, status_after=None, extra=None):
                                        if dry_run:
                                            return
                                        event_payload = {
                                            'paper_executable_pool': bool(payload.get('paper_executable_pool')),
                                            'paper_source_pool': payload.get('paper_source_pool'),
                                            'paper_strong_entry': bool(payload.get('paper_strong_entry')),
                                            'paper_experiment_type': payload.get('paper_experiment_type'),
                                            'paper_experiment_reason': payload.get('paper_experiment_reason'),
                                            'signal_bucket': r.get('signal_bucket'),
                                            'expires_at': str(r.get('expires_at') or ''),
                                        }
                                        if isinstance(extra, dict):
                                            event_payload.update(extra)
                                        try:
                                            portfolio.log_pending_entry_check_event(
                                                pending_id=pid,
                                                trade_date=trade_date,
                                                code=code,
                                                account=acct,
                                                strategy=strat,
                                                check_time=now,
                                                bucket=curr_bucket or '',
                                                price=price_value,
                                                pre_close=pre_close_value,
                                                change_pct=change_value,
                                                volume_ratio=volume_ratio,
                                                price_vwap_ratio=price_vwap_ratio,
                                                decision=decision,
                                                reason=reason,
                                                status_before=status_before,
                                                status_after=status_after or status_before,
                                                check_count=int(r.get('check_count', 0) or 0),
                                                payload=event_payload,
                                            )
                                        except Exception:
                                            pass

                                    # Window gate
                                    allowed = _resolve_pending_windows(strat, acct, payload, strategy_windows, paper_windows)
                                    if allowed and (not curr_bucket or curr_bucket not in set(allowed)):
                                        reason = f"window not allowed: strategy={strat} bucket={curr_bucket or '-'} allowed={','.join(allowed)}"
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                            pid,
                                            row_account,
                                            code,
                                            name,
                                            strat,
                                            reason,
                                        )
                                        _log_pending_event('SKIP', reason)
                                        continue

                                    # Optional weather x bucket attack gate. This is a second-pass
                                    # safety check so a queued signal cannot execute after weather
                                    # or intraday regime changes.
                                    if attack_gate_enabled:
                                        weather_allowed = attack_gate_rules.get(weather, None)
                                        if weather_allowed is not None and (not curr_bucket or curr_bucket not in set(weather_allowed or [])):
                                            reason = f"attack_window_gate: weather={weather} bucket={curr_bucket} not in {weather_allowed}"
                                            if dry_run:
                                                logger.info(f"DRY-RUN pending touch skipped: id={pid} reason={reason}")
                                            else:
                                                portfolio.touch_pending_entry_check(signal_id=pid, reason=reason[:255])
                                            _log_pending_event('SKIP', reason)
                                            continue

                                    # Retry limit
                                    cc = int(r.get('check_count', 0) or 0)
                                    if cc >= effective_max_retries:
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending expire skipped: id={pid} reason=max retries reached")
                                        else:
                                            portfolio.mark_pending_entry_status(signal_id=pid, status='EXPIRED', reason='max retries reached')
                                        _log_pending_event('EXPIRED', 'max retries reached', status_after='EXPIRED')
                                        continue

                                    # Cooldown
                                    last_checked = r.get('last_checked_at')
                                    if last_checked:
                                        try:
                                            last_dt = datetime.fromisoformat(last_checked) if isinstance(last_checked, str) else last_checked
                                            if (now - last_dt).total_seconds() < effective_retry_cooldown_sec:
                                                reason = f"retry cooldown: last_checked={last_dt} cooldown={effective_retry_cooldown_sec}s"
                                                logger.info(
                                                    "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                                    pid,
                                                    row_account,
                                                    code,
                                                    name,
                                                    strat,
                                                    reason,
                                                )
                                                _log_pending_event('SKIP', reason)
                                                continue
                                        except Exception:
                                            pass

                                    logger.info(
                                        "PENDING_CHECK id=%s account=%s code=%s name=%s strategy=%s bucket=%s expires_at=%s check_count=%s",
                                        pid,
                                        acct,
                                        code,
                                        name,
                                        strat,
                                        curr_bucket or "-",
                                        r.get('expires_at'),
                                        r.get('check_count', 0),
                                    )

                                    # Already holding?
                                    if any(p.get('code') == code and p.get('account') == acct for p in portfolio.load_all_positions()):
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=already holding",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending cancel skipped: id={pid} reason=already holding")
                                        else:
                                            portfolio.mark_pending_entry_status(signal_id=pid, status='CANCELLED', reason='already holding')
                                        _log_pending_event('CANCELLED', 'already holding', status_after='CANCELLED')
                                        continue

                                    if account_position_count(portfolio.load_all_positions(), acct) >= max_total:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=position cap reached",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=position cap reached")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason='position cap reached')
                                        _log_pending_event('SKIP', 'position cap reached')
                                        continue

                                    # Fetch realtime
                                    rt = provider.get_realtime_quotes([ts_code])
                                    if not rt or ts_code not in rt:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=no realtime quote",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=no realtime quote")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason='no realtime quote')
                                        _log_pending_event('SKIP', 'no realtime quote')
                                        continue

                                    qt = rt[ts_code]
                                    qt = _repair_realtime_quote(ts_code, qt, daily_map=daily_map)
                                    price = float(qt.get('price', 0) or 0)
                                    if price <= 0:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=invalid price price=%s",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                            price,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=invalid price")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason='invalid price')
                                        _log_pending_event('SKIP', 'invalid price', price_value=price)
                                        continue
                                    pre_close = float(qt.get('pre_close', 0) or 0)

                                    cand = {
                                        'code': code,
                                        'name': name or qt.get('name', ''),
                                        'ts_code': ts_code,
                                        'target_account': acct,
                                        'price': price,
                                        'vol': qt.get('vol', 0),
                                        'amount': qt.get('amount', 0),
                                        'pre_close': pre_close,
                                        'high': qt.get('high', 0),
                                        'change': ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0,
                                    }
                                    for key in (
                                        'open_change',
                                        'turnover',
                                        'prev_limit_present',
                                        'prev_limit_times',
                                        'is_continue_board_candidate',
                                        'is_first_board_candidate',
                                        'board_context',
                                        'zt_tag',
                                        'risk_tags',
                                        'base_tags_json',
                                        'cold_start_model_tags',
                                        'cold_start_delayed_confirm',
                                        'cold_start_early_absorb',
                                        'cold_start_pullback_entry_candidate',
                                        'paper_executable_pool',
                                        'paper_source_pool',
                                        'paper_strong_entry',
                                        'paper_experiment',
                                        'paper_experiment_type',
                                        'paper_experiment_reason',
                                        'paper_original_filter_reason',
                                        'paper_max_buy_change',
                                        'paper_experiment_metrics',
                                    ):
                                        if payload.get(key) is not None:
                                            cand[key] = payload.get(key)

                                    # Refresh config so OpenClaw can toggle gates without restart
                                    try:
                                        Config.load_strategy_config()
                                    except Exception:
                                        pass

                                    # [VNext] Win-rate gate (shared with main.py; default disabled)
                                    win_gate_cfg = Config.STRATEGY.get('win_rate_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
                                    win_gate_enabled = bool((win_gate_cfg or {}).get('enabled', False))
                                    win_gate_cycle = str((win_gate_cfg or {}).get('analysis_cycle', 'T+1') or 'T+1')
                                    win_gate_days = int((win_gate_cfg or {}).get('lookback_days', 20) or 20)
                                    win_gate_min_samples = int((win_gate_cfg or {}).get('min_samples', 30) or 30)
                                    win_gate_min_wr = float((win_gate_cfg or {}).get('min_win_rate', 0.55) or 0.55)
                                    win_gate_statuses = (win_gate_cfg or {}).get('win_statuses', None)
                                    if not isinstance(win_gate_statuses, list) or not win_gate_statuses:
                                        win_gate_statuses = ['涨停', '吃肉']
                                    insufficient_action = str((win_gate_cfg or {}).get('insufficient_samples_action', 'ALLOW_WITH_WARN') or 'ALLOW_WITH_WARN')

                                    canon_strat = canonical_strategy_name(strat)
                                    stats = portfolio.get_strategy_win_rate(
                                        strategy=canon_strat,
                                        analysis_cycle=win_gate_cycle,
                                        lookback_days=win_gate_days,
                                        win_statuses=win_gate_statuses,
                                    )
                                    if win_gate_enabled:
                                        cnt = int((stats or {}).get('cnt', 0) or 0)
                                        win_rate = float((stats or {}).get('win_rate', 0.0) or 0.0)

                                        if cnt < win_gate_min_samples:
                                            if insufficient_action.upper() == 'BLOCK':
                                                reason = f"win_rate_gate blocked: insufficient samples n={cnt}"
                                                logger.info(
                                                    "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                                    pid,
                                                    acct,
                                                    code,
                                                    name,
                                                    strat,
                                                    reason,
                                                )
                                                if dry_run:
                                                    logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=win_rate_gate insufficient samples n={cnt}")
                                                else:
                                                    portfolio.touch_pending_entry_check(signal_id=pid, reason=reason)
                                                _log_pending_event('SKIP', reason, price_value=price, pre_close_value=pre_close, change_value=cand.get('change'), extra={'win_stats': stats or {}})
                                                continue
                                        else:
                                            if win_rate < win_gate_min_wr:
                                                reason = f"win_rate_gate blocked: win_rate={win_rate:.2f}<{win_gate_min_wr:.2f} n={cnt}"
                                                logger.info(
                                                    "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                                    pid,
                                                    acct,
                                                    code,
                                                    name,
                                                    strat,
                                                    reason,
                                                )
                                                if dry_run:
                                                    logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=win_rate_gate blocked")
                                                else:
                                                    portfolio.touch_pending_entry_check(signal_id=pid, reason=reason)
                                                _log_pending_event('SKIP', reason, price_value=price, pre_close_value=pre_close, change_value=cand.get('change'), extra={'win_stats': stats or {}})
                                                continue

                                    pre_gate = evaluate_pre_trade_gate(
                                        cand,
                                        market_env=market_env,
                                        strategy=canon_strat or strat,
                                        account=acct,
                                        now=now,
                                        mode='monitor',
                                        win_rate_stats=stats,
                                    )
                                    if not pre_gate.get('allow', True):
                                        gate_reason = " | ".join(pre_gate.get('reasons') or ['pre_trade_gate blocked'])
                                        gate_tags = pre_gate.get('tags') or []
                                        gate_action = str(pre_gate.get('action') or '').upper()
                                        logger.warning(
                                            f"⛔ 动态入场门禁拦截: {cand.get('name') or name}({code}) "
                                            f"strategy={pre_gate.get('strategy') or canon_strat or strat} "
                                            f"action={gate_action or 'BLOCKED'} tags={','.join(gate_tags)} reason={gate_reason}"
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=pre_trade_gate: {gate_reason}")
                                        else:
                                            if gate_action in {'BLOCK', 'OBSERVE'}:
                                                portfolio.mark_pending_entry_status(signal_id=pid, status='CANCELLED', reason=f"pre_trade_gate {gate_action}: {gate_reason}"[:255])
                                            else:
                                                portfolio.touch_pending_entry_check(signal_id=pid, reason=f"pre_trade_gate: {gate_reason}"[:255])
                                            try:
                                                portfolio.log_risk_event(
                                                    account=acct,
                                                    code=code,
                                                    event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                                                    weather=weather,
                                                    reason=gate_reason[:255],
                                                    params={
                                                        'strategy': pre_gate.get('strategy') or canon_strat or strat,
                                                        'action': pre_gate.get('action'),
                                                        'tags': gate_tags,
                                                        'metrics': pre_gate.get('metrics'),
                                                        'win_stats': stats or {},
                                                        'position_multiplier': pre_gate.get('position_multiplier'),
                                                        'required_confirmations': pre_gate.get('required_confirmations'),
                                                        'data_quality': pre_gate.get('data_quality'),
                                                    },
                                                )
                                            except Exception:
                                                pass
                                        _log_pending_event(
                                            'CANCELLED' if gate_action in {'BLOCK', 'OBSERVE'} else 'SKIP',
                                            f"pre_trade_gate {gate_action}: {gate_reason}" if gate_action in {'BLOCK', 'OBSERVE'} else f"pre_trade_gate: {gate_reason}",
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            status_after='CANCELLED' if gate_action in {'BLOCK', 'OBSERVE'} else status_before,
                                            extra={
                                                'gate': 'pre_trade_gate',
                                                'gate_action': gate_action,
                                                'gate_tags': gate_tags,
                                                'gate_metrics': pre_gate.get('metrics'),
                                                'win_stats': stats or {},
                                            },
                                        )
                                        continue

                                    if pre_gate.get('action') in ('OBSERVE', 'BLOCK'):
                                        gate_reason = " | ".join(pre_gate.get('reasons') or ['permission matrix blocked pending entry'])
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending blocked: id={pid} action={pre_gate.get('action')} reason={gate_reason}")
                                        else:
                                            portfolio.mark_pending_entry_status(signal_id=pid, status='CANCELLED', reason=f"permission {pre_gate.get('action')}: {gate_reason}"[:255])
                                        _log_pending_event(
                                            'CANCELLED',
                                            f"permission {pre_gate.get('action')}: {gate_reason}",
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            status_after='CANCELLED',
                                            extra={'gate': 'permission_matrix', 'gate_action': pre_gate.get('action'), 'gate_metrics': pre_gate.get('metrics')},
                                        )
                                        continue

                                    cap_reason = _paper_weak_daily_cap_reason(portfolio, acct, pre_gate, trade_date)
                                    if cap_reason:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                            cap_reason,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason={cap_reason}")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason=cap_reason[:255])
                                        _log_pending_event(
                                            'SKIP',
                                            cap_reason,
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            extra={
                                                'gate': 'paper_weak_daily_cap',
                                                'gate_tags': pre_gate.get('tags') or [],
                                                'gate_metrics': pre_gate.get('metrics'),
                                            },
                                        )
                                        continue

                                    paper_entry_policy = _paper_entry_policy_for_pending(acct, cand)
                                    if paper_entry_policy:
                                        logger.info(
                                            "PAPER_TRAINING_PENDING_CHECK id=%s account=%s code=%s name=%s strategy=%s reason=%s max_buy_change=%s",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                            cand.get('paper_experiment_reason') or '',
                                            cand.get('paper_max_buy_change') or '',
                                        )
                                        unfillable_reason = _paper_strong_unfillable_limit_up(cand, qt)
                                        if unfillable_reason:
                                            logger.info(
                                                "PAPER_STRONG_UNFILLABLE_LIMIT_UP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                                pid,
                                                acct,
                                                code,
                                                name,
                                                strat,
                                                unfillable_reason,
                                            )
                                            if dry_run:
                                                logger.info(f"DRY-RUN pending touch skipped: id={pid} reason={unfillable_reason}")
                                            else:
                                                portfolio.touch_pending_entry_check(signal_id=pid, reason=unfillable_reason[:255])
                                            _log_pending_event(
                                                'UNFILLABLE',
                                                unfillable_reason,
                                                price_value=price,
                                                pre_close_value=pre_close,
                                                change_value=cand.get('change'),
                                                extra={'gate': 'paper_strong_unfillable_limit_up'},
                                            )
                                            continue

                                    verify_result = verify_entry_flow(
                                        cand,
                                        analyzer=analyzer,
                                        market_env=market_env,
                                        weather=weather,
                                        strategy=strat,
                                        now=now,
                                        realtime_map=rt,
                                        pending_retry=True,
                                        policy_max_vwap_ratio=policy_max_vwap_ratio,
                                        paper_entry_policy=paper_entry_policy,
                                    )
                                    ok = bool(verify_result.get("ok"))
                                    reason = verify_result.get("reason") or "entry_confirm rejected"
                                    confirm = verify_result.get("confirm") or {}
                                    if not ok:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                            reason,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason={reason}")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason=reason)
                                        reject_metrics = verify_result.get("metrics") or (confirm.get("metrics") if isinstance(confirm, dict) else {}) or {}
                                        _log_pending_event(
                                            'SKIP',
                                            reason,
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            volume_ratio=reject_metrics.get('volume_ratio'),
                                            price_vwap_ratio=reject_metrics.get('vwap_ratio') or reject_metrics.get('price_vwap_ratio'),
                                            extra={'verification': verify_result.get('verification'), 'confirm': confirm, 'metrics': reject_metrics},
                                        )
                                        continue
                                    cand["entry_confirm"] = confirm
                                    cand["entry_scenario"] = confirm.get("scenario")
                                    cand["entry_confirmations"] = confirm.get("confirmations")
                                    metrics = confirm.get("metrics") or {}
                                    if metrics.get("vwap"):
                                        cand["vwap"] = metrics.get("vwap")
                                    if metrics.get("volume_ratio"):
                                        cand["volume_ratio"] = metrics.get("volume_ratio")

                                    if is_virtual_account(acct):
                                        cash_before = portfolio.load_cash(account=acct)
                                    else:
                                        cash_before = portfolio.load_cash_for_trading(account=acct)
                                    if cash_before is None:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=cash unavailable",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=cash unavailable")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason='cash unavailable')
                                        _log_pending_event(
                                            'SKIP',
                                            'cash unavailable',
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            volume_ratio=metrics.get('volume_ratio'),
                                            price_vwap_ratio=metrics.get('vwap_ratio') or metrics.get('price_vwap_ratio'),
                                            extra={'confirm': confirm, 'metrics': metrics},
                                        )
                                        continue
                                    sizing = PositionSizer(portfolio, provider).calculate(
                                        account=acct,
                                        price=price,
                                        cash_available=cash_before,
                                        positions=portfolio.load_all_positions(),
                                        market_env=market_env,
                                        strategy=strat,
                                        pre_gate=pre_gate,
                                        candidate=cand,
                                        ts_code=ts_code,
                                        max_position_mult=max_position_mult,
                                        market_status=market_status,
                                    )
                                    buy_vol = int(sizing.get('quantity') or 0)
                                    logger.info(
                                        f"📐 动态入场仓位预算: {cand.get('name') or name}({code}) "
                                        f"pct={float(sizing.get('position_pct') or 0):.2%} vol={buy_vol} "
                                        f"amount={float(sizing.get('amount') or 0):.0f} "
                                        f"reason={' | '.join(sizing.get('reasons') or [])}"
                                    )
                                    if buy_vol <= 0:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=budget too small",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason=budget too small")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason='budget too small')
                                        _log_pending_event(
                                            'SKIP',
                                            'budget too small',
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            volume_ratio=metrics.get('volume_ratio'),
                                            price_vwap_ratio=metrics.get('vwap_ratio') or metrics.get('price_vwap_ratio'),
                                            extra={'confirm': confirm, 'metrics': metrics, 'sizing': sizing},
                                        )
                                        continue

                                    if dry_run:
                                        success, msg = True, "dry-run: buy skipped"
                                        logger.info(f"DRY-RUN buy skipped: {cand.get('name') or name} ({code}) account={acct} vol={buy_vol} price={price:.2f}")
                                    else:
                                        final_tags_json = merge_entry_confirm_tags_json(
                                            merge_signal_tags_json(
                                                cand.get("base_tags_json") or _payload_base_tags_json(payload),
                                                pre_gate,
                                            ),
                                            cand.get("entry_confirm"),
                                        )
                                        success, msg = portfolio.execute_buy(
                                            code,
                                            cand.get('name') or name,
                                            price,
                                            buy_vol,
                                            account=acct,
                                            snapshot_id=None,
                                            source_strategy=strat,
                                            weather=weather,
                                            signal_tags_json=final_tags_json,
                                        )
                                    if success:
                                        if not dry_run and is_virtual_account(acct):
                                            logger.info(
                                                "SIM_TRADE_BUY account=%s code=%s name=%s strategy=%s time=%s qty=%s price=%.3f cost=%.2f cash_before=%.2f pending_id=%s reason=dynamic_window_confirm",
                                                acct,
                                                code,
                                                cand.get('name') or name,
                                                strat,
                                                now.strftime('%Y-%m-%d %H:%M:%S'),
                                                buy_vol,
                                                price,
                                                price * buy_vol * 1.0003,
                                                float(cash_before or 0),
                                                pid,
                                            )
                                        if not dry_run and not is_virtual_account(acct):
                                            try:
                                                mirror_paper_buy(
                                                    portfolio,
                                                    source_account=acct,
                                                    code=code,
                                                    name=cand.get('name') or name,
                                                    price=price,
                                                    quantity=buy_vol,
                                                    snapshot_id=None,
                                                    source_strategy=strat,
                                                    weather=weather,
                                                    signal_tags_json=final_tags_json,
                                                )
                                            except Exception:
                                                pass
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending mark BOUGHT skipped: id={pid}")
                                        else:
                                            portfolio.mark_pending_entry_status(signal_id=pid, status='BOUGHT', reason='buy executed by sentinel')
                                        _log_pending_event(
                                            'BOUGHT',
                                            'buy executed by sentinel' if not dry_run else 'dry-run: buy skipped',
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            volume_ratio=metrics.get('volume_ratio'),
                                            price_vwap_ratio=metrics.get('vwap_ratio') or metrics.get('price_vwap_ratio'),
                                            status_after='BOUGHT' if not dry_run else status_before,
                                            extra={
                                                'confirm': confirm,
                                                'metrics': metrics,
                                                'sizing': sizing,
                                                'quantity': buy_vol,
                                                'cash_before': cash_before,
                                                'simulated': is_virtual_account(acct),
                                            },
                                        )
                                        try:
                                            cash_after = portfolio.load_cash(account=acct)
                                            buy_cost = price * buy_vol * 1.0003
                                            content = reporter.format_buy_alert(code, cand.get('name') or name, price, buy_vol, buy_cost, f"{strat}(动态时间窗)", cash_before, cash_after, account=acct, weather=weather, snapshot_id=None)
                                            if not (dry_run or no_email):
                                                if is_virtual_account(acct):
                                                    subject = f"🟢【模拟仓动态入场】{cand.get('name') or name} @{price:.2f}"
                                                else:
                                                    subject = f"🟢【动态入场哨兵】{cand.get('name') or name} @{price:.2f}"
                                                reporter.send_email(subject, content)
                                        except Exception:
                                            pass
                                    else:
                                        logger.info(
                                            "PENDING_SKIP id=%s account=%s code=%s name=%s strategy=%s reason=%s",
                                            pid,
                                            acct,
                                            code,
                                            name,
                                            strat,
                                            msg or 'buy failed',
                                        )
                                        if dry_run:
                                            logger.info(f"DRY-RUN pending touch skipped: id={pid} reason={msg or 'buy failed'}")
                                        else:
                                            portfolio.touch_pending_entry_check(signal_id=pid, reason=msg or 'buy failed')
                                        _log_pending_event(
                                            'SKIP',
                                            msg or 'buy failed',
                                            price_value=price,
                                            pre_close_value=pre_close,
                                            change_value=cand.get('change'),
                                            volume_ratio=metrics.get('volume_ratio'),
                                            price_vwap_ratio=metrics.get('vwap_ratio') or metrics.get('price_vwap_ratio'),
                                            extra={'confirm': confirm, 'metrics': metrics, 'sizing': sizing, 'quantity': buy_vol},
                                        )
                                except Exception as e:
                                    logger.warning(
                                        "PENDING_CHECK_ERROR id=%s code=%s name=%s error=%s",
                                        r.get('id'),
                                        r.get('code'),
                                        r.get('name') or '',
                                        str(e)[:200],
                                    )
                                    try:
                                        pid = r.get('id')
                                        if pid:
                                            if dry_run:
                                                logger.info(f"DRY-RUN pending exception touch skipped: id={pid} reason={str(e)[:120]}")
                                            else:
                                                portfolio.touch_pending_entry_check(signal_id=int(pid), reason=str(e)[:200])
                                            try:
                                                if callable(_log_pending_event):
                                                    _log_pending_event('ERROR', str(e)[:200])
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                        else:
                            reason = "position cap reached for all pending accounts"
                            logger.info("PENDING_BATCH_SKIP count=%s reason=%s", len(pending), reason)
                            if not dry_run:
                                for pending_row in pending:
                                    try:
                                        pending_id = int(pending_row.get('id'))
                                        portfolio.touch_pending_entry_check(signal_id=pending_id, reason=reason)
                                        row_payload = {}
                                        try:
                                            row_payload = json.loads(pending_row.get('payload_json') or '{}')
                                            if not isinstance(row_payload, dict):
                                                row_payload = {}
                                        except Exception:
                                            row_payload = {}
                                        row_account = row_payload.get('target_account') or pending_row.get('target_account') or 'main'
                                        portfolio.log_pending_entry_check_event(
                                            pending_id=pending_id,
                                            trade_date=trade_date,
                                            code=pending_row.get('code'),
                                            account=row_account,
                                            strategy=pending_row.get('source_strategy') or '',
                                            check_time=now,
                                            bucket=curr_bucket or '',
                                            decision='SKIP',
                                            reason=reason,
                                            status_before=pending_row.get('status') or 'PENDING',
                                            status_after=pending_row.get('status') or 'PENDING',
                                            check_count=int(pending_row.get('check_count', 0) or 0),
                                            payload={
                                                'batch_skip': True,
                                                'paper_executable_pool': bool(row_payload.get('paper_executable_pool')),
                                                'paper_source_pool': row_payload.get('paper_source_pool'),
                                            },
                                        )
                                    except Exception:
                                        pass

            except Exception:
                pass

            # Load positions across ALL accounts for monitoring
            positions = portfolio.load_all_positions()
            positions = [p for p in positions if _position_visible_to_monitor(p, paper_only=paper_only)]
            if not positions:
                if once:
                    logger.info("No positions to monitor. Monitor once mode completed.")
                    break
                time.sleep(60)
                continue

            ts_codes = []
            for p in positions:
                c = p['code']
                if c in code_ts_map: ts_codes.append(code_ts_map[c])

            if not ts_codes:
                if once:
                    logger.info("No valid ts_codes found. Monitor once mode completed.")
                    break
                time.sleep(60)
                continue

            # Quick real-time snapshot
            rt_data = provider.get_realtime_quotes(ts_codes)

            for p in positions:
                code = p['code']
                ts_code = code_ts_map.get(code)
                account = p['account']
                sell_key = f"{code}_{account}"
                
                if not ts_code or ts_code not in rt_data:
                    continue
                
                # [BUG FIX #4] 如果今天已经卖出过该股票，跳过
                if sell_key in sold_today:
                    continue
                    
                qt = rt_data[ts_code]
                curr_price = float(qt.get('price', 0) or 0)
                
                if curr_price <= 0: continue
                
                buy_price = float(p.get('avg_price') or 0)
                if buy_price <= 0: buy_price = float(p.get('buy_price') or 0)
                if buy_price <= 0: continue
                
                qty = int(p['quantity'])
                name = qt.get('name', p['name'])
                pct_change = (curr_price - buy_price) / buy_price
                default_trailing_config = {
                    '☀️晴天': 0.03,   # 晴天回撤3% (15%锁住12%)
                    '☁️多云': 0.08,
                    '⚠️暴雨': 0.05
                }
                exit_settings = _exit_settings_for_account(
                    account,
                    base_stop_loss=base_stop_loss,
                    max_hold_days=max_hold_days,
                    min_hold_ret=min_hold_ret,
                    ladder=Config.RISK_MANAGEMENT.get("LADDERED_TAKE_PROFIT", []),
                    trailing_config=default_trailing_config,
                )

                # High-water preview for T+1 checks. For same-day entries,
                # ignore quote.high because it may be a pre-entry full-day high.
                preview_highest_price = _effective_position_high(p, qt, curr_price, buy_price)
                max_pct_change_preview = (preview_highest_price - buy_price) / buy_price

                dynamic_sl_preview = get_dynamic_stop_loss(
                    max_pct_change_preview,
                    exit_settings['stop_loss'],
                    weather,
                    exit_settings['trailing_config'],
                )
                dynamic_sl_preview = max(dynamic_sl_preview, exit_settings['stop_loss'])
                if max_pct_change_preview >= exit_settings['min_profit_lock_trigger']:
                    dynamic_sl_preview = max(dynamic_sl_preview, exit_settings['min_profit_lock_pct'])

                try:
                    _send_manual_watch_alerts(
                        p=p,
                        ts_code=ts_code,
                        qt=qt,
                        curr_price=curr_price,
                        buy_price=buy_price,
                        pct_change=pct_change,
                        weather=weather,
                        provider=provider,
                        portfolio=portfolio,
                        reporter=reporter,
                        analyzer=analyzer,
                        alerted_today=manual_alerted_today,
                        dry_run=dry_run,
                        no_email=no_email,
                    )
                except Exception as e:
                    logger.debug(f"Manual watch alert check failed for {code}/{account}: {e}")

                # [VNext] T+1 blocked sell event: A-share cannot sell positions bought today,
                # but we still record that a stop/lock line would have triggered. This feeds audit/evolution.
                if check_t1_limit(p):
                    if pct_change <= dynamic_sl_preview:
                        t1_key = f"T1_BLOCKED_{sell_key}"
                        reason = (
                            f"T+1阻止卖出: 现价利润{pct_change*100:.2f}% <= 止损/保护线{dynamic_sl_preview*100:.2f}%, "
                            f"买入日当日不可卖"
                        )
                        if t1_key not in t0_alerted_today:
                            t0_alerted_today.add(t1_key)
                            logger.warning(f"⛔ {name}({code}) {reason} @ {curr_price:.2f}")
                            if not dry_run:
                                try:
                                    portfolio.log_risk_event(
                                        account=account,
                                        code=code,
                                        event_type='T1_BLOCKED_SELL_SIGNAL',
                                        weather=weather,
                                        reason=reason[:255],
                                        params={
                                            'pct_change': pct_change,
                                            'dynamic_sl': dynamic_sl_preview,
                                            'max_pct_change': max_pct_change_preview,
                                            'curr_price': curr_price,
                                            'buy_price': buy_price,
                                            'quantity': qty,
                                            'entry_time': str(p.get('created_at') or p.get('update_time') or ''),
                                            'entry_strategy': p.get('entry_strategy'),
                                            'entry_tags_json': p.get('entry_tags_json'),
                                        }
                                    )
                                except Exception:
                                    pass
                            if t1_alert_enabled and not is_virtual_account(account) and not (dry_run or no_email):
                                try:
                                    pct_value = pct_change * 100
                                    triggered_level = None
                                    for level in t1_alert_thresholds:
                                        if pct_value <= level:
                                            triggered_level = level
                                    if triggered_level is not None:
                                        subject = f"⛔【T+1风险阻断】{name}({code}) 浮亏{pct_value:.2f}%"
                                        content = f"<h3>{subject}</h3>"
                                        content += f"<p>账户: <b>{display_account(account)}</b></p>"
                                        content += f"<p>现价: <b>{curr_price:.2f}</b> / 成本: <b>{buy_price:.2f}</b></p>"
                                        content += f"<p>当前盈亏: <b>{pct_value:.2f}%</b>，止损/保护线: <b>{dynamic_sl_preview*100:.2f}%</b></p>"
                                        content += "<p>系统已识别止损/保护线触发，但 A 股 T+1 规则导致今日不可卖出。</p>"
                                        content += "<p>建议：明日开盘前重点复核集合竞价、跌停风险、流动性和是否需要手动风控。</p>"
                                        reporter.send_email(subject, content)
                                except Exception as e:
                                    logger.warning(f"T+1 blocked alert email failed for {code}: {e}")
                    continue
                
                # High-water tracking. For same-day entries, do not promote
                # the position high from full-day quote.high if that high may
                # have happened before the buy.
                old_highest_price = float(p.get('highest_price', 0) or 0)
                if old_highest_price <= 0:
                    old_highest_price = buy_price
                highest_price = _effective_position_high(p, qt, curr_price, buy_price)
                if highest_price > old_highest_price:
                    if dry_run:
                        logger.info(f"DRY-RUN highest price update skipped: {code}/{account} -> {highest_price:.2f}")
                    else:
                        portfolio.update_highest_price(code, account, highest_price)
                    
                max_pct_change = (highest_price - buy_price) / buy_price
                
                action = None
                reason = ""
                
                # [Phase 20] Manual Rescue Strategy Isolation
                # If the account is marked as 'rescue' (manual T+0 target), bypass all automated SL/TP
                # [V10] High-Water Mark Trailing Stop Loss Logic (Weather-Aware)
                is_limit_up = qt.get('pct_chg', 0) >= 9.5 if qt else False
                
                if weather == '⚠️暴雨' and is_limit_up:
                    logger.info(f"  ☔ {name} 涨停封板，暴雨天气允许持有")
                    continue
                
                dynamic_sl = get_dynamic_stop_loss(
                    max_pct_change,
                    exit_settings['stop_loss'],
                    weather,
                    exit_settings['trailing_config'],
                )
                dynamic_sl = max(dynamic_sl, exit_settings['stop_loss'])
                
                # [V19] 盈利保护底线：一旦最高盈利过2%，锁死 1% 利润 (覆手续费)
                if max_pct_change >= exit_settings['min_profit_lock_trigger']:
                    dynamic_sl = max(dynamic_sl, exit_settings['min_profit_lock_pct'])

                # [V18] Laddered Take Profit Logic
                ladder = exit_settings['ladder']
                sell_stage = int(p.get('sell_stage', 0))
                
                # Check Price Limits & Laddered Take Profit
                if pct_change <= dynamic_sl:
                    action = "SELL"
                    if dynamic_sl >= 0:
                        reason = f"保本止损触发: 现价利润{pct_change*100:.2f}% (<= 保本线 {dynamic_sl*100:.2f}%) [历史最高{max_pct_change*100:.2f}%]"
                    else:
                        reason = f"高水位止损触发: 现价利润{pct_change*100:.2f}% (<= 止损线 {dynamic_sl*100:.2f}%)"
                else:
                    # Check Laddered Exit (Take Profit)
                    for i, (tp_pct, sell_pct) in enumerate(ladder, 1):
                        if pct_change >= tp_pct and sell_stage < i:
                            if float(sell_pct) >= 0.999:
                                action = "SELL"
                                reason = f"短线止盈全平触发 (阶段{i}): 盈利 {pct_change*100:.2f}% (>= {tp_pct*100:.1f}%)"
                            else:
                                action = "SELL_LADDER"
                                reason = f"阶梯止盈触发 (阶段{i}): 盈利 {pct_change*100:.2f}% (>= {tp_pct*100:.1f}%)"
                                ladder_target_stage = i
                                ladder_sell_pct = sell_pct
                            break
                    
                    if not action:
                        # Check Time Limits
                        triggered, time_reason = check_time_exit(
                            p,
                            pct_change,
                            exit_settings['max_hold_days'],
                            exit_settings['min_hold_return'],
                        )
                        if triggered:
                            action = "SELL"
                            reason = time_reason
                        
                if action in ["SELL", "SELL_LADDER"]:
                    logger.warning(f"🚨 SELL SIGNAL TRIGGERED for {name} ({code}) [{account}]: {reason} @ {curr_price}")
                    
                    if account.lower() == 'rescue':
                        if sell_key not in t0_alerted_today:
                            t0_alerted_today.add(sell_key)
                            subject = f"🚨【自救保护触发】 {name} 达到原定止损/止盈线"
                            content = "<h3>🚨 T+0 卖点/止损点触发报警</h3>"
                            content += f"<p>您目前持有 <b>{name} ({code})</b> - 账户: {display_account(account)}</p>"
                            content += f"<p>触发原因: <b>{humanize_text(reason)}</b></p>"
                            content += f"<p>💡 <b>建议操作：</b> 由于该持仓处于【自救隔离区】，系统已<b>拦截机器人的自动平仓操作</b>。</p>"
                            content += f"<p>此时说明股价可能已达到阶段高点或破位点，如果你之前进行了 <span style='color:red;'>买入做T</span> 操作，<b>建议现在立马卖出对应的T+0仓位份额</b>以锁定差价！如果你正满仓被套，建议适当减仓三分之一避免扩大损失。</p>"
                            if not (dry_run or no_email):
                                reporter.send_email(subject, content)
                            logger.info(f"Rescue alert sent: intercepted {action} for {code}")
                    else:
                        pct_to_sell = ladder_sell_pct if action == "SELL_LADDER" else 1.0

                        # [VNext] Risk event + sell attribution (best-effort)
                        if not dry_run:
                            try:
                                portfolio.log_risk_event(
                                    account=account,
                                    code=code,
                                    event_type='SELL_TRIGGER',
                                    weather=weather,
                                    reason=reason,
                                    params={
                                        'action': action,
                                        'pct_change': pct_change,
                                        'max_pct_change': max_pct_change,
                                        'dynamic_sl': dynamic_sl,
                                        'curr_price': curr_price,
                                        'pct_to_sell': pct_to_sell,
                                        'sell_stage': int(p.get('sell_stage', 0)),
                                    }
                                )
                            except Exception:
                                pass

                        entry_snapshot_id = p.get('entry_snapshot_id')
                        entry_strategy = p.get('entry_strategy')
                        entry_tags_json = p.get('entry_tags_json')

                        if dry_run:
                            success, msg = True, "dry-run: sell skipped"
                            logger.info(f"DRY-RUN sell skipped: {name} ({code}) account={account} pct={pct_to_sell:.2f}")
                        else:
                            success, msg = portfolio.execute_sell(
                                code,
                                curr_price,
                                reason,
                                account=account,
                                percentage=pct_to_sell,
                                snapshot_id=entry_snapshot_id,
                                source_strategy=entry_strategy,
                                weather=weather,
                                signal_tags_json=entry_tags_json,
                            )
                        
                        if success:
                            if not dry_run:
                                try:
                                    portfolio.log_risk_event(
                                        account=account,
                                        code=code,
                                        event_type='SELL_EXECUTED',
                                        weather=weather,
                                        reason=reason,
                                        params={
                                            'action': action,
                                            'curr_price': curr_price,
                                            'pct_to_sell': pct_to_sell,
                                        }
                                    )
                                except Exception:
                                    pass

                            if action == "SELL_LADDER":
                                # Update sell stage in DB so we don't trigger the same stage again
                                if dry_run:
                                    logger.info(f"DRY-RUN sell stage update skipped: {code}/{account} -> {ladder_target_stage}")
                                else:
                                    portfolio.update_sell_stage(code, account, ladder_target_stage)
                            else:
                                # Final sell: mark today as sold to avoid monitor re-trigger
                                sold_today.add(sell_key)
                            
                            sell_amount = curr_price * (qty * pct_to_sell) * 0.999 
                            sell_pnl = sell_amount - (buy_price * (qty * pct_to_sell))
                            sell_pnl_pct = (sell_pnl / (buy_price * (qty * pct_to_sell))) * 100 if buy_price > 0 else 0
                            cash_after = portfolio.load_cash(account=account)
                            sell_qty = int(qty * pct_to_sell)
                            if not dry_run and is_virtual_account(account):
                                logger.info(
                                    "SIM_TRADE_SELL account=%s code=%s name=%s strategy=%s time=%s qty=%s buy=%.3f sell=%.3f pnl=%.2f pnl_pct=%.2f reason=%s",
                                    account,
                                    code,
                                    name,
                                    entry_strategy or "-",
                                    now.strftime('%Y-%m-%d %H:%M:%S'),
                                    sell_qty,
                                    buy_price,
                                    curr_price,
                                    sell_pnl,
                                    sell_pnl_pct,
                                    str(reason or "")[:120],
                                )
                            
                            sell_tag = "【部分平仓】" if action == "SELL_LADDER" else "【自动卖出】"
                            sell_content = reporter.format_sell_alert(
                                code,
                                name,
                                curr_price,
                                sell_qty,
                                sell_pnl,
                                sell_pnl_pct,
                                reason,
                                cash_after,
                                action=action,
                                pct_to_sell=pct_to_sell,
                                account=account,
                                weather=weather,
                            )
                            sell_emoji = "🔴" if sell_pnl < 0 else "🟢"
                            if not (dry_run or no_email):
                                if is_virtual_account(account):
                                    if paper_only:
                                        paper_sell_tag = "部分平仓" if action == "SELL_LADDER" else "自动卖出"
                                        reporter.send_email(f"{sell_emoji}【模拟仓{paper_sell_tag}】{name} {sell_pnl:+.0f}元", sell_content)
                                else:
                                    reporter.send_email(f"{sell_emoji}{sell_tag}{name} {sell_pnl:+.0f}元", sell_content)
                            logger.info(f"Execution successful: {msg}")
                        else:
                            logger.error(f"Execution failed: {msg}")
                else:
                    # [Phase 20 Stage 3] T+0 Sentinel Push Alert
                    is_rescue = (account.lower() == 'rescue')
                    if buy_price > 0 and not str(account or '').lower().startswith('paper_') and sell_key not in t0_alerted_today and (is_rescue or pct_change < -0.05):
                        try:
                            history = provider.get_history_data(ts_code, count=25)
                            if len(history) >= 20:
                                df_t0 = pd.DataFrame(history)
                                df_t0['close'] = df_t0['close'].astype(float)
                                df_t0['trade_date'] = pd.to_datetime(df_t0['trade_date'])
                                df_t0 = df_t0.sort_values('trade_date').reset_index(drop=True)
                                
                                # Append current realtime pseudo-daily bar to calculate MA accurately
                                current_row = pd.DataFrame([{
                                    'trade_date': now,
                                    'open': float(qt.get('open', curr_price)),
                                    'high': float(qt.get('high', curr_price)),
                                    'low': float(qt.get('low', curr_price)),
                                    'close': curr_price,
                                    'vol': float(qt.get('vol', 0)),
                                    'amount': float(qt.get('amount', 0))
                                }])
                                df_t0 = pd.concat([df_t0, current_row], ignore_index=True)
                                
                                from core.tech_analyzer import TechAnalyzer
                                df_t0 = TechAnalyzer.calculate_indicators(df_t0)
                                t0_data = TechAnalyzer.calculate_t0_score(df_t0, curr_price)
                                
                                if t0_data['score'] >= 60:
                                    t0_alerted_today.add(sell_key)
                                    score = t0_data['score']
                                    desc = t0_data['desc']
                                    
                                    prefix = "【自救T+0降本低吸买点】" if is_rescue else "【深水做T提示】"
                                    subject = f"🚨{prefix} {name} 评分:{score}分 评级:{desc}"
                                    
                                    content = f"<h3>{subject}</h3>"
                                    content += f"<p>您目前持有 <b>{name} ({code})</b> - 账户: {display_account(account)}</p>"
                                    content += f"<p>当前浮亏: <b>{pct_change*100:.2f}%</b> (现价 {curr_price:.2f} / 成本 {buy_price:.2f})</p>"
                                    content += f"<p>系统哨兵检测到绝佳的做T低吸(正T)机会，量化系统打分：<b><span style='color:red;'>{score}分</span></b>，评级：<b><span style='color:red;'>{desc}</span></b></p>"
                                    
                                    content += "<h4>📊 因子实时拆解：</h4><ul>"
                                    for v in t0_data.get('details', {}).values():
                                        content += f"<li>{v}</li>"
                                    content += "</ul>"
                                    
                                    content += "<p>💡 <b>建议操作：</b> 请在交易软件中观察分时图回踩，寻找承接位买入相同股数进行【正T】降本。买入后挂单反弹的高位点卖出今日买入份额。</p>"
                                    
                                    if not (dry_run or no_email):
                                        reporter.send_email(subject, content)
                                    logger.info(f"T+0 Alert sent for {name} ({code}) - Score: {score}")
                        except Exception as e:
                            logger.error(f"Failed to check T+0 score for {code}: {e}")
                        
        except Exception as e:
            logger.error(f"Monitor iteration error: {e}")
        
        # === WATCHLIST PRUNING (every 10 minutes) ===
        try:
            if (not paper_only) and (force or is_trading_hours()) and (time.time() - last_watchlist_check) >= WATCHLIST_CHECK_INTERVAL:
                last_watchlist_check = time.time()
                watchlist = portfolio.get_watchlist(days=5)
                if watchlist:
                    wl_codes = []
                    wl_code_map = {}
                    for item in watchlist:
                        c = item['code']
                        if c in code_ts_map:
                            wl_codes.append(code_ts_map[c])
                            wl_code_map[c] = code_ts_map[c]
                    
                    if wl_codes:
                        wl_rt = provider.get_realtime_quotes(wl_codes)
                        removed_count = 0
                        
                        for item in watchlist:
                            c = item['code']
                            ts = wl_code_map.get(c)
                            if not ts or ts not in wl_rt:
                                continue
                            
                            qt = wl_rt[ts]
                            price = float(qt.get('price', 0))
                            if price <= 0:
                                continue
                            
                            # Fetch history for MA20
                            try:
                                history = provider.get_history_data(ts, count=25)
                                if len(history) < 20:
                                    continue
                                df = pd.DataFrame(history)
                                df['close'] = df['close'].astype(float)
                                current_row = pd.DataFrame([{'close': price}])
                                df = pd.concat([df, current_row], ignore_index=True)
                                ma20 = df['close'].rolling(window=20).mean().iloc[-1]
                                
                                if price < ma20 * 0.995:
                                    if dry_run:
                                        logger.info(f"DRY-RUN watchlist prune skipped: {item['name']}({c}) broken MA20 ({ma20:.2f}) @ {price:.2f}")
                                    else:
                                        portfolio.update_zt_result(item['date'], c, 'removed', strategy=item.get('strategy'))
                                    removed_count += 1
                                    logger.info(f"Watchlist pruned: {item['name']}({c}) broken MA20 ({ma20:.2f}) @ {price:.2f}")
                            except Exception as e:
                                logger.debug(f"Watchlist check error for {c}: {e}")
                        
                        if removed_count > 0:
                            logger.info(f"Watchlist pruning: removed {removed_count} stocks")
        except Exception as e:
            logger.error(f"Watchlist pruning error: {e}")
            
        if once:
            logger.info("Monitor once mode completed.")
            break

        time.sleep(60) # Scan every 60 seconds

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stock Analyzer realtime position monitor')
    parser.add_argument('--once', action='store_true', help='Run one monitor iteration and exit')
    parser.add_argument('--dry-run', action='store_true', help='Evaluate signals without executing trades or DB updates')
    parser.add_argument('--no-email', action='store_true', help='Disable email sending')
    parser.add_argument('--force', action='store_true', help='Run even outside trading hours and ignore active lock')
    parser.add_argument('--paper-only', action='store_true', help='Only process paper_* pending entries and paper positions')
    args = parser.parse_args()

    try:
        run_monitor(once=args.once, dry_run=args.dry_run, no_email=args.no_email, force=args.force, paper_only=args.paper_only)
    except KeyboardInterrupt:
        print("\nMonitor stopped by user.")
    finally:
        # Only clean up a lock this process actually wrote.
        import os
        lock_file = _ACTIVE_LOCK_FILE
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                print(f"Lock file cleaned up: {lock_file}")
            except Exception:
                pass
