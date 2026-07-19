import json
import pymysql
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from core.config import Config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - EvolutionEngine - %(levelname)s - %(message)s',
    filename=os.path.join(LOG_DIR, f"evolution-{datetime.now().strftime('%Y%m%d')}.log"),
    filemode='a'
)
logger = logging.getLogger('EvolutionEngine')

class StrategyEvolver:
    """
    [V18.6] 策略自进化引擎 - 选股与实盘对齐版
    
    核心逻辑:
    1. 每周自动复盘近15日实测数据
    2. 区分 T+1 (日内) 与 T+2 (盘后) 生命周期
    3. [NEW] 引入实盘成交 (Transactions) 分析，对比选股胜率与实盘盈利
    4. 自动调整各类超参数，并在报表中展示“实盘/选股一致性”
    """
    
    def __init__(self):
        Config.load_strategy_config()
        self.config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "strategy_config.json")
        self.db_conn = self._connect_db()
        self.history_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "evolution_history.json")

    def __del__(self):
        """Ensure DB connection is closed on garbage collection"""
        try:
            if hasattr(self, 'db_conn') and self.db_conn:
                self.db_conn.close()
        except Exception:
            pass

    def _connect_db(self):
        return pymysql.connect(
            host=Config.DB_HOST,
            user=Config.DB_USER,
            password=Config.DB_PASS,
            database=Config.DB_NAME,
            port=Config.DB_PORT,
            charset='utf8mb4'
        )

    def fetch_performance_data(self, days=15):
        """[V18.4] Fetch stats from strategy_selection"""
        query = """
            SELECT strategy, analysis_cycle, zt_result, count(*) as cnt 
            FROM strategy_selection 
            WHERE date >= %s AND zt_result IN ('吃肉', '吃面')
            GROUP BY strategy, analysis_cycle, zt_result
        """
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            with self.db_conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(query, (cutoff,))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch selection stats: {e}")
            return []

    def fetch_trade_performance(self, days=15):
        """Build realized trade stats from transactions using FIFO matching.

        Returns a dict with:
        - closed_trades: realized sell records, one row per SELL transaction
        - buy_stats: buy-side counts grouped by source strategy
        - sell_reason_stats: sell-side counts grouped by reason
        - trade_stats: headline realized metrics
        """
        query = """
            SELECT date, account, type, code, name, price, quantity, amount,
                   reason, source_strategy
            FROM transactions 
            WHERE date >= %s
            ORDER BY date ASC, id ASC
        """
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            with self.db_conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(query, (cutoff,))
                txs = cursor.fetchall()

            buy_queues = defaultdict(list)
            buy_stats = {}
            sell_reason_stats = {}
            closed_trades = []

            for tx in txs:
                code = str(tx.get('code') or '')
                account = str(tx.get('account') or 'main')
                tx_type = str(tx.get('type') or '').upper()
                strategy = str(tx.get('source_strategy') or '').strip() or '手动/未知'
                reason = str(tx.get('reason') or '').strip() or '未注明原因'
                quantity = int(tx.get('quantity') or 0)
                amount = float(tx.get('amount') or 0.0)
                trade_dt = tx.get('date')
                key = (account, code)

                if tx_type == 'BUY':
                    cost = abs(amount)
                    buy_queues[key].append({
                        'qty': quantity,
                        'cost': cost,
                        'date': trade_dt,
                        'source_strategy': strategy,
                    })
                    stat = buy_stats.setdefault(strategy, {'count': 0, 'amount': 0.0, 'quantity': 0})
                    stat['count'] += 1
                    stat['amount'] += cost
                    stat['quantity'] += quantity
                elif tx_type == 'SELL':
                    sell_qty_remaining = quantity
                    allocated_cost = 0.0
                    source_counter = {}
                    holding_days = []

                    while sell_qty_remaining > 0 and buy_queues[key]:
                        lot = buy_queues[key][0]
                        lot_qty = int(lot.get('qty') or 0)
                        if lot_qty <= 0:
                            buy_queues[key].pop(0)
                            continue

                        matched_qty = min(sell_qty_remaining, lot_qty)
                        lot_cost = float(lot.get('cost') or 0.0)
                        lot_ratio = matched_qty / lot_qty if lot_qty > 0 else 0.0
                        matched_cost = lot_cost * lot_ratio
                        allocated_cost += matched_cost

                        lot_strategy = str(lot.get('source_strategy') or '').strip() or '手动/未知'
                        source_counter[lot_strategy] = source_counter.get(lot_strategy, 0) + matched_qty

                        if lot.get('date') and trade_dt:
                            try:
                                holding_days.append((trade_dt - lot['date']).days)
                            except Exception:
                                pass

                        lot['qty'] = lot_qty - matched_qty
                        lot['cost'] = lot_cost - matched_cost
                        sell_qty_remaining -= matched_qty
                        if lot['qty'] <= 0:
                            buy_queues[key].pop(0)

                    if allocated_cost <= 0:
                        logger.warning(f"Skip SELL matching for {account}/{code}: no matched BUY lot")
                        continue

                    pnl = amount - allocated_cost
                    pnl_pct = (pnl / allocated_cost) * 100 if allocated_cost > 0 else 0.0
                    primary_strategy = max(source_counter.items(), key=lambda x: x[1])[0] if source_counter else strategy
                    avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else None

                    closed_trade = {
                        'account': account,
                        'code': code,
                        'name': tx.get('name'),
                        'sell_time': trade_dt,
                        'sell_reason': reason,
                        'sell_quantity': quantity,
                        'sell_amount': amount,
                        'allocated_cost': allocated_cost,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'source_strategy': primary_strategy,
                        'holding_days': avg_holding_days,
                    }
                    closed_trades.append(closed_trade)

                    reason_stat = sell_reason_stats.setdefault(reason, {'count': 0, 'wins': 0, 'pnl_sum': 0.0})
                    reason_stat['count'] += 1
                    if pnl > 0:
                        reason_stat['wins'] += 1
                    reason_stat['pnl_sum'] += pnl_pct

            win_count = len([t for t in closed_trades if float(t.get('pnl') or 0.0) > 0])
            loss_count = len([t for t in closed_trades if float(t.get('pnl') or 0.0) < 0.0])
            flat_count = len(closed_trades) - win_count - loss_count
            avg_pnl_pct = (sum(float(t.get('pnl_pct') or 0.0) for t in closed_trades) / len(closed_trades)) if closed_trades else 0.0
            total_pnl = sum(float(t.get('pnl') or 0.0) for t in closed_trades)
            trade_win_rate = (win_count / len(closed_trades)) if closed_trades else 0.0

            return {
                'closed_trades': closed_trades,
                'buy_stats': buy_stats,
                'sell_reason_stats': sell_reason_stats,
                'trade_stats': {
                    'count': len(closed_trades),
                    'wins': win_count,
                    'losses': loss_count,
                    'flats': flat_count,
                    'win_rate': trade_win_rate,
                    'avg_pnl_pct': avg_pnl_pct,
                    'total_pnl': total_pnl,
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch trade performance: {e}")
            return {
                'closed_trades': [],
                'buy_stats': {},
                'sell_reason_stats': {},
                'trade_stats': {
                    'count': 0,
                    'wins': 0,
                    'losses': 0,
                    'flats': 0,
                    'win_rate': 0.0,
                    'avg_pnl_pct': 0.0,
                    'total_pnl': 0.0,
                }
            }

    def fetch_gate_event_stats(self, days=15):
        """Fetch pre-trade/T+1 risk event stats for permission-matrix evolution."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        result = {
            'summary': {},
            'by_strategy': {},
            'by_tag': {},
            'recent': [],
        }
        try:
            with self.db_conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT event_time, account, code, event_type, weather, reason, params_json
                    FROM risk_event_log
                    WHERE event_time >= %s
                      AND event_type IN ('BUY_BLOCKED_PRE_TRADE_GATE', 'PRE_TRADE_BLOCK', 'T1_BLOCKED_SELL_SIGNAL')
                    ORDER BY event_time DESC
                    LIMIT 200
                    """,
                    (cutoff,),
                )
                rows = cursor.fetchall() or []

            for row in rows:
                event_type = row.get('event_type') or 'UNKNOWN'
                result['summary'][event_type] = result['summary'].get(event_type, 0) + 1
                if len(result['recent']) < 20:
                    result['recent'].append({
                        'event_time': str(row.get('event_time') or ''),
                        'account': row.get('account'),
                        'code': row.get('code'),
                        'event_type': event_type,
                        'weather': row.get('weather'),
                        'reason': str(row.get('reason') or '')[:160],
                    })

                params = {}
                try:
                    raw = row.get('params_json')
                    params = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
                except Exception:
                    params = {}

                strategy = str(params.get('strategy') or '').strip() or '未知策略'
                if strategy:
                    bucket = result['by_strategy'].setdefault(strategy, {'total': 0, 'events': {}})
                    bucket['total'] += 1
                    bucket['events'][event_type] = bucket['events'].get(event_type, 0) + 1

                tags = params.get('risk_tags') or params.get('tags') or []
                if isinstance(tags, str):
                    tags = [tags]
                for tag in tags if isinstance(tags, list) else []:
                    tag = str(tag or '').strip()
                    if tag:
                        result['by_tag'][tag] = result['by_tag'].get(tag, 0) + 1

            return result
        except Exception as e:
            logger.warning(f"Failed to fetch gate event stats: {e}")
            return result

    def build_strategy_patch_suggestions(self, strategy_stats, trade_data, gate_stats):
        """Build conservative strategy_patch suggestions from observed failures.

        This returns suggestions only. It does not mutate Config.STRATEGY directly,
        because gate/permission changes should be reviewed when samples are small.
        """
        patch = {}
        notes = []
        current = Config.STRATEGY if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}

        trade_stats = trade_data.get('trade_stats') or {}
        trade_count = int(trade_stats.get('count') or 0)
        trade_win_rate = float(trade_stats.get('win_rate') or 0.0)
        trade_avg_pnl = float(trade_stats.get('avg_pnl_pct') or 0.0)
        t1_blocks = int((gate_stats.get('summary') or {}).get('T1_BLOCKED_SELL_SIGNAL', 0) or 0)
        buy_blocks = int((gate_stats.get('summary') or {}).get('BUY_BLOCKED_PRE_TRADE_GATE', 0) or 0)

        def ensure(path):
            node = patch
            for key in path:
                node = node.setdefault(key, {})
            return node

        matrix = current.get('strategy_permission_matrix') or {}
        weak_row = matrix.get('weak_market') or {}
        normal_row = matrix.get('normal_uptrend') or {}

        # A股 T+1 下，早盘竞价在弱市不应自动进攻；冷启动只允许小仓确认，
        # 用于把强票/龙头观察转成可验证样本。
        weak_matrix = {}
        for strategy in ('集合竞价', '早盘竞价首选'):
            if weak_row.get(strategy) != 'BLOCK':
                weak_matrix[strategy] = 'BLOCK'
        if weak_row.get('冷启动') not in ('LOW_SIZE_CONFIRM', 'CONFIRM_ONLY', 'OBSERVE', 'BLOCK'):
            weak_matrix['冷启动'] = 'LOW_SIZE_CONFIRM'
        if weak_row.get('午盘精选') not in ('LOW_SIZE_CONFIRM', 'CONFIRM_ONLY', 'OBSERVE', 'BLOCK'):
            weak_matrix['午盘精选'] = 'LOW_SIZE_CONFIRM'
        if weak_row.get('技术突破') not in ('CONFIRM_ONLY', 'LOW_SIZE_CONFIRM', 'OBSERVE', 'BLOCK'):
            weak_matrix['技术突破'] = 'CONFIRM_ONLY'
        if weak_matrix:
            ensure(['strategy_permission_matrix'])['weak_market'] = weak_matrix
            notes.append('弱市权限矩阵建议继续保持防守：早盘BLOCK，冷启动/午盘/技术只确认。')
        else:
            notes.append('弱市权限矩阵当前已处于防守状态。')

        if normal_row.get('集合竞价') != 'CONFIRM_ONLY':
            ensure(['strategy_permission_matrix']).setdefault('normal_uptrend', {})['集合竞价'] = 'CONFIRM_ONLY'
            notes.append('普通多头下集合竞价建议默认pending确认，避免09:26直接追高。')

        # If realized trades are weak or T+1 blocked risk appears, tighten weak-market gap/chase thresholds.
        # Keep the open-gap guard strict, but do not revert the current-change probe
        # below its floor; recent audits showed 2.5% over-kills many strong confirmations.
        if (trade_count >= 5 and (trade_win_rate < 0.45 or trade_avg_pnl < 0)) or t1_blocks > 0:
            gate_patch = ensure(['weak_market_entry_gate'])
            curr_open = float((current.get('weak_market_entry_gate') or {}).get('weak_max_open_change', 3.0) or 3.0)
            curr_chg = float((current.get('weak_market_entry_gate') or {}).get('weak_max_change', 3.0) or 3.0)
            chg_floor = float((current.get('weak_market_entry_gate') or {}).get('weak_max_change_floor', 4.5) or 4.5)
            if curr_open > 2.5:
                gate_patch['weak_max_open_change'] = 2.5
            if curr_chg > chg_floor:
                gate_patch['weak_max_change'] = chg_floor
            gate_patch['block_insufficient_samples'] = True
            gate_patch['min_win_rate_samples'] = max(30, int((current.get('weak_market_entry_gate') or {}).get('min_win_rate_samples', 30) or 30))
            notes.append(f'实盘收益/胜率或T+1阻断显示风险偏高，建议弱市高开阈值保持2.5%，当前涨幅阈值不低于{chg_floor:.1f}%。')

        # Entry confirm should stay strict when gate blocks are already firing.
        if buy_blocks > 0:
            entry_patch = ensure(['entry_confirm'])
            entry_patch['enabled'] = True
            entry_patch['max_price_vwap_ratio'] = min(1.025, float((current.get('entry_confirm') or {}).get('max_price_vwap_ratio', 1.025) or 1.025))
            entry_patch['min_volume_ratio'] = max(1.8, float((current.get('entry_confirm') or {}).get('min_volume_ratio', 1.8) or 1.8))
            notes.append('门禁已产生拦截样本，pending确认建议保持VWAP偏离<=2.5%、量比>=1.8。')

        # Selection stats can flag strategy-level downgrade candidates.
        for (strategy, cycle), stat in sorted(strategy_stats.items(), key=lambda x: (x[0][1], x[0][0])):
            total = int(stat.get('total') or 0)
            if total < 10:
                continue
            hit_rate = float(stat.get('hits') or 0) / total
            if cycle == 'T+1' and hit_rate < 0.35:
                ensure(['strategy_permission_matrix']).setdefault('range_market', {})[strategy] = 'CONFIRM_ONLY'
                notes.append(f'{strategy} 近15日T+1命中率 {hit_rate:.1%}，震荡市建议降级为CONFIRM_ONLY。')

        return {
            'strategy_patch': patch,
            'notes': notes,
            'metrics': {
                'trade_count': trade_count,
                'trade_win_rate': trade_win_rate,
                'trade_avg_pnl_pct': trade_avg_pnl,
                'buy_block_events': buy_blocks,
                't1_block_events': t1_blocks,
            },
        }

    def _format_patch_review(self, patch_info):
        patch = (patch_info or {}).get('strategy_patch') or {}
        notes = (patch_info or {}).get('notes') or []
        lines = []
        if notes:
            lines.extend(f"• {note}" for note in notes)
        if patch:
            lines.append("• 建议 strategy_patch:")
            lines.append(json.dumps(patch, ensure_ascii=False, indent=2))
        else:
            lines.append("• 当前暂无需要新增的权限/门禁配置patch。")
        return lines

    def _build_trade_review(self, trade_data):
        """Format buy-side and sell-side review lines for the evolution report."""
        closed_trades = trade_data.get('closed_trades') or []
        trade_stats = trade_data.get('trade_stats') or {}
        buy_stats = trade_data.get('buy_stats') or {}
        sell_reason_stats = trade_data.get('sell_reason_stats') or {}

        lines = []
        if not closed_trades:
            lines.append("🏆 实盘卖出闭环: 暂无已平仓交易，暂不能计算真实胜率")
        else:
            lines.append(
                "🏆 实盘卖出闭环: "
                f"{trade_stats.get('count', 0)}笔 | 盈利 {trade_stats.get('wins', 0)} | "
                f"亏损 {trade_stats.get('losses', 0)} | 持平 {trade_stats.get('flats', 0)} | "
                f"胜率 {trade_stats.get('win_rate', 0.0):.1%} | "
                f"平均收益 {trade_stats.get('avg_pnl_pct', 0.0):+.2f}% | "
                f"累计盈亏 {trade_stats.get('total_pnl', 0.0):+.2f}元"
            )

            by_strategy = {}
            for trade in closed_trades:
                strat = str(trade.get('source_strategy') or '').strip() or '手动/未知'
                stat = by_strategy.setdefault(strat, {'count': 0, 'wins': 0, 'pnl_sum': 0.0})
                stat['count'] += 1
                if float(trade.get('pnl') or 0.0) > 0:
                    stat['wins'] += 1
                stat['pnl_sum'] += float(trade.get('pnl_pct') or 0.0)

            for strat, stat in sorted(by_strategy.items(), key=lambda x: (-x[1]['count'], x[0]))[:5]:
                avg_pct = stat['pnl_sum'] / stat['count'] if stat['count'] else 0.0
                win_rate = stat['wins'] / stat['count'] if stat['count'] else 0.0
                lines.append(f"• [卖出归因] {strat}: {stat['count']}笔 | 胜率 {win_rate:.1%} | 平均收益 {avg_pct:+.2f}%")

        if buy_stats:
            for strat, stat in sorted(buy_stats.items(), key=lambda x: (-x[1]['count'], x[0]))[:5]:
                avg_amount = stat['amount'] / stat['count'] if stat['count'] else 0.0
                lines.append(f"• [买入来源] {strat}: {stat['count']}笔 | 平均投入 {avg_amount:.0f}元")

        if sell_reason_stats:
            for reason, stat in sorted(sell_reason_stats.items(), key=lambda x: (-x[1]['count'], x[0]))[:5]:
                avg_pct = stat['pnl_sum'] / stat['count'] if stat['count'] else 0.0
                win_rate = stat['wins'] / stat['count'] if stat['count'] else 0.0
                lines.append(f"• [卖出原因] {reason}: {stat['count']}笔 | 胜率 {win_rate:.1%} | 平均收益 {avg_pct:+.2f}%")

        return lines

    def _load_history(self):
        """Load evolution history"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {'weekly_win_rates': [], 'hsgt_weight': 5, 'last_evolution': None}

    def _save_history(self, history):
        """Save evolution history"""
        history['last_evolution'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(self.history_file, 'w') as f:
            json.dump(history, f, indent=2)

    def evolve(self, dry_run=False):
        """[V18.6] Main evolution loop with Selection + Trade Integration"""
        stats = self.fetch_performance_data()
        trade_data = self.fetch_trade_performance()
        gate_stats = self.fetch_gate_event_stats()
        real_trades = trade_data.get('closed_trades') or []
        
        if not stats and not real_trades:
            logger.info("No sufficient data for evolution yet.")
            return

        # 1. Selection Data Processing
        processed_stats = []
        for s in stats:
            strat = s['strategy']
            if strat == '早盘竞价首选': strat = '集合竞价'
            cyc = s.get('analysis_cycle', 'T+1')
            if strat == '盘后资金流': cyc = 'T+2'
            elif strat in ['集合竞价', '午盘精选', '龙头跟踪', '技术突破']: cyc = 'T+1'
            s['strategy'], s['analysis_cycle'] = strat, cyc
            processed_stats.append(s)
        
        overall_total = sum(s['cnt'] for s in processed_stats)
        overall_wins = sum(s['cnt'] for s in processed_stats if s['zt_result'] == '吃肉')
        current_win_rate = overall_wins / overall_total if overall_total > 0 else 0
        
        strategy_stats = {}
        for s in processed_stats:
            key = (s['strategy'], s['analysis_cycle'])
            if key not in strategy_stats: strategy_stats[key] = {'total': 0, 'hits': 0}
            strategy_stats[key]['total'] += s['cnt']
            if s['zt_result'] == '吃肉': strategy_stats[key]['hits'] += s['cnt']

        # 2. Performance Reviews
        selection_review = []
        all_keys = sorted(strategy_stats.keys(), key=lambda x: (x[1], x[0]))
        for (strat, cycle) in all_keys:
            data = strategy_stats[(strat, cycle)]
            hit_rate = data['hits'] / data['total']
            selection_review.append(f"• [{cycle}] {strat}: {data['total']}笔 | 胜率 {hit_rate:.1%}")

        real_review = self._build_trade_review(trade_data)

        # 3. Tuning Logic
        changes_made = []
        for (strat, cycle) in all_keys:
            data = strategy_stats[(strat, cycle)]
            hit_rate = data['hits'] / data['total']
            
            if cycle == 'T+1':
                if strat in ['集合竞价', '龙头跟踪'] and hit_rate < 0.35:
                    self._tune_weight('afternoon', 'weight_turnover', increment=5)
                    changes_made.append(f"T+1 {strat}: 选股弱势 -> 强化换手过滤")
                elif hit_rate > 0.6:
                    self._tune_weight('afternoon', 'weight_change', increment=3)
                    changes_made.append(f"T+1 {strat}: 选股强势 -> 提升涨幅权重")
            elif cycle == 'T+2' and strat == '盘后资金流':
                if hit_rate < 0.4:
                    self._tune_weight('afternoon', 'weight_elg_ratio', increment=5)
                    changes_made.append(f"T+2 {strat}: 选股过载 -> 强化资金过滤")
                elif hit_rate > 0.65:
                    self._tune_weight('afternoon', 'weight_elg_ratio', increment=-3)
                    changes_made.append(f"T+2 {strat}: 选股精确 -> 优化入场门槛")

        # 4. Long-term Trend (HSGT)
        history = self._load_history()
        history['weekly_win_rates'].append(current_win_rate)
        history['weekly_win_rates'] = history['weekly_win_rates'][-8:]
        if len(history['weekly_win_rates']) >= 4:
            recent = history['weekly_win_rates'][-4:]
            if all(recent[i] <= recent[i+1] for i in range(len(recent)-1)):
                curr_hsgt = history.get('hsgt_weight', 5)
                if curr_hsgt < 15:
                    new_hsgt = curr_hsgt + 2
                    if 'afternoon' not in Config.STRATEGY: Config.STRATEGY['afternoon'] = {}
                    Config.STRATEGY['afternoon']['weight_hsgt'] = new_hsgt
                    changes_made.append(f"🔝 胜率持续改善! 北向因子权重强化: {curr_hsgt}% -> {new_hsgt}%")
                    history['hsgt_weight'] = new_hsgt

        # 5. Permission/gate patch suggestions from risk events and realized trades.
        patch_info = self.build_strategy_patch_suggestions(strategy_stats, trade_data, gate_stats)
        patch_review = self._format_patch_review(patch_info)
        if patch_info.get('strategy_patch'):
            changes_made.append("生成权限矩阵/门禁/入场确认 strategy_patch 建议，需人工确认后应用")

        # 6. Entry policy evolution (dynamic window suggestions; never auto-enable)
        try:
            entry_changes = self.evolve_entry_policy(window_days=30)
            if entry_changes:
                changes_made.extend(entry_changes)
        except Exception:
            pass

        # 7. Finalize
        promotion = self._check_promotion()
        if promotion: changes_made.append(promotion)

        if not dry_run:
            self._save_history(history)
            self._save_config()

            # [VNext] Persist evolution audit log (best-effort)
            try:
                from core.portfolio import PortfolioManager
                pm = PortfolioManager()
                pm.save_evolution_audit(
                    dry_run=False,
                    window_days=15,
                    changes=changes_made,
                    metrics={
                        'overall_total': overall_total,
                        'current_win_rate': current_win_rate,
                        'trade_count': len(real_trades or []),
                        'trade_win_rate': float((trade_data.get('trade_stats') or {}).get('win_rate', 0.0) or 0.0),
                        'trade_avg_pnl_pct': float((trade_data.get('trade_stats') or {}).get('avg_pnl_pct', 0.0) or 0.0),
                        'trade_total_pnl': float((trade_data.get('trade_stats') or {}).get('total_pnl', 0.0) or 0.0),
                        'gate_stats': gate_stats,
                        'strategy_patch': patch_info,
                    },
                )
            except Exception:
                pass

            neg_stats = self._get_negative_filter_stats()

            notification = f"""📊 策略自进化周报 (V18.6)

🗓️ 周期: 近15日选股与实盘对齐数据
📈 综合选股胜率: {current_win_rate:.1%}

🔍 实盘与选股一致性分析:
{chr(10).join(real_review)}
{chr(10).join(selection_review)}

📝 本周系统自调优:
{chr(10).join(f'• {c}' for c in changes_made) if changes_made else '• 系统运行稳定，无需参数调整'}

🧭 权限矩阵/门禁建议:
{chr(10).join(patch_review)}

🚫 负面过滤器拦截:
{neg_stats}

💡 当前系统关键配置:
• 北向资金权重: {Config.STRATEGY.get('afternoon', {}).get('weight_hsgt', 5)}%
• 权限板块: {Config.get_allowed_boards()}
"""
            print(f"✅ Strategy evolved based on {overall_total} records.")
            self._send_evolution_email(notification)
        else:
            logger.info("Dry run complete.")

    def _get_negative_filter_stats(self):
        """[V10.8] 获取本周负面过滤器统计数据"""
        conn = None
        try:
            conn = self._connect_db()
            week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT COUNT(*) as cnt FROM strategy_selection WHERE date >= %s AND turnover > 15", (week_ago,))
                high_to = cursor.fetchone()['cnt']
                cursor.execute("SELECT COUNT(*) as cnt FROM strategy_selection WHERE date >= %s AND change_pct BETWEEN 4 AND 8 AND turnover BETWEEN 5 AND 12", (week_ago,))
                mid_mid = cursor.fetchone()['cnt']
                return f"• 高换手(>15%): {high_to}只\n• 中庸陷阱(4-8%涨+5-12%换): {mid_mid}只"
        except Exception as e:
            return "• 数据获取失败"
        finally:
            if conn: conn.close()

    def _send_evolution_email(self, message):
        """[V10.8] 发送进化邮件通知"""
        try:
            msg_for_cmd = message.replace('\n', '\\n').replace('"', '\\"')
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cmd = f'cd "{project_root}" && python main.py --mode notify_update --msg "{msg_for_cmd}"'
            os.system(cmd)
        except Exception:
            pass

    def evolve_entry_policy(self, window_days: int = 30):
        """Evolve entry_policy models (dynamic window suggestions) from observed stats.

        Safety principles:
        - Never toggles entry_policy.enabled
        - Only adjusts window buckets under entry_policy.models.dynamic_window.strategy_windows
        - Uses time_bucket_weather_daily_stats (already produced by daily track)

        Returns:
            list[str]: change descriptions
        """

        # Only makes sense if config already has entry_policy block
        entry_policy = Config.STRATEGY.get('entry_policy', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
        models = (entry_policy or {}).get('models') or {}
        dyn = (models or {}).get('dynamic_window') or {}
        strategy_windows = (dyn.get('strategy_windows') or {}) if isinstance(dyn.get('strategy_windows'), dict) else {}
        if not strategy_windows:
            return []

        # Use attack_window_gate.min_samples as default sample requirement
        try:
            min_samples = int((Config.STRATEGY.get('attack_window_gate', {}) or {}).get('min_samples', 30) or 30)
        except Exception:
            min_samples = 30

        # Principal safety floor for tail risk (p5_close_ret)
        try:
            floor = float(((dyn or {}).get('p5_close_ret_floor') if isinstance(dyn, dict) else None) or -0.02)
        except Exception:
            floor = -0.02

        conn = None
        try:
            conn = self._connect_db()
            cutoff = (datetime.now() - timedelta(days=int(window_days or 30))).strftime('%Y-%m-%d')
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT weather, time_bucket,
                           SUM(cnt) AS cnt,
                           SUM(win_cnt) AS win_cnt,
                           AVG(win_rate) AS win_rate,
                           AVG(avg_max_ret) AS avg_max_ret,
                           AVG(avg_close_ret) AS avg_close_ret,
                           AVG(p5_close_ret) AS p5_close_ret
                    FROM time_bucket_weather_daily_stats
                    WHERE date >= %s AND analysis_cycle='T+1'
                    GROUP BY weather, time_bucket
                    """,
                    (cutoff,),
                )
                rows = cursor.fetchall() or []

            if not rows:
                return []

            # Build ranking per weather
            by_weather = {}
            for r in rows:
                w = r.get('weather')
                b = r.get('time_bucket')
                if not w or not b:
                    continue
                try:
                    cnt = int(r.get('cnt', 0) or 0)
                    p5 = float(r.get('p5_close_ret', 0.0) or 0.0)
                except Exception:
                    continue
                if cnt < min_samples:
                    continue
                if p5 < floor:
                    continue
                by_weather.setdefault(w, []).append(r)

            if not by_weather:
                return []

            def _bucket_score(rr: dict) -> float:
                # Conservative: prioritize tail safety then win_rate then avg_max_ret
                try:
                    p5 = float(rr.get('p5_close_ret', 0.0) or 0.0)
                    wr = float(rr.get('win_rate', 0.0) or 0.0)
                    mx = float(rr.get('avg_max_ret', 0.0) or 0.0)
                except Exception:
                    return -999
                return (p5 * 100.0) + (wr * 10.0) + (mx * 5.0)

            changes = []

            # For now: we only propose 1 best bucket per weather, mapped onto each strategy.
            # Strategy windows are per-strategy; we update them uniformly by weather best bucket.
            # (Keeps the rule simple and safe; user can refine later.)
            best_bucket_by_weather = {}
            for w, lst in by_weather.items():
                best = sorted(lst, key=_bucket_score, reverse=True)[0]
                best_bucket_by_weather[w] = best.get('time_bucket')

            # Apply suggestions: if a strategy has windows that include buckets not in best set,
            # we gently bias toward the best bucket but keep at least 1 bucket.
            # Note: we don't know run-time weather at config-level; so we keep strategy windows broad
            # but reorder so that best buckets appear earlier.
            for strat, buckets in list(strategy_windows.items()):
                if not isinstance(buckets, list) or not buckets:
                    continue

                preferred = []
                for w in ('☀️晴天', '☁️多云', '⚠️暴雨'):
                    bb = best_bucket_by_weather.get(w)
                    if bb and bb not in preferred:
                        preferred.append(bb)

                # Merge keeping existing buckets, but preferred first
                new_list = []
                for b in preferred + buckets:
                    if b and b not in new_list:
                        new_list.append(b)

                # Keep list size bounded (avoid exploding buckets)
                new_list = new_list[: max(1, min(4, len(new_list)))]

                if new_list != buckets:
                    strategy_windows[strat] = new_list
                    changes.append(f"入场模型(dynamic_window)窗口建议调整: {strat} {buckets} -> {new_list}")

            # Persist back into Config.STRATEGY (but do not enable)
            try:
                if 'entry_policy' not in Config.STRATEGY or not isinstance(Config.STRATEGY.get('entry_policy'), dict):
                    Config.STRATEGY['entry_policy'] = {}
                if 'models' not in Config.STRATEGY['entry_policy'] or not isinstance(Config.STRATEGY['entry_policy'].get('models'), dict):
                    Config.STRATEGY['entry_policy']['models'] = {}
                if 'dynamic_window' not in Config.STRATEGY['entry_policy']['models'] or not isinstance(Config.STRATEGY['entry_policy']['models'].get('dynamic_window'), dict):
                    Config.STRATEGY['entry_policy']['models']['dynamic_window'] = {}
                Config.STRATEGY['entry_policy']['models']['dynamic_window']['strategy_windows'] = strategy_windows
            except Exception:
                pass

            return changes
        except Exception as e:
            logger.warning(f"Entry policy evolution skipped: {e}")
            return []
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _check_promotion(self):
        """[V10.8] 自动晋级检查"""
        try:
            from core.portfolio import PortfolioManager
            portfolio = PortfolioManager()
            total_asset = portfolio.get_total_value()
            if not total_asset or total_asset <= 0: return None
            curr_boards = Config.get_allowed_boards()
            msg = None
            if total_asset >= 500000 and 'gem' not in curr_boards:
                curr_boards.append('gem')
                msg = f"🎉 资产达到50万，晋级创业板！开放板块: {curr_boards}"
            elif total_asset >= 1000000 and 'star' not in curr_boards:
                curr_boards.append('star')
                msg = f"🎉 资产达到100万，晋级科创板！开放板块: {curr_boards}"
            if msg:
                if 'market_permission' not in Config.STRATEGY: Config.STRATEGY['market_permission'] = {}
                Config.STRATEGY['market_permission']['current_boards'] = curr_boards
            return msg
        except Exception:
            return None

    def _tune_weight(self, section, key, increment):
        if section in Config.STRATEGY and key in Config.STRATEGY[section]:
            old_val = Config.STRATEGY[section][key]
            new_val = max(1, min(150, old_val + increment))
            Config.STRATEGY[section][key] = new_val
            logger.info(f"Tuning {section}.{key}: {old_val} -> {new_val}")

    def _save_config(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(Config.STRATEGY, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    evolver = StrategyEvolver()
    evolver.evolve()
