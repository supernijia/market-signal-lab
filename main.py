# -*- coding: utf-8 -*-
"""
Stock Analyzer Main Entry Point
"""
from __future__ import annotations

import sys
import argparse
import logging
import json
from datetime import datetime, timedelta
from core.config import Config
from core.utils import setup_logger, is_trading_hours
from core.data_provider import DataProvider
from core.portfolio import PortfolioManager, is_virtual_account
from core.analyzer import StockAnalyzer
from core.reporter import Reporter, log_report_snapshot
from core.strategy_tracker import StrategyTracker
from core.focus_monitor import FocusMonitor
from core.display_labels import humanize_text


def is_paper_training_route(args, target_account: str | None) -> bool:
    """Return True when the current run is a paper-only training route."""
    return bool(getattr(args, 'paper_trade', False) and str(target_account or '').startswith('paper_'))


def mark_paper_filter_bypass(candidate: dict, *, filter_name: str, reason: str | None, tag: str) -> dict:
    """Annotate a paper candidate that bypassed a real-account hard filter."""
    c = dict(candidate or {})
    tags = set(c.get('sector_rotation_tags') or [])
    tags.update({tag, 'PAPER_TRAINING_FILTER_BYPASS'})
    c['sector_rotation_tags'] = sorted(tags)
    c['paper_filter_bypass'] = True
    c['paper_filter_bypass_name'] = filter_name
    c['paper_original_filter_reason'] = c.get('paper_original_filter_reason') or reason or filter_name
    reasons = list(c.get('sector_rotation_reasons') or [])
    if reason:
        reasons.append(f"paper训练保留: {reason}")
    c['sector_rotation_reasons'] = reasons[:8]
    return c


def main():
    parser = argparse.ArgumentParser(description='Stock Analyzer')
    parser.add_argument('--mode', type=str, default='pre_market', help='Analysis mode: pre_market, afternoon, post_market, track, focus_monitor, analyze, watchlist, audit, risk_dashboard')
    parser.add_argument('--code', type=str, help='Stock code to analyze in analyze mode (e.g. 000001.SZ)')
    parser.add_argument('--buy-price', type=float, default=None, help='Your buy price for individual stock diagnosis')
    parser.add_argument('--buy-time', type=str, default=None, help='Your buy time for T+0 chart marking (e.g. "2026-03-06 10:30" or "10:30")')
    parser.add_argument('--hold-vol', type=int, default=None, help='Your holding volume for T+0 calculations')
    parser.add_argument('--no-email', action='store_true', help='Disable email sending')
    parser.add_argument('--auto-trade', action='store_true', help='Enable automatic simulated trading')
    parser.add_argument('--paper-trade', action='store_true', help='Route dynamic entry buys to paper_main/paper_watchlist only')
    parser.add_argument('--queue-entry', action='store_true', help='Create dynamic entry PENDING signals without executing buys')
    parser.add_argument('--monitor', action='store_true', help='Run one report-only snapshot without saving selections or auto-buying')
    parser.add_argument('--date', type=str, help='Specific date YYYYMMDD')
    parser.add_argument('--msg', type=str, help='Message content for notifications')
    parser.add_argument('--strategy', type=str, default='all', help='Strategy filter for replay/backtest modes')
    parser.add_argument('--horizon-days', type=int, default=3, help='Replay horizon in daily bars for replay_day mode')
    
    args = parser.parse_args()
    
    # Setup Logic
    logger = setup_logger()
    logger.info(f"Starting Stock Analyzer in {args.mode} mode...")
    logger.info(
        "Runtime flags: queue_entry=%s auto_trade=%s paper_trade=%s monitor=%s no_email=%s date=%s strategy=%s",
        args.queue_entry,
        args.auto_trade,
        args.paper_trade,
        args.monitor,
        args.no_email,
        args.date or "",
        args.strategy or "",
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

    # Initialize Modules
    provider = DataProvider()
    portfolio = PortfolioManager()
    
    # Init Tables
    portfolio.init_tables()
    
    analyzer = StockAnalyzer(provider)
    reporter = Reporter()
    tracker = StrategyTracker(portfolio, provider)
    focus_monitor = FocusMonitor(portfolio, provider, analyzer)
    
    # [V15] Macro Mode - Pre-market Macro Data Fetching
    if args.mode == 'macro':
        logger.info("Running Phase 1 Macro & Overseas Analysis...")
        
        # 1. Fetch crude oil data
        oil_data = provider.get_crude_oil_price()
        
        # 2. Fetch extended macro data (US, A50, FX)
        global_data = provider.get_global_macro()
        
        # Cache file path
        import os
        cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "macro_cache.json")
        
        # Determine signals
        oil_triggered = False
        if oil_data:
            oil_triggered = bool(oil_data['pct_change'] > 2)
            
        overseas_alert = False
        alert_reasons = []
        
        if global_data:
            dji = global_data.get('DJI', {})
            ixic = global_data.get('IXIC', {})
            a50 = global_data.get('XIN9', {})
            usd = global_data.get('USDCNH', {})
            
            # 美股大跌 > 2%
            if dji.get('pct_change', 0) < -2.0:
                alert_reasons.append(f"道指大跌 {dji['pct_change']:.2f}%")
            if ixic.get('pct_change', 0) < -2.5:
                alert_reasons.append(f"纳指暴跌 {ixic['pct_change']:.2f}%")
                
            # A50 期指大跌 > 1.5%
            if a50.get('pct_change', 0) < -1.5:
                alert_reasons.append(f"A50跌 {a50['pct_change']:.2f}%")
                
            # 离岸人民币急挫 (汇率上升代表贬值) > 0.5%
            if usd.get('pct_change', 0) > 0.5:
                alert_reasons.append(f"CNH贬值 {usd['pct_change']:.2f}%")
                
            if alert_reasons:
                overseas_alert = True
                
        # Save to cache file for later tasks (09:26 auction)
        cache_data = {
            'date': datetime.now().strftime('%Y%m%d'),
            'oil_data': oil_data,
            'oil_triggered': oil_triggered,
            'global_data': global_data,
            'overseas_alert': overseas_alert,
            'alert_reasons': alert_reasons,
            'triggered_at': datetime.now().isoformat()
        }
        
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Macro cache saved to {cache_file}")
            print(f"💾 Cache saved: oil_triggered={oil_triggered}, overseas_alert={overseas_alert}")
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")
        
        # Build Report Message
        msg_lines = ["🌐 【全域宏观视野】"]
        
        if oil_data:
            msg_lines.append(f"\n🛢️ 原油期货 (SC): {oil_data.get('settle', 0):.1f} ({oil_data.get('pct_change', 0):.2f}%)")
            if oil_triggered: msg_lines.append("  🔥 触发石油板块+15分抢筹!")
            
        if global_data:
            msg_lines.append("\n🌍 隔夜外盘与汇率:")
            dji = global_data.get('DJI', {})
            ixic = global_data.get('IXIC', {})
            a50 = global_data.get('XIN9', {})
            usd = global_data.get('USDCNH', {})
            
            msg_lines.append(f"  🇺🇸 道琼斯: {dji.get('close', 0):.1f} ({dji.get('pct_change', 0):.2f}%)")
            msg_lines.append(f"  🇺🇸 纳斯达克: {ixic.get('close', 0):.1f} ({ixic.get('pct_change', 0):.2f}%)")
            msg_lines.append(f"  🇨🇳 富时A50: {a50.get('close', 0):.1f} ({a50.get('pct_change', 0):.2f}%)")
            msg_lines.append(f"  💵 离岸人民币: {usd.get('close', 0):.4f} ({usd.get('pct_change', 0):.2f}%)")
            
            if overseas_alert:
                msg_lines.append(f"  ⚠️ 外盘跌破警戒线: {', '.join(alert_reasons)}")
            else:
                msg_lines.append("  ✅ 外围环境平稳")
                
        msg = "\n".join(msg_lines)
        log_report_snapshot("盘前宏观", msg, source=args.mode)
        print("\n" + msg + "\n")
        
        if not args.no_email:
            title_prefix = "⚠️【全球异动】" if overseas_alert else "🌐【盘前宏观】"
            subject = f"{title_prefix} 隔夜外盘数据播报"
            reporter.send_email(subject, msg)
            
        return
    
    # Analyze Mode (Individual Stock)
    if args.mode == 'analyze':
        if not args.code:
            print("Error: --code is required for analyze mode")
            return
        report_text = analyzer.analyze_individual_stock(
            args.code, 
            buy_price=args.buy_price, 
            buy_time=args.buy_time, 
            hold_vol=args.hold_vol
        )
        log_report_snapshot(f"个股深度诊断 {args.code}", report_text, source=args.mode)
        print(report_text)
        
        if not args.no_email:
            subject = f"【个股深度诊断】{args.code} ({datetime.now().strftime('%Y-%m-%d')})"
            reporter.send_email(subject, report_text)
            
        return

    # Track Mode
    if args.mode == 'track':
        track_result = tracker.run_daily_check()
        # Use reporter's rich tracker template
        report_text = reporter.format_tracker_report(track_result)
        # Append history stats
        report_text += "\n\n" + tracker.get_report()
        log_report_snapshot("策略追踪 T+1/T+2 绩效验证", report_text, source=args.mode)
        try:
            print(report_text)
        except UnicodeEncodeError:
            print(report_text.encode('gbk', 'replace').decode('gbk'))
        
        if not args.no_email:
             reporter.send_email(f"📊【策略追踪】T+1/T+2 绩效验证 ({datetime.now().strftime('%Y-%m-%d')})", report_text)
        return

    if args.mode in ('focus_monitor', 'focus'):
        logger.info("Running all-day focus monitor with audit-only shadow pending...")
        snapshot = focus_monitor.build_snapshot(trade_date=args.date, limit=15)
        try:
            env = analyzer.check_market_environment(args.date)
            weather = (env or {}).get("weather")
        except Exception:
            weather = None
        try:
            focus_monitor.record_shadow_pending(snapshot, weather=weather, market_env=env)
        except Exception as e:
            logger.warning(f"Failed to write shadow pending audit rows: {e}")
        if args.queue_entry and args.paper_trade:
            try:
                paper_summary = focus_monitor.record_paper_pending(snapshot, weather=weather, market_env=env)
                logger.info(
                    "PAPER_FOCUS_PENDING_SUMMARY written=%s strategies=%s bucket=%s",
                    paper_summary.get("written", 0),
                    paper_summary.get("strategies", []),
                    paper_summary.get("bucket", ""),
                )
            except Exception as e:
                logger.warning(f"Failed to write focus monitor paper pending rows: {e}")
        report_text = reporter.format_focus_monitor_report(snapshot)
        log_report_snapshot("全天重点雷达", report_text, source=args.mode)
        try:
            print(report_text)
        except UnicodeEncodeError:
            print(report_text.encode('gbk', 'replace').decode('gbk'))

        if not args.no_email:
            reporter.send_email(f"🎯【全天重点雷达】昨日入库/重点观察 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", report_text)
        return

    # [V16] Trade Quality Audit Mode
    if args.mode == 'audit':
        logger.info("Running V16 Trade Quality Audit...")
        from core.trade_auditor import TradeAuditor
        auditor = TradeAuditor(portfolio, provider)
        report = auditor.full_audit()
        log_report_snapshot("V16交易质量审计", report, source=args.mode)
        try:
            print(report)
        except UnicodeEncodeError:
            print(report.encode('gbk', 'replace').decode('gbk'))
        
        if not args.no_email:
            reporter.send_email(f"📋【V16交易质量审计】({datetime.now().strftime('%Y-%m-%d')})", report)
        return

    if args.mode == 'risk_dashboard':
        logger.info("Running read-only risk dashboard...")
        from core.risk_dashboard import RiskDashboard
        dashboard = RiskDashboard(portfolio, provider, analyzer)
        report = dashboard.format_markdown(date=args.date)
        log_report_snapshot("风控仪表盘", report, source=args.mode)
        try:
            print(report)
        except UnicodeEncodeError:
            print(report.encode('gbk', 'replace').decode('gbk'))

        if not args.no_email:
            reporter.send_email(f"📊【风控仪表盘】({datetime.now().strftime('%Y-%m-%d %H:%M')})", report)
        return

    if args.mode in ('replay_day', 'backtest'):
        logger.info("Running A-share constrained replay...")
        from core.backtester import AShareReplayBacktester, ReplayConfig

        cfg = ReplayConfig(horizon_days=max(1, int(args.horizon_days or 3)))
        replay = AShareReplayBacktester(portfolio, provider, cfg)
        replay_date = args.date or datetime.now().strftime('%Y%m%d')
        result = replay.replay_day(replay_date, strategy=args.strategy, code=args.code)
        report = replay.format_report(result)
        log_report_snapshot(f"A股约束回放 {replay_date}", report, source=args.mode)
        try:
            print(report)
        except UnicodeEncodeError:
            print(report.encode('gbk', 'replace').decode('gbk'))

        if not args.no_email:
            reporter.send_email(f"📼【A股约束回放】{replay_date}", report)
        return

    # Notification Only Mode (used by evolve_strategy etc)
    if args.mode == 'notify_update':
        msg = args.msg or "No message provided."
        log_report_snapshot("策略进化自学习引擎周报", msg.replace('\\n', '\n'), source=args.mode)
        try:
            # Replace escaped newlines back to actual newlines
            msg = msg.replace('\\n', '\n')
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('gbk', 'replace').decode('gbk'))
            
        if not args.no_email:
            date_str = datetime.now().strftime('%Y-%m-%d')
            reporter.send_email(f"✨【策略进化】自学习引擎周报 {date_str}", msg)
        return

    # Run Analysis
    logger.info("Running analysis...")
    
    def run_job():
        current_time = datetime.now()
        logger.info(f"Running scheduled analysis job... {current_time}")
        
        base_target_account = 'watchlist' if args.mode == 'watchlist' else 'main'
        if getattr(args, 'paper_trade', False):
            target_account = 'paper_watchlist' if base_target_account == 'watchlist' else 'paper_main'
        else:
            target_account = base_target_account
        
        # Load Both Portfolios for Reporting
        def build_portfolio_summary(account_id):
            positions = portfolio.load_positions(account=account_id)
            cash = portfolio.load_cash(account=account_id)
            
            summary = {
                'total_asset': 0.0,
                'cash': cash,
                'market_value': 0.0,
                'positions': []
            }
            if positions:
                # Map code to ts_code (heuristic or from provider)
                basics = provider.get_stock_basic()
                code_ts_map = {}
                for k in basics:
                    code_ts_map[k.split('.')[0]] = k
                    
                ts_codes = []
                for p in positions:
                    c = p['code']
                    if c in code_ts_map: ts_codes.append(code_ts_map[c])
                
                rt_data = provider.get_realtime_quotes(ts_codes)
                total_mv = 0.0
                
                for p in positions:
                    code = p['code']
                    ts_code = code_ts_map.get(code)
                    
                    buy_price = float(p.get('avg_price', 0))
                    if buy_price <= 0: buy_price = float(p.get('buy_price', 0))
                    curr_price = float(p.get('current_price', 0) or buy_price)
                    name = p['name']
                    
                    if ts_code and ts_code in rt_data:
                        curr_price = float(rt_data[ts_code]['price'])
                        curr_high = float(rt_data[ts_code].get('high', curr_price))
                        name = rt_data[ts_code]['name']
                    else:
                        curr_high = curr_price
                    
                    qty = int(p['quantity'])
                    cost = buy_price * qty
                    mv = curr_price * qty
                    pnl = mv - cost
                    pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                    
                    p_data = {
                        'code': code,
                        'name': name,
                        'account': p.get('account', account_id),
                        'quantity': qty,
                        'buy_price': buy_price,
                        'current_price': curr_price,
                        'curr_high': curr_high,
                        'highest_price': float(p.get('highest_price', 0) or 0),
                        'market_value': mv,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'created_at': p.get('created_at', ''),
                        'update_time': p.get('update_time', '')
                    }
                    summary['positions'].append(p_data)
                    total_mv += mv
                
                summary['market_value'] = total_mv
                summary['total_asset'] = cash + total_mv
            else:
                summary['total_asset'] = cash
            return summary
        
        # Initialize summaries
        portfolio_summary_main = build_portfolio_summary('main')
        portfolio_summary_watch = build_portfolio_summary('watchlist')
        portfolio_summary_rescue = build_portfolio_summary('rescue')
        portfolio_summary_paper_main = build_portfolio_summary('paper_main')
        portfolio_summary_paper_watch = build_portfolio_summary('paper_watchlist')
        
        # Determine current positions for risk control tracking based on target account
        current_summary = build_portfolio_summary(target_account)
        
        # --- 1. PORTFOLIO REAL-TIME TRACKING ---
        # [V21] 职责分离: main.py 仅负责"选股+买入"，所有卖出监控(止损/止盈/时间止损)
        # 由 monitor.py (哨兵模式) 独立执行，避免多个定时任务重复发送卖出邮件。

        # --- 2. MARKET ANALYSIS ---
        if args.mode == 'afternoon':
            # New 14:30 Strategy
            logger.info("Running Afternoon Money Flow Strategy...")
            hot_stocks = analyzer.analyze_sector_flow_afternoon()
            result = {
                'hot_stocks': hot_stocks,
                'candidates_count': len(hot_stocks),
                'total_stocks': len(hot_stocks),
                'sector_analysis': [],
                'fund_analysis': {},
                'recommendation': {
                    'market_power': '资金流向',
                    'position': '根据信号',
                    'suggestion': '关注资金流向强势板块',
                    'top3': [s['name'] for s in hot_stocks[:3]]
                },
                'limit_up_analysis': {},
                'auction_picks': [],
                'sell_signals': {}
            }

            # Attach market_env for snapshot tags
            try:
                result['market_env'] = analyzer.check_market_environment(args.date)
            except Exception:
                result['market_env'] = {}

            # Save Selection for Tracking (T+1). Report-only snapshots must not
            # mutate strategy_selection or factor_snapshot.
            if not args.monitor:
                sel_save = portfolio.save_selection(
                    hot_stocks,
                    args.date or datetime.now().strftime('%Y-%m-%d'),
                    '午盘精选',
                    cycle='T+1',
                    market_env=result.get('market_env')
                )
                result['db_write'] = result.get('db_write', {})
                result['db_write']['午盘精选'] = sel_save

        elif args.mode == 'post_market':
            # New 16:00 Strategy
            logger.info("Running Post-Market Money Flow Strategy...")
            post_picks = analyzer.analyze_sector_flow_post_market()
            moneyflow_date = None
            moneyflow_preferred_date = None
            if post_picks:
                moneyflow_date = post_picks[0].get('_moneyflow_date') or post_picks[0].get('moneyflow_date')
                moneyflow_preferred_date = post_picks[0].get('_moneyflow_preferred_date')
            post_dq = getattr(analyzer, 'last_data_quality', {}).get('post_market', {}) or {}
            moneyflow_date = moneyflow_date or post_dq.get('moneyflow_date')
            moneyflow_preferred_date = moneyflow_preferred_date or post_dq.get('moneyflow_preferred_date')
            result = {
                'hot_stocks': post_picks,
                'candidates_count': len(post_picks),
                'total_stocks': len(post_picks),
                'sector_analysis': [],
                'fund_analysis': {},
                'recommendation': {
                    'market_power': '盘后梳理',
                    'position': '无',
                    'suggestion': '盘后资金流向选股 (明日备选)',
                    'top3': [s['name'] for s in post_picks[:3]]
                },
                'limit_up_analysis': {},
                'auction_picks': [],
                'sell_signals': {}
            }
            if moneyflow_date:
                result['data_quality'] = {
                    'moneyflow_date': moneyflow_date,
                    'moneyflow_preferred_date': moneyflow_preferred_date or moneyflow_date,
                    'moneyflow_fallback': bool(post_dq.get('moneyflow_fallback') or (moneyflow_preferred_date and moneyflow_preferred_date != moneyflow_date)),
                    'filter_stats': post_dq.get('filter_stats') or {},
                }

            # Attach market_env for snapshot tags
            try:
                result['market_env'] = analyzer.check_market_environment(args.date)
            except Exception:
                result['market_env'] = {}

            # Save Selection for Observation (T+2). Report-only snapshots must
            # not mutate strategy_selection or factor_snapshot.
            if not args.monitor:
                sel_save = portfolio.save_selection(
                    post_picks,
                    args.date or datetime.now().strftime('%Y-%m-%d'),
                    '盘后资金流',
                    cycle='T+2',
                    market_env=result.get('market_env')
                )
                result['db_write'] = result.get('db_write', {})
                result['db_write']['盘后资金流'] = sel_save

        elif args.mode == 'watchlist':
             # [V9] Watchlist Lifecycle Monitoring
             logger.info("Running Watchlist Lifecycle Monitoring...")
             watchlist_result = analyzer.monitor_watchlist(portfolio, write_changes=not args.monitor)
             result = {
                 'hot_stocks': watchlist_result['buy_candidates'],
                 'candidates_count': len(watchlist_result['buy_candidates']),
                 'total_stocks': len(portfolio.get_watchlist(days=Config.STRATEGY.get('watchlist', {}).get('observe_days', Config.STRATEGY.get('watchlist', {}).get('days', 5)))),
                 'sector_analysis': [],
                 'fund_analysis': {},
                 'recommendation': {
                     'market_power': '备选池巡航',
                     'position': '按纪律执行',
                     'suggestion': f"剔除破位 {len(watchlist_result['removed'])} 只, 到期停止观察 {len(watchlist_result.get('expired', []))} 只, 潜伏观察 {len(watchlist_result['observed'])} 只",
                     'top3': [s['name'] for s in watchlist_result['buy_candidates'][:3]]
                 },
                 'limit_up_analysis': {},
                 'auction_picks': [],
                 'sell_signals': {},
                 'watchlist_data': watchlist_result,
                 'concept_heat': watchlist_result.get('concept_heat') or {},
             }

        else:
             # Default (Pre-market / Limit-up analysis)
             # Load main or watchlist portfolio
             current_summary = build_portfolio_summary(target_account)
             # Get market environment
             try:
                 market_env = analyzer.check_market_environment()
             except Exception as e:
                 logger.error(f"Failed to check market environment: {e}")
                 market_env = {'weather': '未知', 'desc': '环境检测失败', 'sug': '-'}
                 
             result = analyzer.analyze(args.date, holdings=current_summary['positions'])
             
             # Inject portfolio data into result so reporter can use it
             result[f'portfolio_{target_account}'] = current_summary
                 
             # Add to report
             result['market_env'] = market_env
             
             # Save Auction/Pre-market picks if any (仅在非--monitor模式下入库，避免11:30午间体检重复入库)
             if not args.monitor:
                sel_date = args.date or datetime.now().strftime('%Y-%m-%d')

                # ① 竞价评分 → 入库 (T+1)
                if 'auction_picks' in result and result['auction_picks']:
                    sel_save = portfolio.save_selection(
                        result['auction_picks'],
                        sel_date,
                        '集合竞价',
                        cycle='T+1',
                        market_env=result.get('market_env'),
                    )
                    result['db_write'] = result.get('db_write', {})
                    result['db_write']['集合竞价'] = sel_save

                # ①.5 冷启动 → 入库 (T+1)
                if 'cold_start_picks' in result and result['cold_start_picks']:
                    sel_save = portfolio.save_selection(
                        result['cold_start_picks'],
                        sel_date,
                        '冷启动',
                        cycle='T+1',
                        market_env=result.get('market_env'),
                    )
                    result['db_write'] = result.get('db_write', {})
                    result['db_write']['冷启动'] = sel_save

                # ② 涨停龙头 → 提取每个板块最强龙头 → 入备选池
                if 'limit_up_analysis' in result:
                    lua = result['limit_up_analysis']
                    leader_picks = []
                    for sector_info in lua.get('sectors', [])[:5]:  # Top 5 板块
                        hb = sector_info.get('highest_board')
                        if hb and hb.get('ts_code'):
                            leader_picks.append({
                                'code': hb['ts_code'].split('.')[0],
                                'ts_code': hb['ts_code'],
                                'name': hb.get('name', ''),
                                'price': float(hb.get('price', hb.get('close', 0))),
                                'change': float(hb.get('pct_chg', 0)),
                                'industry': sector_info.get('sector', ''),
                                'turnover': float(hb.get('turnover', hb.get('turnover_rate', 0))),
                                'reason': f"板块{sector_info['sector']}龙头 (连板{hb.get('limit_times', 1)}天, 板块{sector_info['count']}只涨停)",
                            })
                    if leader_picks:
                        result['leader_picks'] = leader_picks
                        sel_save = portfolio.save_selection(
                            leader_picks,
                            sel_date,
                            '龙头跟踪',
                            cycle='T+1',
                            market_env=result.get('market_env'),
                        )
                        result['db_write'] = result.get('db_write', {})
                        result['db_write']['龙头跟踪'] = sel_save
                        logger.info(f"📌 涨停龙头入库备选: {len(leader_picks)}只 → {[s['name'] for s in leader_picks]}")

                # ③ 技术突破精选 → 入备选池 (T+1)
                if 'hot_stocks' in result and result['hot_stocks']:
                    macd_picks = result['hot_stocks'][:5]
                    result['technical_picks'] = macd_picks
                    sel_save = portfolio.save_selection(
                        macd_picks,
                        sel_date,
                        '技术突破',
                        cycle='T+1',
                        market_env=result.get('market_env'),
                    )
                    result['db_write'] = result.get('db_write', {})
                    result['db_write']['技术突破'] = sel_save
                    logger.info(f"📌 MACD金叉入库备选: {len(macd_picks)}只 → {[s['name'] for s in macd_picks]}")

        result['portfolio_main'] = portfolio_summary_main
        result['portfolio_watch'] = portfolio_summary_watch
        if portfolio_summary_rescue and portfolio_summary_rescue.get('positions'):
            result['portfolio_rescue'] = portfolio_summary_rescue
        if portfolio_summary_paper_main and portfolio_summary_paper_main.get('positions'):
            result['portfolio_paper_main'] = portfolio_summary_paper_main
        if portfolio_summary_paper_watch and portfolio_summary_paper_watch.get('positions'):
            result['portfolio_paper_watchlist'] = portfolio_summary_paper_watch
            
        
        # --- 3. AUTO-BUY EXECUTION (Delegated to Strategy Executor) ---
        execute_automated_strategies(analyzer, portfolio, provider, reporter, result, current_summary, args)

        # --- 3.5. READ-ONLY FOCUS RADAR ---
        if args.mode in ['pre_market', 'afternoon', 'post_market', 'watchlist']:
            try:
                result['today_focus'] = focus_monitor.build_today_focus(result, args.mode, limit=8)
                result['focus_monitor'] = focus_monitor.build_snapshot(trade_date=args.date, limit=5)
                focus_monitor.record_shadow_pending(
                    result['focus_monitor'],
                    weather=(result.get('market_env') or {}).get('weather'),
                    market_env=result.get('market_env'),
                )
            except Exception as e:
                logger.warning(f"Failed to build focus monitor snapshot: {e}")

        # [VNext] Persist market environment snapshot (best-effort)
        try:
            env_to_save = result.get('market_env')
            if env_to_save and args.mode in ['pre_market', 'afternoon', 'post_market', 'watchlist']:
                # DB uses YYYY-MM-DD, while args.date is YYYYMMDD
                if args.date and len(args.date) == 8:
                    trade_date_str = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
                else:
                    trade_date_str = datetime.now().strftime('%Y-%m-%d')
                portfolio.save_market_sentiment(trade_date_str, env_to_save)

                # [P1] Persist concept heat (best-effort)
                ch = result.get('concept_heat')
                if ch:
                    portfolio.save_concept_heat(trade_date_str, ch)
        except Exception as e:
            logger.warning(f"Failed to persist market sentiment: {e}")

        # Report
        result['report_only'] = bool(args.monitor)
        report_text = reporter.format_report(result, mode=args.mode)

        # Mode-specific email subjects with time-awareness
        date_str = current_time.strftime('%Y-%m-%d')
        hour = current_time.hour
        
        mode_subjects = {
            'pre_market': f"🌅【早盘竞价】A股选股报告 {date_str}" if hour < 10 else f"📋【午间体检】早盘战果汇总 {date_str} ({hour}:{current_time.strftime('%M')})",
            'afternoon': f"☀️【午盘资金流】A股精选报告 {date_str}",
            'post_market': f"🌙【收盘复盘】明日备选报告 {date_str}",
            'watchlist': f"🔭【备选池巡航】生命周期监控报告 {date_str}",
        }
        email_subject = mode_subjects.get(args.mode, f"[{args.mode}] 股票分析报告 {date_str}")
        
        # [V10] 添加市场天气前缀
        try:
            from core.utils import normalize_weather
            weather = normalize_weather(analyzer.check_market_environment(args.date).get('weather', '☀️晴天'))
        except Exception:
            weather = '☀️晴天'
        if args.mode in ['pre_market', 'afternoon', 'post_market', 'watchlist']:
            email_subject = email_subject.replace('☀️', weather).replace('🌙', weather).replace('🔭', weather)
            if weather not in email_subject:
                email_subject = f"{weather} {email_subject}"

        log_report_snapshot(email_subject, report_text, source=args.mode)
        
        if args.monitor:
             logger.info("Report-only snapshot completed.")
             if not args.no_email:
                 monitor_time = current_time.strftime('%H:%M')
                 if f"({monitor_time})" not in email_subject:
                     email_subject = f"{email_subject} ({monitor_time})"
                 reporter.send_email(email_subject, report_text)
        else:
             try:
                 print("\n" + report_text + "\n")
             except UnicodeEncodeError:
                 import sys
                 sys.stdout.buffer.write(("\n" + report_text + "\n").encode('utf-8', errors='replace'))
                 
             if not args.no_email:
                 reporter.send_email(email_subject, report_text)
             
    # Standard OpenClaw jobs are one-shot. Long-running sell/position monitoring
    # belongs in monitor.py; main.py --monitor means report-only snapshot.
    run_job()

def execute_automated_strategies(analyzer, portfolio, provider, reporter, result, current_summary, args):
    """
    [V9] Specialized Execution Block: Separates BUY logic from Main Analysis Loop.
    This component handles candidates from Auction, Watchlist, and Afternoon strategies.
    """
    from core.utils import is_trading_hours
    from core.config import Config
    from core.entry_flow import verify_entry_flow
    from core.paper_account import account_position_count, mirror_paper_buy
    from core.pre_trade_gate import canonical_strategy_name, evaluate_pre_trade_gate, merge_entry_confirm_tags_json, merge_signal_tags_json
    from core.position_sizer import PositionSizer
    from core.sector_rotation import SectorRotation
    import logging
    logger = logging.getLogger("StockAnalyzer.Executor")

    execution_audit = result.setdefault('execution_audit', [])

    def audit(status, reason, *, code=None, name=None, strategy=None):
        execution_audit.append({
            'status': status,
            'reason': str(reason or '')[:255],
            'code': code,
            'name': name,
            'strategy': strategy,
        })

    def _candidate_attempt_base(candidate=None, *, strategy=None, stage='candidate_selected', action='OBSERVE', reason='', extra=None):
        """Structured candidate attempt/no-attempt evidence for training audits."""
        cand = candidate or {}
        attempt = {
            'schema': 'candidate_attempt_v1',
            'stage': stage,
            'action': action,
            'reason': str(reason or '')[:255],
            'strategy': strategy or cand.get('strategy') or strategy_name,
            'code': cand.get('code'),
            'name': cand.get('name'),
            'queue_entry_intent': bool(getattr(args, 'queue_entry', False)),
            'auto_trade_intent': bool(getattr(args, 'auto_trade', False)),
            'paper_trade': bool(getattr(args, 'paper_trade', False)),
            'mode': getattr(args, 'mode', None),
        }
        if isinstance(extra, dict):
            attempt.update(extra)
        return attempt

    def _audit_with_attempt(status, reason, *, code=None, name=None, strategy=None, attempt=None):
        audit(status, reason, code=code, name=name, strategy=strategy)
        if attempt and execution_audit:
            execution_audit[-1]['candidate_attempt'] = attempt

    def _json_dumps_or_none(value):
        if value in (None, "", []):
            return None
        try:
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        except Exception:
            return None

    def _candidate_base_tags_json(candidate):
        """Keep observe/training tags attached when a signal becomes pending."""
        candidate = candidate or {}
        direct = candidate.get("tags_json") or candidate.get("signal_tags_json") or candidate.get("entry_tags_json")
        if direct:
            return _json_dumps_or_none(direct)
        tags = []
        for tag in candidate.get("cold_start_model_tags") or []:
            tags.append({"tag": str(tag), "weight": 0, "reason": "cold-start observe model"})
        if candidate.get("cold_start_delayed_confirm"):
            tags.append({"tag": "COLD_START_CHAIN_DELAYED_CONFIRM", "weight": 0, "reason": "cold-start delayed confirm"})
        if candidate.get("cold_start_early_absorb"):
            tags.append({"tag": "COLD_START_CHAIN_EARLY_ABSORB", "weight": 0, "reason": "cold-start early absorb"})
        if candidate.get("cold_start_pullback_entry_candidate"):
            tags.append({"tag": "COLD_START_PULLBACK_ENTRY_WATCH", "weight": 0, "reason": "cold-start pullback watch"})
        if "冷启动" in str(candidate.get("strategy") or strategy_name or ""):
            tags.append({"tag": "COLD_START_OBSERVE_SIGNAL", "weight": 0, "reason": "strategy=冷启动"})
        return _json_dumps_or_none(tags)

    # [V9] Market Timing Check & Role Protection
    queue_entry_intent = bool(getattr(args, 'queue_entry', False))
    execution_intent = bool(getattr(args, 'auto_trade', False)) and not queue_entry_intent
    if queue_entry_intent and bool(getattr(args, 'auto_trade', False)):
        audit('INFO', '--queue-entry 优先于 --auto-trade，本次只创建PENDING，不执行买入')
    if not ((execution_intent or queue_entry_intent) and is_trading_hours(allow_auction=True)):
        if queue_entry_intent:
            audit('INFO', '当前不在允许交易时段，跳过动态入场PENDING入队')
        elif not execution_intent:
            audit('INFO', '未启用 --auto-trade/--queue-entry，本次只做选股/入库/邮件报告')
        else:
            audit('INFO', '当前不在允许交易时段，跳过自动买入')
        return

    # [V10] 市场环境感知系统
    market_env = analyzer.check_market_environment(args.date)
    try:
        from core.utils import normalize_weather
        weather = normalize_weather(market_env.get('weather', '☀️晴天'))
    except Exception:
        weather = market_env.get('weather', '☀️晴天')
    is_safe = market_env.get('is_safe', True)
    market_status = market_env.get('message', '')
    adjustments = market_env.get('adjustments', {})

    # 提取调整参数
    max_position_mult = adjustments.get('max_position_mult', 1.0)
    score_threshold_mult = adjustments.get('score_threshold_mult', 1.0)

    # Determine Candidate Pool
    candidates = []
    strategy_name = ""
    current_time = datetime.now()
    hour = current_time.hour
    minute = current_time.minute
    base_target_account = 'watchlist' if args.mode == 'watchlist' else 'main'
    if getattr(args, 'paper_trade', False):
        target_account = 'paper_watchlist' if base_target_account == 'watchlist' else 'paper_main'
        audit('INFO', f"--paper-trade 已启用: 本次只路由到 {target_account}，不触碰 {base_target_account}")
    else:
        target_account = base_target_account

    def _weak_entry_gate_cfg():
        cfg = Config.STRATEGY.get('weak_market_entry_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _storm_probe_enabled():
        return bool(_weak_entry_gate_cfg().get('allow_storm_pending_probe', False))

    def _storm_probe_max_per_run():
        try:
            return max(0, int(_weak_entry_gate_cfg().get('storm_pending_probe_max_per_run', 1) or 1))
        except Exception:
            return 1

    def _paper_strong_entry_cfg():
        cfg = Config.STRATEGY.get('paper_strong_entry_experiment', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _paper_strong_change_ceiling(candidate):
        cfg = _paper_strong_entry_cfg()
        board_limits = cfg.get('board_change_ceiling_pct', {}) if isinstance(cfg.get('board_change_ceiling_pct'), dict) else {}
        code = str((candidate or {}).get('code') or '').strip()
        name = str((candidate or {}).get('name') or '')
        if 'ST' in name.upper():
            return float(board_limits.get('st', 5.2) or 5.2)
        if code.startswith(('300', '301', '688', '689', '4', '8', '9')):
            return float(board_limits.get('growth', 20.2) or 20.2)
        return float(board_limits.get('main', 10.2) or 10.2)

    def _mark_paper_strong_entry(candidate, *, reason, filter_name, metrics=None):
        """Route strong watchlist tickets into paper only, preserving real gates."""
        if not (getattr(args, 'paper_trade', False) and args.mode == 'watchlist' and target_account == 'paper_watchlist'):
            return False
        cfg = _paper_strong_entry_cfg()
        if not bool(cfg.get('enabled', True)):
            return False
        candidate = candidate or {}
        change = float(candidate.get('change', 0) or candidate.get('pct_chg', 0) or 0)
        min_change = float(cfg.get('min_current_change_pct', 5.0) or 5.0)
        ceiling = _paper_strong_change_ceiling(candidate)
        if change < min_change or change > ceiling:
            return False

        candidate['paper_strong_entry'] = True
        candidate['paper_experiment'] = True
        candidate['paper_experiment_type'] = 'watchlist_strong_entry'
        candidate['paper_experiment_reason'] = filter_name
        candidate['paper_original_filter_reason'] = reason
        candidate['paper_max_buy_change'] = ceiling
        if metrics:
            candidate['paper_experiment_metrics'] = metrics
        logger.warning(
            "PAPER_STRONG_PENDING_ROUTE code=%s name=%s change=%.2f min_change=%.2f ceiling=%.2f original_filter=%s",
            candidate.get('code'),
            candidate.get('name'),
            change,
            min_change,
            ceiling,
            filter_name,
        )
        return True

    def _paper_all_pool_cfg():
        cfg = Config.STRATEGY.get('paper_all_pool_execution', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _paper_all_pool_enabled():
        return bool(getattr(args, 'paper_trade', False) and _paper_all_pool_cfg().get('enabled', True))

    def _paper_pool_limit(strategy):
        cfg = _paper_all_pool_cfg()
        limits = cfg.get('max_candidates_per_strategy', {}) if isinstance(cfg.get('max_candidates_per_strategy'), dict) else {}
        try:
            return int(limits.get(strategy, cfg.get('default_max_candidates', 5)) or 5)
        except Exception:
            return 5

    def _paper_pool_candidates(pool, strategy):
        rows = []
        seen = set()
        for raw in (pool or [])[:_paper_pool_limit(strategy)]:
            if not isinstance(raw, dict):
                continue
            c = dict(raw)
            code = str(c.get('code') or c.get('ts_code') or '').split('.')[0]
            if not code or code in seen:
                continue
            seen.add(code)
            c['strategy'] = strategy
            c['paper_executable_pool'] = True
            c['paper_source_pool'] = strategy
            c['paper_experiment'] = True
            c['paper_experiment_type'] = 'all_pool_shadow_buyable'
            c['paper_experiment_reason'] = 'ALL_SELECTION_POOLS_TO_PAPER_PENDING'
            c['paper_strong_entry'] = True
            c['paper_max_buy_change'] = _paper_strong_change_ceiling(c)
            rows.append(c)
        return rows

    def _collect_paper_pre_market_all_pools():
        rows = []
        rows.extend(_paper_pool_candidates(result.get('auction_picks') or [], '集合竞价'))
        rows.extend(_paper_pool_candidates(result.get('cold_start_picks') or [], '冷启动'))
        rows.extend(_paper_pool_candidates(result.get('leader_picks') or [], '龙头跟踪'))
        rows.extend(_paper_pool_candidates(result.get('technical_picks') or result.get('hot_stocks') or [], '技术突破'))
        deduped = []
        seen = set()
        for c in rows:
            key = (str(c.get('code') or ''), str(c.get('strategy') or ''))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        return deduped

    def _log_entry_audit_event(candidate, *, stage, reason, strategy=None, action='BLOCK', tags=None, metrics=None, no_attempt_reason=None, attempt=None):
        """Best-effort candidate audit without changing execution state."""
        cand = candidate or {}
        try:
            strategy_key = canonical_strategy_name(strategy or cand.get('strategy') or strategy_name, args.mode)
            audit_action = 'SKIPPED' if action in ('FILTERED', 'SKIP') else action
            if attempt is None:
                inferred_no_attempt_reason = no_attempt_reason
                if inferred_no_attempt_reason is None and audit_action in ('SKIPPED', 'BLOCK', 'CANCELLED'):
                    inferred_no_attempt_reason = (tags or ['PRE_PENDING_FILTER'])[0]
                attempt = _candidate_attempt_base(
                    cand,
                    strategy=strategy_key,
                    stage=stage,
                    action=audit_action,
                    reason=str(reason or '')[:255],
                    extra={
                        'target_account': target_account,
                        'no_attempt_reason': inferred_no_attempt_reason,
                        'filter_tags': tags or [],
                        'market_regime': market_env.get('regime'),
                        'risk_level': market_env.get('risk_level'),
                    },
                )
            audit_metrics = dict(metrics or {})
            audit_metrics['candidate_attempt'] = attempt
            portfolio.log_risk_event(
                account=target_account,
                code=cand.get('code'),
                event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                weather=weather,
                reason=str(reason or '')[:255],
                params={
                    'stage': stage,
                    'strategy': strategy_key,
                    'action': action,
                    'tags': tags or [],
                    'metrics': audit_metrics,
                    'audit_only': True,
                    'market_regime': market_env.get('regime'),
                    'risk_level': market_env.get('risk_level'),
                    'market_message': market_status,
                    'candidate_attempt': attempt,
                }
            )
            _audit_with_attempt(action, reason, code=cand.get('code'), name=cand.get('name'), strategy=strategy_key, attempt=attempt)
        except Exception:
            pass

    # [VNext] Attack window gate (weather × time bucket) for capital safety (default disabled)
    attack_gate_cfg = Config.STRATEGY.get('attack_window_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    attack_gate_enabled = bool(attack_gate_cfg.get('enabled', False))
    force_queue_block_reason = None

    def _get_time_bucket_now() -> str | None:
        try:
            hh = int(hour)
            mm = int(minute)
        except Exception:
            return None

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

    if attack_gate_enabled:
        curr_bucket = _get_time_bucket_now()
        allowed = (attack_gate_cfg.get('rules', {}) or {}).get(weather, None)
        if allowed is not None:
            # If current bucket is not in allowed list, stop auto-buy early.
            if not curr_bucket or curr_bucket not in set(allowed or []):
                logger.warning(f"⛔ 进攻窗口门禁触发: weather={weather} bucket={curr_bucket} 不在允许窗口 {allowed}")
                reason = f"进攻窗口门禁: weather={weather} bucket={curr_bucket} 不在允许窗口 {allowed}"
                if queue_entry_intent:
                    force_queue_block_reason = reason
                    audit('PAUSED', f"{reason}；继续候选审计但不入PENDING")
                else:
                    audit('PAUSED', reason)
                    return

    if not is_safe:
        logger.warning(f"⚠️ 市场环境恶劣，停止/减少建仓: {market_status}")
        logger.warning(f"   仓位调整: {max_position_mult*100}% | Score门槛调整: +{(score_threshold_mult-1)*100:.0f}%")
        # 暴雨天气完全停止建仓
        if market_env.get('risk_level') == 'high':
            logger.warning("⚠️ 极端市场环境(暴雨)，暂停直接买入操作")
            if queue_entry_intent:
                if _storm_probe_enabled():
                    audit('PAUSED', f"极端市场环境仅允许确认队列探针: max_per_run={_storm_probe_max_per_run()} {market_status}")
                else:
                    force_queue_block_reason = f"极端市场环境暂停入队: {market_status}"
                    audit('PAUSED', force_queue_block_reason)
            else:
                audit('PAUSED', f"极端市场环境暂停买入: {market_status}")
                return

    logger.info(f"市场环境检测: {weather} - {market_status}")
    
    # [V10] Step 2: 板块热度扫描
    industry_stats = []
    try:
        industry_stats = provider.get_industry_stats(args.date)
        if industry_stats:
            logger.info(f"板块热度扫描: {len(industry_stats)} 个行业, Top3: {[s['industry'] for s in industry_stats[:3]]}")
    except Exception as e:
        logger.warning(f"板块热度扫描失败: {e}")
    
    # Selection Logic
    if args.mode == 'pre_market' or (args.monitor and hour == 9 and 25 <= minute <= 35):
        if _paper_all_pool_enabled() and args.mode == 'pre_market':
            candidates = _collect_paper_pre_market_all_pools()
            strategy_name = "影子全池可买"
            logger.info(
                "PAPER_ALL_POOL_PENDING_ROUTE mode=pre_market candidates=%s strategies=%s",
                len(candidates),
                sorted({str(c.get('strategy') or '') for c in candidates}),
            )
        elif 'auction_picks' in result and result['auction_picks']:
            candidates = result['auction_picks'][:3] 
            strategy_name = "早盘竞价首选"
            for c in candidates:
                if c.get('open_change', 0) > 4.0:
                    c['_reduce_position'] = True

    elif args.mode == 'afternoon' or (args.monitor and hour == 14 and 30 <= minute <= 45):
        if 'hot_stocks' in result and result['hot_stocks']:
            candidates = result['hot_stocks'][:3]
            strategy_name = "午盘精选"
            
    elif args.mode == 'watchlist' or (args.monitor and 10 <= hour <= 14):
        if 'watchlist_data' in result and result['watchlist_data']['buy_candidates']:
            raw_cands = result['watchlist_data']['buy_candidates']
            max_chg = Config.RISK_MANAGEMENT.get("MAX_BUY_CHANGE", 7.0)
            
            # [V21] 增强过滤：曾涨停/高开低走过滤
            safe_cands = []
            for c in raw_cands:
                change = c.get('change', 0) or 0
                open_change = c.get('open_change', 0) or 0
                high = c.get('high', 0) or 0
                pre_close = c.get('pre_close', 0) or 0
                
                # 计算当日最高涨幅
                if high > 0 and pre_close > 0:
                    high_change = (high - pre_close) / pre_close * 100
                else:
                    high_change = 0
                
                # 过滤1: 曾涨停的不买 (最高涨幅>=9.5%)
                if high_change >= 9.5:
                    reason = f"买入过滤: 曾涨停(最高{high_change:.1f}%)"
                    metrics = {
                        'high_change': high_change,
                        'max_buy_change': max_chg,
                        'filter': 'watchlist_limit_touch_filter',
                    }
                    if _mark_paper_strong_entry(
                        c,
                        reason=reason,
                        filter_name='WATCHLIST_LIMIT_TOUCH_FILTER',
                        metrics=metrics,
                    ):
                        safe_cands.append(c)
                        _log_entry_audit_event(
                            c,
                            stage='pre_pending_filter',
                            strategy='备选池买入触发',
                            action='QUEUED',
                            reason=f"{reason}；影子强票实验继续入PENDING",
                            tags=['PRE_PENDING_FILTER', 'WATCHLIST_LIMIT_TOUCH_FILTER', 'PAPER_STRONG_ENTRY_ROUTE'],
                            no_attempt_reason=None,
                            metrics=metrics,
                        )
                        continue
                    logger.warning(f"   🚫 {c['name']} {reason}")
                    _log_entry_audit_event(
                        c,
                        stage='pre_pending_filter',
                        strategy='备选池买入触发',
                        action='FILTERED',
                        reason=reason,
                        tags=['PRE_PENDING_FILTER', 'WATCHLIST_LIMIT_TOUCH_FILTER'],
                        no_attempt_reason='WATCHLIST_LIMIT_TOUCH_FILTER',
                        metrics=metrics,
                    )
                    continue
                
                # 过滤2: 高开低走不买 (开盘>5% 且 现在下跌)
                if open_change > 5 and change < 0:
                    reason = f"买入过滤: 高开低走(开{open_change:.1f}% → 现{change:.1f}%)"
                    logger.warning(f"   🚫 {c['name']} {reason}")
                    _log_entry_audit_event(
                        c,
                        stage='pre_pending_filter',
                        strategy='备选池买入触发',
                        action='FILTERED',
                        reason=reason,
                        tags=['PRE_PENDING_FILTER', 'WATCHLIST_OPEN_FADE_FILTER'],
                        no_attempt_reason='WATCHLIST_OPEN_FADE_FILTER',
                        metrics={
                            'open_change': open_change,
                            'change': change,
                            'filter': 'watchlist_open_fade_filter',
                        },
                    )
                    continue
                
                # 涨幅过滤: 保持巡航策略比硬止损留2%余量
                if change <= (max_chg - 2.0):
                    safe_cands.append(c)
                else:
                    watchlist_ceiling = max_chg - 2.0
                    reason = f"买入过滤: 备选池涨幅超过pending前上限(现{change:.1f}% > {watchlist_ceiling:.1f}%)"
                    metrics = {
                        'change': change,
                        'max_buy_change': max_chg,
                        'watchlist_pending_ceiling': watchlist_ceiling,
                        'filter': 'watchlist_chase_ceiling_filter',
                    }
                    if _mark_paper_strong_entry(
                        c,
                        reason=reason,
                        filter_name='WATCHLIST_CHASE_CEILING_FILTER',
                        metrics=metrics,
                    ):
                        safe_cands.append(c)
                        _log_entry_audit_event(
                            c,
                            stage='pre_pending_filter',
                            strategy='备选池买入触发',
                            action='QUEUED',
                            reason=f"{reason}；影子强票实验继续入PENDING",
                            tags=['PRE_PENDING_FILTER', 'WATCHLIST_CHASE_CEILING_FILTER', 'PAPER_STRONG_ENTRY_ROUTE'],
                            no_attempt_reason=None,
                            metrics=metrics,
                        )
                        continue
                    logger.warning(f"   🚫 {c['name']} {reason}")
                    _log_entry_audit_event(
                        c,
                        stage='pre_pending_filter',
                        strategy='备选池买入触发',
                        action='FILTERED',
                        reason=reason,
                        tags=['PRE_PENDING_FILTER', 'WATCHLIST_CHASE_CEILING_FILTER'],
                        no_attempt_reason='WATCHLIST_CHASE_CEILING_FILTER',
                        metrics=metrics,
                    )
            
            paper_max = int(_paper_strong_entry_cfg().get('max_pending_candidates', 5) or 5)
            candidates = safe_cands[:paper_max if (getattr(args, 'paper_trade', False) and args.mode == 'watchlist') else 3]
            strategy_name = "备选池买入触发"
    
    # [V10] Step 3: 板块共振加分
    if candidates and industry_stats:
        logger.info("应用板块共振评分...")
        for c in candidates:
            industry = c.get('industry', '')
            if industry:
                sector_info = analyzer.get_sector_strength(industry, industry_stats)
                c['sector_bonus'] = sector_info.get('sector_score', 0)
                c['sector_rank'] = sector_info.get('rank', 0)
                logger.info(f"  {c['name']}: 行业{industry}, 排名#{sector_info.get('rank', 0)}, 板块加分:{c['sector_bonus']}")

        try:
            sector_rotation = SectorRotation(industry_stats)
            candidates, sector_rejected = sector_rotation.annotate_candidates(candidates, market_env=market_env)
            if sector_rejected:
                logger.warning(
                    f"行业轮动门禁: 原始含{len(candidates) + len(sector_rejected)}只 -> "
                    f"通过{len(candidates)}只, 拒绝{len(sector_rejected)}只"
                )
                paper_restored = []
                for idx, r in enumerate(sector_rejected):
                    if idx < 8:
                        logger.warning(
                            f"  🚫 行业轮动拒绝: {r.get('name')}({r.get('code')}) "
                            f"industry={r.get('industry') or r.get('sector')} "
                            f"state={r.get('sector_state')} reason={r.get('reject_reason')}"
                        )
                    reason = f"行业轮动拒绝: industry={r.get('industry') or r.get('sector')} state={r.get('sector_state')} {r.get('reject_reason')}"
                    if is_paper_training_route(args, target_account):
                        restored = mark_paper_filter_bypass(
                            r,
                            filter_name='sector_rotation',
                            reason=reason,
                            tag='PAPER_SECTOR_FILTER_BYPASS',
                        )
                        paper_restored.append(restored)
                        logger.info(
                            "PAPER_SECTOR_FILTER_BYPASS account=%s code=%s name=%s state=%s reason=%s",
                            target_account,
                            restored.get('code'),
                            restored.get('name'),
                            restored.get('sector_state'),
                            restored.get('paper_original_filter_reason'),
                        )
                        _log_entry_audit_event(
                            restored,
                            stage='pre_pending_filter',
                            strategy=strategy_name,
                            action='QUEUED',
                            reason=f"{reason}；paper训练保留，交给哨兵二次确认",
                            tags=['PRE_PENDING_FILTER', 'SECTOR_ROTATION_FILTER', 'PAPER_SECTOR_FILTER_BYPASS'],
                            no_attempt_reason=None,
                            metrics={
                                'sector_state': restored.get('sector_state'),
                                'sector_rank': restored.get('sector_rank'),
                                'filter': 'sector_rotation',
                            },
                        )
                    else:
                        audit(
                            'REJECTED',
                            reason,
                            code=r.get('code'),
                            name=r.get('name'),
                            strategy=strategy_name,
                        )
                if paper_restored:
                    candidates.extend(paper_restored)
                    logger.info(
                        "PAPER_SECTOR_FILTER_BYPASS_SUMMARY account=%s restored=%s total_candidates=%s",
                        target_account,
                        len(paper_restored),
                        len(candidates),
                    )
            for c in candidates:
                tags = ",".join(c.get('sector_rotation_tags') or [])
                if tags:
                    logger.info(f"  🧭 {c.get('name')}: sector_state={c.get('sector_state')} tags={tags}")
        except Exception as e:
            logger.warning(f"行业轮动确认失败，跳过: {e}")
    
    # [V10] Step 4: 负面过滤器拦截
    filtered_candidates = []
    rejected_info = []
    if candidates:
        filtered_candidates, rejected_info = analyzer.apply_negative_filters(candidates, weather)
        logger.info(f"负面过滤器: 原始{len(candidates)}只 -> 过滤后{len(filtered_candidates)}只")
        for r in rejected_info or []:
            _log_entry_audit_event(
                r,
                stage='pre_pending_filter',
                strategy=strategy_name,
                action='FILTERED',
                reason=f"负面过滤器拦截: {r.get('reason')}",
                tags=['PRE_PENDING_FILTER', 'NEGATIVE_FILTER'],
                no_attempt_reason='NEGATIVE_FILTER',
                metrics={
                    'change': r.get('change'),
                    'turnover': r.get('turnover'),
                    'filter': 'negative_filter',
                },
            )
        candidates = filtered_candidates
    
    # [V10.8] Step 5: 主板权限过滤
    allowed_boards = Config.get_allowed_boards()
    if candidates and allowed_boards:
        board_filtered, board_rejected = analyzer.apply_board_filter(candidates, allowed_boards)
        logger.info(f"主板权限过滤: 原始{len(candidates)}只 -> 过滤后{len(board_filtered)}只 (允许:{allowed_boards})")
        paper_board_restored = []
        for r in board_rejected or []:
            reason = f"主板权限过滤拦截: {r.get('reason')}"
            if is_paper_training_route(args, target_account):
                restored = mark_paper_filter_bypass(
                    r,
                    filter_name='board_filter',
                    reason=reason,
                    tag='PAPER_BOARD_FILTER_BYPASS',
                )
                paper_board_restored.append(restored)
                logger.info(
                    "PAPER_BOARD_FILTER_BYPASS account=%s code=%s name=%s board=%s allowed=%s reason=%s",
                    target_account,
                    restored.get('code'),
                    restored.get('name'),
                    restored.get('board'),
                    allowed_boards,
                    restored.get('paper_original_filter_reason'),
                )
                _log_entry_audit_event(
                    restored,
                    stage='pre_pending_filter',
                    strategy=strategy_name,
                    action='QUEUED',
                    reason=f"{reason}；paper训练保留，交给哨兵二次确认",
                    tags=['PRE_PENDING_FILTER', 'BOARD_PERMISSION_FILTER', 'PAPER_BOARD_FILTER_BYPASS'],
                    no_attempt_reason=None,
                    metrics={
                        'board': restored.get('board'),
                        'allowed_boards': allowed_boards,
                        'filter': 'board_filter',
                    },
                )
            else:
                _log_entry_audit_event(
                    r,
                    stage='pre_pending_filter',
                    strategy=strategy_name,
                    action='FILTERED',
                    reason=reason,
                    tags=['PRE_PENDING_FILTER', 'BOARD_PERMISSION_FILTER'],
                    no_attempt_reason='BOARD_PERMISSION_FILTER',
                    metrics={
                        'board': r.get('board'),
                        'allowed_boards': allowed_boards,
                        'filter': 'board_filter',
                    },
                )
        candidates = board_filtered + paper_board_restored

    # --- Entry policy: dynamic time-window entry (default disabled) ---
    attempts = candidates or []

    entry_policy = Config.STRATEGY.get('entry_policy', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
    entry_enabled = bool((entry_policy or {}).get('enabled', False))
    default_model = (entry_policy or {}).get('default_model') or 'immediate'

    def _normalize_trade_date(date_arg: str | None) -> str:
        if date_arg:
            s = str(date_arg).strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:]}"
            if len(s) == 10 and s[4] == '-' and s[7] == '-':
                return s
        return datetime.now().strftime('%Y-%m-%d')

    def _bucket_end_dt(trade_date: str, bucket: str) -> datetime | None:
        try:
            y, m, d = int(trade_date[:4]), int(trade_date[5:7]), int(trade_date[8:10])
        except Exception:
            dt = datetime.now()
            y, m, d = dt.year, dt.month, dt.day

        ends = {
            'B1': (10, 0),
            'B2': (11, 30),
            'B3': (14, 0),
            'B4': (14, 40),
            'B5': (15, 1),
        }
        hm = ends.get(bucket)
        if not hm:
            return None
        hh, mm = hm
        return datetime(y, m, d, hh, mm, 0)

    def _entry_policy_key(orig: str, default_name: str) -> str:
        s = orig or default_name or ''
        if s in ('集合竞价', '早盘竞价首选'):
            return '集合竞价'
        if s in ('午盘精选',):
            return '午盘精选'
        if s in ('备选池买入触发',):
            return '备选池买入触发'
        if s in ('冷启动',):
            return '冷启动'
        return s

    def _candidate_ts_code(candidate: dict) -> str:
        ts_code = str((candidate or {}).get('ts_code') or '').strip()
        if ts_code.endswith(('.SH', '.SZ', '.BJ')):
            return ts_code
        code = str((candidate or {}).get('code') or '').strip()
        if not code:
            return ''
        if code.startswith('6'):
            return f"{code}.SH"
        if code.startswith(('4', '8', '9')):
            return f"{code}.BJ"
        return f"{code}.SZ"

    def _enrich_candidate_for_pending_gate(candidate: dict, realtime_quotes: dict | None) -> None:
        """Fill candidate quote fields before the pending-create data-quality gate."""
        if not candidate or not isinstance(realtime_quotes, dict):
            return
        ts_code = _candidate_ts_code(candidate)
        quote = realtime_quotes.get(ts_code) if ts_code else None
        if not isinstance(quote, dict):
            return

        candidate['ts_code'] = ts_code
        for key in ('price', 'pre_close', 'vol', 'vol_lots', 'vol_shares', 'amount', 'amount_yuan', 'open', 'high', 'low', 'vwap'):
            value = quote.get(key)
            if value not in (None, ''):
                candidate[key] = value

        price = float(quote.get('price') or candidate.get('price') or 0)
        pre_close = float(quote.get('pre_close') or candidate.get('pre_close') or 0)
        open_price = float(quote.get('open') or candidate.get('open') or 0)
        if price > 0 and pre_close > 0:
            candidate['change'] = (price - pre_close) / pre_close * 100
            candidate['pct_chg'] = candidate['change']
        if open_price > 0 and pre_close > 0:
            candidate['open_change'] = (open_price - pre_close) / pre_close * 100

        dq = quote.get('_data_quality') or realtime_quotes.get('_data_quality')
        if isinstance(dq, dict):
            candidate['_data_quality'] = dq

    # When enabled, we store candidates into DB-backed pending signals,
    # then attempt entry from pending signals within configured windows.
    pending_queue_stats = {
        'mode': 'queue_entry' if queue_entry_intent else 'auto_trade',
        'created': 0,
        'blocked': 0,
        'skipped': 0,
        'errors': 0,
    }
    if entry_enabled and default_model == 'dynamic_window':
        trade_date = _normalize_trade_date(getattr(args, 'date', None))
        now_dt = datetime.now()
        curr_bucket = _get_time_bucket_now()

        model_cfg = ((entry_policy or {}).get('models') or {}).get('dynamic_window', {})
        strategy_windows = (model_cfg.get('strategy_windows') or {}) if isinstance(model_cfg.get('strategy_windows'), dict) else {}
        retry_cooldown_sec = int(model_cfg.get('retry_cooldown_sec', 300) or 300)
        max_retries = int(model_cfg.get('max_retries', 6) or 6)
        expire_at_bucket_end = bool(model_cfg.get('expire_at_bucket_end', True))
        storm_probe_created = 0
        storm_probe_active = bool(
            queue_entry_intent
            and market_env.get('risk_level') == 'high'
            and _storm_probe_enabled()
            and not str(target_account or '').startswith('paper_')
        )

        # Optional extra strict VWAP ratio (if set, it tightens verify_money_flow's VWAP gate)
        try:
            policy_max_vwap_ratio = model_cfg.get('max_price_vwap_ratio', None)
            policy_max_vwap_ratio = None if policy_max_vwap_ratio in (None, '', 'null') else float(policy_max_vwap_ratio)
        except Exception:
            policy_max_vwap_ratio = None

        # 1) Upsert current candidates into pending pool
        if attempts:
            pending_realtime_quotes = {}
            try:
                pending_ts_codes = sorted({ts for ts in (_candidate_ts_code(c) for c in attempts) if ts})
                if pending_ts_codes:
                    pending_realtime_quotes = provider.get_realtime_quotes(pending_ts_codes) or {}
            except Exception as e:
                logger.warning(f"Pending入队前实时行情补全失败: {e}")
                pending_realtime_quotes = {}

            for c in attempts:
                try:
                    from core.pre_trade_gate import evaluate_pre_trade_gate, is_pending_allowed
                    _enrich_candidate_for_pending_gate(c, pending_realtime_quotes)
                    orig = c.get('strategy') or strategy_name
                    if c.get('paper_strong_entry') and args.mode == 'watchlist':
                        orig = '备选池买入触发'
                        c['strategy'] = orig
                    key = _entry_policy_key(str(orig), strategy_name)
                    pre_gate = evaluate_pre_trade_gate(
                        c,
                        market_env=market_env,
                        strategy=key,
                        account=target_account,
                        win_rate_stats=None,
                        now=now_dt,
                        mode=args.mode,
                    )
                    attempt_common = _candidate_attempt_base(
                        c,
                        strategy=key,
                        stage='pending_create',
                        action='EVALUATE',
                        reason='pending_create gate evaluated',
                        extra={
                            'trade_date': trade_date,
                            'signal_bucket': curr_bucket,
                            'target_account': target_account,
                            'market_regime': market_env.get('regime'),
                            'risk_level': market_env.get('risk_level'),
                            'gate_action': pre_gate.get('action'),
                            'gate_reason': pre_gate.get('reason'),
                            'gate_tags': pre_gate.get('tags'),
                        },
                    )
                    if force_queue_block_reason:
                        pending_queue_stats['blocked'] += 1
                        attempt = {
                            **attempt_common,
                            'action': 'BLOCKED',
                            'reason': force_queue_block_reason[:255],
                            'no_attempt_reason': 'MARKET_HIGH_RISK_BLOCK',
                        }
                        logger.warning(
                            f"🚫 极端市场环境拦截PENDING入队: {c.get('name')}({c.get('code')}) "
                            f"strategy={key} reason={force_queue_block_reason}"
                        )
                        try:
                            portfolio.log_risk_event(
                                account=target_account,
                                code=c.get('code'),
                                event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                                weather=weather,
                                reason=force_queue_block_reason[:255],
                                params={
                                    'stage': 'pending_create',
                                    'strategy': key,
                                    'action': 'BLOCK',
                                    'tags': ['MARKET_HIGH_RISK_BLOCK'],
                                    'metrics': {
                                        'risk_level': market_env.get('risk_level'),
                                        'regime': market_env.get('regime'),
                                        'message': market_status,
                                    },
                                    'gate_action': pre_gate.get('action'),
                                    'gate_reason': pre_gate.get('reason'),
                                    'candidate_attempt': attempt,
                                }
                            )
                        except Exception:
                            pass
                        _audit_with_attempt('BLOCKED', force_queue_block_reason, code=c.get('code'), name=c.get('name'), strategy=key, attempt=attempt)
                        continue
                    if not is_pending_allowed(pre_gate.get('action')):
                        pending_queue_stats['blocked'] += 1
                        attempt = {
                            **attempt_common,
                            'action': 'BLOCKED',
                            'reason': (pre_gate.get('reason') or 'pending blocked')[:255],
                            'no_attempt_reason': 'PENDING_CREATE_GATE_BLOCKED',
                        }
                        logger.warning(
                            f"🚫 Pending创建被权限矩阵拦截: {c.get('name')}({c.get('code')}) "
                            f"strategy={key} action={pre_gate.get('action')} reason={pre_gate.get('reason')}"
                        )
                        try:
                            portfolio.log_risk_event(
                                account=target_account,
                                code=c.get('code'),
                                event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                                weather=weather,
                                reason=(pre_gate.get('reason') or 'pending blocked')[:255],
                                params={
                                    'stage': 'pending_create',
                                    'strategy': key,
                                    'action': pre_gate.get('action'),
                                    'tags': pre_gate.get('tags'),
                                    'metrics': pre_gate.get('metrics'),
                                    'data_quality': pre_gate.get('data_quality'),
                                    'candidate_attempt': attempt,
                                }
                            )
                        except Exception:
                            pass
                        _audit_with_attempt('BLOCKED', attempt['reason'], code=c.get('code'), name=c.get('name'), strategy=key, attempt=attempt)
                        continue

                    if storm_probe_active and storm_probe_created >= _storm_probe_max_per_run():
                        pending_queue_stats['blocked'] += 1
                        reason = f"暴雨确认探针每轮限额: created={storm_probe_created} >= max={_storm_probe_max_per_run()}"
                        attempt = {
                            **attempt_common,
                            'action': 'BLOCKED',
                            'reason': reason[:255],
                            'no_attempt_reason': 'STORM_PROBE_LIMIT',
                        }
                        logger.warning(
                            f"🚫 暴雨确认探针限额拦截: {c.get('name')}({c.get('code')}) "
                            f"strategy={key} reason={reason}"
                        )
                        try:
                            portfolio.log_risk_event(
                                account=target_account,
                                code=c.get('code'),
                                event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                                weather=weather,
                                reason=reason[:255],
                                params={
                                    'stage': 'pending_create',
                                    'strategy': key,
                                    'action': pre_gate.get('action'),
                                    'tags': list(pre_gate.get('tags') or []) + ['STORM_PROBE_LIMIT'],
                                    'metrics': pre_gate.get('metrics'),
                                    'candidate_attempt': attempt,
                                }
                            )
                        except Exception:
                            pass
                        _audit_with_attempt('BLOCKED', attempt['reason'], code=c.get('code'), name=c.get('name'), strategy=key, attempt=attempt)
                        continue

                    allowed_buckets = strategy_windows.get(key)
                    if not allowed_buckets:
                        # No configured window → skip dynamic pending for this strategy
                        pending_queue_stats['skipped'] += 1
                        attempt = {
                            **attempt_common,
                            'action': 'SKIPPED',
                            'reason': f"未配置动态入场窗口，跳过PENDING入队: strategy={key}",
                            'no_attempt_reason': 'SCHEDULE_WINDOW_MISSING',
                            'configured_strategies': sorted(strategy_windows.keys()),
                        }
                        _log_entry_audit_event(
                            c,
                            stage='pending_create',
                            strategy=key,
                            action='SKIP',
                            reason=attempt['reason'],
                            tags=['SCHEDULE_WINDOW_MISSING'],
                            metrics={
                                'configured_strategies': sorted(strategy_windows.keys()),
                                'gate_action': pre_gate.get('action'),
                                'gate_reason': pre_gate.get('reason'),
                                'candidate_attempt': attempt,
                            },
                        )
                        _audit_with_attempt('INFO', attempt['reason'], code=c.get('code'), name=c.get('name'), strategy=key, attempt=attempt)
                        continue

                    # Expiry at end of the last bucket in the window
                    expires_at = None
                    if expire_at_bucket_end:
                        last_bucket = allowed_buckets[-1]
                        expires_at = _bucket_end_dt(trade_date, last_bucket)
                    attempt = {
                        **attempt_common,
                        'action': 'QUEUED',
                        'reason': 'dynamic pending created',
                        'allowed_buckets': allowed_buckets,
                        'expires_at': (expires_at or now_dt).strftime('%Y-%m-%d %H:%M:%S'),
                    }

                    payload = {
                        'target_account': target_account,
                        'strategy_key': key,
                        'strategy_name': key,
                        'base_tags_json': _candidate_base_tags_json(c),
                        'cold_start_model_tags': c.get('cold_start_model_tags'),
                        'cold_start_delayed_confirm': c.get('cold_start_delayed_confirm'),
                        'cold_start_early_absorb': c.get('cold_start_early_absorb'),
                        'cold_start_pullback_entry_candidate': c.get('cold_start_pullback_entry_candidate'),
                        'weather': weather,
                        'score': c.get('score'),
                        'open_change': c.get('open_change'),
                        'change': c.get('change') or c.get('pct_chg'),
                        'turnover': c.get('turnover'),
                        'price': c.get('price'),
                        'prev_limit_present': c.get('prev_limit_present'),
                        'prev_limit_times': c.get('prev_limit_times'),
                        'is_continue_board_candidate': c.get('is_continue_board_candidate'),
                        'is_first_board_candidate': c.get('is_first_board_candidate'),
                        'board_context': c.get('board_context'),
                        'zt_tag': c.get('zt_tag'),
                        'risk_tags': c.get('risk_tags'),
                        'entry_scenario': c.get('entry_scenario') or key,
                        'paper_executable_pool': bool(c.get('paper_executable_pool')),
                        'paper_source_pool': c.get('paper_source_pool'),
                        'paper_strong_entry': bool(c.get('paper_strong_entry')),
                        'paper_experiment': bool(c.get('paper_experiment')),
                        'paper_experiment_type': c.get('paper_experiment_type'),
                        'paper_experiment_reason': c.get('paper_experiment_reason'),
                        'paper_original_filter_reason': c.get('paper_original_filter_reason'),
                        'paper_max_buy_change': c.get('paper_max_buy_change'),
                        'paper_experiment_metrics': c.get('paper_experiment_metrics'),
                        'market_regime': market_env.get('regime'),
                        'gate_action': pre_gate.get('action'),
                        'gate_reason': pre_gate.get('reason'),
                        'gate_tags': pre_gate.get('tags'),
                        'position_multiplier': pre_gate.get('position_multiplier'),
                        'candidate_attempt': attempt,
                    }
                    portfolio.upsert_pending_entry_signal(
                        trade_date=trade_date,
                        code=c.get('code'),
                        name=c.get('name'),
                        ts_code=c.get('ts_code'),
                        source_strategy=key,
                        signal_time=now_dt,
                        expires_at=expires_at or now_dt,
                        weather=weather,
                        signal_bucket=curr_bucket,
                        entry_model='dynamic_window',
                        payload=payload,
                        status='PENDING',
                    )
                    pending_queue_stats['created'] += 1
                    if storm_probe_active:
                        storm_probe_created += 1
                    if c.get('paper_strong_entry'):
                        logger.info(
                            "PAPER_STRONG_PENDING_CREATED account=%s code=%s name=%s strategy=%s reason=%s expires_at=%s",
                            target_account,
                            c.get('code'),
                            c.get('name'),
                            key,
                            c.get('paper_experiment_reason') or '',
                            (expires_at or now_dt).strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    _audit_with_attempt('QUEUED', f"动态入场PENDING已入队: strategy={key} expires_at={(expires_at or now_dt).strftime('%Y-%m-%d %H:%M:%S')}", code=c.get('code'), name=c.get('name'), strategy=key, attempt=attempt)
                except Exception as e:
                    pending_queue_stats['errors'] += 1
                    err_attempt = _candidate_attempt_base(
                        c if 'c' in locals() else {},
                        strategy=(key if 'key' in locals() else strategy_name),
                        stage='pending_create',
                        action='ERROR',
                        reason=f"pending_create error: {e}",
                        extra={
                            'target_account': target_account,
                            'no_attempt_reason': 'PENDING_CREATE_ERROR',
                            'error': str(e)[:200],
                        },
                    )
                    _log_entry_audit_event(
                        c if 'c' in locals() else {},
                        stage='pending_create',
                        strategy=(key if 'key' in locals() else strategy_name),
                        action='ERROR',
                        reason=f"pending_create error: {e}",
                        tags=['PENDING_CREATE_ERROR'],
                        metrics={'error': str(e)[:200], 'candidate_attempt': err_attempt},
                    )
                    _audit_with_attempt(
                        'ERROR',
                        f"pending_create error: {e}",
                        code=(c.get('code') if 'c' in locals() and isinstance(c, dict) else None),
                        name=(c.get('name') if 'c' in locals() and isinstance(c, dict) else None),
                        strategy=(key if 'key' in locals() else strategy_name),
                        attempt=err_attempt,
                    )
                    continue

        # 2) Expire old signals then load active pending signals
        try:
            portfolio.expire_old_pending_entries(trade_date=trade_date, now_dt=now_dt)
        except Exception:
            pass

        result['entry_queue'] = pending_queue_stats
        if queue_entry_intent:
            audit(
                'INFO',
                "动态入场只入队模式完成: "
                f"created={pending_queue_stats['created']} "
                f"blocked={pending_queue_stats['blocked']} "
                f"skipped={pending_queue_stats['skipped']} "
                f"errors={pending_queue_stats['errors']}"
            )
            return

        pending_rows = []
        try:
            pending_rows = portfolio.load_pending_entry_signals(trade_date=trade_date, now_dt=now_dt, limit=60)
        except Exception:
            pending_rows = []

        # 3) Convert pending rows to attempt candidates (apply cooldown/max retries/window check)
        attempts = []
        for r in pending_rows or []:
            try:
                pid = r.get('id')
                strat_key = r.get('source_strategy') or ''
                allowed_buckets = strategy_windows.get(strat_key)
                if allowed_buckets and (not curr_bucket or curr_bucket not in set(allowed_buckets)):
                    continue

                # Retry limit
                cc = int(r.get('check_count', 0) or 0)
                if cc >= max_retries:
                    try:
                        portfolio.mark_pending_entry_status(signal_id=int(pid), status='EXPIRED', reason='max retries reached')
                    except Exception:
                        pass
                    continue

                # Cooldown
                last_checked = r.get('last_checked_at')
                if last_checked:
                    try:
                        if isinstance(last_checked, str):
                            last_dt = datetime.fromisoformat(last_checked)
                        else:
                            last_dt = last_checked
                        if (now_dt - last_dt).total_seconds() < retry_cooldown_sec:
                            continue
                    except Exception:
                        pass

                payload = {}
                try:
                    payload = json.loads(r.get('payload_json') or '{}')
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:
                    payload = {}

                acct = payload.get('target_account') or r.get('target_account') or target_account

                cand = {
                    'code': r.get('code'),
                    'name': r.get('name') or '',
                    'ts_code': r.get('ts_code') or '',
                    'strategy': strat_key,
                    'score': payload.get('score'),
                    'open_change': payload.get('open_change'),
                    'change': payload.get('change'),
                    'turnover': payload.get('turnover'),
                    'price': payload.get('price'),
                    'prev_limit_present': payload.get('prev_limit_present'),
                    'prev_limit_times': payload.get('prev_limit_times'),
                    'is_continue_board_candidate': payload.get('is_continue_board_candidate'),
                    'is_first_board_candidate': payload.get('is_first_board_candidate'),
                    'board_context': payload.get('board_context'),
                    'zt_tag': payload.get('zt_tag'),
                    'risk_tags': payload.get('risk_tags'),
                    'entry_scenario': payload.get('entry_scenario'),
                    'market_regime': payload.get('market_regime'),
                    'gate_action': payload.get('gate_action'),
                    'gate_reason': payload.get('gate_reason'),
                    'gate_tags': payload.get('gate_tags'),
                    'position_multiplier': payload.get('position_multiplier'),
                    'candidate_attempt': payload.get('candidate_attempt'),
                    'base_tags_json': payload.get('base_tags_json'),
                    'cold_start_model_tags': payload.get('cold_start_model_tags'),
                    'cold_start_delayed_confirm': payload.get('cold_start_delayed_confirm'),
                    'cold_start_early_absorb': payload.get('cold_start_early_absorb'),
                    'cold_start_pullback_entry_candidate': payload.get('cold_start_pullback_entry_candidate'),
                    '_pending_id': pid,
                    '_from_pending': True,
                    '_target_account': acct,
                    '_entry_model': r.get('entry_model') or 'dynamic_window',
                    '_policy_max_vwap_ratio': policy_max_vwap_ratio,
                }
                attempts.append(cand)
            except Exception:
                continue

    elif queue_entry_intent:
        result['entry_queue'] = pending_queue_stats
        audit('INFO', f"--queue-entry 需要 entry_policy.enabled=true 且 default_model=dynamic_window，当前 enabled={entry_enabled} model={default_model}；本次不执行买入")
        return

    # Execution Loop
    if not attempts and not execution_audit:
        audit('INFO', f"本次无自动买入候选: mode={args.mode}")

    if attempts:
        # If this run didn't rebuild strategy config, refresh it so entry_policy toggles
        # can be changed by evolver/ops without restarting the process.
        try:
            Config.load_strategy_config()
        except Exception:
            pass

        # [VNext] Win-rate gate (strategy T+1 quality gate; default disabled)
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

        def _load_win_stats(canon_strat):
            if not canon_strat:
                return {'cnt': 0, 'win_cnt': 0, 'win_rate': 0.0}
            try:
                return portfolio.get_strategy_win_rate(
                    strategy=canon_strat,
                    analysis_cycle=win_gate_cycle,
                    lookback_days=win_gate_days,
                    win_statuses=win_gate_statuses,
                )
            except Exception:
                return {'cnt': 0, 'win_cnt': 0, 'win_rate': 0.0}

        # [V10] 应用市场环境调整：提高Score门槛 (only affects immediate candidates; pending already filtered)
        if score_threshold_mult > 1.0 and not (entry_enabled and default_model == 'dynamic_window'):
            original_count = len(attempts)
            min_score = 10 * score_threshold_mult
            kept_attempts = []
            for c in attempts:
                score = c.get('score', 0) or 0
                if score >= min_score:
                    kept_attempts.append(c)
                    continue
                _log_entry_audit_event(
                    c,
                    stage='pre_pending_filter',
                    strategy=c.get('strategy') or strategy_name,
                    action='FILTERED',
                    reason=f"Score门槛过滤: score={score:.1f} < min_score={min_score:.1f}",
                    tags=['PRE_PENDING_FILTER', 'SCORE_THRESHOLD_FILTER'],
                    no_attempt_reason='SCORE_THRESHOLD_FILTER',
                    metrics={
                        'score': score,
                        'min_score': min_score,
                        'score_threshold_mult': score_threshold_mult,
                        'filter': 'score_threshold_filter',
                    },
                )
            attempts = kept_attempts
            logger.info(f"   Score门槛调整: {min_score:.1f}分 (过滤{original_count - len(attempts)}只)")

        # [V18] 全局仓位上限检查
        max_total = Config.RISK_MANAGEMENT.get("MAX_TOTAL_POSITIONS", 5)
        current_pos_count = len(current_summary.get('positions', []))
        if current_pos_count >= max_total:
            logger.warning(f"🚫 持仓上限拦截: 当前持仓 {current_pos_count} >= 上限 {max_total}")
            audit('BLOCKED', f"持仓上限拦截: 当前持仓 {current_pos_count} >= 上限 {max_total}")
            return

        for cand in attempts:
            # Re-check remaining capacity in each iteration
            all_positions = portfolio.load_all_positions()
            if account_position_count(all_positions, target_account) >= max_total:
                break

            target_code = cand['code']
            target_name = cand['name']
            orig_strat = cand.get('strategy', strategy_name)
            
            if target_code in Config.BLACKLIST: continue
            
            # Position Double-Check
            if any(p['code'] == target_code for p in current_summary['positions']):
                audit('BLOCKED', '账户已持有该标的，跳过重复买入', code=target_code, name=target_name, strategy=orig_strat)
                continue

            # Fetch Real-time data for V18 Verification
            ts_code = f"{target_code}.SH" if target_code.startswith('6') else f"{target_code}.SZ"
            rt_quotes = provider.get_realtime_quotes([ts_code])
            if not rt_quotes or ts_code not in rt_quotes:
                logger.warning(f"   ⚠️ 跳过 {target_name}: 无法获取实时行情")
                audit('BLOCKED', '无法获取实时行情', code=target_code, name=target_name, strategy=orig_strat)
                continue
            
            qt = rt_quotes[ts_code]
            target_price = float(qt.get('price', 0))
            if target_price <= 0: continue
            pre_close = float(qt.get('pre_close', 0) or 0)
            
            # Update candidate data for verification
            cand.update({
                'ts_code': ts_code,
                'price': target_price,
                'vol': qt.get('vol', 0),
                'amount': qt.get('amount', 0),
                'pre_close': pre_close,
                'high': qt.get('high', 0),
                'change': ((target_price - pre_close) / pre_close * 100) if pre_close > 0 else 0
            })

            # --- Dynamic entry verification ---
            # Existing behavior:
            # - Auction/watchlist flow uses a stricter 9:30-10:00 observation and a 10:00 confirm point.
            # Dynamic-window behavior:
            # - Pending entries can be retried across multiple runs; we still use verify_money_flow,
            #   plus optional entry_policy extra VWAP strictness when configured.

            # Use original strategy if available (from DB)
            is_watchlist_flow = (strategy_name == "备选池买入触发")
            is_from_pending = bool(cand.get('_from_pending'))

            # [VNext] Win-rate gate (default disabled)
            # Note: do this after we know orig_strat (so we can map to canonical strategy)
            canon_strat = canonical_strategy_name(orig_strat)
            stats = _load_win_stats(canon_strat)
            if win_gate_enabled:
                cnt = int((stats or {}).get('cnt', 0) or 0)
                win_rate = float((stats or {}).get('win_rate', 0.0) or 0.0)

                if cnt < win_gate_min_samples:
                    if insufficient_action.upper() == 'BLOCK':
                        logger.warning(f"🚫 胜率门禁拦截: {target_name} strategy={canon_strat} samples={cnt} < {win_gate_min_samples} (insufficient samples)")
                        audit('BLOCKED', f"胜率门禁样本不足: samples={cnt} < {win_gate_min_samples}", code=target_code, name=target_name, strategy=canon_strat)
                        continue
                    logger.warning(f"⚠️ 胜率门禁样本不足(放行): {target_name} strategy={canon_strat} samples={cnt} < {win_gate_min_samples}")
                else:
                    if win_rate < win_gate_min_wr:
                        logger.warning(f"🚫 胜率门禁拦截: {target_name} strategy={canon_strat} win_rate={win_rate:.2f} < {win_gate_min_wr:.2f} (n={cnt}, {win_gate_days}d, {win_gate_cycle})")
                        audit('BLOCKED', f"胜率门禁: win_rate={win_rate:.2f} < {win_gate_min_wr:.2f}", code=target_code, name=target_name, strategy=canon_strat)
                        continue

            # [VNext/P0] Unified pre-trade safety gate. This is enabled
            # independently from win_rate_gate, so weak-market + insufficient
            # samples can be blocked even when global win-rate gate only warns.
            pre_gate = evaluate_pre_trade_gate(
                cand,
                market_env=market_env,
                strategy=canon_strat or orig_strat,
                account=cand.get('_target_account') or target_account,
                win_rate_stats=stats,
                now=current_time,
                mode=args.mode,
            )
            if not pre_gate.get('allow', True):
                gate_reason = " | ".join(pre_gate.get('reasons') or ['pre_trade_gate blocked'])
                gate_tags = pre_gate.get('tags') or []
                logger.warning(
                    f"🚫 开仓前门禁拦截: {target_name}({target_code}) "
                    f"strategy={pre_gate.get('strategy') or canon_strat or orig_strat} "
                    f"action=OBSERVE_ONLY tags={','.join(gate_tags)} reason={gate_reason}"
                )
                try:
                    portfolio.log_risk_event(
                        account=cand.get('_target_account') or target_account,
                        code=target_code,
                        event_type='BUY_BLOCKED_PRE_TRADE_GATE',
                        weather=weather,
                        reason=gate_reason,
                        params={
                            'strategy': pre_gate.get('strategy') or canon_strat or orig_strat,
                            'action': pre_gate.get('action'),
                            'tags': gate_tags,
                            'metrics': pre_gate.get('metrics'),
                            'market_message': market_status,
                            'risk_level': market_env.get('risk_level'),
                            'win_stats': stats or {},
                            'change': cand.get('change'),
                            'open_change': cand.get('open_change'),
                            'position_multiplier': pre_gate.get('position_multiplier'),
                            'required_confirmations': pre_gate.get('required_confirmations'),
                            'data_quality': pre_gate.get('data_quality'),
                        }
                    )
                except Exception:
                    pass
                if cand.get('_from_pending') and cand.get('_pending_id'):
                    try:
                        portfolio.touch_pending_entry_check(signal_id=int(cand.get('_pending_id')), reason=f"pre_trade_gate: {gate_reason}"[:255])
                    except Exception:
                        pass
                audit('BLOCKED', gate_reason, code=target_code, name=target_name, strategy=pre_gate.get('strategy') or canon_strat or orig_strat)
                continue

            # [VNext/P1] Permission matrix actions can downgrade immediate buy to pending/observe/block.
            # Pending rows are already in confirmation mode; allow them to continue to intraday checks.
            if (not cand.get('_from_pending')) and pre_gate.get('action') in ('CONFIRM_ONLY', 'PENDING', 'LOW_SIZE_CONFIRM'):
                logger.info(
                    f"⏳ {target_name}({target_code}) 权限矩阵要求动态确认: "
                    f"action={pre_gate.get('action')} reason={pre_gate.get('reason')}"
                )
                audit('BLOCKED', f"权限矩阵要求动态确认: action={pre_gate.get('action')} {pre_gate.get('reason')}", code=target_code, name=target_name, strategy=pre_gate.get('strategy') or canon_strat or orig_strat)
                continue

            # [Cold Start] 专用买入验证
            if orig_strat in ('冷启动',):
                time_bucket = 'B1'  # 默认B1，后续可根据实际时间调整
                is_ok, reason = analyzer.verify_cold_start_entry(cand, time_bucket=time_bucket)
                if not is_ok:
                    logger.warning(f"   🚫 {target_name} 冷启动验证失败: {reason}")
                    audit('BLOCKED', f"冷启动验证失败: {reason}", code=target_code, name=target_name, strategy=orig_strat)
                    continue
                logger.info(f"   ✅ {target_name} 冷启动验证通过: {reason}")

            policy_ratio = cand.get('_policy_max_vwap_ratio')
            if policy_ratio is not None:
                try:
                    policy_ratio = float(policy_ratio)
                except Exception:
                    policy_ratio = None

            verify_result = verify_entry_flow(
                cand,
                analyzer=analyzer,
                market_env=market_env,
                weather=weather,
                strategy=orig_strat,
                now=current_time,
                realtime_map=rt_quotes,
                pending_retry=is_from_pending,
                watchlist_flow=is_watchlist_flow,
                policy_max_vwap_ratio=policy_ratio,
            )
            is_ok = bool(verify_result.get("ok"))
            reason = verify_result.get("reason") or "entry verification rejected"
            confirm = verify_result.get("confirm") or {}
            if is_ok and confirm:
                metrics = confirm.get("metrics") or {}
                if metrics.get("vwap"):
                    cand["vwap"] = metrics.get("vwap")
                if metrics.get("volume_ratio"):
                    cand["volume_ratio"] = metrics.get("volume_ratio")

            if not is_ok:
                if cand.get('_from_pending') and cand.get('_pending_id'):
                    try:
                        portfolio.touch_pending_entry_check(signal_id=int(cand.get('_pending_id')), reason=reason)
                    except Exception:
                        pass
                logger.warning(f"   🚫 {target_name} 买入验证失败: {reason}")
                audit('BLOCKED', f"买入验证失败: {reason}", code=target_code, name=target_name, strategy=orig_strat)
                continue

            effective_account = cand.get('_target_account') or target_account

            if is_virtual_account(effective_account):
                cash_available = portfolio.load_cash(account=effective_account)
            else:
                cash_available = portfolio.load_cash_for_trading(account=effective_account)
            if cash_available is None:
                logger.error(f"[{effective_account}] 跳过买入: 无法读取交易现金")
                audit('BLOCKED', f"[{effective_account}] 无法读取交易现金", code=target_code, name=target_name, strategy=orig_strat)
                continue
            sizing = PositionSizer(portfolio, provider).calculate(
                account=effective_account,
                price=target_price,
                cash_available=cash_available,
                positions=portfolio.load_all_positions(),
                market_env=market_env,
                strategy=orig_strat,
                pre_gate=pre_gate,
                candidate=cand,
                ts_code=ts_code,
                max_position_mult=max_position_mult,
                market_status=market_status,
                analyzer=analyzer,
            )
            buy_vol = int(sizing.get('quantity') or 0)
            logger.info(
                f"   📐 仓位预算: {target_name} pct={float(sizing.get('position_pct') or 0):.2%} "
                f"vol={buy_vol} amount={float(sizing.get('amount') or 0):.0f} "
                f"reason={' | '.join(sizing.get('reasons') or [])}"
            )
            
            if buy_vol > 0:
                # [VNext] Attribute this buy back to the selection snapshot (best-effort)
                snapshot_id = None
                tags_json = None
                source_strategy = orig_strat
                try:
                    # Note: save_selection stores under strategies like '集合竞价'/'午盘精选'...
                    # while auto-buy uses strategy_name like '早盘竞价首选'. Prefer candidate.strategy when available.
                    # Map auto-buy strategy names back to the selection strategy labels used in DB
                    if orig_strat in ['集合竞价', '早盘竞价首选']:
                        selection_strategy = '集合竞价'
                    elif orig_strat in ['午盘精选']:
                        selection_strategy = '午盘精选'
                    elif orig_strat in ['盘后资金流']:
                        selection_strategy = '盘后资金流'
                    elif orig_strat in ['龙头跟踪']:
                        selection_strategy = '龙头跟踪'
                    elif orig_strat in ['技术突破']:
                        selection_strategy = '技术突破'
                    elif orig_strat in ['冷启动']:
                        selection_strategy = '冷启动'
                    else:
                        selection_strategy = None

                    if selection_strategy:
                        # Date normalization: args.date is YYYYMMDD, DB uses YYYY-MM-DD
                        if args.date and len(args.date) == 8:
                            sel_date = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
                        else:
                            sel_date = datetime.now().strftime('%Y-%m-%d')

                        conn = portfolio._get_connection()
                        if conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SELECT snapshot_id, tags_json, id FROM strategy_selection WHERE date=%s AND strategy=%s AND code=%s ORDER BY id DESC LIMIT 1",
                                    (sel_date, selection_strategy, target_code)
                                )
                                row = cur.fetchone()
                                if row:
                                    snapshot_id = row.get('snapshot_id')
                                    tags_json = row.get('tags_json')
                            conn.close()
                except Exception:
                    pass

                final_tags_json = merge_entry_confirm_tags_json(
                    merge_signal_tags_json(tags_json or cand.get("base_tags_json") or _candidate_base_tags_json(cand), pre_gate),
                    cand.get("entry_confirm"),
                )
                success, msg = portfolio.execute_buy(
                    target_code,
                    target_name,
                    target_price,
                    buy_vol,
                    account=effective_account,
                    snapshot_id=snapshot_id,
                    source_strategy=source_strategy,
                    weather=weather,
                    signal_tags_json=final_tags_json,
                )
                if success:
                    logger.info(f"✅ 执行买入成功: {target_name} ({target_code})")
                    audit('BOUGHT', f"买入成功: {buy_vol}股 @{target_price:.2f}", code=target_code, name=target_name, strategy=source_strategy)
                    if not is_virtual_account(effective_account):
                        mirror_paper_buy(
                            portfolio,
                            source_account=effective_account,
                            code=target_code,
                            name=target_name,
                            price=target_price,
                            quantity=buy_vol,
                            snapshot_id=snapshot_id,
                            source_strategy=source_strategy,
                            weather=weather,
                            signal_tags_json=final_tags_json,
                        )
                    if args.mode == 'watchlist':
                         portfolio.update_zt_result(cand.get('date', datetime.now().strftime('%Y-%m-%d')), target_code, '已买入', strategy=selection_strategy)
                         portfolio.update_selection_observe_status(
                             cand.get('date', datetime.now().strftime('%Y-%m-%d')),
                             target_code,
                             'BOUGHT',
                             strategy=selection_strategy,
                             reason=f"备选池买入成功: {buy_vol}股 @{target_price:.2f}",
                             metrics={'price': target_price, 'quantity': buy_vol, 'account': effective_account},
                         )

                    # If buy was from a pending signal, mark it as BOUGHT.
                    if cand.get('_from_pending') and cand.get('_pending_id'):
                        try:
                            portfolio.mark_pending_entry_status(signal_id=int(cand.get('_pending_id')), status='BOUGHT', reason='buy executed')
                        except Exception:
                            pass

                    if not args.no_email:
                        buy_cost = target_price * buy_vol * 1.0003
                        cash_after = portfolio.load_cash(account=effective_account)
                        content = reporter.format_buy_alert(
                            target_code,
                            target_name,
                            target_price,
                            buy_vol,
                            buy_cost,
                            strategy_name,
                            cash_available,
                            cash_after,
                            account=effective_account,
                            weather=weather,
                            snapshot_id=snapshot_id,
                        )
                        vr = cand.get('volume_ratio', 0)
                        if vr > 0: content += f"\n**实时量比**: {vr:.2f}"
                        if cand.get('_from_pending'):
                            entry_model = cand.get('_entry_model') or 'dynamic_window'
                            content += f"\n\n**入场模式**: {humanize_text(entry_model)}（等待队列重试）"
                        reporter.send_email(f"🟢【自动买入】{target_name} @{target_price:.2f} (量比:{vr:.1f})", content)
            else:
                audit('BLOCKED', f"仓位预算为0: {' | '.join(sizing.get('reasons') or [])}", code=target_code, name=target_name, strategy=orig_strat)

if __name__ == '__main__':
    main()
