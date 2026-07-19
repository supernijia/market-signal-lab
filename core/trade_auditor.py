# -*- coding: utf-8 -*-
"""
Trade Quality Auditor
Generates rich markdown reports for trading execution quality.
"""
import pandas as pd
from datetime import datetime, timedelta
import logging
import pymysql
import json
from core.config import Config
from core.display_labels import (
    display_account,
    display_action,
    display_event_type,
    humanize_text,
)

logger = logging.getLogger("StockAnalyzer.TradeAuditor")

class TradeAuditor:
    def __init__(self, portfolio_manager, data_provider):
        self.portfolio = portfolio_manager
        self.provider = data_provider
        self.AUDIT_START_DATE = '2026-03-05'
        self._daily_map_cache = None

    def _daily_map(self):
        if self._daily_map_cache is not None:
            return self._daily_map_cache
        try:
            self._daily_map_cache = {
                row.get('ts_code'): row
                for row in (self.provider.get_daily_data() or [])
                if isinstance(row, dict) and row.get('ts_code')
            }
        except Exception:
            self._daily_map_cache = {}
        return self._daily_map_cache

    def _repair_realtime_quote(self, ts_code, quote):
        fixed = dict(quote or {})
        try:
            price = self._safe_float(fixed.get('price') or fixed.get('close'))
            pre_close = self._safe_float(fixed.get('pre_close'))
            if pre_close <= 0:
                daily_row = self._daily_map().get(ts_code) or {}
                pre_close = self._safe_float(daily_row.get('pre_close') or daily_row.get('close'))
                if pre_close > 0:
                    fixed['pre_close'] = pre_close
            if price > 0 and pre_close > 0:
                fixed['pct_chg'] = (price - pre_close) / pre_close * 100
        except Exception:
            pass
        return fixed
        
    def _classify_time_period(self, str_time):
        """Classify a time string into trading periods"""
        if not str_time or str_time == 'Unknown':
            return '未知'
        try:
            # Handle full datetime string or just time
            if ' ' in str_time:
                t = datetime.strptime(str_time, '%Y-%m-%d %H:%M:%S').time()
            else:
                try:
                    t = datetime.strptime(str_time, '%H:%M:%S').time()
                except:
                    t = datetime.strptime(str_time, '%H:%M').time()
                
            if t < datetime.strptime('09:30:00', '%H:%M:%S').time():
                return '集合竞价 (09:15-09:30)'
            elif t <= datetime.strptime('10:00:00', '%H:%M:%S').time():
                return '早盘闪击 (09:30-10:00)'
            elif t <= datetime.strptime('11:30:00', '%H:%M:%S').time():
                return '上午盘中 (10:00-11:30)'
            elif t <= datetime.strptime('14:30:00', '%H:%M:%S').time():
                return '下午盘中 (13:00-14:30)'
            else:
                return '尾盘潜伏 (14:30-15:00)'
        except Exception:
            return '未知'

    def full_audit(self):
        """Run full trade quality audit and return markdown report"""
        lines = []
        lines.append(f"📄 【V16 交易质量审计报告】 ({datetime.now().strftime('%Y-%m-%d')})")
        lines.append(f"\n⚙️ 审计起始日: {self.AUDIT_START_DATE} (之前的调试数据已排除)")
        
        # Build sections
        lines.append("\n📊 【当前持仓体检】")
        lines.extend(self.audit_current_positions())
        
        lines.append("\n🎯 【已平仓交易胜率】")
        lines.extend(self.audit_closed_trades())

        lines.append("\n📈 【风险与交易核心指标】")
        lines.extend(self.audit_risk_metrics())

        lines.append("\n⏰ 【买入时段胜率】")
        lines.extend(self.audit_buy_period_win_rate())
        
        lines.append("\n📅 【持仓天数分析】")
        lines.extend(self.audit_holding_days())

        lines.append("\n🎯 【卖出时机分析】")
        lines.extend(self.audit_sell_timing())

        lines.append("\n📊 【策略来源对比】")
        lines.extend(self.audit_strategy_sources())

        lines.append("\n🏷️ 【信号标签收益归因】")
        lines.extend(self.audit_signal_tag_performance())
        
        lines.append("\n📅 【每日买卖成功率】")
        lines.extend(self.audit_daily_success_rate())
        
        lines.append("\n📋 【当日备选池分析】")
        lines.extend(self.audit_watchlist())
        
        lines.append("\n🚦 【开仓门禁与T+1阻断事件】")
        lines.extend(self.audit_risk_gate_events())

        lines.append("\n🔎 【动态入场复核事件】")
        lines.extend(self.audit_pending_entry_check_events())

        lines.append("\n🛰️ 【影子执行审计】")
        lines.extend(self.audit_shadow_execution_reasons())

        lines.append("\n🧪 【门禁拦截候选后续跟踪】")
        lines.extend(self.audit_blocked_candidate_followup())
        
        return "\n".join(lines)

    def _safe_float(self, value, default=0.0):
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _format_code(self, code):
        code = str(code or '').strip()
        if not code:
            return ''
        if '.' in code:
            return code
        return f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

    def _short_text(self, value, max_len=80):
        text = str(value or '').replace('\n', ' ').replace('|', '/').strip()
        return text[:max_len]

    def _parse_json_payload(self, value):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, bytes):
            try:
                value = value.decode('utf-8')
            except Exception:
                return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return text

    def _extract_signal_tags(self, signal_tags_json):
        """Extract compact tag names from transaction/factor tag payloads."""
        parsed = self._parse_json_payload(signal_tags_json)
        tags = []

        def add_tag(raw):
            tag = str(raw or '').strip()
            if tag:
                tags.append(tag[:80])

        def walk(node):
            if node is None:
                return
            if isinstance(node, str):
                text = node.strip()
                if not text:
                    return
                if text.startswith('[') or text.startswith('{'):
                    reparsed = self._parse_json_payload(text)
                    if reparsed is not text:
                        walk(reparsed)
                        return
                add_tag(text)
                return
            if isinstance(node, dict):
                for key in ('tag', 'name', 'type', 'label'):
                    if node.get(key):
                        add_tag(node.get(key))
                        break
                for key in ('tags', 'risk_tags', 'confirmations', 'warnings'):
                    if key in node:
                        walk(node.get(key))
                return
            if isinstance(node, (list, tuple, set)):
                for item in node:
                    walk(item)

        walk(parsed)
        return sorted(set(tags))
        
    def _get_closed_trades_data(self):
        """Fetch and format closed trades with reason and strategy attribution"""
        try:
            conn = self.portfolio._get_connection()
            if not conn: return []
            
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                # Fetch transactions joined with strategy_selection to get the source strategy
                # We use a LEFT JOIN to still show manual trades (strategy='手动')
                query = """
                    SELECT t.*
                    FROM transactions t
                    WHERE t.date >= %s
                    ORDER BY t.date ASC
                """
                cursor.execute(query, (self.AUDIT_START_DATE,))
                records = cursor.fetchall()
            conn.close()
            
            if not records: return []
            
            closed_trades = []
            buys = [r for r in records if r['type'] == 'BUY']
            sells = [r for r in records if r['type'] == 'SELL']
            
            # Simple attribute caching to avoid redundant lookups
            for sell in sells:
                # Find matching buy (simple FIFO matching for the same code)
                matching_buy = next((b for b in buys if b['code'] == sell['code'] and b['date'] <= sell['date']), None)
                
                if matching_buy:
                    profit_pct = (sell['price'] - matching_buy['price']) / matching_buy['price'] * 100
                    pnl_amount = (sell['price'] - matching_buy['price']) * min(sell['quantity'], matching_buy['quantity'])
                    
                    b_dt = matching_buy['date']
                    s_dt = sell['date']
                    holding_days = (s_dt.date() - b_dt.date()).days
                    
                    closed_trades.append({
                        'code': sell['code'],
                        'name': sell['name'],
                        'strategy': matching_buy.get('source_strategy') or '手动/未知',
                        'weather': matching_buy.get('weather'),
                        'signal_tags_json': matching_buy.get('signal_tags_json'),
                        'snapshot_id': matching_buy.get('snapshot_id'),
                        'buy_time': b_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'buy_period': self._classify_time_period(b_dt.strftime('%H:%M:%S')),
                        'sell_time': s_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'buy_price': matching_buy['price'],
                        'sell_price': sell['price'],
                        'profit_pct': profit_pct,
                        'pnl_amount': pnl_amount,
                        'holding_days': holding_days,
                        'sell_reason': sell.get('reason') or '未知'
                    })
                    buys.remove(matching_buy)
            return closed_trades
        except Exception as e:
            logger.error(f"Error fetching closed trades: {e}")
            return []

    def audit_risk_metrics(self):
        """Calculate advanced risk and trading metrics"""
        trades = self._get_closed_trades_data()
        if not trades:
            return ["暂无足量平仓极速，无法计算风险指标。"]
            
        from core.utils import calculate_max_drawdown, calculate_volatility, calculate_sharpe_ratio, calculate_expected_return
        
        # Win Rate
        wins = [t for t in trades if t['profit_pct'] > 0]
        losses = [t for t in trades if t['profit_pct'] <= 0]
        
        win_rate = len(wins) / len(trades)
        
        avg_win_pct = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        
        expected_return = calculate_expected_return(win_rate, avg_win_pct/100, avg_loss_pct/100) * 100
        
        # Profit/Loss Ratio (RRR)
        rr_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else float('inf')
        
        # PnL Curve & Equity (Simulated)
        daily_pnl = {}
        for t in trades:
            d = t['sell_time'].split(' ')[0]
            daily_pnl[d] = daily_pnl.get(d, 0) + (t['profit_pct']/100)
            
        dates = sorted(daily_pnl.keys())
        pnl_series = [daily_pnl[d] for d in dates]
        
        # Equity Curve (Starting at 1.0)
        nav_list = [1.0]
        for p in pnl_series:
            nav_list.append(nav_list[-1] * (1 + p))
            
        max_dd = calculate_max_drawdown(nav_list) * 100
        volatility = calculate_volatility(pnl_series) * 100
        sharpe = calculate_sharpe_ratio(pnl_series)
        
        lines = []
        lines.append(f"- **系统胜率**: {win_rate*100:.1f}%")
        lines.append(f"- **盈亏比**: {rr_ratio:.2f}")
        lines.append(f"- **平均单笔收益预期**: {expected_return:.2f}%")
        lines.append(f"- **最大回撤**: {max_dd:.2f}%")
        lines.append(f"- **年化波动率**: {volatility:.2f}%")
        lines.append(f"- **夏普比率**: {sharpe:.2f}")
        
        return lines

    def audit_current_positions(self):
        """Audit current open positions with markdown tables"""
        lines = []
        try:
            positions = self.portfolio.load_positions()
            if not positions:
                return ["暂无持仓"]
                
            lines.append(f"({len(positions)}只在持)")
            lines.append("\n| 代码 | 名称 | 账户 | 买入均价 | 现价 | 浮动盈亏 | 持仓天数 | 买入时段 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            
            total_held = len(positions)
            unhealthy = 0
            total_holding_days = 0
            total_pnl_pct = 0.0
            
            for p in positions:
                code = p['code']
                name = p['name']
                acc = p.get('account', 'main')
                avg_price = float(p.get('avg_price', 0) or 0)
                
                ts_code = f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ"
                quotes = self.provider.get_realtime_quotes([ts_code])
                if quotes and ts_code in quotes:
                    quotes[ts_code] = self._repair_realtime_quote(ts_code, quotes[ts_code])
                
                # Default current_price to avg_price first
                current_price = float(p.get('current_price', avg_price) or avg_price)
                if quotes and ts_code in quotes:
                    current_price = quotes[ts_code]['price']
                
                profit_pct = p.get('pnl_pct')
                if profit_pct is not None and not quotes:
                    profit_pct = float(profit_pct)
                else:
                    profit_pct = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

                total_pnl_pct += profit_pct
                
                # Health check
                profit_str = f"{profit_pct:.2f}%"
                if profit_pct > 0:
                    profit_str = f"+{profit_str} 🍉"
                elif profit_pct <= -3:
                    profit_str = f"{profit_str} ❌"
                    unhealthy += 1
                elif profit_pct < 0:
                    profit_str = f"{profit_str} 📉"
                
                # Time info
                created_at = p.get('created_at') or p.get('update_time')
                buy_time_str = "未知"
                holding_days = 0
                
                if created_at:
                    try:
                        cr_dt = created_at if isinstance(created_at, datetime) else datetime.strptime(str(created_at), '%Y-%m-%d %H:%M:%S')
                        buy_time_str = self._classify_time_period(cr_dt.strftime('%H:%M:%S'))
                        holding_days = (datetime.now().date() - cr_dt.date()).days
                    except:
                        pass
                
                total_holding_days += holding_days
                lines.append(f"| {code} | {name} | {display_account(acc)} | {avg_price:.2f} | {current_price:.2f} | {profit_str} | {holding_days}天 | {buy_time_str} |")
            
            avg_holding = total_holding_days / total_held if total_held else 0
            avg_pnl = total_pnl_pct / total_held if total_held else 0
            
            lines.append(f"\n*持仓健康度: {total_held - unhealthy}/{total_held}*")
            if total_held - unhealthy <= total_held / 2:
                lines[-1] += " ⚠️"
            
            lines.append(f"平均持仓天数: {avg_holding:.1f}天")
            lines.append(f"平均浮动收益: {avg_pnl:.2f}%")
            
        except Exception as e:
            lines.append(f"查询出错: {e}")
        return lines

    def audit_closed_trades(self):
        """Audit closed trades win rate"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无已平仓交易"]
            
        wins = [t for t in closed_trades if t['profit_pct'] > 0]
        losses = [t for t in closed_trades if t['profit_pct'] <= 0]
        
        win_rate = len(wins) / len(closed_trades) * 100
        avg_win = sum(t['profit_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit_pct'] for t in losses) / len(losses) if losses else 0
        pnl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        
        lines = []
        lines.append(f"- **总平仓笔数**: {len(closed_trades)} 笔 (胜率 {win_rate:.1f}%)")
        lines.append(f"- **平均盈利**: +{avg_win:.2f}% | **平均亏损**: {avg_loss:.2f}%")
        lines.append(f"- **盈亏比**: {pnl_ratio:.2f}")
        return lines

    def audit_buy_period_win_rate(self):
        """Analyze win rate by buy time period"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无交易数据"]
            
        period_stats = {}
        for t in closed_trades:
            period = t['buy_period']
            if period not in period_stats:
                period_stats[period] = {'total': 0, 'wins': 0, 'pnl': 0.0}
            
            period_stats[period]['total'] += 1
            if t['profit_pct'] > 0:
                period_stats[period]['wins'] += 1
            period_stats[period]['pnl'] += t['profit_pct']
            
        lines = ["| 买入时段 | 笔数 | 胜率 | 平均收益 |", "| :--- | :--- | :--- | :--- |"]
        for p in sorted(period_stats.keys()):
            s = period_stats[p]
            wr = s['wins'] / s['total'] * 100
            avg_pnl = s['pnl'] / s['total']
            lines.append(f"| {p} | {s['total']} | {wr:.1f}% | {avg_pnl:+.2f}% |")
        return lines
            
    def audit_holding_days(self):
        """Analyze average holding days for wins vs losses"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无交易数据"]
            
        wins = [t['holding_days'] for t in closed_trades if t['profit_pct'] > 0]
        losses = [t['holding_days'] for t in closed_trades if t['profit_pct'] <= 0]
        
        avg_win_days = sum(wins) / len(wins) if wins else 0
        avg_loss_days = sum(losses) / len(losses) if losses else 0
        
        lines = []
        lines.append(f"- **平均获利持仓**: {avg_win_days:.1f} 天")
        lines.append(f"- **平均止损持仓**: {avg_loss_days:.1f} 天")
        if avg_loss_days > avg_win_days and avg_loss_days > 3:
            lines.append("- ⚠️ **提示**: 止损持仓时间过长，请注意遵守纪律。")
        return lines
        
    def audit_sell_timing(self):
        """Analyze performance by sell reason"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无交易数据"]
            
        reason_stats = {}
        for t in closed_trades:
            reason = humanize_text(t['sell_reason'])
            if reason not in reason_stats:
                reason_stats[reason] = {'total': 0, 'pnl': 0.0}
            reason_stats[reason]['total'] += 1
            reason_stats[reason]['pnl'] += t['profit_pct']
            
        lines = ["| 卖出原因 | 笔数 | 平均收益 |", "| :--- | :--- | :--- |"]
        for r in sorted(reason_stats.keys()):
            s = reason_stats[r]
            lines.append(f"| {r} | {s['total']} | {s['pnl']/s['total']:+.2f}% |")
        return lines
        
    def audit_strategy_sources(self):
        """Analyze performance by strategy source"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无交易数据"]
            
        strat_stats = {}
        for t in closed_trades:
            strat = t['strategy']
            if strat not in strat_stats:
                strat_stats[strat] = {'total': 0, 'wins': 0, 'pnl': 0.0}
            strat_stats[strat]['total'] += 1
            if t['profit_pct'] > 0:
                strat_stats[strat]['wins'] += 1
            strat_stats[strat]['pnl'] += t['profit_pct']
            
        lines = ["| 策略来源 | 笔数 | 胜率 | 平均收益 |", "| :--- | :--- | :--- | :--- |"]
        for s_name in sorted(strat_stats.keys()):
            s = strat_stats[s_name]
            wr = s['wins'] / s['total'] * 100
            avg_pnl = s['pnl'] / s['total']
            lines.append(f"| {s_name} | {s['total']} | {wr:.1f}% | {avg_pnl:+.2f}% |")
        return lines

    def audit_signal_tag_performance(self):
        """Analyze realized trade performance by signal/risk tags."""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无平仓交易，无法按标签归因。"]

        priority_tokens = (
            'WEAK_MARKET',
            'HIGH_GAP',
            'INSUFFICIENT_SAMPLES',
            'VWAP',
            'T1_BLOCKED',
            'ENTRY_SCENARIO_',
            'ENTRY_CONFIRM_',
            'DATA_QUALITY_',
            'PERMISSION_',
            'PRE_TRADE_GATE',
        )

        stats = {}
        for trade in closed_trades:
            tags = self._extract_signal_tags(trade.get('signal_tags_json'))
            if not tags:
                continue
            pnl = self._safe_float(trade.get('profit_pct'), 0.0)
            for tag in tags:
                if not any(token in tag for token in priority_tokens):
                    continue
                bucket = stats.setdefault(tag, {
                    'total': 0,
                    'wins': 0,
                    'pnl': 0.0,
                    'worst': pnl,
                    'best': pnl,
                })
                bucket['total'] += 1
                bucket['wins'] += 1 if pnl > 0 else 0
                bucket['pnl'] += pnl
                bucket['worst'] = min(bucket['worst'], pnl)
                bucket['best'] = max(bucket['best'], pnl)

        if not stats:
            return ["暂无可归因的重点标签。后续交易落库信号标签字段后会自动统计。"]

        def suggestion(tag, total, win_rate, avg_pnl, worst):
            if total < 3:
                return "样本不足，继续观察"
            if avg_pnl < -2 or worst < -5:
                return "偏危险，建议门禁更严"
            if win_rate < 40 and avg_pnl < 0:
                return "胜率偏低，降级为确认/低仓"
            if avg_pnl > 1 and win_rate >= 50:
                return "表现可接受，保留观察"
            if 'DATA_QUALITY_' in tag:
                return "优先修数据链路"
            return "继续累计样本"

        rows = []
        for tag, s in stats.items():
            total = s['total']
            win_rate = s['wins'] / total * 100 if total else 0
            avg_pnl = s['pnl'] / total if total else 0
            rows.append((tag, total, win_rate, avg_pnl, s['worst'], suggestion(tag, total, win_rate, avg_pnl, s['worst'])))

        rows.sort(key=lambda item: (item[3], item[4], -item[1]))
        lines = ["| 标签 | 交易数 | 胜率 | 平均收益 | 最大亏损 | 建议 |", "| :--- | ---: | ---: | ---: | ---: | :--- |"]
        for tag, total, win_rate, avg_pnl, worst, suggest in rows[:20]:
            lines.append(f"| {humanize_text(tag, 60)} | {total} | {win_rate:.1f}% | {avg_pnl:+.2f}% | {worst:+.2f}% | {suggest} |")
        return lines

    def audit_daily_success_rate(self):
        """Audit daily success rate"""
        closed_trades = self._get_closed_trades_data()
        if not closed_trades:
            return ["暂无平仓数据"]
            
        daily_stats = {}
        for t in closed_trades:
            date = t['sell_time'].split(' ')[0]
            if date not in daily_stats:
                daily_stats[date] = {'wins': 0, 'losses': 0, 'pnl': 0.0}
            if t['profit_pct'] > 0:
                daily_stats[date]['wins'] += 1
            else:
                daily_stats[date]['losses'] += 1
            daily_stats[date]['pnl'] += t.get('pnl_amount', 0)
            
        lines = []
        lines.append("| 卖出日期 | 胜局 | 败局 | 日胜率 | 当日实现盈亏 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        
        for date in sorted(daily_stats.keys(), reverse=True)[:10]:
            stats = daily_stats[date]
            total = stats['wins'] + stats['losses']
            win_rate = stats['wins'] / total * 100 if total > 0 else 0
            pnl_str = f"{stats['pnl']:.2f}"
            if stats['pnl'] > 0: pnl_str = f"+{pnl_str}"
            lines.append(f"| {date} | {stats['wins']} | {stats['losses']} | {win_rate:.1f}% | {pnl_str}元 |")
            
        return lines

    def audit_watchlist(self):
        """Audit active watchlist items"""
        try:
            watchlist = self.portfolio.get_watchlist(days=1)
            if not watchlist:
                return ["当日暂无入池个股"]
                
            lines = []
            lines.append("| 代码 | 名称 | 策略 | 入选价格 | 热度(换手率) | 状态 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
            
            for item in watchlist:
                code = item.get('code', '-')
                name = item.get('name', '-')
                strategy = item.get('strategy', '-')
                sel_price = item.get('sel_price', 0)
                turnover = item.get('turnover', 0)
                status = item.get('zt_result', '待验证')
                
                lines.append(f"| {code} | {name} | {strategy} | {sel_price} | {turnover}% | {status} |")
                
            return lines
        except Exception as e:
            return [f"获取备选池失败: {e}"]

    def audit_selection_conversion(self):
        """Audit selection to buy conversion rate"""
        try:
            conn = self.portfolio._get_connection()
            if not conn: return ["查询转化率失败: 无法连接数据库"]
            
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                # Get total selections
                sel_query = "SELECT strategy, COUNT(*) as cnt FROM strategy_selection WHERE date >= %s GROUP BY strategy"
                cursor.execute(sel_query, (self.AUDIT_START_DATE,))
                selections = cursor.fetchall()
                
                total_sel = sum(row['cnt'] for row in selections) if selections else 0
                
                # Get buys and group by strategy using JOIN on code and matching dates
                buy_query = """
                    SELECT s.strategy, COUNT(t.id) as cnt 
                    FROM transactions t 
                    JOIN strategy_selection s ON t.code = s.code AND t.date >= s.date
                    WHERE t.type='BUY' AND t.date >= %s 
                    GROUP BY s.strategy
                """
                cursor.execute(buy_query, (self.AUDIT_START_DATE,))
                buys_by_strat = {row['strategy']: row['cnt'] for row in cursor.fetchall()}
                total_buys = sum(buys_by_strat.values())
                
                # Get win rate from strategy_stats
                stats_query = """
                    SELECT strategy, AVG(success_rate) as avg_rate 
                    FROM strategy_stats 
                    WHERE date >= %s 
                    GROUP BY strategy
                """
                cursor.execute(stats_query, (self.AUDIT_START_DATE,))
                win_rates = {row['strategy']: row['avg_rate'] for row in cursor.fetchall()}
                
            conn.close()
            
            conv_rate = (total_buys / total_sel * 100) if total_sel > 0 else 0
            
            lines = []
            lines.append(f"总选股: {total_sel} 只 | 实际买入: {total_buys} 只 | 总转化率: **{conv_rate:.1f}%**\n")
            
            lines.append("按具体策略分解:")
            lines.append("\n| 策略 | 选入 | 买入 | 转化率 | 胜率 (吃肉率) |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            
            # [V16] Calculate Watchlist Summary (龙头+技术)
            watchlist_strats = ['龙头跟踪', '技术突破']
            w_sel = sum(row['cnt'] for row in selections if row['strategy'] in watchlist_strats)
            w_buys = sum(buys_by_strat.get(s, 0) for s in watchlist_strats)
            w_rate = (w_buys / w_sel * 100) if w_sel > 0 else 0
            
            # Calculate aggregate win rate for watchlist (weighted average)
            w_win_total = sum(win_rates.get(s, 0) * (buys_by_strat.get(s, 0) or 1) for s in watchlist_strats if s in buys_by_strat)
            w_win_buys = sum(buys_by_strat.get(s, 0) for s in watchlist_strats if s in buys_by_strat)
            avg_w_win = (w_win_total / w_win_buys) if w_win_buys > 0 else 0
            
            if total_sel > 0:
                # 1. Show regular strategies
                for row in selections:
                    strat = row['strategy']
                    buys = buys_by_strat.get(strat, 0)
                    win_rate = win_rates.get(strat, 0)
                    strat_rate = (buys / row['cnt'] * 100) if row['cnt'] > 0 else 0
                    lines.append(f"| {strat} | {row['cnt']} | {buys} | **{strat_rate:.1f}%** | **{win_rate:.1f}%** |")
                
                # 2. Add the specialized Watchlist Summary Row at the bottom
                if w_sel > 0:
                    lines.append(f"| 📊 **备选池合计** | {w_sel} | {w_buys} | **{w_rate:.1f}%** | **{avg_w_win:.1f}%** |")
            else:
                lines.append("| 暂无数据 | 0 | 0 | - | - |")
                
            return lines
                
        except Exception as e:
            return [f"查询转化率失败: {e}"]

    def audit_risk_gate_events(self):
        """Audit pre-trade gate blocks and T+1 blocked sell signals."""
        try:
            conn = self.portfolio._get_connection()
            if not conn:
                return ["查询门禁事件失败: 无法连接数据库"]

            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                summary_sql = """
                    SELECT event_type, COUNT(*) AS cnt, MAX(event_time) AS last_time
                    FROM risk_event_log
                    WHERE event_time >= %s
                      AND event_type IN ('BUY_BLOCKED_PRE_TRADE_GATE', 'PRE_TRADE_BLOCK', 'T1_BLOCKED_SELL_SIGNAL')
                    GROUP BY event_type
                    ORDER BY cnt DESC
                """
                cursor.execute(summary_sql, (self.AUDIT_START_DATE,))
                summary = cursor.fetchall()

                recent_sql = """
                    SELECT event_time, account, code, event_type, weather, reason, params_json
                    FROM risk_event_log
                    WHERE event_time >= %s
                      AND event_type IN ('BUY_BLOCKED_PRE_TRADE_GATE', 'PRE_TRADE_BLOCK', 'T1_BLOCKED_SELL_SIGNAL')
                    ORDER BY event_time DESC
                    LIMIT 12
                """
                cursor.execute(recent_sql, (self.AUDIT_START_DATE,))
                recent = cursor.fetchall()
            conn.close()

            lines = []
            if not summary and not recent:
                return ["暂无开仓门禁/T+1阻断事件。"]

            lines.append("事件汇总:")
            lines.append("\n| 事件类型 | 次数 | 最近时间 |")
            lines.append("| :--- | ---: | :--- |")
            for row in summary or []:
                lines.append(f"| {display_event_type(row.get('event_type'))} | {int(row.get('cnt') or 0)} | {row.get('last_time') or '-'} |")

            lines.append("\n最近事件:")
            lines.append("\n| 时间 | 账户 | 代码 | 类型 | 天气 | 原因 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
            for row in recent or []:
                reason = humanize_text(row.get('reason') or '', 80)
                lines.append(
                    f"| {row.get('event_time')} | {display_account(row.get('account'))} | {row.get('code') or '-'} | "
                    f"{display_event_type(row.get('event_type'))} | {row.get('weather') or '-'} | {reason} |"
                )

            return lines
        except Exception as e:
            return [f"查询门禁事件失败: {e}"]

    def _audit_reason_label(self, reason):
        reason = str(reason or '').strip()
        mapping = {
            "PERMISSION_OBSERVE_ONLY": "权限观察",
            "PERMISSION_BLOCK": "权限阻断",
            "SOURCE_NOT_ROUTED": "来源未路由",
            "SCHEDULE_WINDOW_MISSING": "窗口缺失",
            "DATA_QUALITY_BAD": "数据质量",
            "SECTOR_GATE_REJECT": "行业拒绝",
            "PRICE_BAND_CHASE_RISK": "追高风险",
        }
        return mapping.get(reason, humanize_text(reason, 24) if reason else "-")

    def audit_pending_entry_check_events(self):
        """Summarize executable pending re-check events for training attribution."""
        try:
            conn = self.portfolio._get_connection()
            if not conn:
                return ["查询动态入场复核事件失败: 无法连接数据库"]

            since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00')
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                summary_sql = """
                    SELECT account, strategy, decision, COUNT(1) AS cnt, MAX(check_time) AS last_time
                    FROM pending_entry_check_events
                    WHERE check_time >= %s
                    GROUP BY account, strategy, decision
                    ORDER BY account, strategy, cnt DESC
                    LIMIT 80
                """
                cursor.execute(summary_sql, (since,))
                summary = cursor.fetchall()

                reason_sql = """
                    SELECT account, strategy, decision, reason, COUNT(1) AS cnt, MAX(check_time) AS last_time
                    FROM pending_entry_check_events
                    WHERE check_time >= %s
                    GROUP BY account, strategy, decision, reason
                    ORDER BY cnt DESC, last_time DESC
                    LIMIT 12
                """
                cursor.execute(reason_sql, (since,))
                reasons = cursor.fetchall()
            conn.close()

            if not summary and not reasons:
                return ["最近7天暂无动态入场复核事件。"]

            total = sum(int(row.get('cnt') or 0) for row in summary or [])
            bought = sum(int(row.get('cnt') or 0) for row in summary or [] if str(row.get('decision') or '').upper() == 'BOUGHT')
            unfillable = sum(int(row.get('cnt') or 0) for row in summary or [] if str(row.get('decision') or '').upper() == 'UNFILLABLE')
            lines = [
                f"- 最近7天复核事件: {total} 条 | 买入事件: {bought} | 不可成交: {unfillable}",
                "- 说明: 该表记录 monitor 对 PENDING 的每次二次确认，是训练判断“为什么买/没买”的主证据。",
                "",
                "| 账户 | 策略 | 决策 | 次数 | 最近时间 |",
                "| :--- | :--- | :--- | ---: | :--- |",
            ]
            for row in summary or []:
                lines.append(
                    f"| {display_account(row.get('account'))} | {row.get('strategy') or '-'} | "
                    f"{row.get('decision') or '-'} | {int(row.get('cnt') or 0)} | {row.get('last_time') or '-'} |"
                )

            lines.extend([
                "",
                "主要原因:",
                "",
                "| 账户 | 策略 | 决策 | 次数 | 原因 |",
                "| :--- | :--- | :--- | ---: | :--- |",
            ])
            for row in reasons or []:
                lines.append(
                    f"| {display_account(row.get('account'))} | {row.get('strategy') or '-'} | {row.get('decision') or '-'} | "
                    f"{int(row.get('cnt') or 0)} | {humanize_text(row.get('reason') or '-', 80)} |"
                )
            return lines
        except Exception as e:
            return [f"查询动态入场复核事件失败: {e}"]

    def _infer_shadow_reason(self, strategy, payload):
        reason = str((payload or {}).get('failure_reason_primary') or '').strip()
        if reason:
            return reason
        strategy = str(strategy or '').replace('_SHADOW', '')
        regime = str((payload or {}).get('regime_assumption') or 'weak_market')
        try:
            matrix = Config.STRATEGY.get('strategy_permission_matrix', {}) if isinstance(Config.STRATEGY, dict) else {}
            rules = matrix.get(regime) or {}
            permission = str((rules or {}).get(strategy) or (rules or {}).get('*') or '')
            entry_policy = Config.STRATEGY.get('entry_policy', {}) if isinstance(Config.STRATEGY, dict) else {}
            windows = (((entry_policy.get('models') or {}).get('dynamic_window') or {}).get('strategy_windows') or {})
            real_windows = windows.get(strategy) or []
        except Exception:
            permission = ''
            real_windows = []
        if permission in ('OBSERVE', 'OBSERVE_ONLY'):
            return 'PERMISSION_OBSERVE_ONLY'
        if permission == 'BLOCK':
            return 'PERMISSION_BLOCK'
        if not real_windows:
            return 'SCHEDULE_WINDOW_MISSING'
        return 'SOURCE_NOT_ROUTED'

    def audit_shadow_execution_reasons(self):
        """Summarize audit-only SHADOW rows and their failure reasons."""
        try:
            conn = self.portfolio._get_connection()
            if not conn:
                return ["查询影子审计失败: 无法连接数据库"]

            since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00')
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                query = """
                    SELECT trade_date, code, name, source_strategy, status, payload_json,
                           last_reason, check_count, updated_at
                    FROM pending_entry_signals
                    WHERE updated_at >= %s
                      AND (status='SHADOW' OR entry_model='audit_only_shadow' OR source_strategy LIKE '%%_SHADOW')
                    ORDER BY updated_at DESC
                    LIMIT 120
                """
                cursor.execute(query, (since,))
                rows = cursor.fetchall()
            conn.close()

            if not rows:
                return ["最近7天暂无影子审计行。"]

            reason_counts = {}
            strategy_counts = {}
            parsed_rows = []
            with_reason = 0
            for row in rows:
                payload = self._parse_json_payload(row.get('payload_json'))
                if not isinstance(payload, dict):
                    payload = {}
                strategy = str(row.get('source_strategy') or '').replace('_SHADOW', '')
                reason = self._infer_shadow_reason(strategy, payload)
                if reason:
                    with_reason += 1
                reason_counts[reason or 'UNKNOWN'] = reason_counts.get(reason or 'UNKNOWN', 0) + 1
                strategy_counts[strategy or '-'] = strategy_counts.get(strategy or '-', 0) + 1
                parsed_rows.append((row, payload, reason, strategy))

            coverage = with_reason / max(len(rows), 1) * 100
            lines = []
            lines.append(f"- 最近7天影子审计: {len(rows)} 条 | 失败原因覆盖率: {coverage:.1f}%")
            lines.append("- 原因分布: " + "；".join([
                f"{self._audit_reason_label(k)} {v}"
                for k, v in sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)
            ]))
            lines.append("- 策略分布: " + "；".join([
                f"{k} {v}"
                for k, v in sorted(strategy_counts.items(), key=lambda kv: kv[1], reverse=True)
            ]))
            lines.append("- 结论: 该段只用于审计展示；影子审计不会被真实买入流程读取。")

            lines.append("\n| 日期 | 代码 | 名称 | 策略 | 审计原因 | 级别 | 动作 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for row, payload, reason, strategy in parsed_rows[:12]:
                lines.append(
                    f"| {row.get('trade_date') or '-'} | {row.get('code') or '-'} | {self._short_text(row.get('name'), 12) or '-'} | "
                    f"{strategy or '-'} | {self._audit_reason_label(reason)} | {payload.get('level') or '-'} | "
                    f"{display_action(payload.get('action'))} |"
                )

            return lines
        except Exception as e:
            return [f"查询影子审计失败: {e}"]

    def _extract_block_ref_price(self, event, params):
        price = self._safe_float((params or {}).get('price'), 0.0) if isinstance(params, dict) else 0.0
        if price > 0:
            return price, '门禁记录价'

        code = str((event or {}).get('code') or '').strip()
        if not code:
            return 0.0, ''

        event_time = event.get('event_time')
        event_date = ''
        try:
            event_date = event_time.strftime('%Y-%m-%d') if isinstance(event_time, datetime) else str(event_time)[:10]
        except Exception:
            event_date = ''

        conn = self.portfolio._get_connection()
        if not conn:
            return 0.0, ''
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                query = """
                    SELECT sel_price, name, strategy, date
                    FROM strategy_selection
                    WHERE code=%s
                      AND (%s='' OR date<=%s)
                    ORDER BY date DESC, created_at DESC
                    LIMIT 1
                """
                cursor.execute(query, (code, event_date, event_date))
                row = cursor.fetchone()
                if row:
                    sel_price = self._safe_float(row.get('sel_price'), 0.0)
                    if sel_price > 0:
                        return sel_price, f"入库记录:{row.get('date')}"
        except Exception:
            return 0.0, ''
        finally:
            conn.close()
        return 0.0, ''

    def audit_blocked_candidate_followup(self):
        """Estimate whether recently blocked candidates would have helped or hurt."""
        try:
            conn = self.portfolio._get_connection()
            if not conn:
                return ["查询拦截候选失败: 无法连接数据库"]

            since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00')
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                query = """
                    SELECT event_time, account, code, event_type, weather, reason, params_json
                    FROM risk_event_log
                    WHERE event_time >= %s
                      AND event_type IN ('BUY_BLOCKED_PRE_TRADE_GATE', 'PRE_TRADE_BLOCK')
                      AND code IS NOT NULL
                      AND code <> ''
                    ORDER BY event_time DESC
                    LIMIT 30
                """
                cursor.execute(query, (since,))
                events = cursor.fetchall()
            conn.close()

            if not events:
                return ["最近7天暂无可跟踪的开仓门禁拦截候选。"]

            codes = sorted({self._format_code(row.get('code')) for row in events if self._format_code(row.get('code'))})
            quotes = self.provider.get_realtime_quotes(codes) if codes else {}
            if quotes:
                for ts_code in list(quotes.keys()):
                    if ts_code.startswith('_'):
                        continue
                    quotes[ts_code] = self._repair_realtime_quote(ts_code, quotes[ts_code])

            rows = []
            for event in events:
                params = self._parse_json_payload(event.get('params_json'))
                if not isinstance(params, dict):
                    params = {}

                ref_price, price_source = self._extract_block_ref_price(event, params)
                ts_code = self._format_code(event.get('code'))
                quote = quotes.get(ts_code, {}) if isinstance(quotes, dict) else {}
                current_price = self._safe_float(quote.get('price') or quote.get('close'), 0.0)
                if ref_price <= 0 or current_price <= 0:
                    continue

                pnl = (current_price - ref_price) / ref_price * 100
                rows.append({
                    'event_time': event.get('event_time'),
                    'code': event.get('code'),
                    'name': quote.get('name') or params.get('name') or '',
                    'reason': event.get('reason') or params.get('reason') or '',
                    'ref_price': ref_price,
                    'price_source': price_source,
                    'current_price': current_price,
                    'pnl': pnl,
                })

            if not rows:
                return [
                    f"最近7天有 {len(events)} 条门禁拦截，但缺少拦截价或当前价，暂不能计算后续表现。",
                    "建议继续确保买入前门禁事件里的拦截价与候选入库价完整落库。",
                ]

            avg_pnl = sum(row['pnl'] for row in rows) / len(rows)
            worst = min(row['pnl'] for row in rows)
            best = max(row['pnl'] for row in rows)
            would_win = sum(1 for row in rows if row['pnl'] > 0)
            would_loss = len(rows) - would_win

            lines = []
            lines.append(f"- 最近7天可计算拦截候选: {len(rows)}/{len(events)}")
            lines.append(f"- 若当时买入，当前估算平均收益: {avg_pnl:+.2f}% | 最差 {worst:+.2f}% | 最好 {best:+.2f}%")
            lines.append(f"- 当前看会涨: {would_win} 只 | 会跌/不涨: {would_loss} 只")
            if avg_pnl < 0:
                lines.append("- 结论: 门禁整体拦截方向暂时有效，继续保留。")
            elif avg_pnl > 1.5 and len(rows) >= 5:
                lines.append("- 结论: 门禁可能偏严，需要结合 T+1/T+2 精确回测再调。")
            else:
                lines.append("- 结论: 样本或收益边际不足，继续观察。")

            lines.append("\n| 时间 | 代码 | 名称 | 拦截价 | 当前价 | 当前估算 | 价格来源 | 原因 |")
            lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | :--- | :--- |")
            for row in sorted(rows, key=lambda x: x['event_time'], reverse=True)[:12]:
                lines.append(
                    f"| {row['event_time']} | {row['code']} | {self._short_text(row['name'], 16) or '-'} | "
                    f"{row['ref_price']:.2f} | {row['current_price']:.2f} | {row['pnl']:+.2f}% | "
                    f"{humanize_text(row['price_source'] or '-')} | {humanize_text(row['reason'], 48)} |"
                )
            return lines
        except Exception as e:
            return [f"查询拦截候选失败: {e}"]
