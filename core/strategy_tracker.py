# -*- coding: utf-8 -*-
"""
Strategy Tracker
Tracks performance of pre-market selections
"""
from __future__ import annotations

import logging
import json
import math
from datetime import datetime, timedelta
from core.config import Config

logger = logging.getLogger("StockAnalyzer.Tracker")

class StrategyTracker:
    def __init__(self, portfolio_manager, data_provider):
        self.db = portfolio_manager
        self.provider = data_provider

    # -----------------------------
    # Observation-only: time bucket
    # -----------------------------
    _TIME_BUCKETS = [
        # bucket, label, (start_hm inclusive), (end_hm exclusive)
        ("B1", "09:30-10:00", (9, 30), (10, 0)),
        ("B2", "10:00-11:30", (10, 0), (11, 30)),
        ("B3", "13:00-14:00", (13, 0), (14, 0)),
        ("B4", "14:00-14:40", (14, 0), (14, 40)),
        ("B5", "14:40-15:00", (14, 40), (15, 1)),
    ]

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        """Simple percentile (0-100) with linear interpolation."""
        if not values:
            return 0.0
        xs = sorted(float(x) for x in values)
        if len(xs) == 1:
            return float(xs[0])
        p = max(0.0, min(100.0, float(p)))
        k = (len(xs) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return float(xs[int(k)])
        d0 = xs[int(f)] * (c - k)
        d1 = xs[int(c)] * (k - f)
        return float(d0 + d1)

    @classmethod
    def _get_time_bucket(cls, created_at) -> tuple[str | None, str | None]:
        """Return (bucket, label) for a datetime/str created_at."""
        if not created_at:
            return (None, None)

        hh = None
        mm = None
        try:
            if hasattr(created_at, 'hour'):
                hh = int(created_at.hour)
                mm = int(created_at.minute)
            elif isinstance(created_at, str) and len(created_at) >= 16:
                hh = int(created_at[11:13])
                mm = int(created_at[14:16])
        except Exception:
            hh = mm = None

        if hh is None or mm is None:
            return (None, None)

        for b, label, (sh, sm), (eh, em) in cls._TIME_BUCKETS:
            start_ok = (hh > sh) or (hh == sh and mm >= sm)
            end_ok = (hh < eh) or (hh == eh and mm < em)
            if start_ok and end_ok:
                return (b, label)

        return (None, None)

    @staticmethod
    def _extract_tags(tags_json):
        """Parse tags_json (list json) and return unique tag strings."""
        if not tags_json:
            return []
        try:
            obj = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
        except Exception:
            return []
        if not isinstance(obj, list):
            return []
        tags = []
        for it in obj:
            if isinstance(it, dict):
                t = it.get('tag')
            else:
                t = it
            if t:
                tags.append(str(t))
        # de-dupe while keeping order
        seen = set()
        out = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _build_tag_daily_stats(self, selections):
        """Build per-tag performance aggregation from in-memory tracking results."""
        agg = {}
        for s in selections or []:
            max_ret = float(s.get('max_t1_return', 0.0) or 0.0)
            close_ret = float(s.get('close_t1_return', 0.0) or 0.0)
            is_win = 1 if max_ret > 0.02 else 0

            for tag in self._extract_tags(s.get('tags_json')):
                a = agg.get(tag)
                if not a:
                    a = {
                        'tag': tag,
                        'cnt': 0,
                        'win_cnt': 0,
                        'sum_max_ret': 0.0,
                        'sum_close_ret': 0.0,
                        'max_max_ret': None,
                        'min_close_ret': None,
                    }
                    agg[tag] = a
                a['cnt'] += 1
                a['win_cnt'] += is_win
                a['sum_max_ret'] += max_ret
                a['sum_close_ret'] += close_ret
                a['max_max_ret'] = max_ret if a['max_max_ret'] is None else max(a['max_max_ret'], max_ret)
                a['min_close_ret'] = close_ret if a['min_close_ret'] is None else min(a['min_close_ret'], close_ret)

        out = []
        for tag, a in agg.items():
            cnt = int(a['cnt'] or 0)
            if cnt <= 0:
                continue
            out.append({
                'tag': tag,
                'cnt': cnt,
                'win_cnt': int(a['win_cnt'] or 0),
                'win_rate': float(a['win_cnt'] / cnt),
                'avg_max_ret': float(a['sum_max_ret'] / cnt),
                'avg_close_ret': float(a['sum_close_ret'] / cnt),
                'max_max_ret': float(a['max_max_ret'] or 0.0),
                'min_close_ret': float(a['min_close_ret'] or 0.0),
            })
        return out

    def _build_time_bucket_weather_daily_stats(self, selections):
        """Build time-bucket × weather aggregation from in-memory tracking results.

        Uses:
          - created_at (selection time)
          - weather (from market_sentiment_daily join in save_performance_to_db)
          - max_t1_return / close_t1_return
          - analysis_cycle
        """

        agg = {}
        for s in selections or []:
            max_ret = float(s.get('max_t1_return', 0.0) or 0.0)
            close_ret = float(s.get('close_t1_return', 0.0) or 0.0)
            is_win = 1 if max_ret > 0.02 else 0

            created_at = s.get('created_at')
            bucket, label = self._get_time_bucket(created_at)
            if not bucket:
                continue

            weather = s.get('weather')
            if not weather:
                continue

            cycle = s.get('analysis_cycle', 'T+1') or 'T+1'

            k = (str(cycle), str(weather), str(bucket))
            a = agg.get(k)
            if not a:
                a = {
                    'analysis_cycle': str(cycle),
                    'weather': str(weather),
                    'time_bucket': str(bucket),
                    'bucket_label': str(label or ''),
                    'cnt': 0,
                    'win_cnt': 0,
                    'sum_max_ret': 0.0,
                    'sum_close_ret': 0.0,
                    'max_max_ret': None,
                    'min_close_ret': None,
                    'close_rets': [],
                }
                agg[k] = a

            a['cnt'] += 1
            a['win_cnt'] += is_win
            a['sum_max_ret'] += max_ret
            a['sum_close_ret'] += close_ret
            a['max_max_ret'] = max_ret if a['max_max_ret'] is None else max(a['max_max_ret'], max_ret)
            a['min_close_ret'] = close_ret if a['min_close_ret'] is None else min(a['min_close_ret'], close_ret)
            a['close_rets'].append(close_ret)

        out = []
        for _, a in agg.items():
            cnt = int(a['cnt'] or 0)
            if cnt <= 0:
                continue
            p5 = self._percentile(a.get('close_rets') or [], 5)
            out.append({
                'analysis_cycle': a['analysis_cycle'],
                'weather': a['weather'],
                'time_bucket': a['time_bucket'],
                'bucket_label': a.get('bucket_label') or '',
                'cnt': cnt,
                'win_cnt': int(a['win_cnt'] or 0),
                'win_rate': float(a['win_cnt'] / cnt),
                'avg_max_ret': float(a['sum_max_ret'] / cnt),
                'avg_close_ret': float(a['sum_close_ret'] / cnt),
                'max_max_ret': float(a['max_max_ret'] or 0.0),
                'min_close_ret': float(a['min_close_ret'] or 0.0),
                'p5_close_ret': float(p5 or 0.0),
            })

        return out

    def _build_first_board_comparison(self, selections):
        """Build a simple first-board vs non-first-board comparison from in-memory results."""
        groups = {
            'first_board': {'label': '首板标签组', 'cnt': 0, 'win_cnt': 0, 'sum_max_ret': 0.0, 'sum_close_ret': 0.0},
            'non_first_board': {'label': '非首板组', 'cnt': 0, 'win_cnt': 0, 'sum_max_ret': 0.0, 'sum_close_ret': 0.0},
        }
        for s in selections or []:
            max_ret = float(s.get('max_t1_return', 0.0) or 0.0)
            close_ret = float(s.get('close_t1_return', 0.0) or 0.0)
            is_win = 1 if max_ret > 0.02 else 0
            tags = self._extract_tags(s.get('tags_json'))
            has_first_board = any(str(t).startswith('FIRST_BOARD') for t in tags)
            key = 'first_board' if has_first_board else 'non_first_board'
            g = groups[key]
            g['cnt'] += 1
            g['win_cnt'] += is_win
            g['sum_max_ret'] += max_ret
            g['sum_close_ret'] += close_ret

        out = []
        for key in ('first_board', 'non_first_board'):
            g = groups[key]
            cnt = int(g['cnt'] or 0)
            if cnt <= 0:
                continue
            out.append({
                'group': key,
                'label': g['label'],
                'cnt': cnt,
                'win_cnt': int(g['win_cnt'] or 0),
                'win_rate': float(g['win_cnt'] / cnt),
                'avg_max_ret': float(g['sum_max_ret'] / cnt),
                'avg_close_ret': float(g['sum_close_ret'] / cnt),
            })
        return out

    def save_performance_to_db(self, date, results):
        """Save detailed performance data to database table"""
        conn = self.db._get_connection()
        if not conn: return
        
        try:
            with conn.cursor() as cursor:
                # Clear existing for all involved dates (T+1 + T+2) to avoid dupes if re-run
                check_dates = sorted({(s.get('_check_date') or date) for s in (results or [])})
                if check_dates:
                    placeholders = ",".join(["%s"] * len(check_dates))
                    cursor.execute(f"DELETE FROM strategy_performance_history WHERE date IN ({placeholders})", tuple(check_dates))

                # Load market weather by date (best-effort)
                weather_by_date = {}
                try:
                    if check_dates:
                        placeholders = ",".join(["%s"] * len(check_dates))
                        cursor.execute(
                            f"SELECT trade_date, weather FROM market_sentiment_daily WHERE trade_date IN ({placeholders})",
                            tuple(check_dates),
                        )
                        for r in cursor.fetchall() or []:
                            weather_by_date[r.get('trade_date')] = r.get('weather')
                except Exception:
                    weather_by_date = {}

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO strategy_performance_history
                         (date, strategy, code, name, buy_price, max_price, close_price, max_ret, close_ret, status,
                          analysis_cycle, snapshot_id, tags_json, weather,
                          created_at)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                         
                data = []
                for s in results:
                    # Logic to determine buy price
                    # - T+1: use selection price
                    # - T+2: use T-1 open as proxy entry price when available (attached in run_daily_check)
                    sel_p = s.get('buy_price') or s.get('sel_price') or s.get('price', 0)
                    
                    # Logic to determine max/close from T+1 check
                    # These were calculated in run_daily_check and attached to s
                    max_p = s.get('t1_high', 0)
                    close_p = s.get('t1_close', 0)
                    
                    check_date = s.get('_check_date', date)
                    w = weather_by_date.get(check_date)
                    s['weather'] = w
                    data.append((
                        check_date,
                        s.get('strategy', '未知'),
                        s['code'],
                        s['name'],
                        float(sel_p),
                        float(max_p),
                        float(close_p),
                        float(s.get('max_t1_return', 0)),
                        float(s.get('close_t1_return', 0)),
                        s.get('result', '待定'),
                        s.get('analysis_cycle', 'T+1'),
                        s.get('snapshot_id'),
                        s.get('tags_json'),
                        w,
                        now,
                    ))
                    
                if data:
                    cursor.executemany(sql, data)
                conn.commit()
                logger.info(f"Saved database performance records for {date}.")
        except Exception as e:
            logger.error(f"Failed to save performance to DB: {e}")
        finally:
            conn.close()

    def run_daily_check(self):
        """Check yesterday's selections and update stats (V17: Support T+1 and T+2)"""
        logger.info("Running daily strategy check...")
        
        today = datetime.now().strftime("%Y%m%d")
        
        # 1. Determine Previous Trading Days (Need at least 2 for post_market T+2 check)
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        cal = self.provider.get_trade_cal(start_date=start_date, end_date=today)
        
        # Filter for open trading days
        open_days = [d['cal_date'] for d in cal if d['is_open'] == 1]
        open_days.sort()
        
        if not open_days:
            logger.error("Could not fetch trading calendar")
            return
            
        # Identify T-1 and T-2
        if today in open_days:
            idx = open_days.index(today)
            if idx >= 1:
                t_minus_1 = open_days[idx-1]
                t_minus_2 = open_days[idx-2] if idx >= 2 else t_minus_1
            else:
                t_minus_1 = t_minus_2 = open_days[0]
        else:
            t_minus_1 = open_days[-1]
            t_minus_2 = open_days[-2] if len(open_days) >= 2 else t_minus_1
             
        t1_fmt = f"{t_minus_1[:4]}-{t_minus_1[4:6]}-{t_minus_1[6:]}"
        t2_fmt = f"{t_minus_2[:4]}-{t_minus_2[4:6]}-{t_minus_2[6:]}"
        logger.info(f"Tracking: T+1 from {t1_fmt} | T+2 from {t2_fmt}")
        
        # 2. Load Selections with Time-Aware Offsets [V18]
        # T+1: Picks from T-1 (Yesterday)
        # T+2: Picks from T-2 (Day before Yesterday)
        
        all_selections = []
        
        # Load T+1 candidates (Auction, Intraday, Afternoon) from T-1
        t1_sels = self.db.get_selections_by_cycle(t1_fmt, cycle='T+1')
        for s in t1_sels:
            s['_check_date'] = t1_fmt
            all_selections.append(s)
            
        # Load T+2 candidates (Post-market) from T-2
        t2_sels = self.db.get_selections_by_cycle(t2_fmt, cycle='T+2')
        for s in t2_sels:
            s['_check_date'] = t2_fmt
            all_selections.append(s)
            
        if not all_selections:
            logger.info("No selections found to track for today.")
            return {}

        # 3. Get Today's Price Data for All Candidates
        ts_codes = []
        for s in all_selections:
            # Map code to ts_code if needed
            if '.' in s['code']:
                ts_codes.append(s['code'])
            else:
                code_prefix = s['code'][0]
                if code_prefix == '6':
                    suffix = '.SH'
                elif code_prefix in ('4', '8', '9'):
                    suffix = '.BJ'
                else:
                    suffix = '.SZ'
                ts_codes.append(f"{s['code']}{suffix}")
        
        # Get Limit List for TODAY
        limit_data = self.provider.get_limit_list(today)
        limit_codes = set(s['ts_code'].split('.')[0] for s in limit_data) if limit_data else set()
        
        # If limit list API fails, try checking daily data
        if not limit_data:
            daily_data = self.provider.get_daily_data(today)
            if daily_data:
                 limit_codes = set(s.get('ts_code', '').split('.')[0] for s in daily_data if float(s.get('pct_chg', 0)) >= 9.5)

        # Get TODAY's data for performance check (Yesterday/T-2 selections vs Today's high/close)
        today_daily = self.provider.get_daily_data(today)
        today_data_map = {}
        if today_daily:
            for s in today_daily:
                today_data_map[s['ts_code'].split('.')[0]] = s
        else:
            # Fallback to realtime quotes if daily data is not available yet
            logger.info("Falling back to real-time quotes for today's performance check")
            ts_codes = []
            for s in all_selections:
                if 'ts_code' in s and s['ts_code']:
                    ts_codes.append(s['ts_code'])
                elif 'code' in s:
                    code_prefix = s['code'][0]
                    if code_prefix == '6':
                        suffix = '.SH'
                    elif code_prefix in ('4', '8', '9'):
                        suffix = '.BJ'
                    else:
                        suffix = '.SZ'
                    ts_codes.append(f"{s['code']}{suffix}")

            rt_data = self.provider.get_realtime_quotes(ts_codes)
            for ts_code, qt in rt_data.items():
                code = ts_code.split('.')[0]
                curr_price = float(qt.get('price', 0))
                pre_close = float(qt.get('pre_close', curr_price))
                today_data_map[code] = {
                    'close': curr_price,
                    'high': float(qt.get('high', curr_price))
                }
                # Check Limit Up roughly based on pre_close
                if pre_close > 0 and ((curr_price - pre_close) / pre_close) >= 0.095:
                    limit_codes.add(code)

        # For T+2: buy happens on T-1 (yesterday). Use yesterday open as entry price if available.
        yday_daily = None
        try:
            yday_daily = self.provider.get_daily_data(t_minus_1)
        except Exception:
            yday_daily = None
        yday_open_map = {}
        if yday_daily:
            for r in yday_daily:
                try:
                    yday_open_map[r['ts_code'].split('.')[0]] = float(r.get('open', 0) or 0)
                except Exception:
                    continue

        # 4. Calculate Stats Grouped by Strategy and Cycle
        # Initialize all_stats for all strategies found in the selections
        all_stats = {}
        for s in all_selections:
            strat = s.get('strategy', '未知')
            if strat not in all_stats:
                all_stats[strat] = {'total': 0, 'zt_count': 0, 'success_count': 0, 'success_rate': 0, 'cycle': s.get('analysis_cycle', 'T+1')}
            
            all_stats[strat]['total'] += 1
            code = s['code']
            
            # Fetch buy price
            # For T+1: use selection price (approx buy time)
            # For T+2: selection is from T-2, but the entry happens on T-1 (yesterday).
            # Use yesterday (T-1) open as a conservative proxy of entry price when available.
            sel_price = s.get('sel_price') or s.get('price', 0)
            buy_price = float(sel_price or 0)
            if (s.get('analysis_cycle') == 'T+2'):
                yopen = yday_open_map.get(code)
                if yopen and float(yopen) > 0:
                    buy_price = float(yopen)
            
            # Get Today's Action
            t1_data = today_data_map.get(code)
            is_zt = code in limit_codes
            
            if t1_data:
                if buy_price <= 0:
                    buy_price = float(t1_data.get('pre_close', 0))

                if buy_price > 0:
                    t1_high = float(t1_data.get('high', 0))
                    t1_close = float(t1_data.get('close', 0))
                    max_t1_return = (t1_high - buy_price) / buy_price
                    close_t1_return = (t1_close - buy_price) / buy_price

                    # Store raw prices
                    s['t1_high'] = t1_high
                    s['t1_close'] = t1_close
                else:
                    max_t1_return = 0.0
                    close_t1_return = 0.0

                # Expose buy_price for downstream persistence/reporting
                s['buy_price'] = buy_price
            else:
                max_t1_return = 0.0
                close_t1_return = 0.0
                # Still expose buy_price best-effort for DB/reporting
                s['buy_price'] = buy_price
            
            s['max_t1_return'] = max_t1_return
            s['close_t1_return'] = close_t1_return
            s['is_zt'] = is_zt
            
            # Determine Result Status
            if is_zt:
                status = "涨停"
                all_stats[strat]['zt_count'] += 1
                all_stats[strat]['success_count'] += 1
            elif max_t1_return > 0.02:
                status = "吃肉"
                all_stats[strat]['success_count'] += 1
            elif max_t1_return >= 0:
                status = "震荡"
            else:
                status = "吃面"
                
            self.db.update_zt_result(
                s.get('_check_date', t1_fmt),
                code,
                status,
                strategy=strat,
                metrics={
                    'analysis_cycle': s.get('analysis_cycle', 'T+1'),
                    'buy_price': s.get('buy_price'),
                    't1_high': s.get('t1_high'),
                    't1_close': s.get('t1_close'),
                    'max_t1_return': max_t1_return,
                    'close_t1_return': close_t1_return,
                    'is_zt': is_zt,
                },
            )
            s['result'] = status
            
        # Finalize success rates
        for strat in all_stats:
            if all_stats[strat]['total'] > 0:
                all_stats[strat]['success_rate'] = (all_stats[strat]['success_count'] / all_stats[strat]['total'] * 100)
            
        # Aggregate DB stats
        total_selected = sum(st['total'] for st in all_stats.values())
        total_zt = sum(st['zt_count'] for st in all_stats.values())
        total_success = sum(st['success_count'] for st in all_stats.values())
        agg_rate = (total_success / total_selected * 100) if total_selected > 0 else 0
        
        agg_stats = {
            'total': total_selected,
            'zt_count': total_zt,
            'success_count': total_success,
            'success_rate': agg_rate
        }
        
        self.db.save_stats(t1_fmt, agg_stats, strategy_name="当日汇总") # Use T-1 as anchor for aggregated report
            
        logger.info(f"Check complete. Stats: {all_stats}")
        
        # Save Performance to Database
        self.save_performance_to_db(t1_fmt, all_selections)

        # [VNext] Aggregate tag-level performance stats (best-effort, observation-only)
        tag_stats = []
        try:
            tag_stats = self._build_tag_daily_stats(all_selections)
            if tag_stats:
                self.db.save_factor_tag_daily_stats(t1_fmt, tag_stats)
        except Exception:
            tag_stats = []

        # [VNext] Aggregate time-bucket × weather stats (best-effort, observation-only)
        tb_stats = []
        try:
            tb_stats = self._build_time_bucket_weather_daily_stats(all_selections)
            if tb_stats:
                self.db.save_time_bucket_weather_daily_stats(t1_fmt, tb_stats)
        except Exception:
            tb_stats = []

        # [VNext] First-board vs non-first-board comparison (observation-only)
        first_board_comparison = []
        try:
            first_board_comparison = self._build_first_board_comparison(all_selections)
        except Exception:
            first_board_comparison = []

        # Expose in memory for reporter (so report works even if DB write is blocked)
        time_bucket_weather_stats = tb_stats

        # --- Lifecycle Tracking Block [V9] ---
        recent_selections = self.db.get_recent_selections(days=5)
        positions = self.db.load_positions()
        pos_map = {p['code']: p for p in positions}
        
        lifecycle = {
            'observing': [],
            'bought': [],
            'removed': []
        }
        
        for s in recent_selections:
            observe_status = str(s.get('observe_status') or '').upper()
            status = s.get('zt_result', '')
            if observe_status in ['ACTIVE', 'WATCHING', 'PENDING'] or (not observe_status and status in ['待验证', '继续观察']):
                lifecycle['observing'].append(s)
            elif observe_status == 'BOUGHT' or status == '已买入' or s['code'] in pos_map:
                p = pos_map.get(s['code'])
                if p:
                    s['pos_cost'] = p.get('avg_price', p.get('buy_price', 0))
                if s not in lifecycle['bought']:
                    lifecycle['bought'].append(s)
            elif observe_status in ['REMOVED', 'EXPIRED'] or status == '已剔除':
                lifecycle['removed'].append(s)

        # Enhance observing and bought lists with real-time prices
        target_lists = [lifecycle['observing'], lifecycle['bought']]
        all_to_fetch = []
        for lst in target_lists:
            for s in lst:
                c = s['code']
                ts_c = f"{c}.SH" if c.startswith('6') else (f"{c}.BJ" if c.startswith(('4', '8', '9')) else f"{c}.SZ")
                all_to_fetch.append(ts_c)
        
        if all_to_fetch:
            rt_data = self.provider.get_realtime_quotes(list(set(all_to_fetch)))
            for lst in target_lists:
                for s in lst:
                    c = s['code']
                    ts_c = f"{c}.SH" if c.startswith('6') else (f"{c}.BJ" if c.startswith(('4', '8', '9')) else f"{c}.SZ")
                    if ts_c in rt_data:
                        s['current_price'] = float(rt_data[ts_c].get('price', 0))
                    elif 'current_price' not in s:
                        # Fallback ONLY if not already set by legacy logic
                        s['current_price'] = float(s.get('sel_price') or s.get('price', 0))

        return {
            'stats': all_stats,
            'details': all_selections,
            'check_date': t1_fmt,
            'config': getattr(Config, 'STRATEGY', {}),
            'time_bucket_weather_stats': time_bucket_weather_stats,
            'tag_stats': tag_stats,
            'first_board_comparison': first_board_comparison,
            'lifecycle': lifecycle
        }

    def get_report(self):
        """Get history report text"""
        history = self.db.get_history_stats(10)
        lines = []
        lines.append("【策略历史表现 (近10次)】")
        if not history:
            lines.append("暂无数据")
        else:
            lines.append(f"| 日期 | 选中 | 涨停 | 吃肉 | 成功率 |")
            lines.append(f"|:---:|---:|---:|---:|---:|")
            avg_rate = 0
            for h in history:
                # Fallback to success_rate computation depending on old DB compatibility
                total = h.get('total', 0)
                zt_count = h.get('zt_count', 0)
                # Old DB might not have success_count, so fallback based on rate
                success_rate = h.get('success_rate', 0)
                success_count = h.get('success_count', int(total * success_rate / 100))
                
                lines.append(f"| {h['date']} | {total} | {zt_count} | {success_count} | {success_rate:.1f}% |")
                avg_rate += success_rate
            lines.append("")
            lines.append(f"**平均成功率:** {avg_rate/len(history):.1f}%")
            
        return "\n".join(lines)

    def get_detailed_report(self, check_result):
        """Get detailed daily report with individual stock performance"""
        lines = []
        if not check_result:
             lines.append("今日无策略追踪数据")
             return "\n".join(lines)

        check_date = check_result.get('check_date', 'Unknown')
        stats = check_result.get('stats', {})
        details = check_result.get('details', [])
        config = check_result.get('config', {})

        lines.append(f"【策略追踪日报 ({check_date})】")
        lines.append("=" * 40)
        
        # Add Config Summary
        if config:
            lines.append("【当前策略参数】")
            auc = config.get('auction', {})
            aft = config.get('afternoon', {})
            lines.append(f"  竞价: 高开{auc.get('min_open_change')}-{auc.get('max_open_change')}% / 换手>{auc.get('min_turnover')}%")
            lines.append(f"  尾盘: 涨幅{aft.get('min_change')}-{aft.get('max_change')}% / 换手{aft.get('min_turnover')}-{aft.get('max_turnover')}%")
            lines.append("-" * 40)

        lines.append(f"选中: {stats.get('total', 0)} 只")
        lines.append(f"涨停: {stats.get('zt_count', 0)} 只")
        lines.append(f"成功率: {stats.get('success_rate', 0):.1f}%")
        lines.append("-" * 40)
        
        if details:
            lines.append(f"{'代码':^8} {'名称':<8} {'表现'}")
            lines.append("-" * 40)
            for s in details:
                 status = s.get('result', 'N/A') # 'result' col in DB
                 lines.append(f"{s['code']:^8} {s['name']:<8} {status}")
            lines.append("")

        lines.append(self.get_report()) # Append history
        return "\n".join(lines)
