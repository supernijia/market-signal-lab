# -*- coding: utf-8 -*-
"""
Core Analysis Logic
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
from core.combo_signal_adapter import FirstLimitBreakoutComboAdapter
from core.config import Config
from core.tech_analyzer import TechAnalyzer

logger = logging.getLogger("StockAnalyzer.Analyzer")

class StockAnalyzer:
    def __init__(self, data_provider):
        self.provider = data_provider
        self.index_cache = {} # [V20.1] Cache for index returns (SH/SZ)
        self.combo_adapter = FirstLimitBreakoutComboAdapter(data_provider)
        self.last_data_quality = {}

    def _get_prev_trade_date(self, trade_date=None):
        """Get previous open trading day in YYYYMMDD format."""
        try:
            if trade_date:
                td = str(trade_date).replace('-', '')
            else:
                td = self.provider._get_latest_trade_date()
            if len(td) != 8 or not td.isdigit():
                td = datetime.now().strftime('%Y%m%d')

            start = (datetime.strptime(td, '%Y%m%d') - timedelta(days=20)).strftime('%Y%m%d')
            cal = self.provider.get_trade_cal(start, td)
            open_days = [d['cal_date'] for d in cal if d.get('is_open') == 1]
            open_days.sort()
            if td in open_days:
                idx = open_days.index(td)
                if idx >= 1:
                    return open_days[idx - 1]
            if len(open_days) >= 2:
                return open_days[-2]
            if open_days:
                return open_days[-1]
        except Exception as e:
            logger.debug(f"Prev trade date fallback failed: {e}")
        return None

    def _build_prev_limit_context(self, trade_date=None):
        """Build previous-day limit context keyed by ts_code."""
        prev_trade_date = self._get_prev_trade_date(trade_date)
        if not prev_trade_date:
            return {}
        try:
            prev_limits = self.provider.get_stk_limit(prev_trade_date)
            if isinstance(prev_limits, dict):
                return prev_limits
        except Exception as e:
            logger.debug(f"Prev limit context fetch failed: {e}")
        return {}

    @staticmethod
    def _attach_board_context_item(item, prev_limit_map):
        """Attach first-board vs continue-board context onto a candidate dict."""
        if not isinstance(item, dict):
            return item
        ts_code = item.get('ts_code') or ''
        prev_rec = prev_limit_map.get(ts_code, {}) if isinstance(prev_limit_map, dict) else {}
        prev_limit_times = int(prev_rec.get('limit_times', 0) or 0)
        prev_limit_present = prev_limit_times > 0
        is_first_board_candidate = not prev_limit_present
        item['prev_limit_times'] = prev_limit_times
        item['prev_limit_present'] = bool(prev_limit_present)
        item['is_first_board_candidate'] = bool(is_first_board_candidate)
        item['is_continue_board_candidate'] = bool(prev_limit_present)
        item['board_context'] = 'first_board' if is_first_board_candidate else 'continue_board'
        return item

    def _attach_concepts(self, items, *, max_items=30):
        """Attach concepts list onto candidate dicts (best-effort, additive).

        Adds:
          - concepts: list[str]
          - concept_top: str (first concept)
        """
        try:
            if not items:
                return

            # Limit to avoid concept_detail rate limits (cache helps, but keep bounded)
            for it in (items or [])[: int(max_items or 0)]:
                try:
                    ts = it.get('ts_code')
                    if not ts:
                        code = it.get('code')
                        if code:
                            ts = f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ"
                    if not ts:
                        continue

                    concepts = self.provider.get_concept_detail(ts) or []
                    if concepts is None:
                        concepts = []

                    # Normalize to list[str]
                    if isinstance(concepts, str):
                        concepts = [concepts]
                    elif not isinstance(concepts, list):
                        concepts = []

                    it['concepts'] = concepts
                    if concepts:
                        it['concept_top'] = concepts[0]
                except Exception:
                    continue
        except Exception:
            return

    def _repair_realtime_quote(self, ts_code, quote, daily_map=None, basics=None):
        """Best-effort fill for realtime rows coming from old cache/fallbacks."""
        if not isinstance(quote, dict):
            return quote
        fixed = dict(quote)
        try:
            price = float(fixed.get('price') or fixed.get('close') or 0)
            pre_close = float(fixed.get('pre_close') or 0)
        except Exception:
            price = 0.0
            pre_close = 0.0

        daily_row = (daily_map or {}).get(ts_code) or {}
        if pre_close <= 0 and daily_row:
            try:
                pre_close = float(daily_row.get('pre_close') or daily_row.get('close') or 0)
                if pre_close > 0:
                    fixed['pre_close'] = pre_close
            except Exception:
                pass

        if not fixed.get('name'):
            info = (basics or {}).get(ts_code) or daily_row
            fixed['name'] = info.get('name') or fixed.get('name')

        if price > 0 and pre_close > 0:
            fixed['pct_chg'] = (price - pre_close) / pre_close * 100
            fixed['change'] = price - pre_close
        return fixed

    @staticmethod
    def _build_daily_map(daily_data):
        return {
            row.get('ts_code'): row
            for row in (daily_data or [])
            if isinstance(row, dict) and row.get('ts_code')
        }

    def _compute_concept_heat(self, items, *, top_n=12):
        """Compute concept heat from attached concepts (best-effort)."""
        heat = {}
        total = 0
        for it in items or []:
            concepts = it.get('concepts') or []
            if not concepts:
                continue
            total += 1
            # count up to first 8 concepts per stock to avoid long tails
            for c in (concepts[:8] if isinstance(concepts, list) else []):
                if not c:
                    continue
                heat[c] = heat.get(c, 0) + 1

        top = sorted(heat.items(), key=lambda x: x[1], reverse=True)[: int(top_n or 0)]
        top_list = [{'concept': k, 'count': int(v)} for k, v in top]

        return {
            'total_candidates': int(total),
            'heat': heat,
            'top': top_list,
        }

    def _attach_combo_observe(self, items, trade_date=None, rt_data=None):
        """Attach observe-mode combo model fields to first-board candidates."""
        if not items:
            return
        rt_data = rt_data if isinstance(rt_data, dict) else {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                ts_code = item.get('ts_code')
                qt = rt_data.get(ts_code) if ts_code else None
                combo_fields = self.combo_adapter.annotate_candidate(item, trade_date=trade_date, qt=qt)
                if isinstance(combo_fields, dict) and combo_fields:
                    item.update(combo_fields)
            except Exception as e:
                logger.debug(f"combo observe candidate annotate failed for {item.get('code')}: {e}")

    def _attach_lhb_tags(self, items, trade_date=None):
        """Attach 龙虎榜 quick fields onto candidate dicts (best-effort, additive)."""
        try:
            lhb = self.provider.get_top_list(trade_date)
            lhb_map = {}
            for r in lhb or []:
                ts = r.get('ts_code')
                if ts:
                    lhb_map[ts] = r

            for it in items or []:
                ts = it.get('ts_code')
                if not ts:
                    continue
                row = lhb_map.get(ts)
                if row:
                    it['lhb_present_today'] = True
                    it['lhb_reason'] = row.get('reason')
                    it['lhb_net_amount'] = row.get('net_amount')
        except Exception:
            return

    # [V10] 负面过滤器 - 历史亏损归因
    NEGATIVE_FILTERS = {
        # 规律1: 中涨幅(4-8%) + 中换手(5-12%) = "中庸陷阱" - 胜率仅42.9%
        "mid_chg_mid_to": {
            "min_change": 4.0,
            "max_change": 8.0,
            "min_turnover": 5.0,
            "max_turnover": 12.0,
            "action": "HARD_REJECT",
            "reason": "中涨幅+中换手组合胜率仅42.9%（中庸陷阱）"
        },
        # 规律2: 高换手(>15%) = "高位换手诱多" - 胜率仅23.3%
        "high_turnover": {
            "min_turnover": 15.0,
            "action": "HARD_REJECT",
            "reason": "高换手(>15%)诱多陷阱，胜率仅23.3%"
        },
        # 规律3: 暴雨天 + 高换手 = 双重风险
        "storm_risk": {
            "weather": "⚠️暴雨",
            "min_turnover": 5.0,
            "action": "HARD_REJECT",
            "reason": "暴雨天高换手=双重风险"
        },
        # [V11] 散户陷阱: 股东人数暴增>10%
        "shareholder_trap": {
            "max_increase_rate": 10.0,
            "action": "HARD_REJECT",
            "reason": "股东人数暴增>10%，散户踩踏陷阱"
        }
    }

    def apply_negative_filters(self, candidates, market_weather='☀️晴天'):
        """
        [V10] 应用负面过滤器 - 基于历史亏损模式的一票否决
        
        Args:
            candidates: 候选股票列表
            market_weather: 当前市场天气
        
        Returns:
            filtered_candidates: 通过过滤的股票
            rejected_info: 被拒绝的股票及原因
        """
        if not candidates:
            return [], []
        
        filtered = []
        rejected = []
        
        for stock in candidates:
            rejected_reason = None
            
            # Filter 1: Mid Change + Mid Turnover (中庸陷阱)
            nf1 = self.NEGATIVE_FILTERS["mid_chg_mid_to"]
            change = stock.get('change', 0) or stock.get('open_change', 0) or 0
            turnover = stock.get('turnover', 0) or 0
            
            if (nf1["min_change"] <= change <= nf1["max_change"] and 
                nf1["min_turnover"] <= turnover <= nf1["max_turnover"]):
                rejected_reason = nf1["reason"]
            
            # Filter 2: High Turnover (诱多陷阱)
            elif turnover > self.NEGATIVE_FILTERS["high_turnover"]["min_turnover"]:
                rejected_reason = self.NEGATIVE_FILTERS["high_turnover"]["reason"]
            
            # Filter 3: Storm + High Turnover (双重风险)
            elif (market_weather == '⚠️暴雨' and 
                  turnover > self.NEGATIVE_FILTERS["storm_risk"]["min_turnover"]):
                rejected_reason = self.NEGATIVE_FILTERS["storm_risk"]["reason"]
            
            if rejected_reason:
                rejected.append({
                    'code': stock.get('code', ''),
                    'name': stock.get('name', ''),
                    'reason': rejected_reason,
                    'change': change,
                    'turnover': turnover
                })
            else:
                filtered.append(stock)
        
        if rejected:
            logger.warning(f"🚫 负面过滤器拦截了 {len(rejected)} 只股票:")
            for r in rejected[:5]:  # Log first 5
                logger.warning(f"   {r['name']}({r['code']}): {r['reason']}")
        
        return filtered, rejected

    # [V10.8] 主板权限锁定 - 股票代码判定正则
    # 注意: 检查顺序重要 - STAR(688/689) > GEM(300/301) > MAIN
    # 主板: 000xxx, 001xxx, 600xxx, 601xxx, 603xxx, 605xxx (上海/深圳主板)
    # 创业板: 300xxx, 301xxx (深圳创业板)
    # 科创板: 688xxx, 689xxx (上海科创板)
    BOARD_PATTERNS = {
        'star': r'^(688|689)[0-9]{3}$',
        'gem': r'^(300|301)[0-9]{3}$',
        'main': r'^(000|001|600|601|603|605)[0-9]{3}$',
    }

    def apply_board_filter(self, candidates, allowed_boards=None):
        """
        [V10.8] 主板权限过滤器
        
        根据配置只允许交易特定板块的股票
        
        Args:
            candidates: 候选股票列表
            allowed_boards: 允许的板块列表 ['main', 'gem', 'star']
        
        Returns:
            filtered_candidates: 通过过滤的股票
            rejected_info: 被拒绝的股票及原因
        """
        import re
        
        if allowed_boards is None:
            allowed_boards = ['main']  # 默认仅主板
        
        if not candidates:
            return [], []
        
        filtered = []
        rejected = []
        
        for stock in candidates:
            code = stock.get('code', '')
            if not code:
                filtered.append(stock)
                continue
            
            # Determine board type
            board_type = None
            for board, pattern in self.BOARD_PATTERNS.items():
                if re.match(pattern, code):
                    board_type = board
                    break
            
            # Check if board is allowed
            if board_type and board_type not in allowed_boards:
                rejected.append({
                    'code': code,
                    'name': stock.get('name', ''),
                    'reason': f'{board_type.upper()}板块未开放 (仅{allowed_boards})',
                    'board': board_type
                })
            else:
                stock['board'] = board_type or 'unknown'
                filtered.append(stock)
        
        if rejected:
            logger.warning(f"🚫 主板权限过滤拦截了 {len(rejected)} 只股票:")
            for r in rejected[:5]:
                logger.warning(f"   {r['name']}({r['code']}): {r['reason']}")
        
        return filtered, rejected

    def calculate_volume_ratio(self, ts_code, current_vol_lots, history=None):
        """
        [V18] 计算实时量比 (Volume Ratio)
        量比 = (当前成交量 / 当前交易分钟数) / (过去5日平均每分钟成交量)
        
        Args:
            ts_code: 股票代码
            current_vol_lots: 当前今日总成交量 (手)
            history: optional daily records, sorted ascending, to avoid duplicate API calls
        """
        try:
            # 1. 确定今日已交易分钟数
            now = datetime.now()
            # 交易时间段: 09:30-11:30 (120 min), 13:00-15:00 (120 min)
            if now.hour < 9 or (now.hour == 9 and now.minute < 30):
                minutes_elapsed = 1 # 最小1分钟，防止除以0
            elif now.hour < 12:
                # 09:30 - 11:30
                minutes_elapsed = (now.hour - 9) * 60 + now.minute - 30
                minutes_elapsed = min(120, max(1, minutes_elapsed))
            elif now.hour < 13:
                minutes_elapsed = 120 # 中午休市按120分钟算
            else:
                # 13:00 - 15:00
                minutes_elapsed = 120 + (now.hour - 13) * 60 + now.minute
                minutes_elapsed = min(240, max(121, minutes_elapsed))
            
            # 2. 获取过去5日平均成交量
            # Tushare daily vol is in 'lots' (100 shares)
            if not history:
                history = self.provider.get_history_data(ts_code, count=5)
            if not history or len(history) < 5:
                return 1.0 # 数据不足返回 1.0 中性值
            
            avg_daily_vol = sum(float(day['vol']) for day in history) / len(history)
            avg_vol_per_min = avg_daily_vol / 240.0
            
            if avg_vol_per_min <= 0: return 1.0
            
            # 3. 计算量比
            current_vol_per_min = current_vol_lots / minutes_elapsed
            volume_ratio = current_vol_per_min / avg_vol_per_min
            
            return volume_ratio
        except Exception as e:
            logger.warning(f"Failed to calculate volume ratio for {ts_code}: {e}")
            return 1.0

    def verify_money_flow(self, stock_data, weather='☀️晴天'):
        """
        [V18] 综合资金验证 (Buy Confirmation)
        
        验证维度:
        1. 即时量比 (Volume Ratio) > 2.0
        2. 价格偏离度 (Price / VWAP) < 1.03 (拦截追高)
        3. 涨幅上限 (Change) < 7.0% (拦截封板前夕，除非是大单封板策略)
        
        Returns:
            bool: 是否通过验证
            str: 拒绝原因
        """
        ts_code = stock_data.get('ts_code') or f"{stock_data['code']}.SH" # Simple guess for fallback
        price = float(stock_data.get('price', 0))
        change = float(stock_data.get('change', 0) or stock_data.get('open_change', 0) or 0)
        
        # 1. 涨幅硬拦截（legacy）
        max_chg = Config.RISK_MANAGEMENT.get("MAX_BUY_CHANGE", 7.0)
        if change > max_chg:
            return False, f"涨幅拦截: 当前 {change:.1f}% > 门槛 {max_chg}% (防追高)"

        # 1.1 涨停/临近涨停/盘中摸板禁买（更贴近真实成交）
        # - near-limit: 当前涨幅接近涨停
        # - touch-limit: 盘中最高价触及/接近涨停（摸板/开板也禁买）
        try:
            cfg = Config.STRATEGY.get('limit_up_gate', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
            if bool((cfg or {}).get('enabled', True)):
                code = str(stock_data.get('code') or '').strip()
                board = (stock_data.get('board') or '').strip() or None

                # Best-effort board inference if not provided by apply_board_filter
                if not board and code:
                    try:
                        import re
                        for b, pattern in self.BOARD_PATTERNS.items():
                            if re.match(pattern, code):
                                board = b
                                break
                    except Exception:
                        board = None

                # ST stocks: 5% limit (best-effort name-based)
                name = str(stock_data.get('name') or '')
                is_st = ('ST' in name.upper()) if name else False

                board_limit_pct = (cfg or {}).get('board_limit_pct', {}) if isinstance((cfg or {}).get('board_limit_pct', {}), dict) else {}
                fallback_limit_pct = float((cfg or {}).get('fallback_limit_pct', 10) or 10)
                limit_pct = float(board_limit_pct.get(board, fallback_limit_pct) if board else fallback_limit_pct)
                if is_st:
                    limit_pct = float(board_limit_pct.get('st', 5) if isinstance(board_limit_pct, dict) else 5)

                near_buf = float((cfg or {}).get('near_limit_buffer_pct', 0.5) or 0.5)
                touch_buf = float((cfg or {}).get('touch_limit_buffer_pct', 0.2) or 0.2)

                # near-limit by current change
                if change >= (limit_pct - near_buf):
                    return False, f"涨停禁买: 临近涨停 (chg={change:.2f}% >= {limit_pct-near_buf:.2f}%, limit={limit_pct:.0f}%, board={board or 'unknown'})"

                # touch-limit by intraday high change
                high = float(stock_data.get('high', 0) or 0)
                pre_close = float(stock_data.get('pre_close', 0) or 0)
                if high > 0 and pre_close > 0:
                    high_chg = (high - pre_close) / pre_close * 100
                    if high_chg >= (limit_pct - touch_buf):
                        return False, f"涨停禁买: 盘中摸板/触板 (high_chg={high_chg:.2f}% >= {limit_pct-touch_buf:.2f}%, limit={limit_pct:.0f}%, board={board or 'unknown'})"
        except Exception as e:
            logger.debug(f"limit_up_gate check failed: {e}")

        # 2. 量比验证 (需要实时数据中的成交量)
        current_vol_lots = float(stock_data.get('vol_lots', stock_data.get('vol', 0)) or 0)
        current_vol_shares = float(stock_data.get('vol_shares', current_vol_lots * 100) or 0)
        if current_vol_lots > 0:
            vr = self.calculate_volume_ratio(ts_code, current_vol_lots)
            min_vr = Config.RISK_MANAGEMENT.get("MIN_VOLUME_RATIO", 2.0)
            if vr < min_vr:
                return False, f"量比不足: 当前量比 {vr:.2f} < 门槛 {min_vr} (无量拉升)"
            stock_data['volume_ratio'] = vr
            
        # 3. 分时均价 (VWAP) 验证
        amount_yuan = float(stock_data.get('amount_yuan', stock_data.get('amount', 0)) or 0)
        if amount_yuan > 0 and current_vol_shares > 0:
            # VWAP = total amount (yuan) / total volume (shares)
            vwap = float(stock_data.get('vwap', 0) or 0)
            if vwap <= 0:
                vwap = amount_yuan / current_vol_shares

            # Always attach vwap for downstream factor snapshot tags
            stock_data['vwap'] = vwap

            # Base guard (legacy): fixed 3%
            vwap_cfg = Config.STRATEGY.get('intraday_structure', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
            enabled = bool(vwap_cfg.get('enabled', False))
            max_ratio = float(vwap_cfg.get('max_price_vwap_ratio', 1.03) or 1.03)

            # If enabled, use configurable ratio; otherwise keep old behavior
            threshold = max_ratio if enabled else 1.03
            if vwap > 0 and price > vwap * threshold:
                return False, f"均价偏离: 现价 {price:.2f} > 均价 {vwap:.2f} 超过{(threshold-1)*100:.0f}% (分时追多风险)"

        return True, "验证通过"

    def verify_cold_start_entry(self, stock_data, time_bucket=None):
        """
        冷启动买入验证
        
        根据配置的分层策略进行验证:
        - 开盘急拉型 (aggressive): 高开>5%，严格VWAP保护
        - 震荡上行型 (conservative): 高开2-5%，午后确认
        
        Args:
            stock_data: 股票数据
            time_bucket: 当前时间窗口 (B1/B2/B3/B4)
        
        Returns:
            bool: 是否通过验证
            str: 拒绝原因
        """
        cs_cfg = Config.STRATEGY.get('cold_start_entry', {})
        if not cs_cfg.get('enabled', False):
            return True, "冷启动验证未启用"
        
        open_change = float(stock_data.get('open_change', 0) or stock_data.get('change', 0) or 0)
        
        # 通用风控检查
        common = cs_cfg.get('common', {})
        
        # 1. 资金流强度检查
        mf_intensity = float(stock_data.get('mf_intensity', 0) or 0)
        min_mf = common.get('min_mf_intensity_yi', 1.0)
        if mf_intensity < min_mf:
            return False, f"资金流不足: {mf_intensity:.1f}亿 < {min_mf}亿"
        
        # 2. 5日涨幅检查
        gain_5d = float(stock_data.get('gain_5d', 0) or 0)
        max_5d = common.get('max_5d_gain', 15.0)
        if gain_5d > max_5d:
            return False, f"5日涨幅过大: {gain_5d:.1f}% > {max_5d}%"
        
        # 3. 换手率检查
        turnover = float(stock_data.get('turnover', 0) or 0)
        max_to = common.get('max_turnover', 8.0)
        if turnover > max_to:
            return False, f"换手率过高: {turnover:.1f}% > {max_to}%"
        
        # 4. 昨日涨幅检查
        prev_change = float(stock_data.get('prev_change', 0) or 0)
        min_prev = common.get('min_prev_change', -5.0)
        max_prev = common.get('max_prev_change', 3.0)
        if not (min_prev <= prev_change <= max_prev):
            return False, f"昨日涨幅不符: {prev_change:.1f}% (需{min_prev}~{max_prev}%)"
        
        # 分层策略判断
        aggressive_cfg = cs_cfg.get('aggressive', {})
        conservative_cfg = cs_cfg.get('conservative', {})
        
        # 开盘急拉型: 高开 >= 5%
        if open_change >= aggressive_cfg.get('min_open_change', 5.0):
            max_vwap_ratio = aggressive_cfg.get('max_price_vwap_ratio', 1.03)
            max_chg_tol = aggressive_cfg.get('max_change_tolerance', 0.02)
            
            # 检查VWAP偏离
            vwap = float(stock_data.get('vwap', 0) or 0)
            price = float(stock_data.get('price', 0))
            if vwap > 0 and price > vwap * max_vwap_ratio:
                return False, f"急拉型VWAP偏离: 现价 {price:.2f} > VWAP {vwap:.2f} {(max_vwap_ratio-1)*100:.0f}%"
            
            # 检查涨幅容忍度 (不超过开盘涨幅+2%)
            change = float(stock_data.get('change', 0) or open_change)
            if change > open_change + max_chg_tol * 100:
                return False, f"急拉型涨幅超限: {change:.1f}% > 开盘{open_change:.1f}%+2%"
            
            return True, f"冷启动-急拉型: 高开{open_change:.1f}%"
        
        # 震荡上行型: 2% <= 高开 < 5%
        elif conservative_cfg.get('min_open_change', 2.0) <= open_change < aggressive_cfg.get('min_open_change', 5.0):
            # 需要午后确认 (B3或之后)
            if conservative_cfg.get('require_bucket_B3', True):
                if time_bucket not in ['B3', 'B4', 'B5']:
                    return False, f"震荡型需午后确认: 当前{time_bucket} < B3"
            
            max_vwap_ratio = conservative_cfg.get('max_price_vwap_ratio', 1.02)
            max_chg_tol = conservative_cfg.get('max_change_tolerance', 0.01)
            
            # 检查VWAP偏离 (更严格)
            vwap = float(stock_data.get('vwap', 0) or 0)
            price = float(stock_data.get('price', 0))
            if vwap > 0 and price > vwap * max_vwap_ratio:
                return False, f"震荡型VWAP偏离: 现价 {price:.2f} > VWAP {vwap:.2f} {(max_vwap_ratio-1)*100:.0f}%"
            
            # 检查涨幅 (更严格)
            change = float(stock_data.get('change', 0) or open_change)
            if change > open_change + max_chg_tol * 100:
                return False, f"震荡型涨幅超限: {change:.1f}% > 开盘{open_change:.1f}%+1%"
            
            return True, f"冷启动-震荡型: 高开{open_change:.1f}%，午后确认"
        
        # 不符合分层条件
        return False, f"冷启动分层不符: 高开{open_change:.1f}% (需>=2%或<5%)"

    # [V10] 板块热度共振参数
    SECTOR_BONUS = {
        "hot_rank_bonus": 15,      # 行业涨幅前3 +15分
        "cold_penalty": -10,        # 行业下跌<-1% 扣10分
        "mf_inflow_bonus": 5,      # 行业资金净流入 +5分
    }

    def get_sector_strength(self, industry, industry_stats):
        """
        [V10] 评估个股所在行业的热度
        
        Args:
            industry: 行业名称
            industry_stats: 行业统计数据列表
        
        Returns:
            dict: {'sector_score': int, 'is_hot': bool, 'is_cold': bool, 'rank': int, 'avg_change': float}
        """
        if not industry_stats:
            return {'sector_score': 0, 'is_hot': False, 'is_cold': False, 'rank': 0, 'avg_change': 0}
        
        # Find industry rank
        for rank, stats in enumerate(industry_stats, 1):
            if stats['industry'] == industry:
                avg_change = stats['avg_change']
                net_mf = stats.get('net_money_flow', 0)
                
                # Hot sector: top 3 by change
                is_hot = rank <= 3
                
                # Cold sector: avg change < -1%
                is_cold = avg_change < -1.0
                
                # Calculate bonus/penalty
                sector_score = 0
                if is_hot:
                    sector_score += self.SECTOR_BONUS["hot_rank_bonus"]
                elif is_cold:
                    sector_score += self.SECTOR_BONUS["cold_penalty"]
                
                # Money flow bonus
                if net_mf > 0:
                    sector_score += self.SECTOR_BONUS["mf_inflow_bonus"]
                
                return {
                    'sector_score': sector_score,
                    'is_hot': is_hot,
                    'is_cold': is_cold,
                    'rank': rank,
                    'avg_change': avg_change,
                    'net_money_flow': net_mf
                }
        
        return {'sector_score': 0, 'is_hot': False, 'is_cold': False, 'rank': 0, 'avg_change': 0}

    def _calculate_macd(self, df, fast=12, slow=26, signal=9):
        """Calculate MACD indicators"""
        # EMA12
        ema12 = df['close'].ewm(span=fast, adjust=False).mean()
        # EMA26
        ema26 = df['close'].ewm(span=slow, adjust=False).mean()
        # DIF
        dif = ema12 - ema26
        # DEA
        dea = dif.ewm(span=signal, adjust=False).mean()
        # MACD (Bar)
        macd = (dif - dea) * 2
        return dif, dea, macd

    def _check_macd(self, ts_code, history=None):
        """
        Check if stock has bullish MACD pattern.
        Returns True if MACD is bullish (DIF > DEA or histogram turning positive).
        """
        try:
            if history is None:
                history = self.provider.get_history_data(ts_code, count=60)
            
            if len(history) < 30:
                return True  # Not enough data, skip filter
            
            df = pd.DataFrame(history)
            dif, dea, macd = self._calculate_macd(df)
            
            # Bullish signals:
            # 1. DIF > DEA (MACD above signal line)
            # 2. Recent MACD histogram turning positive
            latest = dif.iloc[-1]
            prev = dif.iloc[-2] if len(dif) > 1 else latest
            
            dea_latest = dea.iloc[-1]
            
            # Main condition: DIF > DEA
            if latest > dea_latest:
                return True
            
            # Secondary: MACD histogram turning up (even if negative)
            if macd.iloc[-1] > macd.iloc[-2] if len(macd) > 1 else False:
                return True
            
            return False
            
        except Exception as e:
            logger.debug(f"MACD check failed for {ts_code}: {e}")
            return True  # On error, don't filter out

    def check_trend_strength(self, ts_code, history=None, adx_threshold=25):
        """
        [V20] Multi-level Trend Strength Audit
        Ref: OpenClaw ADX Scheme
        - < 20: Weak (Reject)
        - 20-25: Forming (Reject/Caution)
        - 25-40: Strong Up/Down (Target)
        - > 40: Overheated (Reject/Reversal risk)
        """
        try:
            if history is None:
                history = self.provider.get_history_data(ts_code, count=60)
            
            if len(history) < 30:
                return {'adx': 0, 'trend': 'unknown', 'can_buy': False, 'reason': '数据不足'}
            
            df = pd.DataFrame(history)
            df = TechAnalyzer.calculate_adx(df)
            
            if df is None or 'ADX' not in df.columns:
                return {'adx': 0, 'trend': 'unknown', 'can_buy': False, 'reason': 'ADX计算失败'}
            
            latest = df.iloc[-1]
            adx = latest['ADX']
            plus_di = latest['plus_DI']
            minus_di = latest['minus_DI']
            
            if pd.isna(adx):
                return {'adx': 0, 'trend': 'unknown', 'can_buy': False, 'reason': 'ADX数据缺失'}
            
            # Judgment Logic (V20)
            if adx < 20:
                trend = 'weak'
                can_buy = False
                reason = f"ADX={adx:.1f}<20，趋势弱，震荡市场"
            elif adx < 25:
                trend = 'forming'
                can_buy = False 
                reason = f"ADX={adx:.1f}<25，趋势正在形成，观望"
            elif adx >= 25 and adx <= 40:
                if plus_di > minus_di:
                    trend = 'strong_up'
                    can_buy = True
                    reason = f"ADX={adx:.1f}，+DI升穿，上升趋势明确"
                else:
                    trend = 'strong_down'
                    can_buy = False
                    reason = f"ADX={adx:.1f}，-DI占优，下降趋势明确"
            else: # adx > 40
                trend = 'overbought'
                can_buy = False
                reason = f"ADX={adx:.1f}>40，趋势过强，防范反转"
            
            return {
                'adx': round(adx, 1),
                'trend': trend,
                'can_buy': can_buy,
                'reason': reason
            }
        except Exception as e:
            logger.error(f"Trend strength check failed {ts_code}: {e}")
            return {'adx': 0, 'trend': 'error', 'can_buy': False, 'reason': str(e)}

    def _prepare_index_cache(self, trade_date=None, lookback=5):
        """
        [V20.1] Pre-fetch and cache index returns for RS comparison.
        Reduces API calls from N to 2 per analysis run.
        """
        try:
            indices = {
                'SH': '000001.SH',
                'SZ': '399006.SZ'
            }
            self.index_cache = {}
            for key, code in indices.items():
                hist = self.provider.get_index_daily(code, trade_date, count=lookback+1)
                if hist and len(hist) >= 2:
                    # Calculate return: (latest - first) / first
                    # Tushare index data is usually DESC sorted in get_index_daily? 
                    # check data_provider search or view. 
                    # In data_provider.get_index_daily (not shown but usually desc)
                    # Let's assume the provider returns a LIST.
                    try:
                        first = float(hist[0]['close'])
                        latest = float(hist[-1]['close'])
                        self.index_cache[key] = (latest - first) / first
                    except (ValueError, KeyError, IndexError):
                        self.index_cache[key] = 0.0
                else:
                    self.index_cache[key] = 0.0
            logger.info(f"🚀 [V20.1] Index Cache Prepared: SH={self.index_cache.get('SH',0)*100:.2f}%, SZ={self.index_cache.get('SZ',0)*100:.2f}%")
        except Exception as e:
            logger.error(f"Failed to prepare index cache: {e}")

    def _check_rs(self, ts_code, trade_date=None, lookback=5, stock_history=None):
        """
        [V20.1] Refactored RS check.
        Uses cached index returns and provided stock history to avoid API calls.
        Returns True if stock outruns index.
        """
        try:
            # 1. Determine index type
            idx_type = 'SH' if ts_code.startswith('6') else 'SZ'
            i_ret = self.index_cache.get(idx_type)
            
            # If cache missing, fallback to single fetch (legacy/safety)
            if i_ret is None:
                index_code = '000001.SH' if idx_type == 'SH' else '399006.SZ'
                index_hist = self.provider.get_index_daily(index_code, trade_date, count=lookback+1)
                if index_hist and len(index_hist) >= 2:
                    i_ret = (float(index_hist[-1]['close']) - float(index_hist[0]['close'])) / float(index_hist[0]['close'])
                else:
                    i_ret = 0.0

            # 2. Get stock return
            if stock_history is None:
                stock_history = self.provider.get_history_data(ts_code, trade_date, count=lookback+1)
            
            if not stock_history or len(stock_history) < 2:
                return True # Default to True
            
            # Use only the last 'lookback+1' records if history is longer (e.g. 60 days)
            s_hist = stock_history[-(lookback+1):] if len(stock_history) > (lookback+1) else stock_history
            
            s_first = float(s_hist[0]['close'])
            s_latest = float(s_hist[-1]['close'])
            s_ret = (s_latest - s_first) / s_first if s_first > 0 else 0
            
            return s_ret > i_ret
        except Exception as e:
            logger.error(f"RS check error for {ts_code}: {e}")
            return True

    def check_market_environment(self, trade_date=None):
        """
        [V10] 市场环境感知系统 (Market Regime Detection)
        
        判定基准：
        - 夏季 (晴天): 指数 > MA20 且 无暴跌
        - 冬季 (暴雨): 指数 < MA20 或 单日跌幅 > 2%
        
        Returns: 
            dict: {
                'weather': '☀️晴天'/'☁️多云'/'⚠️暴雨',
                'is_safe': True/False,
                'message': str,
                'sh_index': {'close': float, 'ma20': float, 'pct_chg': float},
                'cy_index': {'close': float, 'ma20': float, 'pct_chg': float},
                'risk_level': 'low'/'medium'/'high',
                'adjustments': {
                    'max_position_mult': 0.5,  # 仓位减半
                    'score_threshold_mult': 1.1  # 门槛提高10%
                }
            }
        """
        try:
            # 获取上证指数和创业板指数据
            indices = {
                'sh': {'code': '000001.SH', 'name': '上证指数'},
                'cy': {'code': '399006.SZ', 'name': '创业板指'}
            }
            
            results = {}
            risk_signals = []
            
            for key, info in indices.items():
                history = self.provider.get_index_daily(info['code'], count=30)
                
                if not history or len(history) < 20:
                    results[key] = {'status': 'data_insufficient', 'close': 0, 'ma20': 0, 'pct_chg': 0}
                    continue
                
                df = pd.DataFrame(history)
                df['close'] = df['close'].astype(float)
                df['pct_chg'] = df['pct_chg'].astype(float)
                
                # 计算 MA20
                df['ma20'] = df['close'].rolling(window=20).mean()
                
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                
                close = latest['close']
                ma20 = latest['ma20']
                pct_chg = latest['pct_chg']
                
                results[key] = {
                    'status': 'ok',
                    'close': close,
                    'ma20': ma20,
                    'pct_chg': pct_chg,
                    'below_ma20': close < ma20 if not pd.isna(ma20) else False,
                    'crash': pct_chg < -2.0  # 断头铡刀
                }
                
                # 风险信号收集
                if not pd.isna(ma20) and close < ma20:
                    risk_signals.append(f"{info['name']}跌破MA20")
                if pct_chg < -2.0:
                    risk_signals.append(f"{info['name']}暴跌{pct_chg:.1f}%")
            
            # 综合判定
            sh = results.get('sh', {})
            cy = results.get('cy', {})
            
            # [Phase 2] 短线真实情绪温度计 (跌停比涨停多则直接暴雨)
            limit_up_count = 0
            limit_down_count = 0
            try:
                # Get the daily market data for the target date to calculate limit ups/downs
                df_daily = pd.DataFrame(self.provider.get_daily_data(trade_date))
                if not df_daily.empty and 'pct_chg' in df_daily.columns:
                    df_daily['pct_chg'] = df_daily['pct_chg'].astype(float)
                    limit_up_count = len(df_daily[df_daily['pct_chg'] >= 9.8])
                    limit_down_count = len(df_daily[df_daily['pct_chg'] <= -9.8])
                    
                    if limit_down_count > (limit_up_count * 1.5) and limit_down_count > 50:
                        risk_signals.append(f"短线退潮 (跌停{limit_down_count}家 远超 涨停{limit_up_count}家)")
                    elif limit_down_count > 100:
                        risk_signals.append(f"千股跌停惨案 ({limit_down_count}家跌停)")
            except Exception as e:
                logger.warning(f"Error calculating market sentiment: {e}")
            
            # 判定逻辑
            is_below_ma20 = sh.get('below_ma20', False) or cy.get('below_ma20', False)
            is_crashed = sh.get('crash', False) or cy.get('crash', False)
            sentiment_crashed = limit_down_count > 100 or (limit_down_count > (limit_up_count * 1.5) and limit_down_count > 50)
            
            if is_crashed or sentiment_crashed:
                # 极端暴跌 或 短线极端退潮 = 冬季/暴雨
                weather = "⚠️暴雨"
                is_safe = False
                risk_level = "high"
                adjustments = {'max_position_mult': 0.3, 'score_threshold_mult': 1.2}
                msg = f"极端警报! {'; '.join(risk_signals)}"
            elif is_below_ma20:
                # 跌破MA20 = 冬季/多云
                weather = "☁️多云"
                is_safe = False
                risk_level = "medium"
                adjustments = {'max_position_mult': 0.5, 'score_threshold_mult': 1.1}
                msg = f"市场转弱: {'; '.join(risk_signals) if risk_signals else '指数在MA20下方'}"
            else:
                # 正常 = 夏季/晴天
                weather = "☀️晴天"
                is_safe = True
                risk_level = "low"
                adjustments = {'max_position_mult': 1.0, 'score_threshold_mult': 1.0}
                msg = f"市场环境良好，指数在20日均线上方"
                if limit_up_count > 0:
                    msg += f" (涨跌停比: {limit_up_count}/{limit_down_count})"
            
            # Structured market regime + trading permissions (additive)
            if risk_level == "high":
                regime = "storm_market"
                trend_state = "crash_or_sentiment_storm"
                sentiment_state = "panic"
                allow_auto_buy = False
                allow_pending_entry = False
                max_position_mult_perm = 0.0
                blocked_strategies = ["*"]
                confirm_only_strategies = []
            elif is_below_ma20:
                regime = "weak_market"
                trend_state = "below_ma20"
                sentiment_state = "weak" if sentiment_crashed else "neutral_to_weak"
                allow_auto_buy = False
                allow_pending_entry = True
                max_position_mult_perm = min(float(adjustments.get('max_position_mult', 0.5) or 0.5), 0.2)
                blocked_strategies = ["集合竞价", "早盘竞价首选", "冷启动"]
                confirm_only_strategies = ["技术突破", "午盘精选", "备选池买入触发"]
            else:
                regime = "normal_uptrend"
                trend_state = "above_ma20"
                sentiment_state = "healthy"
                allow_auto_buy = True
                allow_pending_entry = True
                max_position_mult_perm = float(adjustments.get('max_position_mult', 1.0) or 1.0)
                blocked_strategies = []
                confirm_only_strategies = []

            risk_reasons = list(risk_signals or [])
            permission = {
                'allow_auto_buy': allow_auto_buy,
                'allow_pending_entry': allow_pending_entry,
                'max_position_mult': max_position_mult_perm,
                'blocked_strategies': blocked_strategies,
                'confirm_only_strategies': confirm_only_strategies,
                'reason': msg,
            }

            return {
                'weather': weather,
                'regime': regime,
                'trend_state': trend_state,
                'sentiment_state': sentiment_state,
                'permission': permission,
                'risk_reasons': risk_reasons,
                'is_safe': is_safe,
                'message': msg,
                'sh_index': {'close': sh.get('close', 0), 'ma20': sh.get('ma20', 0), 'pct_chg': sh.get('pct_chg', 0)},
                'cy_index': {'close': cy.get('close', 0), 'ma20': cy.get('ma20', 0), 'pct_chg': cy.get('pct_chg', 0)},
                'sentiment': {'limit_up': limit_up_count, 'limit_down': limit_down_count},
                'risk_level': risk_level,
                'adjustments': adjustments
            }
            
        except Exception as e:
            logger.error(f"Market environment check error: {e}")
            return {
                'weather': '☀️晴天',
                'regime': 'normal_uptrend',
                'trend_state': 'unknown_fallback',
                'sentiment_state': 'unknown_fallback',
                'permission': {
                    'allow_auto_buy': True,
                    'allow_pending_entry': True,
                    'max_position_mult': 1.0,
                    'blocked_strategies': [],
                    'confirm_only_strategies': [],
                    'reason': f'环境检查出错: {e}',
                },
                'risk_reasons': [f'环境检查出错: {e}'],
                'is_safe': True,
                'message': f'环境检查出错: {e}',
                'risk_level': 'low',
                'adjustments': {'max_position_mult': 1.0, 'score_threshold_mult': 1.0}
            }

    def _v20_technical_audit(self, ts_code, trade_date=None, history=None):
        """
        [V20] Comprehensive Technical Audit
        1. MACD Golden Cross & Above Zero
        2. MACD Overheat Protection
        3. [V20] ADX Trend Strength Audit (25-40 Zone)
        4. Relative Strength (RS) vs Index
        """
        try:
            # 1. Fetch history if not provided
            if history is None:
                history = self.provider.get_history_data(ts_code, trade_date, count=60)
            if len(history) < 30:
                return False
                
            df = pd.DataFrame(history)
            df['close'] = df['close'].astype(float)
            
            # --- 2. MACD Logic ---
            dif, dea, _ = self._calculate_macd(df)
            today_dif, today_dea = dif.iloc[-1], dea.iloc[-1]
            yest_dif, yest_dea = dif.iloc[-2], dea.iloc[-2]
            
            golden_cross = (yest_dif <= yest_dea) and (today_dif > today_dea)
            above_zero = (today_dif > 0) and (today_dea > 0)
            
            # MACD Overheat Protection
            close_price = df['close'].iloc[-1]
            if close_price > 0:
                dif_ratio = abs(today_dif) / close_price
                if dif_ratio > 0.03:
                    logger.info(f"🔥 MACD过热拦截 {ts_code}: DIF占比={dif_ratio*100:.1f}% (>3%)")
                    return False
            
            if not (golden_cross and above_zero):
                return False

            # --- 3. [V20] ADX Trend Strength ---
            # Added config check for V20 enablement
            trend_cfg = Config.STRATEGY.get('trend_filter', {})
            if trend_cfg.get('enabled', True):
                trend_info = self.check_trend_strength(ts_code, history=history)
                if not trend_info['can_buy']:
                    logger.info(f"🚫 趋势过滤拒绝 {ts_code}: {trend_info['reason']}")
                    return False
            
            # --- 4. Relative Strength (RS) ---
            if not self._check_rs(ts_code, trade_date, stock_history=history):
                logger.debug(f"🐢 RS值不足 {ts_code}: 未跑赢大盘")
                return False

            return True
        except Exception as e:
            logger.error(f"V20 Technical Audit Error {ts_code}: {e}")
            return False

    def analyze(self, trade_date=None, holdings=None):
        """Perform full analysis"""
        from datetime import datetime
        
        # 1. Get Market Data (baseline from previous trading day)
        daily_data = self.provider.get_daily_data(trade_date)
        if not daily_data:
            return {'error': 'Failed to fetch market data'}
            
        basics = self.provider.get_stock_basic()
        daily_basics = self.provider.get_daily_basic(trade_date)
        
        # [V20.1] Pre-fetch index returns for RS comparison (Zero-API Audit)
        self._prepare_index_cache(trade_date)
        
        # --- REAL-TIME OVERLAY (During trading hours) ---
        current_hour = datetime.now().hour
        current_minute = datetime.now().minute
        is_trading_hours = (current_hour == 9 and current_minute >= 30) or \
                           (10 <= current_hour <= 14) or \
                           (current_hour == 15 and current_minute <= 5)
        
        realtime_data = {}
        if is_trading_hours:
            logger.info("Trading hours detected - fetching real-time quotes from Tushare...")
            all_ts_codes = [d['ts_code'] for d in daily_data]
            
            # Fetch in ONE go (Parallelized in DataProvider)
            realtime_data = self.provider.get_realtime_quotes(all_ts_codes)
            
            logger.info(f"Fetched real-time data for {len(realtime_data)} stocks")
            
            # Overlay real-time prices onto daily_data
            for stock in daily_data:
                ts_code = stock.get('ts_code')
                rt = realtime_data.get(ts_code)
                if rt and rt.get('price', 0) > 0:
                    pre_close = rt.get('pre_close', stock.get('close', 0))
                    if pre_close > 0:
                        stock['close'] = rt['price']
                        stock['pct_chg'] = (rt['price'] - pre_close) / pre_close * 100
                        # DataProvider normalizes realtime vol to lots and amount to yuan.
                        # Convert amount to Tushare daily scale (thousand yuan) for downstream amount-y.
                        stock['vol'] = rt.get('vol_lots', rt.get('vol', stock.get('vol', 0)))
                        stock['amount'] = rt.get('amount_yuan', rt.get('amount', stock.get('amount', 0))) / 1000
        
        # 2. Process and Filter
        active_stocks = []
        pre_candidates = []
        candidates = []
        
        # [NEW] Sell Signal Check for Holdings
        sell_signals = {}
        try:
            if holdings:
                # Support list of dicts (from DB) or list of codes
                holding_codes = [h['code'] for h in holdings] if isinstance(holdings[0], dict) else holdings
                # Need ts_code for history, assuming DB has code without suffix, we need to find ts_code?
                # Actually provider.get_history_data needs ts_code.
                # Let's map code -> ts_code using daily_data or basics
                # Optimization: Create code map
                code_to_ts = {d['ts_code'].split('.')[0]: d['ts_code'] for d in daily_data}
                
                final_holdings = []
                for c in holding_codes:
                    if c in code_to_ts: final_holdings.append(code_to_ts[c])
                
                sell_signals = self.check_holdings(final_holdings, trade_date)
        except Exception as e:
            logger.error(f"Sell signal check failed: {e}")
        
        for stock in daily_data:
            ts_code = stock.get('ts_code')
            code = ts_code.split('.')[0]
            
            # [NEW] Blacklist Check
            if code in Config.BLACKLIST or ts_code in Config.BLACKLIST:
                continue
            
            info = basics.get(ts_code, {})
            db = daily_basics.get(ts_code, {})
            
            name = info.get('name', '')
            
            # Skip ST
            if 'ST' in name or '*' in name:
                continue
                
            # Parse values
            try:
                def to_float(x):
                    try: return float(x)
                    except: return 0.0

                close = to_float(stock.get('close'))
                change = to_float(stock.get('pct_chg'))
                
                # Handle turnover: Use daily_basic if available
                # Prioritize turnover_rate_f (free float turnover) > turnover_rate (total)
                if db:
                    t_f = to_float(db.get('turnover_rate_f'))
                    t_total = to_float(db.get('turnover_rate'))
                    turnover = t_f if t_f > 0 else t_total
                else:
                    turnover = to_float(stock.get('turnover_rate'))
                
                amount = to_float(stock.get('amount')) # thousand val
                vol = to_float(stock.get('vol'))       # lots
                
                # Calculate Avg Price
                avg_price = (amount * 10) / vol if vol > 0 else 0
                
                stock_data = {
                    'code': ts_code.split('.')[0],
                    'ts_code': ts_code,
                    'name': name,
                    'price': close,
                    'change': change,
                    'turnover': turnover,
                    'avg': avg_price,
                    'amount': amount / 100000,
                    'volume': vol,
                    'rise_from_avg': ((close - avg_price) / avg_price * 100) if avg_price > 0 else 0,
                    'industry': info.get('industry', '其他')
                }
                
                active_stocks.append(stock_data)

                # Filter Logic (Dynamic)
                afternoon_cfg = Config.STRATEGY.get('afternoon', {})
                min_to = afternoon_cfg.get('min_turnover', Config.MIN_TURNOVER)
                max_to = afternoon_cfg.get('max_turnover', Config.MAX_TURNOVER)
                min_chg = afternoon_cfg.get('min_change', Config.MIN_CHANGE)
                max_chg = afternoon_cfg.get('max_change', Config.MAX_CHANGE)
                trend_factor = afternoon_cfg.get('trend_factor', 1.01)

                # Turnover
                pass_turnover = (min_to <= turnover <= max_to) if turnover > 0 else True
                
                # Filter: Price > Avg * Factor
                if (min_chg <= change <= max_chg and 
                    pass_turnover and 
                    close >= avg_price * trend_factor):
                    
                    # [NEW] Pre-screen: defer MACD check to batch process
                    pre_candidates.append(stock_data)
                    
            except Exception as e:
                logger.error(f"Error parsing stock {stock}: {e}")
                continue

        # [NEW] Batch MACD Check
        if pre_candidates:
            logger.info(f"Batched MACD screening for {len(pre_candidates)} candidates...")
            ts_codes_to_check = [s['ts_code'] for s in pre_candidates]
            history_batch = self.provider.get_batch_history_data(ts_codes_to_check, trade_date, count=60)
            
            for stock_data in pre_candidates:
                ts_code = stock_data['ts_code']
                history = history_batch.get(ts_code, [])
                if self._v20_technical_audit(ts_code, trade_date, history=history):
                    candidates.append(stock_data)
                    if len(candidates) % 5 == 0: logger.info(f"Found {len(candidates)} MACD candidates...")

        # Sort candidates by amount (liquidity)
        candidates.sort(key=lambda x: x['amount'], reverse=True)

        # Sector Analysis (Use ALL active stocks to get REAL market hot sectors, not just our MACD picks)
        sector_analysis = self._analyze_sectors(active_stocks)

        # Funds Analysis (Keep this focused on our candidates)
        fund_analysis = self._analyze_funds(candidates)

        # Recommendation
        recommendation = self._generate_recommendation(candidates)

        limit_up_analysis = self._analyze_limit_pool(daily_data, trade_date, rt_data=realtime_data, daily_basics=daily_basics)
        auction_picks = self._analyze_auction(daily_data, trade_date, rt_data=realtime_data)
        cold_start_picks = self._analyze_cold_start(daily_data, trade_date, rt_data=realtime_data)

        # Market Weather Assessment (V10)
        market_env = self.check_market_environment(trade_date)

        # [P1] LHB (龙虎榜) quick tags for auction picks / top candidates (best-effort)
        self._attach_lhb_tags(auction_picks, trade_date=trade_date)
        self._attach_lhb_tags(candidates[:20], trade_date=trade_date)

        # [P1] Concept resonance (best-effort, additive)
        self._attach_concepts(auction_picks, max_items=20)
        self._attach_concepts(candidates[:20], max_items=20)

        # [Observe] Training combo scores, enabled by default for first-board candidates.
        self._attach_combo_observe(auction_picks, trade_date=trade_date, rt_data=realtime_data)
        self._attach_combo_observe(candidates[:20], trade_date=trade_date, rt_data=realtime_data)
        self._attach_combo_observe(cold_start_picks, trade_date=trade_date, rt_data=realtime_data)

        try:
            concept_heat = self._compute_concept_heat((auction_picks or []) + (candidates[:20] or []), top_n=12)
        except Exception:
            concept_heat = {}

        # Attach lightweight ecosystem summary to market_env (additive)
        try:
            if isinstance(limit_up_analysis, dict):
                ecosystem = {
                    'limit_up_height': limit_up_analysis.get('height'),
                    'ladder_distribution': limit_up_analysis.get('ladder'),
                    'sector_concentration': limit_up_analysis.get('sector_concentration'),
                    'sector_top': limit_up_analysis.get('sectors', [])[:5]
                }
                market_env['ecosystem'] = ecosystem
        except Exception:
            pass

        return {
            'total_stocks': len(active_stocks),
            'candidates_count': len(candidates),
            'hot_stocks': candidates[:10], # Top 10
            'sector_analysis': sector_analysis,
            'fund_analysis': fund_analysis,
            'recommendation': recommendation,
            'market_env': market_env,
            'limit_up_analysis': limit_up_analysis,
            'auction_picks': auction_picks,
            'cold_start_picks': cold_start_picks,
            'concept_heat': concept_heat,
            'sell_signals': sell_signals
        }

    def _analyze_sectors(self, candidates):
        """Analyze hot sectors"""
        sectors = {}
        for s in candidates:
            sec = s.get('industry', '其他')
            if not sec: sec = '其他'
            
            if sec not in sectors:
                sectors[sec] = {'count': 0, 'total_change': 0, 'total_amount': 0, 'stocks': []}
            
            sectors[sec]['count'] += 1
            sectors[sec]['total_change'] += s['change']
            sectors[sec]['total_amount'] += s['amount']
            sectors[sec]['stocks'].append(s)
            
        result = []
        for k, v in sectors.items():
            # For real market logic, discard tiny sectors with < 3 stocks to avoid skewed avg_change
            if v['count'] < 3:
                continue
                
            # Get Top 3 by Change
            v['stocks'].sort(key=lambda x: x.get('change', 0), reverse=True)
            # Safe access to name/change
            top_stocks = [f"{s.get('name','')}({s.get('change',0):.1f}%)" for s in v['stocks'][:3]]
            
            result.append({
                'sector': k,
                'count': v['count'],
                'avg_change': v['total_change'] / v['count'],
                'amount': v['total_amount'],
                'top_stocks': top_stocks
            })
            
        # [NEW] Rank sectors by avg_change instead of count
        result.sort(key=lambda x: x['avg_change'], reverse=True)
        return result[:5]

    def check_holdings(self, holdings_codes, trade_date=None):
        """Check holdings for sell signals (MA20 break)"""
        if not holdings_codes: return {}
        
        signals = {}
        for code in holdings_codes:
            # Get history (approx 25 days)
            hist = self.provider.get_history_data(code, trade_date, count=30)
            if len(hist) < 20: continue
            
            df = pd.DataFrame(hist)
            df['close'] = df['close'].astype(float)
            
            # Calculate MA20
            ma20 = df['close'].rolling(window=20).mean().iloc[-1]
            close = df['close'].iloc[-1]
            
            if close < ma20:
                signals[code] = {
                    'signal': 'SELL',
                    'reason': f'跌破20日均线 (Close {close:.2f} < MA20 {ma20:.2f})'
                }
        return signals

    def _analyze_limit_pool(self, daily_data, trade_date=None, rt_data=None, daily_basics=None):
        """Analyze limit up stocks - Uses REAL-TIME data during trading hours"""
        from datetime import datetime
        
        current_hour = datetime.now().hour
        current_minute = datetime.now().minute
        is_trading_hours = (current_hour == 9 and current_minute >= 30) or \
                           (10 <= current_hour <= 14) or \
                           (current_hour == 15 and current_minute <= 5)
        
        basics = self.provider.get_stock_basic()
        limit_stocks = []
        prev_limit_map = self._build_prev_limit_context(trade_date)
        daily_map = self._build_daily_map(daily_data)

        # Map ts_code to turnover from daily_basics (preferred) or daily_data
        if daily_basics:
            turnover_map = {ts: float(v.get('turnover_rate', 0) or 0) for ts, v in daily_basics.items()}
        else:
            turnover_map = {s['ts_code']: float(s.get('turnover_rate', s.get('turnover', 0)) or 0) for s in daily_data} if daily_data else {}
            
        logger.info(f"DEBUG: turnover_map size: {len(turnover_map)}, Sample: {list(turnover_map.items())[:5] if turnover_map else 'Empty'}")
        
        if is_trading_hours:
            # --- REAL-TIME MODE ---
            logger.info("Extracting real-time limit-up candidates from fetched rt_data...")
            all_codes = list(basics.keys())
            
            if not rt_data:
                rt_data = self.provider.get_realtime_quotes(all_codes)

            # Filter stocks with pct_chg >= 9.5% (limit-up candidates)
            for ts_code, qt in rt_data.items():
                try:
                    qt = self._repair_realtime_quote(ts_code, qt, daily_map=daily_map, basics=basics)
                    price = qt.get('price', 0)
                    pre_close = qt.get('pre_close', 0)
                    
                    if price <= 0 or pre_close <= 0:
                        continue
                    
                    pct_chg = (price - pre_close) / pre_close * 100
                    
                    if pct_chg >= 9.5:  # Limit-up threshold
                        info = basics.get(ts_code, {})
                        item = {
                            'ts_code': ts_code,
                            'name': qt.get('name', info.get('name', '')),
                            'pct_chg': pct_chg,
                            'price': price,
                            'current_price': price,
                            'close': price,  # Added for compatibility
                            'turnover': qt.get('turnover', turnover_map.get(ts_code, 0)),
                            'limit_times': 1,  # overwritten by previous-day board context below
                            'industry': info.get('industry', '其他'),
                            'limit_type': 'U'
                        }
                        prev_rec = prev_limit_map.get(ts_code, {}) if isinstance(prev_limit_map, dict) else {}
                        prev_limit_times = int(prev_rec.get('limit_times', 0) or 0)
                        item['limit_times'] = prev_limit_times + 1 if prev_limit_times > 0 else 1
                        limit_stocks.append(self._attach_board_context_item(item, prev_limit_map))
                except:
                    continue
            
            logger.info(f"Found {len(limit_stocks)} real-time limit-up stocks")
        else:
            # --- EOD MODE: Use Tushare limit_list ---
            raw_limits = self.provider.get_limit_list(trade_date)
            
            if not raw_limits:
                # Fallback to daily data filter
                for s in daily_data:
                    if float(s.get('pct_chg', 0)) >= 9.5:
                        ts_code = s.get('ts_code')
                        info = basics.get(ts_code, {})
                        s_copy = s.copy()
                        s_copy['name'] = info.get('name', '')
                        s_copy['industry'] = info.get('industry', '其他')
                        s_copy['turnover'] = turnover_map.get(ts_code, 0.0)
                        s_copy['limit_type'] = 'U'
                        s_copy['price'] = s_copy.get('close', 0)
                        limit_stocks.append(self._attach_board_context_item(s_copy, prev_limit_map))
            else:
                # Enrich limit_list with indicators
                for s in raw_limits:
                    ts_code = s.get('ts_code')
                    info = basics.get(ts_code, {})
                    s['name'] = info.get('name', '')
                    s['turnover'] = turnover_map.get(ts_code, 0.0)
                    s['limit_type'] = s.get('limit_type', 'U') # Ensure limit_type is set
                    s['price'] = s.get('close', s.get('price', 0))
                    s = self._attach_board_context_item(s, prev_limit_map)
                    if int(s.get('limit_times', 0) or 0) <= 0:
                        prev_limit_times = int(s.get('prev_limit_times', 0) or 0)
                        s['limit_times'] = prev_limit_times + 1 if prev_limit_times > 0 else 1
                    limit_stocks.append(s)
        
        logger.info(f"Extracted {len(limit_stocks)} limit-up stocks.")
        
        # Filter only Limit Up
        limit_ups = [s for s in limit_stocks if s.get('limit_type', 'U') == 'U' and float(s.get('pct_chg', 0)) > 0]
        self._attach_combo_observe(limit_ups, trade_date=trade_date, rt_data=rt_data)
        
        # Sector Analysis for Limit Ups
        sectors = {}
        ladder = {}
        max_height = 0

        for s in limit_ups:
            sec = s.get('industry', '其他')
            if not sec:
                sec = '其他'

            if sec not in sectors:
                sectors[sec] = {'count': 0, 'highest_board': None, 'score': 0}

            sectors[sec]['count'] += 1

            times = int(s.get('limit_times', 1) or 1)
            change = float(s.get('pct_chg', 0) or 0)

            # ladder distribution
            ladder[times] = ladder.get(times, 0) + 1
            if times > max_height:
                max_height = times

            current_highest = sectors[sec]['highest_board']
            if not current_highest:
                sectors[sec]['highest_board'] = s
            else:
                curr_times = int(current_highest.get('limit_times', 1) or 1)
                if times > curr_times:
                    sectors[sec]['highest_board'] = s
                elif times == curr_times:
                    if change > float(current_highest.get('pct_chg', 0) or 0):
                        sectors[sec]['highest_board'] = s

        # Normalize ladder into compact buckets
        ladder_distribution = {}
        for k, v in ladder.items():
            if k >= 4:
                ladder_distribution['4+'] = ladder_distribution.get('4+', 0) + v
            else:
                ladder_distribution[str(k)] = ladder_distribution.get(str(k), 0) + v

        # Calculate Sentiment Score
        sector_list = []
        for k, v in sectors.items():
            hb = v['highest_board']
            hb_change = float(hb.get('pct_chg', 0) or 0) if hb else 0
            score = v['count'] * 100 + hb_change * 10

            sector_list.append({
                'sector': k,
                'count': v['count'],
                'highest_board': hb,
                'score': score
            })

        sector_list.sort(key=lambda x: x['score'], reverse=True)

        # Sector concentration (top1/top3 share)
        total = len(limit_ups)
        sorted_counts = sorted([s['count'] for s in sector_list], reverse=True)
        top1_share = (sorted_counts[0] / total) if total and sorted_counts else 0
        top3_share = (sum(sorted_counts[:3]) / total) if total and sorted_counts else 0

        return {
            'count': len(limit_ups),
            'height': max_height,
            'ladder': ladder_distribution,
            'sector_concentration': {'top1': round(top1_share, 3), 'top3': round(top3_share, 3)},
            'sectors': sector_list[:5],
            'stocks': limit_ups
        }


    def _analyze_auction(self, daily_data, trade_date=None, rt_data=None):
        """Analyze auction/pre-market strategy using Real-time Data"""
        
        candidates = []
        
        # 1. Get Universe (All Active Stocks)
        basics = self.provider.get_stock_basic()
        all_codes = list(basics.keys())
        
        logger.info(f"Scanning {len(all_codes)} stocks for Auction Strategy...")
        
        # 2. Try to get official Auction Data and Limit-up Booster Data
        auction_data = self.provider.get_stk_auction()
        limit_data = self.provider.get_stk_limit() # Yesterday's limits (Doc 369)
        auc_cfg = Config.STRATEGY.get('auction', {})
        boost_cfg = auc_cfg.get('limit_up_boost', {})
        
        # [V21] 提前获取资金流数据 (竞价资金流加分)
        money_flow_data = {}
        try:
            mf_days = auc_cfg.get('mf_days', 3)
            mf = self.provider.get_individual_money_flow(all_codes, days=mf_days)
            money_flow_data = mf if mf else {}
            logger.info(f"📊 Auction: Fetched money flow for {len(money_flow_data)} stocks")
        except Exception as e:
            logger.warning(f"Failed to get money flow data: {e}")
        
        # 3. Fallback to Real-time Quotes if auction_data is empty
        if not rt_data and not auction_data:
            rt_data = self.provider.get_realtime_quotes(all_codes)
        
        # 4. Get Yesterday's Circulating Shares for Turnover Calc (Fallback source)
        yest_basics = self.provider.get_daily_basic()
        
        # Merge sources: Priority stk_auction > real-time
        if auction_data:
            logger.info(f"Using official stk_auction data for {len(auction_data)} stocks.")
            for ts_code, row in auction_data.items():
                try:
                    price = float(row.get('price', 0))
                    pre_close = float(row.get('pre_close', 0))
                    basic_info = basics.get(ts_code, {}) or {}
                    name = basic_info.get('name') or row.get('name') or code
                    code = ts_code.split('.')[0]
                    
                    if 'ST' in name or '*' in name: continue
                    if code in Config.BLACKLIST: continue
                    if price <= 0 or pre_close <= 0: continue
                    
                    open_change = (price - pre_close) / pre_close * 100
                    turnover = float(row.get('turnover_rate', 0))
                    
                    # Filter 2: High Open (Dynamic)
                    auc_cfg = Config.STRATEGY.get('auction', {})
                    if not (auc_cfg.get('min_open_change', 2.0) <= open_change <= auc_cfg.get('max_open_change', 5.0)):
                        continue
                    
                    if turnover >= auc_cfg.get('min_turnover', 1.0):
                        # Calculate Base Score
                        score = (open_change * 4) + (turnover * 5) + (float(row.get('amount', 0)) / 1e8)
                        
                        # [首板增强] 昨日未涨停 + 开盘质量适中 = 首板候选加分
                        prev_limit_present = ts_code in limit_data
                        board_context = 'continue_board' if prev_limit_present else 'first_board'
                        first_board_bonus = 0.0
                        first_board_tag = ''
                        if auc_cfg.get('prefer_first_board', True) and not prev_limit_present:
                            fb_min_open = auc_cfg.get('first_board_min_open_change', 1.5)
                            fb_max_open = auc_cfg.get('first_board_max_open_change', 4.8)
                            fb_min_to = auc_cfg.get('first_board_min_turnover', 0.8)
                            fb_max_to = auc_cfg.get('first_board_max_turnover', 8.0)
                            if fb_min_open <= open_change <= fb_max_open and fb_min_to <= turnover <= fb_max_to:
                                first_board_bonus = float(auc_cfg.get('first_board_bonus', 12) or 0)
                                score += first_board_bonus
                                first_board_tag = f'🥇首板候选|高开{open_change:.1f}%|换手{turnover:.1f}%'
                        
                        # Limit-up Booster (Boost score for leaders)
                        if ts_code in limit_data:
                            ld = limit_data[ts_code]
                            lt = int(ld.get('limit_times', 1))
                            fd = float(ld.get('fd_amount', 0)) / 1e8
                            weight = boost_cfg.get('weight', 1.0)
                            # Boost for consecutive limits and closing strength
                            score += (lt * 1.5 * weight)
                            if fd > boost_cfg.get('min_fd_amount_yi', 0.1):
                                score += 2.0 * weight
                        
                        # [V21] 资金流加分: 主力净流入+机构净流入
                        mf_info = money_flow_data.get(ts_code, {})
                        if mf_info:
                            net_inflow = mf_info.get('net_inflow', 0) or 0  # 主力净流入(万)
                            elg_net = mf_info.get('elg_net', 0) or 0  # 机构净流入(超大单, 万)
                            yz_net = net_inflow - elg_net  # 游资净流入
                            
                            # 资金加分: 主力净流入>0 加分
                            if net_inflow > 0:
                                # 机构资金加分 (更稳健)
                                if elg_net > 0:
                                    score += 15  # 机构主导+15分
                                # 游资资金加分 (更激进)
                                elif yz_net > 0:
                                    score += 10  # 游资主导+10分
                            elif yz_net < 0:  # 游资净流出扣分
                                score -= 20
                            
                            # [V21] 连板潜力加分 (核心特征) - 优化版
                            # 特征: 昨日涨停 + 今日高开5-7% + 换手50-80% + 机构净流入
                            if ts_code in limit_data:  # 昨日涨停
                                if 5 <= open_change < 7.0:  # 今日高开5-7%，更窄区间
                                    if 50 <= turnover <= 80:  # 换手率50-80%黄金区间
                                        if elg_net > 0:  # 机构净流入
                                            score += 35  # 连板潜力强加分！
                            
                            # 记录资金信息到候选
                            logger.info(f"  💰 {name}: 主力{int(net_inflow)}万, 机构{int(elg_net)}万, 游资{int(yz_net)}万")
                                
                        # [V21 Merged] 涨停续涨标签 (训练准确率63%)
                        # 昨日涨停 + 今日高开>=5% + 缩量 → 打标签并加分
                        zt_tag = ''
                        zt_boost_cfg = auc_cfg.get('zt_continue_boost', {})
                        if ts_code in limit_data and open_change >= zt_boost_cfg.get('min_open_change', 5.0):
                            # Calculate volume ratio for zt_continue check
                            yest_amount = float(limit_data[ts_code].get('amount', 0))
                            curr_amount = float(row.get('amount', 0))
                            zt_vol_ratio = curr_amount / yest_amount if yest_amount > 0 else 1.0
                            if zt_vol_ratio < zt_boost_cfg.get('max_vol_ratio', 0.8):
                                zt_tag = f'\U0001f525\u6da8\u505c\u7eed\u6da8|\u9ad8\u5f00{open_change:.1f}%|\u7f29\u91cf{zt_vol_ratio:.0%}'
                                score += zt_boost_cfg.get('score_bonus', 30)
                            
                        candidates.append({
                            'code': code,
                            'ts_code': ts_code,
                            'name': name,
                            'open_change': open_change,
                            'turnover': turnover,
                            'price': price,
                            'amount_yi': float(row.get('amount', 0)) / 1e8,
                            'industry': basic_info.get('industry') or row.get('industry') or '\u5176\u4ed6',
                            'score': score,
                            'zt_tag': zt_tag,
                            'prev_limit_times': int(limit_data.get(ts_code, {}).get('limit_times', 0) or 0),
                            'prev_limit_present': bool(ts_code in limit_data),
                            'is_first_board_candidate': bool(ts_code not in limit_data),
                            'is_continue_board_candidate': bool(ts_code in limit_data),
                            'board_context': board_context,
                            'first_board_bonus': first_board_bonus,
                            'first_board_tag': first_board_tag
                        })
                except: continue
        
        # If no candidates from stk_auction (or it was empty), try real-time quotes logic
        if not candidates and rt_data:
            for ts_code, qt in rt_data.items():
                try:
                    open_p = qt['open']
                    pre_close = qt['pre_close']
                    name = qt.get('name', '')
                    code = ts_code.split('.')[0]
                    
                    # [Fix] Filter ST and Blacklist
                    if 'ST' in name or '*' in name: continue
                    if code in Config.BLACKLIST or ts_code in Config.BLACKLIST: continue
                    
                    # Filter 1: Valid Pricing
                    if open_p <= 0 or pre_close <= 0: continue
                    
                    open_change_pct = (open_p - pre_close) / pre_close * 100
                    
                    # Filter 2: High Open (Dynamic)
                    auc_cfg = Config.STRATEGY.get('auction', {})
                    min_open = auc_cfg.get('min_open_change', 2.0)
                    max_open = auc_cfg.get('max_open_change', 5.0)

                    if not (min_open <= open_change_pct <= max_open):
                        continue
                        
                    # Calculate Turnover
                    db = yest_basics.get(ts_code, {})
                    circ_mv = float(db.get('circ_mv', 0)) * 10000 # to Yuan
                    amount = qt['amount'] # Yuan
                    
                    if circ_mv > 0:
                        turnover = (amount / circ_mv) * 100
                    else:
                        turnover = 0
                    
                    # Filter 3: Turnover (Dynamic)
                    min_auc_to = auc_cfg.get('min_turnover', 1.0)
                    
                    if turnover >= min_auc_to:
                        # Calculate Base Score (RT version)
                        score = (open_change_pct * 4) + (turnover * 5) + (amount / 1e8)
                        prev_limit_present = ts_code in limit_data
                        board_context = 'continue_board' if prev_limit_present else 'first_board'
                        first_board_bonus = 0.0
                        first_board_tag = ''
                        if auc_cfg.get('prefer_first_board', True) and not prev_limit_present:
                            fb_min_open = auc_cfg.get('first_board_min_open_change', 1.5)
                            fb_max_open = auc_cfg.get('first_board_max_open_change', 4.8)
                            fb_min_to = auc_cfg.get('first_board_min_turnover', 0.8)
                            fb_max_to = auc_cfg.get('first_board_max_turnover', 8.0)
                            if fb_min_open <= open_change_pct <= fb_max_open and fb_min_to <= turnover <= fb_max_to:
                                first_board_bonus = float(auc_cfg.get('first_board_bonus', 12) or 0)
                                score += first_board_bonus
                                first_board_tag = f'🥇首板候选|高开{open_change_pct:.1f}%|换手{turnover:.1f}%'

                        # Limit-up Booster
                        if ts_code in limit_data:
                            ld = limit_data[ts_code]
                            lt = int(ld.get('limit_times', 1))
                            fd = float(ld.get('fd_amount', 0)) / 1e8
                            weight = boost_cfg.get('weight', 1.0)
                            score += (lt * 1.5 * weight)
                            if fd > boost_cfg.get('min_fd_amount_yi', 0.1):
                                score += 2.0 * weight
                                
                        # [V21 Merged] 涨停续涨标签
                        zt_tag = ''
                        zt_boost_cfg = auc_cfg.get('zt_continue_boost', {})
                        if ts_code in limit_data and open_change_pct >= zt_boost_cfg.get('min_open_change', 5.0):
                            yest_amount = float(limit_data[ts_code].get('amount', 0))
                            zt_vol_ratio = amount / yest_amount if yest_amount > 0 else 1.0
                            if zt_vol_ratio < zt_boost_cfg.get('max_vol_ratio', 0.8):
                                zt_tag = f'\U0001f525\u6da8\u505c\u7eed\u6da8|\u9ad8\u5f00{open_change_pct:.1f}%|\u7f29\u91cf{zt_vol_ratio:.0%}'
                                score += zt_boost_cfg.get('score_bonus', 30)
                                
                        candidates.append({
                            'code': ts_code.split('.')[0],
                            'ts_code': ts_code,
                            'name': qt['name'],
                            'open_change': open_change_pct,
                            'turnover': turnover,
                            'price': open_p,
                            'amount_yi': amount / 1e8,
                            'industry': basics.get(ts_code, {}).get('industry', '\u5176\u4ed6'),
                            'score': score,
                            'zt_tag': zt_tag,
                            'prev_limit_times': int(limit_data.get(ts_code, {}).get('limit_times', 0) or 0),
                            'prev_limit_present': bool(ts_code in limit_data),
                            'is_first_board_candidate': bool(ts_code not in limit_data),
                            'is_continue_board_candidate': bool(ts_code in limit_data),
                            'board_context': board_context,
                            'first_board_bonus': first_board_bonus,
                            'first_board_tag': first_board_tag
                        })
                         
                except Exception as e:
                    continue
        
        # === OVERHEATED FILTER ===
        if candidates:
            logger.info(f"Running overheated filter on {len(candidates)} auction candidates...")
            ts_codes_to_check = [c['ts_code'] for c in candidates]
            history_batch = self.provider.get_batch_history_data(ts_codes_to_check, count=25)
            
            # Sort by Score for Top List
            candidates.sort(key=lambda x: x.get('score', 0), reverse=True)
            
            max_10d_gain = auc_cfg.get('max_10d_gain', 30)
            max_5d_gain = auc_cfg.get('max_5d_gain', 20)
            max_ma20_dev = auc_cfg.get('max_ma20_deviation', 25)
            max_consec_zt = auc_cfg.get('max_consecutive_limit_up', 3)
            
            filtered = []
            for c in candidates:
                hist = history_batch.get(c['ts_code'], [])
                is_hot, reason, gain_10d, ma20_dev = self._is_overheated(
                    hist, max_10d_gain, max_5d_gain, max_ma20_dev, max_consec_zt
                )
                c['gain_10d'] = gain_10d
                c['ma20_dev'] = ma20_dev
                
                if is_hot:
                    logger.info(f"  [HOT] Auction filtered: {c['name']}({c['code']}) - {reason}")
                else:
                    filtered.append(c)
            
            logger.info(f"Overheated filter: {len(candidates)} -> {len(filtered)} candidates")
            candidates = filtered
                
        for c in candidates:
            # Preserve accumulated scoring and keep first-board bonus additive
            base_score = c.get('score')
            if base_score is None:
                base_score = (c['open_change'] * 4) + (c['turnover'] * 5) + (c.get('amount_yi', 0) * 1)
            c['score'] = float(base_score)
        
        # [V15] Apply macro boost from cache (ZERO network request!)
        candidates = self._apply_macro_boost_to_candidates(candidates)
        
        # [V21] 读取配置中的选股数量限制
        max_candidates = Config.STRATEGY.get('auction', {}).get('max_candidates', 5)
        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates[:max_candidates]

    def _analyze_cold_start(self, daily_data, trade_date=None, rt_data=None):
        """
        冷启动策略：抓取昨日未涨/横盘，但今日早盘突然异动的个股
        核心思路：昨日涨幅<3%，未涨停，近期5日涨幅<10%，今日竞价高开2%+突然启动
        """
        import logging
        logger = logging.getLogger('StockAnalyzer.Analyzer')
        try:
            from core.cold_start_model import ColdStartModelScorer
            cold_start_scorer = ColdStartModelScorer()
        except Exception:
            cold_start_scorer = None
        
        # 检查配置是否启用
        cs_cfg = Config.STRATEGY.get('auction', {}).get('cold_start', {})
        if not cs_cfg.get('enabled', False):
            logger.info("Cold start strategy is disabled")
            return []
        
        logger.info("=== Running Cold Start Strategy ===")
        
        # 获取配置参数
        min_open = cs_cfg.get('min_open_change', 2.0)
        max_open = cs_cfg.get('max_open_change', 9.0)
        min_turnover = cs_cfg.get('min_turnover', 0.5)
        max_turnover = cs_cfg.get('max_turnover', 5.0)
        prev_max_change = cs_cfg.get('prev_max_change', 3.0)
        prev_allow_zt = cs_cfg.get('prev_allow_zt', False)
        max_5d_gain = cs_cfg.get('max_5d_gain', 10.0)
        max_10d_gain = cs_cfg.get('max_10d_gain', 15.0)
        max_candidates = cs_cfg.get('max_candidates', 5)
        mf_days = cs_cfg.get('mf_days', 3)
        max_ma20_dev = cs_cfg.get('max_ma20_deviation', 20)
        
        candidates = []
        
        # 获取股票基本信息 (与 auction 策略保持一致)
        basics = self.provider.get_stock_basic()
        all_codes = list(basics.keys())
        
        # 获取昨日涨停数据
        limit_data = self.provider.get_stk_limit()
        
        # 批量获取历史数据用于计算前期涨幅
        all_codes = list(basics.keys())
        history_batch = self.provider.get_batch_history_data(all_codes, count=15)
        
        # 获取资金流信息
        try:
            mf_data = self.provider.get_individual_money_flow(all_codes, days=mf_days)
            money_flow_data = mf_data if mf_data else {}
            logger.info(f"Cold Start: Fetched money flow for {len(money_flow_data)} stocks")
        except Exception as e:
            logger.warning(f"Failed to get money flow data: {e}")
            money_flow_data = {}
        
        # 如果没有实时数据，尝试获取实时行情
        if not rt_data:
            rt_data = self.provider.get_realtime_quotes(all_codes)
        
        yest_basics = self.provider.get_daily_basic()
        daily_map = self._build_daily_map(daily_data)
        
        # 遍历所有股票，筛选冷启动候选
        for ts_code, qt in rt_data.items():
            try:
                qt = self._repair_realtime_quote(ts_code, qt, daily_map=daily_map, basics=basics)
                code = ts_code.split('.')[0]
                basic_info = basics.get(ts_code, {}) or {}
                name = qt.get('name') or basic_info.get('name') or code
                
                # 黑名单过滤
                if 'ST' in name or '*' in name: 
                    continue
                if code in Config.BLACKLIST or ts_code in Config.BLACKLIST: 
                    continue
                
                price = float(qt.get('price', 0))
                pre_close = float(qt.get('pre_close', 0))
                
                if price <= 0 or pre_close <= 0: 
                    continue
                
                open_change = (price - pre_close) / pre_close * 100
                
                # Filter 1: 高开幅度
                if not (min_open <= open_change <= max_open):
                    continue
                
                # 计算换手率
                db = yest_basics.get(ts_code, {})
                circ_mv = float(db.get('circ_mv', 0)) * 10000
                amount = qt.get('amount', 0)
                
                if circ_mv > 0:
                    turnover = (amount / circ_mv) * 100
                else:
                    continue
                
                # Filter 2: 换手率
                if not (min_turnover <= turnover <= max_turnover):
                    continue
                
                # 获取历史数据
                hist = history_batch.get(ts_code, [])
                if len(hist) < 2:
                    continue
                
                # 获取昨日收盘价和涨幅
                yest_data = hist[-1]
                yest_close = float(yest_data.get('close', 0))
                # 获取昨日涨幅 (直接用 pct_chg 字段)
                prev_change = float(yest_data.get('pct_chg', 0) or 0)
                
                # 检查昨日涨停状态
                prev_zt = ts_code in limit_data
                
                # Filter 3: 昨日未涨停 (冷启动关键：昨日没涨)
                if not prev_allow_zt and prev_zt:
                    logger.info(f"  [COLD] Filtered {name}: Yesterday limit-up")
                    continue
                
                # Filter 4: 昨日涨幅不超过阈值
                if prev_change > prev_max_change:
                    logger.info(f"  [COLD] Filtered {name}: Prev change {prev_change:.1f}% > {prev_max_change}%")
                    continue
                
                # Filter 5: 5日涨幅不超过阈值
                gain_5d = 0.0
                if len(hist) >= 5:
                    close_5d_ago = float(hist[-5].get('close', 0))
                    if close_5d_ago > 0:
                        gain_5d = (yest_close - close_5d_ago) / close_5d_ago * 100
                        if gain_5d > max_5d_gain:
                            logger.info(f"  [COLD] Filtered {name}: 5d gain {gain_5d:.1f}% > {max_5d_gain}%")
                            continue
                
                # Filter 6: MA20偏离度
                ma20_dev = 0.0
                if len(hist) >= 20:
                    ma20 = sum([float(h.get('close', 0)) for h in hist[-20:]]) / 20
                    if ma20 > 0:
                        ma20_dev = (price - ma20) / ma20 * 100
                        if abs(ma20_dev) > max_ma20_dev:
                            logger.info(f"  [COLD] Filtered {name}: MA20 deviation {ma20_dev:.1f}%")
                            continue
                
                # === 形态检查 ===
                shape_cfg = cs_cfg.get('shape_check', {})
                if shape_cfg.get('enabled', False):
                    # 计算均线
                    ma5 = sum([float(h.get('close', 0)) for h in hist[-5:]]) / 5
                    ma10 = sum([float(h.get('close', 0)) for h in hist[-10:]]) / 10
                    ma20 = sum([float(h.get('close', 0)) for h in hist[-20:]]) / 20 if len(hist) >= 20 else ma10
                    
                    # 检查1: 站上MA5 (当日价格站在MA5上方)
                    if shape_cfg.get('require_above_ma5', True):
                        if price < ma5:
                            logger.info(f"  [COLD] Filtered {name}: Below MA5 (price={price:.2f}, ma5={ma5:.2f})")
                            continue
                    
                    # 检查2: 不跌破MA10 (确保不是下跌中继)
                    if shape_cfg.get('require_not_below_ma10', True):
                        if price < ma10:
                            logger.info(f"  [COLD] Filtered {name}: Below MA10 (price={price:.2f}, ma10={ma10:.2f})")
                            continue
                    
                    # 检查3: 放量 (今日成交额 > 昨日成交额 * min_volume_ratio)
                    if shape_cfg.get('require_volume_up', True):
                        yest_amount = float(yest_data.get('amount', 0) or 0)
                        min_vol_ratio = shape_cfg.get('min_volume_ratio', 1.0)
                        if yest_amount > 0 and amount < yest_amount * min_vol_ratio:
                            logger.info(f"  [COLD] Filtered {name}: Volume not放大 (amt={amount/1e8:.2f}y, yest={yest_amount/1e8:.2f}y)")
                            continue
                
                # 计算评分
                weight_open = cs_cfg.get('weight_open_change', 4)
                weight_turnover = cs_cfg.get('weight_turnover', 5)
                weight_mf = cs_cfg.get('weight_mf_intensity', 30)
                weight_first = cs_cfg.get('weight_first_board', 15)
                
                score = (open_change * weight_open) + (turnover * weight_turnover)
                
                # 资金流加分 (使用正确的字段名: net_inflow, elg_net)
                mf_intensity = 0.0
                mf_info = money_flow_data.get(ts_code, {})
                if mf_info:
                    net_inflow = float(mf_info.get('net_inflow', 0) or 0)
                    elg_net = float(mf_info.get('elg_net', 0) or 0)
                    # 资金流强度 = 主力净流入 + 机构净流入 (单位: 万元)
                    mf_intensity = (net_inflow + elg_net) / 10000  # 转换为亿元
                    score += mf_intensity * weight_mf
                
                # 首板加分 (昨日未涨停=潜在首板)
                if not prev_zt:
                    score += weight_first
                
                candidate = {
                    'code': code,
                    'ts_code': ts_code,
                    'name': name,
                    'price': price,
                    'open_change': open_change,
                    'turnover': turnover,
                    'prev_change': prev_change,
                    'prev_zt': prev_zt,
                    'gain_5d': gain_5d,
                    'ma20_dev': ma20_dev,
                    'score': score,
                    'amount_yi': amount / 1e8,
                    'industry': basic_info.get('industry') or '其他',
                    'board_context': '首板候选' if not prev_zt else '接力候选',
                    'mf_intensity': mf_intensity
                }
                if cold_start_scorer is not None:
                    try:
                        obs = cold_start_scorer.score_candidate(candidate, signal_time="09:35:00")
                        candidate.update(obs)
                    except Exception:
                        candidate['cold_start_model_available'] = False
                candidates.append(candidate)
                
            except Exception as e:
                continue
        
        # 按评分排序，返回前N个候选
        candidates.sort(key=lambda x: x.get('score', 0), reverse=True)
        logger.info(f"Cold Start: {len(candidates)} candidates passed all filters")
        
        return candidates[:max_candidates]

    def _is_overheated(self, history_records, max_10d_gain=30, max_5d_gain=20, max_ma20_dev=25, max_consec_zt=3):
        """
        Check if a stock is overheated based on recent price history.
        Returns: (is_overheated: bool, reason: str, gain_10d: float, ma20_dev: float)
        """
        if not history_records or len(history_records) < 5:
            return False, "", 0.0, 0.0
        
        closes = [float(r['close']) for r in history_records]
        
        # Rule 0: 5-day rapid gain
        gain_5d = 0.0
        if len(closes) >= 5:
            close_5d_ago = closes[-5]
            close_now = closes[-1]
            if close_5d_ago > 0:
                gain_5d = (close_now - close_5d_ago) / close_5d_ago * 100
            if gain_5d > max_5d_gain:
                return True, f"5日涨幅{gain_5d:.1f}% > {max_5d_gain}%", gain_5d, 0.0
        
        # Rule 1: 10-day cumulative gain
        gain_10d = 0.0
        if len(closes) >= 10:
            close_10d_ago = closes[-10]
            close_now = closes[-1]
            if close_10d_ago > 0:
                gain_10d = (close_now - close_10d_ago) / close_10d_ago * 100
            if gain_10d > max_10d_gain:
                return True, f"10日涨幅{gain_10d:.1f}% > {max_10d_gain}%", gain_10d, 0.0
        
        # Rule 2: MA20 deviation
        ma20_dev = 0.0
        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20
            if ma20 > 0:
                ma20_dev = (closes[-1] - ma20) / ma20 * 100
            if ma20_dev > max_ma20_dev:
                return True, f"偏离MA20 {ma20_dev:.1f}% > {max_ma20_dev}%", gain_10d, ma20_dev
        
        # Rule 3: Limit-up days in last 5 days (total, not just consecutive)
        recent_pct = []
        for r in history_records[-5:]:
            pct = float(r.get('pct_chg', 0) or 0)
            recent_pct.append(pct)
        
        total_zt = sum(1 for pct in recent_pct if pct >= 9.5)
        
        if total_zt >= max_consec_zt:
            return True, f"近5日涨停{total_zt}天 >= {max_consec_zt}", gain_10d, ma20_dev
        
        return False, "", gain_10d, ma20_dev

    def _analyze_funds(self, candidates):
        """Analyze capital flow of top candidates"""
        if not candidates:
            return {}
            
        top10 = candidates[:10]
        total = sum(s['amount'] for s in top10)
        top3 = sum(s['amount'] for s in top10[:3])
        
        return {
            'top10_amount': total,
            'top3_amount': top3,
            'top3_ratio': (top3 / total * 100) if total > 0 else 0,
            'avg_turnover': sum(s['turnover'] for s in top10) / len(top10) if top10 else 0,
            'avg_change': sum(s['change'] for s in top10) / len(top10) if top10 else 0,
            'avg_rise_from_avg': sum(s['rise_from_avg'] for s in top10) / len(top10) if top10 else 0
        }

    def _generate_recommendation(self, candidates):
        """Generate trading advice"""
        if not candidates:
            return {
                'market_power': '弱势',
                'position': '0-30%',
                'suggestion': '无符合条件标的，建议观望',
                'top3': []
            }
            
        top10 = candidates[:10]
        avg_change = sum(s['change'] for s in top10) / len(top10)
        
        if avg_change > 3.5:
            power = '强'
            pos = '60-80%'
            sugg = '市场活跃，积极参与'
        elif avg_change > 2.5:
            power = '中强'
            pos = '40-60%'
            sugg = '温和上涨，精选个股'
        else:
            power = '一般'
            pos = '30-50%'
            sugg = '注意节奏，低吸为主'
            
        return {
            'market_power': power,
            'position': pos,
            'suggestion': sugg,
            'top3': [f"{s['name']}({s['code']})" for s in top10[:3]]
        }

    def analyze_sector_flow_afternoon(self):
        """
        [14:30 Strategy] Hybrid Money Flow Strategy
        1. Find sectors with accumulating inflows over last 10 days (from Tushare).
        2. Check if these sectors are RISING today (from Real-time data).
        3. Pick stocks in these sectors with good shape.
        """
        logger.info("Starting [Afternoon Sector Flow] analysis...")
        
        # 1. Get Sector Flow (Aggregation Method)
        # Use new method to ensure industry name match
        sector_flow = self.provider.get_sector_rank_by_aggregated_flow(days=10)
        
        top_names_list = [item['name'] for item in sector_flow[:10]]
        logger.info(f"Top 10 Inflow Sectors (10-day Aggregated): {top_names_list}")
        prev_limit_map = self._build_prev_limit_context()
        
        # 2. Get Real-time confirmation
        basics = self.provider.get_stock_basic()
        candidate_stocks = []
        
        # Filter stocks in top sectors
        for code, info in basics.items():
            if info['industry'] in top_names_list:
                candidate_stocks.append(code)
                
        logger.info(f"Found {len(candidate_stocks)} candidates in top sectors.")
        
        if not candidate_stocks:
            return []

        # Real-time check
        rt_data = self.provider.get_realtime_quotes(candidate_stocks)
        
        # [NEW] Calculate Sector Average Change (Real-time)
        sector_changes = {} # sector -> [pct_chg]
        for code, data in rt_data.items():
            try:
                chg = (data['price'] - data['pre_close']) / data['pre_close'] * 100
                ind = basics[code]['industry']
                if ind not in sector_changes: sector_changes[ind] = []
                sector_changes[ind].append(chg)
            except:
                continue
                
        sector_avg_map = {}
        for ind, chgs in sector_changes.items():
            if chgs:
                sector_avg_map[ind] = sum(chgs) / len(chgs)
        
        # Get Circulating Shares for Turnover Calc
        float_share_map = self.provider.get_circulating_share_map()
        
        selected = []
        for code, data in rt_data.items():
            try:
                pct_chg = (data['price'] - data['pre_close']) / data['pre_close'] * 100
                
                # Condition 1: Rising > X% but not Limit Up (> 9.5%)
                # Use Config parameters
                min_chg = Config.STRATEGY.get('afternoon', {}).get('min_change', Config.MIN_CHANGE)
                max_chg = Config.STRATEGY.get('afternoon', {}).get('max_change', Config.MAX_CHANGE)
                
                if min_chg < pct_chg < max_chg:
                    stock_industry = basics[code]['industry']
                    # Amount in Yi
                    amt_yi = data['amount'] / 100000000 
                    
                    # Calculate real-time turnover.
                    # Provider normalizes vol to lots; float_share is in 10k shares.
                    # Turnover = (lots * 100) / (float_share * 10000) * 100
                    tr = 0.0
                    f_share = float_share_map.get(code)
                    if f_share and f_share > 0:
                        vol_shares = data.get('vol_shares')
                        if vol_shares is None:
                            vol_shares = (data.get('vol', 0) or 0) * 100
                        tr = vol_shares / (f_share * 10000) * 100
 
                    # Add Sector Avg to Reason
                    sec_avg = sector_avg_map.get(stock_industry, 0.0)
                    
                    item = {
                        'code': code.split('.')[0],
                        'ts_code': code,
                        'name': data['name'],
                        'price': data['price'],
                        'change': round(pct_chg, 2),
                        'turnover': round(tr, 2),
                        'amount': round(amt_yi, 2),
                        'industry': stock_industry,
                        'reason': f"Top Sector ({stock_industry} {sec_avg:.1f}%)"
                    }
                    selected.append(self._attach_board_context_item(item, prev_limit_map))
            except:
                continue

        # [NEW] Batch Money Flow Filter
        # 1. Extract candidate codes
        pre_selected_codes = [s['ts_code'] for s in selected]
        
        if pre_selected_codes:
            days = Config.INDIVIDUAL_FLOW_DAYS # Default 3
            logger.info(f"Checking Individual Money Flow (Last {days} days) for {len(pre_selected_codes)} candidates...")
            
            mf_data = self.provider.get_individual_money_flow(pre_selected_codes, days=days)
            
            # [V10] Fetch HSGT Top data for Northbound factor
            hsgt_data = {}
            try:
                hsgt_data = self.provider.get_hsgt_top10()
                logger.info(f"HSGT Top: found {len(hsgt_data)} stocks")
            except Exception as e:
                logger.warning(f"Failed to fetch HSGT data: {e}")
            
            final_selected = []
            for s in selected:
                ts_code = s['ts_code']
                mf_stats = mf_data.get(ts_code, {})
                
                # Net Inflow (Big Order)
                net_inflow = mf_stats.get('net_inflow', 0)
                elg_net = mf_stats.get('elg_net', 0)
                stability = mf_stats.get('inflow_days', 0)
                
                # [V10] HSGT Factor: Check if in Northbound Top
                hsgt_rank = -1
                if ts_code in hsgt_data:
                    hsgt_rank = hsgt_data[ts_code].get('rank', -1)
                s['hsgt_rank'] = hsgt_rank
                
                # Filter condition: Net Inflow > 0 (Positive Smart Money)
                if net_inflow > 0:
                    # moneyflow_dc.net_amount is already in 10k yuan.
                    s['money_flow_wan'] = round(net_inflow, 2)
                    total_amt_yuan = s.get('amount', 0) * 1e8
                    
                    # NBO Intensity: (Lg + ELG Net) / Total Amount
                    s['mf_intensity'] = (net_inflow * 10000) / total_amt_yuan if total_amt_yuan > 0 else 0
                    # ELG Net Ratio: Super-Large Net / Total Amount
                    s['elg_net_ratio'] = (elg_net * 10000) / total_amt_yuan if total_amt_yuan > 0 else 0
                    s['mf_stability'] = stability
                    
                    hsgt_str = f" | 北向Rank:{hsgt_rank}" if hsgt_rank > 0 else ""
                    mf_yi = s['money_flow_wan'] / 10000
                    s['reason'] += f" | MF: {mf_yi:.2f}亿 | 机构力度: {s['elg_net_ratio']*100:.1f}% | 持续: {stability}D{hsgt_str}"
                    final_selected.append(s)
                else:
                    logger.debug(f"Filtered out {s['name']}: Negative Money Flow ({net_inflow})")
                    
            selected = final_selected
                
        # [NEW] Overheated + MACD Filter
        if selected:
            afternoon_cfg = Config.STRATEGY.get('afternoon', {})
            max_10d = afternoon_cfg.get('max_10d_gain', 30)
            max_5d = afternoon_cfg.get('max_5d_gain', 20)
            max_ma20 = afternoon_cfg.get('max_ma20_deviation', 25)
            max_zt = afternoon_cfg.get('max_consecutive_limit_up', 2)
            
            ts_codes_check = [s['ts_code'] for s in selected]
            history_batch = self.provider.get_batch_history_data(ts_codes_check, count=60)
            
            safe_selected = []
            for s in selected:
                hist = history_batch.get(s['ts_code'], [])
                
                # Overheated check
                is_hot, reason, _, _ = self._is_overheated(hist, max_10d, max_5d, max_ma20, max_zt)
                if is_hot:
                    logger.info(f"  [HOT] Afternoon filtered: {s['name']}({s['code']}) - {reason}")
                    continue
                
                # MACD trend check
                if self._check_macd(s['ts_code'], history=hist):
                    safe_selected.append(s)
                else:
                    logger.debug(f"  Afternoon MACD fail: {s['name']}({s['code']})")
            
            logger.info(f"Afternoon filter: {len(selected)} -> {len(safe_selected)} (overheated+MACD)")
            selected = safe_selected

        # Multi-Factor Scoring (Updated with ELG, Stability and HSGT)
        aft_cfg = Config.STRATEGY.get('afternoon', {})
        for s in selected:
            # Score Components: 
            # - Change & Turnover (Base Momentum)
            # - NBO Intensity (Big Order Strength)
            # - ELG Net Ratio (Institutional Conviction - Highest Weight)
            # - MF Stability (Sustainability)
            # - HSGT Rank (Northbound Capital - V10 New Factor)
            
            # HSGT Score: If rank > 0 (in top 20), add points inversely proportional to rank
            hsgt_rank = s.get('hsgt_rank', -1)
            hsgt_score = 0
            if hsgt_rank > 0 and hsgt_rank <= 10:
                hsgt_score = (11 - hsgt_rank) * aft_cfg.get('weight_hsgt', 5)  # Rank 1 = 50pts, Rank 10 = 5pts
            elif hsgt_rank > 10:
                hsgt_score = 2  # Small bonus for being in top 20
            
            score = (s['change'] * aft_cfg.get('weight_change', 2)) + \
                    (s['turnover'] * aft_cfg.get('weight_turnover', 2)) + \
                    (s.get('mf_intensity', 0) * aft_cfg.get('weight_nbo_intensity', 20)) + \
                    (s.get('elg_net_ratio', 0) * aft_cfg.get('weight_elg_ratio', 60)) + \
                    (s.get('mf_stability', 0) * aft_cfg.get('weight_stability', 10)) + \
                    hsgt_score
            if s.get('board_context') == 'first_board':
                score += float(aft_cfg.get('first_board_bonus', 6) or 0)
                s['first_board_tag'] = f"🥇午盘首板候选|涨幅{s['change']:.1f}%|换手{s['turnover']:.1f}%"
            s['score'] = round(score, 2)
            s['hsgt_score'] = hsgt_score

        # [V21] 读取配置中的选股数量限制
        max_candidates = Config.STRATEGY.get('afternoon', {}).get('max_candidates', 3)
        # Sort by Score
        selected.sort(key=lambda x: x['score'], reverse=True)
        # [P1] LHB quick tags (best-effort)
        self._attach_lhb_tags(selected, trade_date=self.provider._get_latest_trade_date())

        # [P1] Concept resonance (best-effort)
        self._attach_concepts(selected, max_items=10)

        self._attach_combo_observe(selected, trade_date=self.provider._get_latest_trade_date(), rt_data=rt_data)

        return selected[:max_candidates]

    def analyze_sector_flow_post_market(self):
        """
        [16:00 Strategy] Daily Money Flow Summary
        Analyze TODAY's money flow to find "Super Inflow" sectors/stocks for tomorrow.
        """
        logger.info("Starting [Post-Market Flow] analysis...")
        
        # Today detection
        today = self.provider._get_latest_trade_date()
        flow_date = self.provider.resolve_moneyflow_trade_date(today)
        self.last_data_quality['post_market'] = {
            'moneyflow_date': flow_date,
            'moneyflow_preferred_date': today,
            'moneyflow_fallback': flow_date != today,
        }
        
        # [FIX] Use aggregated flow to ensure sector name match
        sector_flow = self.provider.get_sector_rank_by_aggregated_flow(days=1, end_date=flow_date)
        
        # Filter: Inflow > 0
        positive_sectors = [s for s in sector_flow if s.get('net_inflow', 0) > 0]
        positive_sectors.sort(key=lambda x: x.get('net_inflow', 0), reverse=True)
        
        top_5 = positive_sectors[:5]
        
        results = []
        filter_stats = {
            'positive_sector_count': len(positive_sectors),
            'top_sector_count': len(top_5),
            'candidate_count': 0,
            'quote_count': 0,
            'pass_close_change': 0,
            'pass_turnover': 0,
            'pass_amount': 0,
            'pass_pre_risk': 0,
            'filtered_overheated': 0,
            'filtered_macd': 0,
            'final_count': 0,
        }
        
        # Collect candidate stocks from Top 5 Sectors
        candidate_codes = []
        stock_sector_map = {} # code -> sector_name
        stock_inflow_map = {} # code -> individual stock inflow (raw)
        sector_inflow_map = {} # sector_name -> sector total inflow (raw)
        
        for sec in top_5:
            sector_name = sec['name']
            sector_inflow_map[sector_name] = sec.get('net_inflow', 0)
            # Pick Top 3 stocks from each top sector (more candidates since we'll filter)
            top_stocks = sec.get('top_stocks', [])
            for s in top_stocks[:3]: 
                code = s['code']
                candidate_codes.append(code)
                stock_sector_map[code] = sector_name
                stock_inflow_map[code] = s['net_inflow']
        filter_stats['candidate_count'] = len(candidate_codes)
                
        # Fetch Real-time (Closing) Data
        rt_data = self.provider.get_realtime_quotes(candidate_codes)
        filter_stats['quote_count'] = len([k for k in (rt_data or {}) if not str(k).startswith('_')])
        
        # Get Circulating Shares for Turnover Calc
        float_share_map = self.provider.get_circulating_share_map()
        
        pm_cfg = Config.STRATEGY.get('post_market', {})
        prev_limit_map = self._build_prev_limit_context(today)
        min_close_chg = pm_cfg.get('min_close_change', 1.0)
        min_tr = pm_cfg.get('min_turnover', 2.0)
        min_amt_yi = pm_cfg.get('min_amount_yi', 0.5)
        
        for code, data in rt_data.items():
            if str(code).startswith('_') or not isinstance(data, dict):
                continue
            # Calculate Change
            pct_chg = 0.0
            try:
                pct_chg = (data['price'] - data['pre_close']) / data['pre_close'] * 100
            except:
                pass
            
            # [FILTER] Close change threshold
            if pct_chg < min_close_chg:
                continue
            filter_stats['pass_close_change'] += 1
            
            sector_name = stock_sector_map.get(code, "Unknown")
            
            # Tushare moneyflow amount unit is 万元 (10K yuan)
            stock_inflow_raw = stock_inflow_map.get(code, 0)
            stock_inflow_wan = round(stock_inflow_raw, 2) if stock_inflow_raw else 0
            
            sec_inflow_raw = sector_inflow_map.get(sector_name, 0)
            sec_inflow_yi = round(sec_inflow_raw / 10000, 2) if sec_inflow_raw else 0
            
            # Calculate Turnover from float_share
            tr = 0.0
            f_share = float_share_map.get(code)
            if f_share and f_share > 0:
                vol_shares = data.get('vol_shares')
                if vol_shares is None:
                    vol_shares = (data.get('vol', 0) or 0) * 100
                tr = vol_shares / (f_share * 10000) * 100
            
            # Actual Trading Amount in Yi
            amt_yi = round(data['amount'] / 100000000, 2)
            
            # [FILTER] Turnover and Amount gates
            if tr < min_tr:
                continue
            filter_stats['pass_turnover'] += 1
            if amt_yi < min_amt_yi:
                continue
            filter_stats['pass_amount'] += 1

            item = {
                'code': code.split('.')[0],
                'ts_code': code,
                'name': data['name'],
                'price': data['price'],
                'change': round(pct_chg, 2),
                'turnover': round(tr, 2),
                'amount': amt_yi,
                'industry': sector_name,
                'reason': f"{sector_name}板块净流入{sec_inflow_yi}亿 | 个股净流入{stock_inflow_wan}万",
                'moneyflow_date': flow_date,
                'data_note': f"资金流日期 {flow_date}" + (f" (当日{today}暂未返回，已回退)" if flow_date != today else "")
            }
            results.append(self._attach_board_context_item(item, prev_limit_map))
        filter_stats['pass_pre_risk'] = len(results)
            
        # [NEW] Overheated + MACD Filter
        if results:
            max_10d = pm_cfg.get('max_10d_gain', 30)
            max_5d = pm_cfg.get('max_5d_gain', 20)
            max_ma20 = pm_cfg.get('max_ma20_deviation', 25)
            max_zt = pm_cfg.get('max_consecutive_limit_up', 2)
            
            ts_codes_check = [s['ts_code'] for s in results]
            history_batch = self.provider.get_batch_history_data(ts_codes_check, count=60)
            
            safe_results = []
            for s in results:
                hist = history_batch.get(s['ts_code'], [])
                
                is_hot, reason, _, _ = self._is_overheated(hist, max_10d, max_5d, max_ma20, max_zt)
                if is_hot:
                    filter_stats['filtered_overheated'] += 1
                    logger.info(f"  [HOT] Post-market filtered: {s['name']}({s['code']}) - {reason}")
                    continue
                
                if self._check_macd(s['ts_code'], history=hist):
                    safe_results.append(s)
                else:
                    filter_stats['filtered_macd'] += 1
                    logger.debug(f"  Post-market MACD fail: {s['name']}({s['code']})")
            
            logger.info(f"Post-market filter: {len(results)} -> {len(safe_results)} (overheated+MACD)")
            results = safe_results
        filter_stats['final_count'] = len(results)
        self.last_data_quality.setdefault('post_market', {})
        self.last_data_quality['post_market']['filter_stats'] = filter_stats
        logger.info(f"Post-market filter stats: {filter_stats}")

        # [V21] 读取配置中的选股数量限制
        for s in results:
            if s.get('board_context') == 'first_board':
                s['first_board_tag'] = f"🥇盘后首板候选|涨幅{s.get('change', 0):.1f}%|换手{s.get('turnover', 0):.1f}%"
        max_candidates = Config.STRATEGY.get('post_market', {}).get('max_candidates', 5)
        # Sort by Inflow
        results.sort(key=lambda x: x['amount'], reverse=True)

        # [P1] LHB quick tags (best-effort)
        self._attach_lhb_tags(results, trade_date=today)

        # [P1] Concept resonance (best-effort)
        self._attach_concepts(results, max_items=10)

        self._attach_combo_observe(results, trade_date=today, rt_data=rt_data)

        final_results = results[:max_candidates]
        if final_results:
            final_results[0]['_moneyflow_date'] = flow_date
            final_results[0]['_moneyflow_preferred_date'] = today
        return final_results

    def analyze_individual_stock(self, ts_code, buy_price=None, buy_time=None, hold_vol=None):
        """
        [V3/V4] Individual Stock Deep Analysis Mode
        ts_code: str (e.g. '000001.SZ')
        buy_price: float, optional user's buy price
        buy_time: str, optional user's buy time
        hold_vol: int, optional user's hold volume
        """
        logger.info(f"Starting V6 deep analysis for {ts_code}...")
        
        # Ensure code format
        if not ts_code.endswith('.SZ') and not ts_code.endswith('.SH') and not ts_code.endswith('.BJ'):
            if ts_code.startswith('6'): ts_code += '.SH'
            elif ts_code.startswith('8') or ts_code.startswith('4'): ts_code += '.BJ'
            else: ts_code += '.SZ'
            
        code = ts_code.split('.')[0]
        
        # 1. Base Info
        basics = self.provider.get_stock_basic()
        info = basics.get(ts_code, {'ts_code': ts_code, 'name': '未知'})

        selection_context = {}
        try:
            from core.portfolio import PortfolioManager
            selection = PortfolioManager().get_latest_selection_for_code(code, days=15)
            if selection:
                selection_context = {
                    'date': selection.get('date'),
                    'strategy': selection.get('strategy'),
                    'sel_price': selection.get('sel_price'),
                    'change_pct': selection.get('change_pct'),
                    'turnover': selection.get('turnover'),
                    'sector': selection.get('sector'),
                    'zt_result': selection.get('zt_result'),
                    'observe_status': selection.get('observe_status'),
                    'observe_reason': selection.get('observe_reason'),
                    'analysis_cycle': selection.get('analysis_cycle'),
                    'created_at': selection.get('created_at'),
                    'data_quality': selection.get('data_quality'),
                }
        except Exception as e:
            logger.debug(f"Selection context lookup failed for {code}: {e}")
        
        # 2. Real-time Quotes
        qt_map = self.provider.get_realtime_quotes([ts_code])
        qt = qt_map.get(ts_code, {})
        if not qt:
            return f"❌ 无法获取 {ts_code} 的实时行情，请检查代码是否正确。"
        try:
            daily_map = self._build_daily_map(self.provider.get_daily_data())
            qt = self._repair_realtime_quote(ts_code, qt, daily_map=daily_map, basics=basics)
        except Exception:
            pass

        # rt_k does not include turnover. Fill it from daily_basic so the
        # individual diagnosis dashboard uses the same percentage as strategies.
        try:
            qt_basic = self.provider.get_stock_valuation(ts_code)
            if isinstance(qt_basic, dict):
                turnover = qt_basic.get('turnover_rate_f') or qt_basic.get('turnover_rate')
                if turnover is not None:
                    qt['turnover_rate'] = float(turnover or 0)
        except Exception as e:
            logger.debug(f"Turnover enrichment failed for {ts_code}: {e}")

        # --- Data quality & best-effort meta ---
        def _extract_dq(obj, *, default_source="unknown", note=""):
            try:
                if isinstance(obj, dict) and '_data_quality' in obj and isinstance(obj['_data_quality'], dict):
                    dq = dict(obj['_data_quality'])
                else:
                    dq = {'source': default_source, 'fallback_used': False, 'note': '', 'ts': ''}
                if note:
                    # do not overwrite existing note unless empty
                    if not dq.get('note'):
                        dq['note'] = note
                    else:
                        dq['note'] = f"{dq.get('note')} | {note}"
                return dq
            except Exception:
                return {'source': default_source, 'fallback_used': False, 'note': note, 'ts': ''}

        data_quality = {}
        data_quality['realtime'] = _extract_dq(qt_map, default_source='api', note='Tushare realtime (cached)')

        # Prefer DataProvider's latest trade date helper for cross-section alignment
        try:
            latest_trade_date = self.provider._get_latest_trade_date()
        except Exception:
            latest_trade_date = datetime.now().strftime('%Y%m%d')

        # 3. Fetch Money Flow Data (HSGT or Individual)
        # 扩充为10日资金流累加
        flow_days = 10
        flow_map = self.provider.get_individual_money_flow([ts_code], days=flow_days)
        data_quality['moneyflow'] = _extract_dq(flow_map if isinstance(flow_map, dict) else {}, default_source='api', note=f"moneyflow aggregated ({flow_days}d)")

        stock_mf_dict = flow_map.get(ts_code, {}) if isinstance(flow_map, dict) else {}
        if isinstance(stock_mf_dict, dict):
            stock_inflow = stock_mf_dict.get('net_inflow', 0) or 0
            stock_elg_net = stock_mf_dict.get('elg_net', 0) or 0
            inflow_days = int(stock_mf_dict.get('inflow_days', 0) or 0)
        else:
            stock_inflow = stock_mf_dict or 0
            stock_elg_net = 0
            inflow_days = 0

        money_flow_str = f"净流入 {stock_inflow/10000:.2f}亿 (近{flow_days}日累计)" if stock_inflow > 0 else f"净流出 {abs(stock_inflow)/10000:.2f}亿 (近{flow_days}日累计)"
        if stock_inflow == 0:
            money_flow_str = "未检测到异常主力异动"

        money_flow_detail = {
            'days': flow_days,
            'net_inflow': stock_inflow,
            'elg_net': stock_elg_net,
            'inflow_days': inflow_days,
        }
        
        # 4. Fetch Historical Data & Tech Analysis
        from core.tech_analyzer import TechAnalyzer
        # Get last 100 days to calculate 20-day MAs and MACD accurately
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=100)).strftime('%Y%m%d')
        df_hist = self.provider.get_stock_hist(ts_code, start_date, end_date)
        data_quality['daily_hist'] = {'source': 'api', 'fallback_used': False, 'note': f"daily hist {start_date}-{end_date}", 'ts': ''}
        
        if df_hist is None or df_hist.empty:
            return f"❌ 无法获取 {ts_code} 的历史日线数据，请检查网络或频率限制。"
            
        # Add today's real-time quote to history to make indicators real-time
        # Determine latest trade date to see if we need to append
        latest_hist_date = df_hist['trade_date'].iloc[-1]
        today_date = datetime.now().strftime('%Y%m%d')
        # Simple overlay of real-time quote for today's indicator math
        # NOTE: If today is not a trading day, we still overlay for intraday diagnostics; report will show data quality markers.
        rt_row = {
            'trade_date': today_date,
            'open': qt.get('open', qt.get('close', 0)),
            'high': qt.get('high', qt.get('close', 0)),
            'low': qt.get('low', qt.get('close', 0)),
            'close': qt.get('price', qt.get('close', 0)),
            'pre_close': qt.get('pre_close', 0),
            'vol': qt.get('volume', qt.get('vol', 0)),
            'amount': qt.get('amount', 0) / 1000  # Map Sina amount to tushare scale
        }
        
        if latest_hist_date != today_date and qt.get('price', 0) > 0:
            df_hist_augmented = pd.concat([df_hist, pd.DataFrame([rt_row])], ignore_index=True)
        else:
            df_hist_augmented = df_hist
            
        # Calculate Indicators
        df_tech = TechAnalyzer.calculate_indicators(df_hist_augmented)

        # Trend strength (ADX) & risk metrics
        risk_metrics = {}
        risk_plus = {}
        trend_strength = {}

        try:
            trend_strength = self.check_trend_strength(ts_code, history=df_hist_augmented, adx_threshold=25)
        except Exception as e:
            logger.debug(f"Trend strength check failed: {e}")
            trend_strength = {}

        # Returns series (use augmented history)
        returns_all = []
        try:
            returns_all = df_hist_augmented['close'].pct_change().dropna().tolist()
        except Exception:
            returns_all = []

        if returns_all:
            try:
                from core.utils import calculate_volatility, calculate_sharpe_ratio
                risk_plus['volatility_annual'] = calculate_volatility(returns_all) * 100
                risk_plus['sharpe'] = calculate_sharpe_ratio(returns_all)
            except Exception as e:
                logger.debug(f"Risk plus metrics failed: {e}")

            try:
                # tail proxy: max daily drop / rise
                risk_plus['max_daily_drop_pct'] = min(returns_all) * 100
                risk_plus['max_daily_rise_pct'] = max(returns_all) * 100
            except Exception:
                pass

        # Gap metrics (today & recent)
        try:
            o = float(qt.get('open', 0) or 0)
            pc = float(qt.get('pre_close', 0) or 0)
            if o > 0 and pc > 0:
                risk_plus['gap_today_pct'] = (o - pc) / pc * 100
        except Exception:
            pass

        try:
            if df_hist is not None and not df_hist.empty and 'open' in df_hist.columns and 'pre_close' in df_hist.columns:
                gaps = []
                for _, r in df_hist.tail(60).iterrows():
                    try:
                        op = float(r.get('open', 0) or 0)
                        pcc = float(r.get('pre_close', 0) or 0)
                        if op > 0 and pcc > 0:
                            gaps.append((op - pcc) / pcc * 100)
                    except Exception:
                        continue
                if gaps:
                    risk_plus['gap_60d_max_abs_pct'] = max(abs(g) for g in gaps)
                    risk_plus['gap_60d_avg_abs_pct'] = sum(abs(g) for g in gaps) / len(gaps)
        except Exception:
            pass

        # ATR% (risk sizing aligned) and position sizing suggestion (best-effort)
        atr_data = None
        try:
            atr_data = self.provider.get_atr(ts_code)
            data_quality['atr'] = _extract_dq(atr_data if isinstance(atr_data, dict) else {}, default_source='api', note='provider.get_atr')
        except Exception as e:
            logger.debug(f"ATR fetch failed: {e}")

        pos_suggest = None
        try:
            from core.portfolio import PortfolioManager
            pm = PortfolioManager()
            price_for_size = float(qt.get('price', 0) or 0)
            if price_for_size > 0 and atr_data:
                pos_suggest = pm.calculate_risk_size(code=code, price=price_for_size, atr_data=atr_data, account='main')
        except Exception as e:
            logger.debug(f"Position sizing suggestion failed: {e}")

        # Risk Metrics if buy_price exists (holding-period)
        if buy_price and df_hist_augmented is not None:
            buy_date_str = buy_time.split(' ')[0].replace('/', '').replace('-', '') if buy_time else None
            if buy_date_str:
                df_held = df_hist_augmented[df_hist_augmented['trade_date'] >= buy_date_str]
            else:
                df_held = df_hist_augmented.tail(30)

            if not df_held.empty:
                from core.utils import calculate_max_drawdown, calculate_volatility
                prices = df_held['close'].tolist()
                if prices:
                    nav_list = [p / buy_price for p in prices]
                    returns = df_held['close'].pct_change().dropna().tolist()
                    risk_metrics['max_drawdown'] = calculate_max_drawdown(nav_list) * 100
                    risk_metrics['volatility'] = calculate_volatility(returns) * 100
                    risk_metrics['current_profit'] = (qt.get('price', 0) - buy_price) / buy_price * 100 if buy_price > 0 else 0

        # Attach computed risk extensions
        risk_plus['atr_data'] = atr_data
        risk_plus['position_suggest'] = pos_suggest
        
        # Index Performance / Relative Strength
        index_data = self.provider.get_index_daily('000001.SH', count=20)
        data_quality['index_daily'] = _extract_dq(index_data[0] if isinstance(index_data, list) and index_data else {}, default_source='api', note='index_daily 000001.SH (cached)')
        index_rs_str = "数据不足，无法计算相对强弱"
        if index_data and isinstance(index_data, list) and len(index_data) >= 2 and len(df_hist_augmented) >= 20:
             # Make sure we don't divide by zero
             idx_first_close = index_data[0].get('close', 1)
             idx_last_close = index_data[-1].get('close', 1)
             if idx_first_close > 0:
                 idx_ret = (idx_last_close - idx_first_close) / idx_first_close * 100
                 stk_df_20 = df_hist_augmented.tail(20)
                 stk_ret = (stk_df_20.iloc[-1]['close'] - stk_df_20.iloc[0]['close']) / stk_df_20.iloc[0]['close'] * 100
                 rs_diff = stk_ret - idx_ret
                 index_rs_str = f"个股20日涨幅: **{stk_ret:+.2f}%** | 上证20日涨幅: {idx_ret:+.2f}% | 相对强弱(Alpha): **{rs_diff:+.2f}%**"
             
        # Multi-period emulation
        multi_period = {}
        if df_tech is not None and not df_tech.empty:
            latest_tech = df_tech.iloc[-1]
            c = latest_tech['close']
            ma20 = latest_tech.get('MA20', 0)
            ma30 = latest_tech.get('MA30', 0)
            ma60 = latest_tech.get('MA60', 0)
            ma120 = latest_tech.get('MA120', 0)
            
            multi_period['daily'] = {'trend': '🟢上涨' if c > ma20 else '🔴下跌', 'status': '站上20日线' if c > ma20 else '跌破20日线'}
            if not pd.isna(ma60):
                multi_period['weekly'] = {'trend': '🟢上涨' if c > ma60 else '🔴下跌', 'status': '站上季线(周级)' if c > ma60 else '失守季线(周级)'}
            if not pd.isna(ma120):
                multi_period['monthly'] = {'trend': '🟢上涨' if c > ma120 else '🔴下跌', 'status': '站上牛熊线(月级)' if c > ma120 else '长线空头'}
        
        # Get Actionable Advice
        base_trend = TechAnalyzer.evaluate_trend(df_tech)
        advanced_advice = TechAnalyzer.get_advanced_advice(df_tech)
        actionable = TechAnalyzer.get_actionable_advice(df_tech, current_price=qt.get('price'))
        
        # [Phase 20] Calculate T+0 Quantitative Score and fetch historical win rate
        from core.portfolio import PortfolioManager
        pm = PortfolioManager()
        t0_score_data = TechAnalyzer.calculate_t0_score(df_tech, current_price=qt.get('price'))
        t0_historical_stats = pm.get_t0_stats(code=info.get('symbol'))

        volume_context = {}
        try:
            latest_vol = float(df_hist_augmented.iloc[-1].get('vol', 0) or 0)
            avg5_vol = float(df_hist_augmented['vol'].tail(6).iloc[:-1].mean() or 0) if len(df_hist_augmented) >= 6 else 0
            avg20_vol = float(df_hist_augmented['vol'].tail(21).iloc[:-1].mean() or 0) if len(df_hist_augmented) >= 21 else 0
            volume_context = {
                'turnover': qt.get('turnover_rate', qt.get('turnover')),
                'volume_ratio': qt.get('volume_ratio', qt.get('vol_ratio')),
                'amount_yi': float(qt.get('amount', 0) or 0) / 100000000,
                'latest_vol': latest_vol,
                'avg5_vol': avg5_vol,
                'avg20_vol': avg20_vol,
                'vol_vs_5d': (latest_vol / avg5_vol) if avg5_vol > 0 else None,
                'vol_vs_20d': (latest_vol / avg20_vol) if avg20_vol > 0 else None,
            }
        except Exception as e:
            logger.debug(f"Volume context build failed: {e}")

        position_context = {}
        try:
            current_price = float(qt.get('price', qt.get('close', 0)) or 0)
            source_price = float(selection_context.get('sel_price') or 0)
            buy_price_float = float(buy_price or 0)
            hold_vol_int = int(hold_vol or 0)
            position_context = {
                'buy_price': buy_price_float,
                'buy_time': buy_time,
                'hold_vol': hold_vol_int,
                'current_price': current_price,
                'market_value': current_price * hold_vol_int if hold_vol_int > 0 else None,
                'float_pnl_amount': (current_price - buy_price_float) * hold_vol_int if buy_price_float > 0 and hold_vol_int > 0 else None,
                'float_pnl_pct': ((current_price - buy_price_float) / buy_price_float * 100) if buy_price_float > 0 else None,
                'break_even_price': buy_price_float if buy_price_float > 0 else None,
                'break_even_need_pct': ((buy_price_float - current_price) / current_price * 100) if current_price > 0 and buy_price_float > 0 else None,
                'source_price': source_price if source_price > 0 else None,
                'source_slippage_pct': ((buy_price_float - source_price) / source_price * 100) if buy_price_float > 0 and source_price > 0 else None,
                'selection': selection_context,
            }
        except Exception as e:
            logger.debug(f"Position context build failed: {e}")
        
        tech_advice = {
            'trend': base_trend,
            'advanced': advanced_advice,
            'multi_period': multi_period,
            'risk_metrics': risk_metrics,
            'risk_plus': risk_plus,
            'trend_strength': trend_strength,
            'money_flow': money_flow_detail,
            'data_quality': data_quality,
            'index_rs': index_rs_str,
            'volume_context': volume_context,
            'stop_loss': actionable.get('stop_loss', 0) if actionable else 0,
            'support': actionable.get('support', 0) if actionable else 0,
            'resistance': actionable.get('resistance', 0) if actionable else 0,
            'atr': actionable.get('atr', 0) if actionable else 0,
            't0_score_data': t0_score_data,
            't0_stats': t0_historical_stats
        }

        # Merge actionable into trend for reporter compatibility
        if actionable:
            base_trend.update({
                'atr': actionable.get('atr', 0),
                'action': actionable.get('action', '观望'),
                'entry_condition': actionable.get('entry_condition', ''),
                'max_position': actionable.get('max_position', '5%')
            })
        
        # 5. [V3] Fetch Advanced Data (best-effort)
        fina = None
        daily_basic = None
        surv = None
        concepts = None
        margin = None

        try:
            fina = self.provider.get_fina_indicator(ts_code)
            data_quality['fina_indicator'] = _extract_dq(fina if isinstance(fina, dict) else {}, default_source='api', note='fina_indicator')
        except Exception as e:
            logger.debug(f"Fetch fina_indicator failed: {e}")

        try:
            # [V5] PE/PB come from daily_basic, NOT fina_indicator
            daily_basic = self.provider.get_stock_valuation(ts_code)
            data_quality['daily_basic'] = _extract_dq(daily_basic if isinstance(daily_basic, dict) else {}, default_source='api', note='daily_basic(single)')
        except Exception as e:
            logger.debug(f"Fetch daily_basic failed: {e}")

        if fina is None:
            fina = {}

        if daily_basic:
            fina['pe_ttm'] = daily_basic.get('pe_ttm', 0)
            fina['pb'] = daily_basic.get('pb', 0)
            fina['total_mv'] = daily_basic.get('total_mv', 0)

        try:
            surv = self.provider.get_stk_surv(ts_code)
            data_quality['stk_surv'] = _extract_dq(surv if isinstance(surv, dict) else {}, default_source='api', note='stk_surv')
        except Exception as e:
            logger.debug(f"Fetch stk_surv failed: {e}")

        try:
            concepts = self.provider.get_concept_detail(ts_code)
            data_quality['concept_detail'] = _extract_dq({'count': len(concepts)} if isinstance(concepts, list) else {}, default_source='api', note='concept_detail')
        except Exception as e:
            logger.debug(f"Fetch concept_detail failed: {e}")

        try:
            margin = self.provider.get_margin_data(ts_code)
            data_quality['margin'] = _extract_dq(margin if isinstance(margin, dict) else {}, default_source='api', note='margin_detail')
        except Exception as e:
            logger.debug(f"Fetch margin_data failed: {e}")

        # Extended contexts (best-effort, cached)
        northbound = {}
        lhb_recent = []
        holders = {}
        industry_context = {}

        try:
            elig = self.provider.check_hsgt_eligibility(ts_code)
            top10 = self.provider.get_hsgt_top10(trade_date=latest_trade_date)
            hit = top10.get(ts_code) if isinstance(top10, dict) else None
            northbound = {
                'eligibility': elig,
                'in_top10': True if hit else False,
                'top10': hit,
                'trade_date': latest_trade_date,
            }
            data_quality['northbound'] = _extract_dq(top10 if isinstance(top10, dict) else {}, default_source='api', note='hsgt_top10')
        except Exception as e:
            logger.debug(f"Northbound fetch failed: {e}")

        try:
            lhb_recent = self.provider.get_top_list_recent(ts_code, days=5, end_date=latest_trade_date)
            data_quality['lhb_recent'] = _extract_dq({'count': len(lhb_recent)} if isinstance(lhb_recent, list) else {}, default_source='api', note='top_list_recent')
        except Exception as e:
            logger.debug(f"LHB recent fetch failed: {e}")

        try:
            holder_num = self.provider.get_holder_number(ts_code, periods=2)
            state_fund = self.provider.check_state_fund(ts_code)
            holders = {
                'holder_number': holder_num,
                'state_fund': state_fund,
            }
            data_quality['holders'] = _extract_dq({'ok': True}, default_source='api', note='holder_number/top10_holders')
        except Exception as e:
            logger.debug(f"Holders fetch failed: {e}")

        try:
            industry = info.get('industry') or basics.get(ts_code, {}).get('industry')
            if industry:
                industry_stats = self.provider.get_industry_stats(trade_date=latest_trade_date)
                sector_rank = self.provider.get_sector_rank_by_aggregated_flow(days=10, end_date=latest_trade_date)

                # daily rank
                daily_rank_change = None
                daily_rank_mf = None
                daily_rec = None
                if isinstance(industry_stats, list) and industry_stats:
                    by_chg = sorted(industry_stats, key=lambda x: x.get('avg_change', 0), reverse=True)
                    by_mf = sorted(industry_stats, key=lambda x: x.get('net_money_flow', 0), reverse=True)
                    for idx, r in enumerate(by_chg, 1):
                        if r.get('industry') == industry:
                            daily_rank_change = idx
                            daily_rec = r
                            break
                    for idx, r in enumerate(by_mf, 1):
                        if r.get('industry') == industry:
                            daily_rank_mf = idx
                            if not daily_rec:
                                daily_rec = r
                            break

                # 10d agg rank
                agg_rank = None
                agg_rec = None
                if isinstance(sector_rank, list) and sector_rank:
                    for idx, r in enumerate(sector_rank, 1):
                        if r.get('name') == industry:
                            agg_rank = idx
                            agg_rec = r
                            break

                is_top_stock = False
                top_stocks = []
                if agg_rec and isinstance(agg_rec.get('top_stocks'), list):
                    top_stocks = agg_rec.get('top_stocks')
                    for s in top_stocks:
                        if (s.get('code') or '').startswith(code):
                            is_top_stock = True
                            break

                industry_context = {
                    'industry': industry,
                    'daily': daily_rec,
                    'daily_rank_change': daily_rank_change,
                    'daily_rank_moneyflow': daily_rank_mf,
                    'agg_days': 10,
                    'agg': agg_rec,
                    'agg_rank': agg_rank,
                    'is_top_stock': is_top_stock,
                    'top_stocks': top_stocks,
                }

                data_quality['industry'] = _extract_dq({'ok': True}, default_source='api', note='industry_stats/sector_flow_agg')
        except Exception as e:
            logger.debug(f"Industry context build failed: {e}")

        v3_data = {
            'fina': fina,
            'surv': surv,
            'concepts': concepts,
            'margin': margin,
            'northbound': northbound,
            'lhb_recent': lhb_recent,
            'holders': holders,
            'industry_context': industry_context,
        }
        
        # 6. Generate T+0 Visual Guide
        from core.chart_engine import ChartEngine
        chart_engine = ChartEngine(self.provider)
        chart_paths, t0_advice = chart_engine.generate_t0_charts(ts_code, info.get('name', '未知'), buy_price=buy_price, buy_time=buy_time, hold_vol=hold_vol)
        
        # 7. Generate Final Report via Reporter
        from core.reporter import Reporter
        reporter = Reporter()
        report_text = reporter.generate_stock_report_v4(
            info,
            qt,
            tech_advice,
            money_flow_str,
            buy_price=buy_price,
            v3_data=v3_data,
            chart_paths=chart_paths,
            t0_advice=t0_advice,
            position_context=position_context,
        )
        
        return report_text

    def monitor_watchlist(self, portfolio, write_changes: bool = True):
        """
        [V9] Watchlist Lifecycle Monitoring
        Monitor the stocks generated by previous strategies that are in '待验证' or '继续观察' state.
        Executes removal if broken, signals buys if flying, observes otherwise.
        Set write_changes=False for report-only snapshots so monitoring emails do
        not mutate strategy_selection state.
        """
        logger.info("Starting Watchlist Monitoring...")
        wl_cfg = Config.STRATEGY.get('watchlist', {}) if isinstance(Config.STRATEGY, dict) else {}
        observe_days = int(wl_cfg.get('observe_days', wl_cfg.get('days', 5)) or 5)
        expire_grace_days = int(wl_cfg.get('expire_grace_days', 3) or 3)
        watchlist = portfolio.get_watchlist(days=max(observe_days + expire_grace_days, observe_days))
        if not watchlist:
            logger.info("No active watchlist candidates to monitor.")
            return {'buy_candidates': [], 'removed': [], 'expired': [], 'observed': []}
            
        codes = [s['code'] for s in watchlist]
        
        # Mapping code to ts_code
        basics = self.provider.get_stock_basic()
        ts_codes = []
        code_map = {}
        for c in codes:
            for ts, info in basics.items():
                if ts.startswith(c):
                    ts_codes.append(ts)
                    code_map[c] = ts
                    break
                    
        rt_data = self.provider.get_realtime_quotes(ts_codes)
        history_batch = self.provider.get_batch_history_data(ts_codes, count=30) if ts_codes else {}
        daily_map = {}
        try:
            daily_map = self._build_daily_map(self.provider.get_daily_data())
        except Exception:
            daily_map = {}
        
        buy_candidates = []
        removed = []
        expired = []
        observed = []
        money_flow_stats = None

        def _age_days(item):
            raw = item.get('created_at') or item.get('date')
            try:
                if hasattr(raw, 'date'):
                    base = raw.date()
                else:
                    text = str(raw or '')[:10]
                    base = datetime.strptime(text, "%Y-%m-%d").date()
                return max(0, (datetime.now().date() - base).days)
            except Exception:
                return 0

        def _metrics_payload(info, ma5=None, ma20=None, vol_ratio=None, net_inflow=None):
            payload = {
                'price': info.get('price'),
                'sel_price': info.get('sel_price'),
                'total_chg': info.get('total_chg'),
                'change': info.get('change'),
                'age_days': info.get('age_days'),
            }
            if ma5 is not None:
                payload['ma5'] = float(ma5)
            if ma20 is not None:
                payload['ma20'] = float(ma20)
            if vol_ratio is not None:
                payload['vol_ratio'] = float(vol_ratio)
            if net_inflow is not None:
                payload['net_inflow'] = float(net_inflow)
            return payload
        
        for item in watchlist:
            code = item['code']
            ts_code = code_map.get(code)
            if not ts_code or ts_code not in rt_data:
                continue
                
            qt = self._repair_realtime_quote(ts_code, rt_data[ts_code], daily_map=daily_map, basics=basics)
            price = float(qt.get('price', 0))
            if price <= 0: continue
            
            pre_close = float(qt.get('pre_close', 0))
            
            # Fetch history to calc MA5 and MA20
            history = history_batch.get(ts_code) or self.provider.get_history_data(ts_code, count=30)
            if len(history) < 20: continue
            
            df = pd.DataFrame(history)
            df['close'] = df['close'].astype(float)
            
            # Overlay current price for precise real-time MAs
            current_row = pd.DataFrame([{'close': price}])
            df = pd.concat([df, current_row], ignore_index=True)
            
            ma5 = df['close'].rolling(window=5).mean().iloc[-1]
            ma20 = df['close'].rolling(window=20).mean().iloc[-1]
            
            # Strategy transitions
            sel_date = item['date']
            sel_price_raw = item.get('sel_price')
            sel_price = float(sel_price_raw) if sel_price_raw is not None else 0.0
            if sel_price <= 0:
                 sel_price = price
                 
            total_chg = (price - sel_price) / sel_price * 100 if sel_price > 0 else 0
            
            # Common info payload
            info = {
                'code': code,
                'ts_code': ts_code,
                'name': item['name'],
                'price': price,
                'change': (price - pre_close) / pre_close * 100 if pre_close > 0 else 0,
                'date': sel_date,
                'sel_price': sel_price,
                'total_chg': total_chg,
                'industry': item.get('sector', '其他'),
                'strategy': item.get('strategy', '未知策略'),
                'board_context': item.get('board_context') or ('first_board' if item.get('strategy') in ['集合竞价', '龙头跟踪'] and str(item.get('tags_json', '')).find('FIRST_BOARD') >= 0 else 'unknown'),
                'is_first_board_candidate': bool(item.get('is_first_board_candidate', False)),
                'age_days': _age_days(item),
                'observe_status': item.get('observe_status') or 'ACTIVE',
                'observe_reason': item.get('observe_reason') or '',
            }
            try:
                combo_fields = self.combo_adapter.annotate_watchlist_item(item, qt)
                if isinstance(combo_fields, dict) and combo_fields:
                    info.update(combo_fields)
            except Exception as e:
                logger.debug(f"combo observe annotate failed for {code}: {e}")

            # 1. Removal Condition: Price clearly drops below MA20 (e.g. 0.5% below to prevent noise)
            if price < ma20 * 0.995:
                info['reason'] = f'弱势破位: 跌破20日均线 ({ma20:.2f})'
                removed.append(info)
                if write_changes:
                    portfolio.update_zt_result(item['date'], code, '已剔除', strategy=item.get('strategy'))
                    portfolio.update_selection_observe_status(
                        item['date'],
                        code,
                        'REMOVED',
                        strategy=item.get('strategy'),
                        reason=info['reason'],
                        metrics=_metrics_payload(info, ma5=ma5, ma20=ma20),
                    )
                    logger.info(f"Watchlist: {item['name']} 破位MA20，已剔除")
                else:
                    logger.info(f"Watchlist: {item['name']} 破位MA20，报告模式仅提示剔除")
            elif info.get('board_context') == 'first_board' and total_chg <= Config.STRATEGY.get('watchlist', {}).get('first_board_remove_below_sel_pct', -3.0):
                info['reason'] = f"首板转弱: 相对入选价回撤 {total_chg:.1f}%"
                removed.append(info)
                if write_changes:
                    portfolio.update_zt_result(item['date'], code, '已剔除', strategy=item.get('strategy'))
                    portfolio.update_selection_observe_status(
                        item['date'],
                        code,
                        'REMOVED',
                        strategy=item.get('strategy'),
                        reason=info['reason'],
                        metrics=_metrics_payload(info, ma5=ma5, ma20=ma20),
                    )
                    logger.info(f"Watchlist: {item['name']} 首板承接转弱，已剔除")
                else:
                    logger.info(f"Watchlist: {item['name']} 首板承接转弱，报告模式仅提示剔除")
                continue
            elif price >= ma5:
                # [NEW] Check Money Flow for Watchlist (Institutional Support)
                if money_flow_stats is None:
                    mf_days = Config.INDIVIDUAL_FLOW_DAYS
                    money_flow_stats = self.provider.get_individual_money_flow(ts_codes, days=mf_days)
                mf_stats = money_flow_stats.get(ts_code, {})
                net_inflow = mf_stats.get('net_inflow', 0)
                
                # [Filter] Must have positive big money inflow in last N days
                if net_inflow <= 0:
                    info['reason'] = f'震荡整理: 站上MA5但主力筹码松动 (MF: {net_inflow/10000:.2f}亿)'
                    if info['age_days'] > observe_days:
                        info['reason'] = f"观测期满未触发买点: 已观察{info['age_days']}天，站上MA5但资金未确认"
                        expired.append(info)
                        if write_changes:
                            portfolio.update_selection_observe_status(
                                item['date'],
                                code,
                                'EXPIRED',
                                strategy=item.get('strategy'),
                                reason=info['reason'],
                                metrics=_metrics_payload(info, ma5=ma5, ma20=ma20, net_inflow=net_inflow),
                            )
                    else:
                        observed.append(info)
                        if write_changes:
                            portfolio.update_selection_observe_status(
                                item['date'],
                                code,
                                'WATCHING',
                                strategy=item.get('strategy'),
                                reason=info['reason'],
                                metrics=_metrics_payload(info, ma5=ma5, ma20=ma20, net_inflow=net_inflow),
                            )
                    continue

                # Calculate factors for scoring
                # [V18] Standardize Volume Ratio calculation using time-normalized logic
                curr_vol = float(qt.get('vol', 0))
                vol_ratio = self.calculate_volume_ratio(ts_code, curr_vol, history=history[-5:])
                wl_cfg = Config.STRATEGY.get('watchlist', {})
                if info.get('board_context') == 'first_board':
                    fb_min_vr = wl_cfg.get('first_board_min_vol_ratio', 1.5)
                    fb_min_chg = wl_cfg.get('first_board_min_intraday_change', 0.5)
                    fb_max_pullback = wl_cfg.get('first_board_max_pullback_from_sel', -2.0)
                    if vol_ratio < fb_min_vr or info['change'] < fb_min_chg or total_chg < fb_max_pullback:
                        info['reason'] = f'首板确认不足: 量比{vol_ratio:.1f}, 当日涨幅{info["change"]:.1f}%, 相对入选{total_chg:.1f}%'
                        if info['age_days'] > observe_days:
                            info['reason'] = f"观测期满未触发买点: 已观察{info['age_days']}天，首板确认不足"
                            expired.append(info)
                            if write_changes:
                                portfolio.update_selection_observe_status(
                                    item['date'],
                                    code,
                                    'EXPIRED',
                                    strategy=item.get('strategy'),
                                    reason=info['reason'],
                                    metrics=_metrics_payload(info, ma5=ma5, ma20=ma20, vol_ratio=vol_ratio, net_inflow=net_inflow),
                                )
                        else:
                            observed.append(info)
                            if write_changes:
                                portfolio.update_zt_result(item['date'], code, '继续观察', strategy=item.get('strategy'))
                                portfolio.update_selection_observe_status(
                                    item['date'],
                                    code,
                                    'WATCHING',
                                    strategy=item.get('strategy'),
                                    reason=info['reason'],
                                    metrics=_metrics_payload(info, ma5=ma5, ma20=ma20, vol_ratio=vol_ratio, net_inflow=net_inflow),
                                )
                        continue
                dist_ma5_pct = (price - ma5) / ma5 if ma5 > 0 else 0

                # MF Intensity for Watchlist
                total_amt_yuan = (price * curr_vol)
                mf_intensity = net_inflow / total_amt_yuan if total_amt_yuan > 0 else 0

                # Dynamic Scoring for Watchlist
                w_chg = wl_cfg.get('weight_change', 4)
                w_vol = wl_cfg.get('weight_vol_ratio', 5)
                w_mf = wl_cfg.get('weight_mf_intensity', 30)
                p_dist = wl_cfg.get('penalty_dist_ma5', 20)

                # Score = (Change * W) + (VolRatio * W) + (MF_Intensity * W) - (DistMA5 * P)
                score = (info['change'] * w_chg) + (vol_ratio * w_vol) + (mf_intensity * w_mf) - (dist_ma5_pct * p_dist)
                if info.get('board_context') == 'first_board':
                    score += float(wl_cfg.get('first_board_buy_score_bonus', 5) or 0)
                
                info['score'] = round(score, 2)
                info['vol_ratio'] = round(vol_ratio, 2)
                info['mf_intensity'] = round(mf_intensity * 100, 2)
                info['reason'] = f'向上突破 [资金强]: 站上5日线, 量比:{vol_ratio:.1f}, MF强度:{info["mf_intensity"]}%'
                buy_candidates.append(info)
                if write_changes:
                    portfolio.update_selection_observe_status(
                        item['date'],
                        code,
                        'PENDING',
                        strategy=item.get('strategy'),
                        reason=info['reason'],
                        metrics=_metrics_payload(info, ma5=ma5, ma20=ma20, vol_ratio=vol_ratio, net_inflow=net_inflow),
                    )
                logger.info(f"Watchlist: {item['name']} 形态&资金均满足, 评分: {info['score']}")
                
            # 3. Observe Condition: Consolidating
            else:
                info['reason'] = f'震荡整理: 现价在MA20({ma20:.2f})上方'
                if info['age_days'] > observe_days:
                    info['reason'] = f"观测期满未触发买点: 已观察{info['age_days']}天，仍未站上MA5"
                    expired.append(info)
                    if write_changes:
                        portfolio.update_selection_observe_status(
                            item['date'],
                            code,
                            'EXPIRED',
                            strategy=item.get('strategy'),
                            reason=info['reason'],
                            metrics=_metrics_payload(info, ma5=ma5, ma20=ma20),
                        )
                else:
                    observed.append(info)
                    if write_changes:
                        portfolio.update_zt_result(item['date'], code, '继续观察', strategy=item.get('strategy'))
                        portfolio.update_selection_observe_status(
                            item['date'],
                            code,
                            'WATCHING',
                            strategy=item.get('strategy'),
                            reason=info['reason'],
                            metrics=_metrics_payload(info, ma5=ma5, ma20=ma20),
                        )
                
        # [V21] 读取配置中的选股数量限制
        max_candidates = Config.STRATEGY.get('watchlist', {}).get('max_candidates', 3)
        # Sort buy candidates by Score (Multi-Factor)
        buy_candidates.sort(key=lambda x: x.get('score', 0), reverse=True)

        # [P1] Concept resonance for buy candidates (best-effort)
        self._attach_concepts(buy_candidates, max_items=max_candidates)

        try:
            concept_heat = self._compute_concept_heat(buy_candidates[:max_candidates], top_n=10)
        except Exception:
            concept_heat = {}

        return {
            'buy_candidates': buy_candidates[:max_candidates],
            'removed': removed,
            'expired': expired,
            'observed': observed,
            'concept_heat': concept_heat,
        }

    # [V15] 宏观联动参数
    MACRO_BOOST = {
        "oil_threshold": 2.0,  # 原油涨幅>2%触发
        "oil_bonus": 15,  # 石油板块+15分
        "usd_volatility_threshold": 1.0,  # 美元指数波动>1%
        "sl_tighten": 0.01,  # 止损收紧1%
    }

    def _read_macro_cache(self):
        """
        [V15] Read macro cache file - Zero network request!
        Returns dict with macro data
        """
        import json
        import os
        from datetime import datetime
        
        cache_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "macro_cache.json")
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            
            # Check if cache is from today
            today = datetime.now().strftime('%Y%m%d')
            if cache.get('date') == today:
                return cache
        except:
            pass
        return None

    def _apply_macro_boost_to_candidates(self, candidates):
        """
        [V15] Apply macro boost/penalty from cache - Zero network request!
        """
        cache = self._read_macro_cache()
        if not cache:
            return candidates
        
        oil_triggered = cache.get('oil_triggered', False)
        overseas_alert = cache.get('overseas_alert', False)
        
        if oil_triggered:
            oil_sectors = ['石油开采', '石油加工', '石油贸易', '油气改革', '供气供热', '水运']
            # In old cache structure it's oil_change, in new one it's oil_data.pct_change
            oil_change = cache.get('oil_change', cache.get('oil_data', {}).get('pct_change', 0))
            logger.info(f"📈 Macro cache hit: oil_triggered with {oil_change}%")
            
        if overseas_alert:
            logger.warning(f"⚠️ 海外异动警报触发: {', '.join(cache.get('alert_reasons', []))}")
            
        for c in candidates:
            # 1. 负面外盘联动：海外暴跌/汇率急贬，全局扣除 10 分，压制高分股
            if overseas_alert:
                c['score'] -= 10
                c['macro_penalty'] = -10
                # 如果没达到0分以下也降一个名次
                
            # 2. 原油暴涨联动：石油相关板块+15分
            if oil_triggered:
                industry = c.get('industry', '')
                if industry in oil_sectors:
                    c['score'] += 15
                    c['macro_boost'] = 15
                    logger.info(f"  🌐 {c['name']}: 宏观联动+15分 (原油大涨)")
        
        return candidates
