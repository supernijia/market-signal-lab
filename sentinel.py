# -*- coding: utf-8 -*-
"""
Sentinel - One-shot position check for cron jobs
Runs once and exits immediately
"""
import logging
import argparse
from datetime import datetime
from core.config import Config
from core.utils import setup_logger, get_dynamic_stop_loss, get_weather_risk_params, is_trading_hours
from core.data_provider import DataProvider
from core.portfolio import PortfolioManager
from core.reporter import Reporter
from core.analyzer import StockAnalyzer
import pandas as pd


def check_t1_limit(p):
    """Check A-share T+1 limit: Cannot sell positions bought today"""
    try:
        entry_time_str = str(p.get('created_at', '') or p.get('update_time', ''))
        if entry_time_str:
            entry_date = datetime.strptime(entry_time_str.split()[0], '%Y-%m-%d').date()
            today = datetime.now().date()
            if entry_date == today:
                return True
    except Exception:
        pass
    return False


def run_sentinel(*, dry_run=False, no_email=False):
    """Run one-shot check and exit"""
    logger = setup_logger("StockAnalyzer.Sentinel")
    logger.info("=== Starting Sentinel One-shot Check ===")
    
    now = datetime.now()
    
    # Environment checks
    missing = []
    if not Config.DB_PASS:
        missing.append('DB_PASS')
    if Config.EMAIL_ENABLED and not Config.EMAIL_PWD:
        missing.append('EMAIL_PWD')
    if missing:
        logger.warning(f"Missing env vars: {', '.join(missing)}")
    
    provider = DataProvider()
    portfolio = PortfolioManager()
    reporter = Reporter()
    
    # Load settings
    base_stop_loss = Config.RISK_MANAGEMENT.get('STOP_LOSS', -0.05)
    take_profit_max = Config.RISK_MANAGEMENT.get('TAKE_PROFIT', 0.20)
    ladder = Config.RISK_MANAGEMENT.get("LADDERED_TAKE_PROFIT", [])
    max_hold_days = Config.RISK_MANAGEMENT.get('MAX_HOLD_DAYS', 5)
    min_hold_ret = Config.RISK_MANAGEMENT.get('MIN_HOLD_RETURN', 0.01)
    
    # Get market weather
    try:
        analyzer = StockAnalyzer(provider)
        market_env = analyzer.check_market_environment()
        try:
            from core.utils import normalize_weather
            weather = normalize_weather(market_env.get('weather', '☀️晴天'))
        except Exception:
            weather = market_env.get('weather', '☀️晴天')
        risk_params = get_weather_risk_params(weather, Config.STRATEGY.get('weather_risk', None))
        
        base_stop_loss = risk_params.get('stop_loss', -0.05)
        take_profit_max = risk_params.get('take_profit', 0.20)
        trailing_retrace = risk_params.get('trailing_retrace', 0.10)
        
        logger.info(f"🌤️ Weather: {weather} | SL: {base_stop_loss*100:.0f}% | TP: {take_profit_max*100:.0f}%")
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        weather = '☀️晴天'
        base_stop_loss = -0.05
        take_profit_max = 0.20
    
    # Load positions
    positions = portfolio.load_all_positions()
    if not positions:
        logger.info("No positions to monitor. Exiting.")
        return
    
    logger.info(f"Monitoring {len(positions)} position(s)...")
    
    # Pre-fetch stock basics for code mapping
    basics = provider.get_stock_basic()
    code_ts_map = {}
    for k in basics:
        code_ts_map[k.split('.')[0]] = k
    
    # Get realtime quotes
    ts_codes = []
    for p in positions:
        c = p['code']
        if c in code_ts_map:
            ts_codes.append(code_ts_map[c])
    
    if not ts_codes:
        logger.warning("No valid ts_codes found. Exiting.")
        return
    
    rt_data = provider.get_realtime_quotes(ts_codes)
    
    # Track sold today to avoid duplicate sells
    sold_today = set()
    
    trailing_config = {
        '☀️晴天': 0.03,
        '☁️多云': 0.08,
        '⚠️暴雨': 0.05
    }
    
    alerts = []
    
    for p in positions:
        code = p['code']
        ts_code = code_ts_map.get(code)
        account = p['account']
        sell_key = f"{code}_{account}"
        
        if not ts_code or ts_code not in rt_data:
            continue
        
        if sell_key in sold_today:
            continue
        
        qt = rt_data[ts_code]
        curr_price = float(qt.get('price', 0) or 0)
        
        if curr_price <= 0:
            continue
        
        buy_price = float(p.get('avg_price') or 0)
        if buy_price <= 0:
            buy_price = float(p.get('buy_price') or 0)
        if buy_price <= 0:
            continue
        
        # T+1 check
        if check_t1_limit(p):
            continue
        
        qty = int(p['quantity'])
        name = qt.get('name', p.get('name', code))
        
        pct_change = (curr_price - buy_price) / buy_price
        
        # High-water mark
        highest_price = float(p.get('highest_price', 0) or 0)
        if highest_price <= 0:
            highest_price = buy_price
        
        curr_high = float(qt.get('high', curr_price))
        if curr_high > highest_price:
            highest_price = curr_high
        
        max_pct_change = (highest_price - buy_price) / buy_price
        
        # Weather-aware trailing stop
        dynamic_sl = get_dynamic_stop_loss(max_pct_change, base_stop_loss, weather, trailing_config)
        
        # Profit protection底线
        if max_pct_change >= 0.02:
            dynamic_sl = max(dynamic_sl, 0.01)
        
        action = None
        reason = ""
        
        # Check sell signals
        if pct_change <= dynamic_sl:
            action = "SELL"
            if dynamic_sl >= 0:
                reason = f"保本止损: {pct_change*100:.2f}% (<= {dynamic_sl*100:.2f}%) [历史最高{max_pct_change*100:.2f}%]"
            else:
                reason = f"高水位止损: {pct_change*100:.2f}% (<= {dynamic_sl*100:.2f}%)"
        else:
            # Check laddered take profit
            sell_stage = int(p.get('sell_stage', 0))
            for i, (tp_pct, sell_pct) in enumerate(ladder, 1):
                if pct_change >= tp_pct and sell_stage < i:
                    action = "SELL_LADDER"
                    reason = f"阶梯止盈(阶段{i}): {pct_change*100:.2f}% (>= {tp_pct*100:.0f}%)"
                    break
        
        if action:
            logger.warning(f"🚨 {action} for {name} ({code}): {reason}")
            
            # Skip rescue accounts (manual T+0)
            if account.lower() == 'rescue':
                logger.info(f"Skipping rescue account: {code}")
                continue
            
            pct_to_sell = 1.0 if action == "SELL" else 0.5
            
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
                    snapshot_id=p.get('entry_snapshot_id'),
                    source_strategy=p.get('entry_strategy'),
                    weather=weather,
                )
            
            if success:
                sold_today.add(sell_key)
                sell_amount = curr_price * (qty * pct_to_sell) * 0.999
                sell_pnl = sell_amount - (buy_price * (qty * pct_to_sell))
                sell_pnl_pct = (sell_pnl / (buy_price * (qty * pct_to_sell))) * 100 if buy_price > 0 else 0
                cash_after = portfolio.load_cash(account=account)
                
                alerts.append({
                    'name': name,
                    'code': code,
                    'action': action,
                    'reason': reason,
                    'price': curr_price,
                    'qty': int(qty * pct_to_sell),
                    'pnl': sell_pnl,
                    'pnl_pct': sell_pnl_pct,
                    'account': account,
                })
                
                logger.info(f"✓ Executed: {msg}")
            else:
                logger.error(f"✗ Failed: {msg}")
    
    # Send summary email if any alerts
    if alerts and not (dry_run or no_email):
        subject = f"🚨【哨兵触发】{len(alerts)}只股票触发卖出信号"
        content = "<h3>哨兵自动卖出报告</h3><table border='1'><tr><th>股票</th><th>操作</th><th>原因</th><th>现价</th><th>数量</th><th>盈亏</th><th>盈亏%</th></tr>"
        for a in alerts:
            emoji = "🔴" if a['pnl'] < 0 else "🟢"
            content += f"<tr><td>{a['name']}({a['code']})</td><td>{a['action']}</td><td>{a['reason']}</td><td>{a['price']:.2f}</td><td>{a['qty']}</td><td>{emoji}{a['pnl']:+.0f}</td><td>{a['pnl_pct']:+.1f}%</td></tr>"
        content += "</table>"
        reporter.send_email(subject, content)
    
    action_word = "simulated" if dry_run else "executed"
    logger.info(f"=== Sentinel check complete. {len(alerts)} sell(s) {action_word} ===")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stock Analyzer one-shot position sentinel')
    parser.add_argument('--dry-run', action='store_true', help='Evaluate signals without executing sells')
    parser.add_argument('--no-email', action='store_true', help='Disable email sending')
    args = parser.parse_args()

    try:
        run_sentinel(dry_run=args.dry_run, no_email=args.no_email)
    except Exception as e:
        print(f"Sentinel error: {e}")
        import traceback
        traceback.print_exc()
