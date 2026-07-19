# -*- coding: utf-8 -*-
"""
Tushare Data Provider
"""
import tushare as ts
import tushare.pro.client as ts_client
import time
import logging
import math
from core.config import Config
from core.data_cache import RedisCache
from datetime import datetime, timedelta

logger = logging.getLogger("StockAnalyzer.DataProvider")

class DataProvider:
    def __init__(self):
        self.token = Config.TUSHARE_TOKEN
        self.url = Config.TUSHARE_URL
        self.max_retries = Config.MAX_RETRIES
        self._last_api_call_ts = 0.0
        self._rt_min_single_calls = 0
        
        # Initialize Tushare Pro API with custom endpoint
        ts_client.DataApi._DataApi__http_url = self.url
        self.pro = ts.pro_api(self.token)
        self.pro._DataApi__token = self.token
        self.pro._DataApi__http_url = self.url
        logger.info(f"Tushare API initialized with URL: {self.url}")
        
        # Initialize Cache Layer [V18]
        self.cache = RedisCache()

    @staticmethod
    def _is_non_retryable_tushare_error(msg):
        msg = str(msg or "")
        return any(s in msg for s in ["权限不足", "参数不对", "没有权限", "抱歉", "不存在", "必填参数"])

    @staticmethod
    def _is_rate_limit_error(msg):
        msg = str(msg or "")
        return any(s in msg for s in ["每分钟最多访问", "访问频次", "频率", "limit", "too many"])

    @staticmethod
    def _clean_db_value(value):
        """Convert pandas/numpy NaN-like values to None before MySQL writes."""
        try:
            if value is None:
                return None
            if isinstance(value, float) and math.isnan(value):
                return None
            if hasattr(value, "item"):
                raw = value.item()
                if isinstance(raw, float) and math.isnan(raw):
                    return None
                return raw
        except Exception:
            return value
        return value

    def _pace_api_call(self, api_name):
        """Local process pacing to reduce bursty Tushare calls."""
        try:
            min_interval = float(getattr(Config, "TUSHARE_MIN_REQUEST_INTERVAL_SEC", 0.0) or 0.0)
            if min_interval <= 0:
                return
            now = time.time()
            wait = min_interval - (now - self._last_api_call_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_api_call_ts = time.time()
        except Exception:
            return

    def _fetch_with_cache(self, cache_type, identifier, fetch_func, ttl):
        """Wrapper to fetch data through Redis cache.

        Important: we cache the *final processed* object (list/dict/df) so call sites
        see consistent types regardless of cache.
        """
        cached_data = self.cache.get(cache_type, identifier)
        if cached_data is not None:
            logger.debug(f"[Cache Hit] {cache_type}:{identifier}")
            return cached_data

        # Cache Miss
        data = fetch_func()
        if data is not None:
            self.cache.set(cache_type, identifier, data, ttl)
            logger.debug(f"[Cache Miss -> Stored] {cache_type}:{identifier}")
        return data

    def _get_cached_minute(self, ts_code, start_date, end_date, freq='1min'):
        cache_key = {'ts_code': ts_code, 'start': start_date, 'end': end_date, 'freq': freq}
        return self.cache.get('minute', cache_key)

    def _with_data_quality(self, data, *, source: str, fallback_used: bool = False, note: str = ""):
        """Attach a light-weight data_quality marker.

        - For list results: attach to each dict item if feasible
        - For single-record dict results: set key '_data_quality'
        - For dict-of-dicts keyed by ts_code/code: do not add pseudo keys

        This is additive; existing call sites ignore unknown keys.
        """
        try:
            quality = {
                'source': source,  # 'api'|'cache'|'fallback'
                'fallback_used': bool(fallback_used),
                'note': note,
                'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            if isinstance(data, dict):
                if not data:
                    return data
                # Avoid adding pseudo rows to keyed maps like {'000001.SZ': {...}}.
                values = [v for k, v in data.items() if k != '_data_quality']
                is_keyed_map = bool(values) and all(isinstance(v, dict) for v in values)
                if not is_keyed_map and '_data_quality' not in data:
                    data['_data_quality'] = quality
                return data

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and '_data_quality' not in item:
                        item['_data_quality'] = quality
                return data

            return data
        except Exception:
            return data

    def _request(self, api_name, params=None, fields=""):
        """Use tushare library to fetch data"""
        if params is None:
            params = {}
            
        last_error_msg = ""
        for attempt in range(self.max_retries):
            try:
                # Call tushare API dynamically
                api_func = getattr(self.pro, api_name, None)
                if api_func is None:
                    logger.error(f"Unknown API: {api_name}")
                    return None
                
                # Build kwargs from params
                kwargs = params.copy()
                if fields:
                    kwargs['fields'] = fields
                
                self._pace_api_call(api_name)
                df = api_func(**kwargs)
                
                if df is None or df.empty:
                    return None
                    
                # Convert DataFrame to dict format matching old API
                return {
                    'fields': df.columns.tolist(),
                    'items': df.values.tolist()
                }
                
            except Exception as e:
                msg = str(e)
                last_error_msg = msg
                logger.warning(f"API call failed ({api_name}): {msg}")
                if self._is_non_retryable_tushare_error(msg):
                    break
            
            if attempt < self.max_retries - 1:
                delay = Config.RETRY_DELAY
                if self._is_rate_limit_error(last_error_msg):
                    delay = max(delay, 5)
                time.sleep(delay)
                
        return None

    def _get_latest_trade_date(self):
        """Helper to find the latest trading date with data.
        
        IMPORTANT: Tushare daily API only has data AFTER market close (typically 16:00+).
        During trading hours, we use PREVIOUS trading day's data as baseline,
        then overlay real-time quotes from Tushare in the analyzer.
        """
        try:
            today = datetime.now().strftime('%Y%m%d')
            current_hour = datetime.now().hour
            
            # Fetch calendar for last 20 days
            start = (datetime.now() - timedelta(days=20)).strftime('%Y%m%d')
            cal = self.get_trade_cal(start, today)
            open_days = [d['cal_date'] for d in cal if d.get('is_open') == 1]
            open_days.sort()
            
            if not open_days:
                return (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            if today in open_days:
                # If after 16:00, today's data should be ready
                if current_hour >= 16:
                    return today
                else:
                    # During trading hours or before open: use PREVIOUS trading day
                    # (Real-time data will be overlaid in analyzer)
                    idx = open_days.index(today)
                    if idx > 0: return open_days[idx-1]
                    else: return open_days[0]
            else:
                # Today is not open (weekend/holiday), return last open day
                return open_days[-1]
                     
        except Exception as e:
            logger.error(f"Error determining trade date: {e}")
            return (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')

    def _get_previous_trade_date(self, current_date_str):
        """Helper to find the previous trading date given a current date string (YYYYMMDD)."""
        try:
            current_date = datetime.strptime(current_date_str, '%Y%m%d')
            # Fetch calendar for a reasonable period before the current date
            start = (current_date - timedelta(days=30)).strftime('%Y%m%d')
            end = current_date.strftime('%Y%m%d')
            cal = self.get_trade_cal(start, end)
            open_days = [d['cal_date'] for d in cal if d.get('is_open') == 1]
            open_days.sort()

            if not open_days:
                return (current_date - timedelta(days=1)).strftime('%Y%m%d') # Fallback to yesterday

            if current_date_str in open_days:
                idx = open_days.index(current_date_str)
                if idx > 0:
                    return open_days[idx - 1]
                else:
                    # If current_date_str is the first open day in the range,
                    # try to find an earlier one or return a default
                    return (current_date - timedelta(days=1)).strftime('%Y%m%d')
            else:
                # current_date_str is not an open day, return the last open day before it
                for d in reversed(open_days):
                    if d < current_date_str:
                        return d
                return (current_date - timedelta(days=1)).strftime('%Y%m%d') # Fallback if no previous found

        except Exception as e:
            logger.error(f"Error determining previous trade date for {current_date_str}: {e}")
            return (datetime.strptime(current_date_str, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')


    def get_daily_data(self, trade_date=None):
        """Get daily market data with daily_basic enrichment.

        Tushare daily does not provide turnover_rate; turnover and valuation fields
        are merged from daily_basic when available.
        """
        is_auto_date = trade_date is None
        if is_auto_date:
            trade_date = self._get_latest_trade_date()
            
        def _fetch():
            logger.info(f"Fetching daily data for {trade_date}...")
            fields = "ts_code,trade_date,close,pct_chg,vol,amount,open,high,low,pre_close"
            data = self._request("daily", {"trade_date": trade_date}, fields)
            data_trade_date = trade_date
            
            is_empty = not data or not data.get('items')
            
            # Check for zero-filled data at EOD
            if not is_empty and len(data.get('items', [])) > 0:
                sample = data['items'][:20]
                cols = [c.lower() for c in data.get('fields', [])]
                try:
                    close_idx = cols.index('close') if 'close' in cols else -1
                    vol_idx = cols.index('vol') if 'vol' in cols else -1
                    amount_idx = cols.index('amount') if 'amount' in cols else -1
                    
                    zeros = 0
                    total_checked = 0
                    for s in sample:
                        close_val = s[close_idx] if close_idx >= 0 and len(s) > close_idx else 0
                        vol_val = s[vol_idx] if vol_idx >= 0 and len(s) > vol_idx else 0
                        amount_val = s[amount_idx] if amount_idx >= 0 and len(s) > amount_idx else 0
                        
                        if (close_val or 0) <= 0 or ((vol_val or 0) <= 0 and (amount_val or 0) <= 0):
                            zeros += 1
                        total_checked += 1
                    
                    if total_checked > 0 and (zeros / total_checked) >= 0.7:
                        is_empty = True
                        logger.warning(f"Daily data for {trade_date} appears incomplete/zeroed ({zeros}/{total_checked} price/volume zeros)")
                except Exception as e:
                    logger.debug(f"Zero-check error: {e}")

            if is_empty and is_auto_date:
                prev_date = self._get_previous_trade_date(trade_date)
                logger.warning(f"Daily data for {trade_date} is empty/zeroed, falling back to {prev_date}")
                data = self._request("daily", {"trade_date": prev_date}, fields)
                data_trade_date = prev_date

            if not data or not data.get('items'):
                logger.warning(f"No daily data found for {trade_date}")
                return []
                
            columns = data.get('fields', [])
            items = data.get('items', [])
            result = [dict(zip(columns, item)) for item in items]

            # Enrich with same-day daily_basic fields. This is best-effort because
            # daily may become available before daily_basic on some endpoints.
            try:
                basic_fields = "ts_code,trade_date,turnover_rate,turnover_rate_f,pe,pe_ttm,pb,total_mv,circ_mv,float_share,free_share"
                basic = self._request("daily_basic", {"trade_date": data_trade_date}, basic_fields)
                if basic and basic.get('items'):
                    basic_cols = basic.get('fields', [])
                    basic_map = {
                        row.get('ts_code'): row
                        for row in (dict(zip(basic_cols, item)) for item in basic.get('items', []))
                        if row.get('ts_code')
                    }
                    for row in result:
                        ts_code = row.get('ts_code')
                        if ts_code in basic_map:
                            row.update({k: v for k, v in basic_map[ts_code].items() if k not in ('ts_code', 'trade_date')})
                    logger.info(f"Enriched daily data for {data_trade_date} with {len(basic_map)} daily_basic rows.")
                else:
                    logger.warning(f"No daily_basic enrichment data for daily {data_trade_date}")
            except Exception as e:
                logger.warning(f"Failed to enrich daily data with daily_basic for {data_trade_date}: {e}")

            return result

        # Include a schema marker so old Redis daily rows without daily_basic
        # enrichment do not survive deployments.
        cache_key = {"trade_date": trade_date, "schema": "daily_basic_enriched_v1"}
        return self._fetch_with_cache("daily", cache_key, _fetch, self.cache.TTL_DAILY)

    def _get_db_connection(self):
        """Create database connection"""
        import pymysql
        try:
            return pymysql.connect(
                host=Config.DB_HOST,
                port=Config.DB_PORT,
                user=Config.DB_USER,
                password=Config.DB_PASS,
                database=Config.DB_NAME,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
        except Exception as e:
            logger.error(f"DB Connection failed: {e}")
            return None

    def _init_stock_basic_table(self):
        """Initialize stock_basic table"""
        conn = self._get_db_connection()
        if not conn: return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS stock_basic (
                        ts_code VARCHAR(10) PRIMARY KEY,
                        name VARCHAR(50),
                        industry VARCHAR(50),
                        area VARCHAR(50),
                        market VARCHAR(20),
                        list_status VARCHAR(10),
                        updated_at DATETIME
                    )
                """)
            conn.commit()
        finally:
            conn.close()

    def get_stock_basic(self):
        """Get stock basic info from DB (Cache) or Tushare (Refresh).

        Note: result is a dict-of-dicts keyed by ts_code, so we do NOT attach _data_quality markers.
        """

        def _fetch():
            # 1. Try Load from DB
            self._init_stock_basic_table()

            conn = self._get_db_connection()
            data_map = {}

            if conn:
                try:
                    with conn.cursor() as cursor:
                        # Check if data is fresh. New listings appear frequently;
                        # a high row count alone can still be stale.
                        cursor.execute("SELECT count(*) as cnt, MAX(updated_at) as max_updated FROM stock_basic")
                        stat = cursor.fetchone() or {}
                        cnt = int(stat.get('cnt') or 0)
                        max_updated = stat.get('max_updated')
                        fresh = False
                        if max_updated:
                            try:
                                if isinstance(max_updated, str):
                                    max_updated_dt = datetime.strptime(max_updated.split('.')[0], '%Y-%m-%d %H:%M:%S')
                                else:
                                    max_updated_dt = max_updated
                                fresh = (datetime.now() - max_updated_dt).total_seconds() < 86400
                            except Exception:
                                fresh = False

                        if cnt > 4000 and fresh:
                            cursor.execute("SELECT * FROM stock_basic")
                            rows = cursor.fetchall()
                            for r in rows:
                                data_map[r['ts_code']] = r
                            logger.info(f"Loaded {len(data_map)} stocks from DB.")
                            return data_map
                        logger.info(f"Stock basic DB stale or incomplete (rows={cnt}, fresh={fresh}); refreshing from Tushare.")
                except Exception as e:
                    logger.warning(f"Failed to load FROM DB: {e}")
                finally:
                    conn.close()

            # 2. If DB empty or failed, Fetch from Tushare
            logger.info("Fetching stock basics from Tushare...")
            fields = "ts_code,name,industry,area,market,list_status"
            data = self._request("stock_basic", {"list_status": "L"}, fields)

            if not data or not data.get('items'):
                return {}

            columns = data.get('fields', [])
            items = data.get('items', [])

            # 3. Save to DB
            conn = self._get_db_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        sql = """REPLACE INTO stock_basic
                                 (ts_code, name, industry, area, market, list_status, updated_at)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s)"""

                        batch_data = []
                        for item in items:
                            d = dict(zip(columns, item))
                            data_map[d['ts_code']] = d
                            batch_data.append((
                                self._clean_db_value(d['ts_code']),
                                self._clean_db_value(d['name']),
                                self._clean_db_value(d.get('industry')),
                                self._clean_db_value(d.get('area')),
                                self._clean_db_value(d.get('market')),
                                self._clean_db_value(d.get('list_status')),
                                now
                            ))

                        # Batch insert (batch size 1000)
                        for i in range(0, len(batch_data), 1000):
                            cursor.executemany(sql, batch_data[i:i+1000])
                        conn.commit()
                        logger.info(f"Saved {len(items)} stocks to DB.")
                except Exception as e:
                    logger.error(f"Failed to save to DB: {e}")
                finally:
                    conn.close()

            return data_map

        # Redis cache (best-effort). Schema key prevents stale pre-refresh
        # stock_basic maps from hiding newly listed stocks.
        return self._fetch_with_cache('stock_basic', {'list_status': 'L', 'schema': 'fresh_24h_v1'}, _fetch, self.cache.TTL_BASIC)

    def get_stk_auction(self, trade_date=None):
        """Get daily auction data (9:25-9:30).

        trade_date: 'YYYYMMDD'
        Returns: dict keyed by ts_code.
        """
        if not getattr(Config, "TUSHARE_STK_AUCTION_ENABLED", False):
            logger.info(
                "Tushare stk_auction disabled by TUSHARE_STK_AUCTION_ENABLED=0; "
                "auction strategy will use rt_k/realtime fallback."
            )
            return {}

        if not trade_date:
            # stk_auction is usually available after 9:25
            trade_date = datetime.now().strftime('%Y%m%d')

        def _fetch():
            logger.info(f"Fetching auction data for {trade_date} from Tushare stk_auction...")
            res = self._request('stk_auction', params={'trade_date': trade_date})

            if not res:
                return {}

            results = {}
            fields = res['fields']
            for item in res['items']:
                row = dict(zip(fields, item))
                ts_c = row.get('ts_code')
                if ts_c:
                    results[ts_c] = self._normalize_auction_row(row, source='stk_auction')
            return results

        cache_key = {'trade_date': trade_date, 'schema': 'no_map_dq_v1'}
        data = self._fetch_with_cache('auction', cache_key, _fetch, self.cache.TTL_AUCTION)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_stk_auction_session(self, session='open', trade_date=None):
        """Get official auction OHLC data.

        session:
        - open: stk_auction_o
        - close: stk_auction_c
        Returns dict keyed by ts_code. If a requested date is empty, no silent
        fallback is applied; callers can inspect _data_quality.
        """
        api_name = 'stk_auction_o' if str(session).lower() in ('open', 'o') else 'stk_auction_c'
        if not trade_date:
            trade_date = datetime.now().strftime('%Y%m%d')

        def _fetch():
            logger.info(f"Fetching {api_name} data for {trade_date}...")
            res = self._request(api_name, params={'trade_date': trade_date})
            if not res:
                return {}

            results = {}
            fields = res.get('fields', [])
            for item in res.get('items', []):
                row = dict(zip(fields, item))
                ts_c = row.get('ts_code')
                if ts_c:
                    results[ts_c] = self._normalize_auction_row(row, source=api_name)
            return results

        cache_key = {'trade_date': trade_date, 'schema': 'no_map_dq_v1'}
        data = self._fetch_with_cache(f'auction_{api_name}', cache_key, _fetch, self.cache.TTL_AUCTION)
        note = f'{api_name} official auction session'
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api', note=note)

    def get_daily_basic(self, trade_date=None):
        """Get daily basic data (turnover, pe, etc) with fallback.

        Returns: dict keyed by ts_code.
        """
        is_auto_date = trade_date is None
        if is_auto_date:
            trade_date = self._get_latest_trade_date()

        def _fetch():
            logger.info(f"Fetching daily basics for {trade_date}...")
            # Add turnover_rate_f (Free Float Turnover)
            fields = "ts_code,trade_date,turnover_rate,turnover_rate_f,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
            data = self._request("daily_basic", {"trade_date": trade_date}, fields)

            is_empty = not data or not data.get('items')
            if not is_empty and len(data.get('items', [])) > 0:
                sample = data['items'][:20]
                cols = [c.lower() for c in data.get('fields', [])]
                try:
                    to_idx = cols.index('turnover_rate') if 'turnover_rate' in cols else -1
                    zeros = 0
                    for s in sample:
                        val = s[to_idx] if to_idx >= 0 and len(s) > to_idx else None
                        if val is None or (val == 0):
                            zeros += 1

                    if len(sample) > 0 and (zeros / len(sample)) >= 0.7:
                        is_empty = True
                        logger.warning(f"Daily basic for {trade_date} appears incomplete/zeroed ({zeros}/{len(sample)} nulls/zeros)")
                except Exception as e:
                    logger.debug(f"Basic zero-check error: {e}")

            fallback_used = False
            if is_empty and is_auto_date:
                prev_date = self._get_previous_trade_date(trade_date)
                fallback_used = True
                logger.warning(f"Daily basic for {trade_date} is empty/zeroed, falling back to {prev_date}")
                data = self._request("daily_basic", {"trade_date": prev_date}, fields)

            if not data or not data.get('items'):
                logger.warning(f"No daily basic data for {trade_date}")
                return {}

            columns = data.get('fields', [])
            items = data.get('items', [])

            # Return dict mapped by ts_code
            result = {}
            for item in items:
                d = dict(zip(columns, item))
                result[d['ts_code']] = d

            if fallback_used:
                return self._with_data_quality(result, source='fallback', fallback_used=True, note='daily_basic fallback to previous trade date')
            return result

        cache_key = {'trade_date': trade_date, 'schema': 'no_map_dq_v1'}
        data = self._fetch_with_cache('daily_basic', cache_key, _fetch, self.cache.TTL_DAILY)
        # cache may be disabled
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_circulating_share_map(self):
        """Get map of {ts_code: float_share} for turnover calculation.

        Best-effort cached. Internally reuses get_daily_basic() (which is already cached)
        to avoid extra API calls.
        """

        def _fetch():
            daily_basic = self.get_daily_basic(None)  # allow provider's fallback logic
            if not daily_basic:
                return {}

            res = {}
            for ts_code, row in daily_basic.items():
                try:
                    fs = row.get('float_share')
                    res[ts_code] = float(fs) if fs is not None else None
                except Exception:
                    res[ts_code] = row.get('float_share')
            return res

        cache_key = {'latest': True}
        return self._fetch_with_cache('float_share', cache_key, _fetch, self.cache.TTL_HISTORY)

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_str(value, default=""):
        try:
            if value is None:
                return default
            return str(value)
        except Exception:
            return default

    def _normalize_realtime_row(self, row, *, source):
        """Normalize rt_k/rt_min rows.

        Project convention after normalization:
        - price/open/high/low/pre_close: yuan
        - vol: lots (100 shares), with vol_shares also preserved
        - amount: yuan, with amount_yuan also preserved
        """
        row = row or {}
        ts_code = row.get('ts_code')
        close = self._safe_float(row.get('close') or row.get('price'))
        pre_close = self._safe_float(row.get('pre_close'))
        vol_shares = self._safe_float(row.get('vol_shares'), self._safe_float(row.get('vol')))
        vol_lots = self._safe_float(row.get('vol_lots'))
        if vol_lots <= 0 and vol_shares > 0:
            vol_lots = vol_shares / 100
        if vol_shares <= 0 and vol_lots > 0:
            vol_shares = vol_lots * 100
        amount_yuan = self._safe_float(row.get('amount_yuan'), self._safe_float(row.get('amount')))
        pct_chg = self._safe_float(row.get('pct_chg'))
        if close > 0 and pre_close > 0:
            pct_chg = (close - pre_close) / pre_close * 100
        vwap = amount_yuan / vol_shares if amount_yuan > 0 and vol_shares > 0 else self._safe_float(row.get('vwap'))

        return {
            'ts_code': ts_code,
            'name': row.get('name', ''),
            'open': self._safe_float(row.get('open'), close),
            'pre_close': pre_close,
            'price': close,
            'close': close,
            'high': self._safe_float(row.get('high'), close),
            'low': self._safe_float(row.get('low'), close),
            'vol': vol_lots,
            'vol_lots': vol_lots,
            'vol_shares': vol_shares,
            'amount': amount_yuan,
            'amount_yuan': amount_yuan,
            'pct_chg': pct_chg,
            'change': close - pre_close if close > 0 and pre_close > 0 else self._safe_float(row.get('change')),
            'vwap': vwap,
            'num': self._safe_float(row.get('num')),
            'time': row.get('time') or row.get('trade_time'),
            'source_api': source,
        }

    def _normalize_auction_row(self, row, *, source):
        """Normalize stk_auction/stk_auction_o/stk_auction_c rows."""
        row = row or {}
        price = self._safe_float(row.get('price'), self._safe_float(row.get('close')))
        vol_shares = self._safe_float(row.get('vol'))
        vol_lots = vol_shares / 100 if vol_shares > 0 else 0.0
        amount_yuan = self._safe_float(row.get('amount'))
        normalized = dict(row)
        normalized.update({
            'ts_code': row.get('ts_code'),
            'trade_date': self._safe_str(row.get('trade_date')),
            'price': price,
            'close': self._safe_float(row.get('close'), price),
            'open': self._safe_float(row.get('open'), price),
            'high': self._safe_float(row.get('high'), price),
            'low': self._safe_float(row.get('low'), price),
            'pre_close': self._safe_float(row.get('pre_close')),
            'vol': vol_lots,
            'vol_lots': vol_lots,
            'vol_shares': vol_shares,
            'amount': amount_yuan,
            'amount_yuan': amount_yuan,
            'turnover_rate': self._safe_float(row.get('turnover_rate')),
            'volume_ratio': self._safe_float(row.get('volume_ratio')),
            'float_share': self._safe_float(row.get('float_share')),
            'vwap': self._safe_float(row.get('vwap')),
            'source_api': source,
        })
        return normalized

    def _get_realtime_enrichment_maps(self):
        """Build enrichment maps for rt_min fallback from daily and stock_basic data."""
        daily_map = {}
        name_map = {}

        try:
            daily_data = self.get_daily_data()
            if daily_data:
                daily_map = {
                    row.get('ts_code'): row
                    for row in daily_data
                    if isinstance(row, dict) and row.get('ts_code')
                }
        except Exception as e:
            logger.warning(f"Failed to load daily enrichment data for realtime fallback: {e}")

        try:
            basics = self.get_stock_basic()
            if basics:
                name_map = {
                    ts_code: row.get('name', '')
                    for ts_code, row in basics.items()
                    if isinstance(row, dict)
                }
        except Exception as e:
            logger.warning(f"Failed to load stock basic enrichment data for realtime fallback: {e}")

        return daily_map, name_map

    def _enrich_realtime_quote(self, quote, daily_row=None, name=""):
        """Fill realtime fields from daily data without changing quote prices."""
        if not isinstance(quote, dict):
            return quote

        daily_row = daily_row or {}
        price = self._safe_float(quote.get('price'))
        quote['name'] = quote.get('name') or name or daily_row.get('name', '')

        pre_close = self._safe_float(quote.get('pre_close'))
        minute_date = str(quote.get('trade_time') or quote.get('time') or '')[:10].replace('-', '')
        daily_date = str(daily_row.get('trade_date') or '')

        if pre_close <= 0 and daily_row:
            today = datetime.now().strftime('%Y%m%d')
            if (minute_date and daily_date and minute_date > daily_date) or (daily_date and daily_date < today):
                pre_close = self._safe_float(daily_row.get('close'))
            else:
                pre_close = self._safe_float(daily_row.get('pre_close')) or self._safe_float(daily_row.get('close'))

        pct_chg = self._safe_float(quote.get('pct_chg'))
        if price > 0 and pre_close > 0:
            pct_chg = (price - pre_close) / pre_close * 100

        quote['pre_close'] = pre_close
        quote['pct_chg'] = pct_chg
        quote['change'] = price - pre_close if price > 0 and pre_close > 0 else self._safe_float(quote.get('change'))
        vol_shares = self._safe_float(quote.get('vol_shares'))
        vol_lots = self._safe_float(quote.get('vol_lots'))
        if vol_shares <= 0 and vol_lots > 0:
            vol_shares = vol_lots * 100
        if vol_lots <= 0 and vol_shares > 0:
            vol_lots = vol_shares / 100
        quote['vol_lots'] = vol_lots
        quote['vol_shares'] = vol_shares
        quote['vol'] = vol_lots
        quote['amount_yuan'] = self._safe_float(quote.get('amount_yuan'), self._safe_float(quote.get('amount')))
        quote['amount'] = quote['amount_yuan']
        if quote['vol_shares'] > 0 and quote['amount_yuan'] > 0:
            quote['vwap'] = quote['amount_yuan'] / quote['vol_shares']
        return quote

    def _enrich_rt_min_quote(self, quote, daily_row=None, name=""):
        """Backward-compatible alias for older call sites."""
        return self._enrich_realtime_quote(quote, daily_row=daily_row, name=name)

    def get_history_data(self, ts_code, end_date=None, count=60):
        """Get history data for specific stock (for indicators like MACD) (with cache)"""
        if not end_date:
            end_date = self._get_latest_trade_date()
            
        def _fetch():
            # Approximate start date (count * 1.5 days to account for weekends)
            start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=int(count*1.5))).strftime('%Y%m%d')
            
            logger.debug(f"Fetching history for {ts_code} ({start_date}-{end_date})...")
            fields = "ts_code,trade_date,open,close,high,low,vol,pct_chg"
            # Use daily API with specific ts_code
            params = {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
            data = self._request("daily", params, fields)
            
            if not data or not data.get('items'):
                return []
                
            columns = data.get('fields', [])
            items = data.get('items', [])
            
            # Return sorted by date ASC for calculation
            result = [dict(zip(columns, item)) for item in items]
            result.sort(key=lambda x: x['trade_date']) # Sort ascending (oldest first)
            return result
            
        cache_key = f"{ts_code}_{end_date}_{count}"
        return self._fetch_with_cache("history", cache_key, _fetch, self.cache.TTL_HISTORY)

    def get_batch_history_data(self, ts_code_list, end_date=None, count=60):
        """Get history data for multiple stocks in batches (with cache)"""
        if not ts_code_list:
            return {}
            
        if not end_date:
            end_date = self._get_latest_trade_date()
            
        codes = list(dict.fromkeys(ts_code_list))

        def _fetch_codes(codes_chunk):
            start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=int(count*1.5))).strftime('%Y%m%d')
            logger.info(f"Fetching batch history for {len(codes_chunk)} stocks ({start_date}-{end_date})...")
            
            results = {} # {ts_code: [asc sorted daily records]}
            
            # Tushare daily API usually accepts multiple ts_codes separated by commas
            # Max 50-100 codes per request is safe
            batch_size = max(1, int(getattr(Config, "TUSHARE_BATCH_HISTORY_SIZE", 50) or 50))
            batches = [codes_chunk[i:i+batch_size] for i in range(0, len(codes_chunk), batch_size)]
            
            for idx, batch in enumerate(batches):
                ts_code_str = ",".join(batch)
                fields = "ts_code,trade_date,open,close,high,low,vol,pct_chg"
                params = {"ts_code": ts_code_str, "start_date": start_date, "end_date": end_date}
                
                data = self._request("daily", params, fields)
                if not data or not data.get('items'):
                    continue
                    
                columns = data.get('fields', [])
                items = data.get('items', [])
                
                # Group by ts_code
                for item in items:
                    row = dict(zip(columns, item))
                    c = row['ts_code']
                    if c not in results:
                        results[c] = []
                    results[c].append(row)

                if idx < len(batches) - 1:
                    time.sleep(float(getattr(Config, "TUSHARE_BATCH_HISTORY_SLEEP_SEC", 0.15) or 0.0))
                    
            # Sort each list by trade_date ASC
            for c in results:
                results[c].sort(key=lambda x: x['trade_date'])
                
            return results

        cache_chunk_size = int(getattr(Config, "REDIS_BATCH_HISTORY_CACHE_CHUNK_SIZE", 200) or 0)
        if cache_chunk_size > 0 and len(codes) > cache_chunk_size:
            merged = {}
            for i in range(0, len(codes), cache_chunk_size):
                chunk = codes[i:i + cache_chunk_size]
                cache_key = {
                    'codes': chunk,
                    'end': end_date,
                    'count': count,
                    'schema': 'chunked_v1'
                }
                data = self._fetch_with_cache(
                    "batch_history",
                    cache_key,
                    lambda chunk=chunk: _fetch_codes(chunk),
                    self.cache.TTL_HISTORY
                )
                if isinstance(data, dict):
                    merged.update(data)
            return merged

        cache_key = {'codes': codes, 'end': end_date, 'count': count, 'schema': 'chunked_v1'}
        return self._fetch_with_cache("batch_history", cache_key, lambda: _fetch_codes(codes), self.cache.TTL_HISTORY)
            

    def _get_realtime_quotes_from_rt_min(self, ts_code_list):
        """Realtime quotes from the authorized rt_min endpoint."""
        results = {}
        codes_list = list(set(ts_code_list))
        chunk_size = max(1, int(getattr(Config, "TUSHARE_RT_MIN_CHUNK_SIZE", 200) or 200))
        daily_map, name_map = self._get_realtime_enrichment_maps()

        logger.info(f"Fetching real-time quotes for {len(codes_list)} stocks using Tushare rt_min...")

        for i in range(0, len(codes_list), chunk_size):
            chunk = codes_list[i:i + chunk_size]
            ts_codes = ",".join(chunk)
            try:
                df = self.pro.rt_min(ts_code=ts_codes)
            except Exception as e:
                logger.warning(f"Tushare rt_min failed for chunk {i // chunk_size + 1}: {e}")
                continue

            if df is None or df.empty:
                continue

            if 'time' in df.columns:
                df = df.sort_values('time', ascending=True)
            elif 'trade_time' in df.columns:
                df = df.sort_values('trade_time', ascending=True)

            records = df.to_dict('records')
            for row in records:
                ts_c = row.get('ts_code')
                if not ts_c:
                    continue
                try:
                    quote = self._normalize_realtime_row(row, source='rt_min')
                    results[ts_c] = self._enrich_realtime_quote(
                        quote,
                        daily_row=daily_map.get(ts_c, {}),
                        name=name_map.get(ts_c, ''),
                    )
                except (ValueError, TypeError):
                    continue

            time.sleep(float(getattr(Config, "TUSHARE_RT_MIN_SLEEP_SEC", 0.08) or 0.0))

        enriched = sum(1 for row in results.values() if row.get('pre_close', 0) > 0)
        logger.info(f"Finished fetching rt_min quotes. Got {len(results)} records, enriched {enriched} with daily fields.")
        return results

    def get_realtime_quotes(self, ts_code_list):
        """Get real-time quotes. Defaults to rt_k, with rt_min fallback when enabled.

        Returns: dict keyed by ts_code.
        """
        if not ts_code_list:
            return {}

        # Cache: very short TTL, but massively reduces realtime API pressure.
        primary = str(getattr(Config, "TUSHARE_REALTIME_PRIMARY", "rt_min") or "rt_min").lower()
        cache_key = {'codes': list(set(ts_code_list)), 'primary': primary, 'schema': 'rt_enriched_v2'}

        def _fetch():
            daily_map, name_map = self._get_realtime_enrichment_maps()
            if primary == 'rt_min':
                results = self._get_realtime_quotes_from_rt_min(ts_code_list)
                if results:
                    results['_data_quality'] = {
                        'source': 'api',
                        'fallback_used': False,
                        'note': 'Tushare rt_min with daily enrichment',
                        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    }
                return results

            logger.info(f"Fetching real-time quotes for {len(ts_code_list)} stocks using Tushare rt_k...")

            results = {}
            target_codes = set(ts_code_list)

            # Tushare rt_k strict limit: max 50 calls per minute per account.
            # Wildcards ('3*.SZ,6*.SH...') expand internally to ~50 calls, instantly blowing the limit!
            # Fix: Send ONLY the required codes, grouped in chunks of 200.
            chunk_size = max(1, int(getattr(Config, "TUSHARE_REALTIME_CHUNK_SIZE", 200) or 200))
            codes_list = list(target_codes)

            try:
                for i in range(0, len(codes_list), chunk_size):
                    chunk = codes_list[i:i + chunk_size]
                    ts_codes = ",".join(chunk)

                    df = self.pro.rt_k(ts_code=ts_codes)

                    if df is not None and not df.empty:
                        records = df.to_dict('records')
                        for row in records:
                            ts_c = row.get('ts_code')
                            if not ts_c:
                                continue
                            try:
                                quote = self._normalize_realtime_row(row, source='rt_k')
                                results[ts_c] = self._enrich_realtime_quote(
                                    quote,
                                    daily_row=daily_map.get(ts_c, {}),
                                    name=name_map.get(ts_c, ''),
                                )
                            except (ValueError, TypeError):
                                continue

                    time.sleep(float(getattr(Config, "TUSHARE_REALTIME_SLEEP_SEC", 0.08) or 0.0))

                logger.info(f"Finished fetching real-time quotes. Got {len(results)} records.")

            except Exception as e:
                logger.error(f"Failed to fetch realtime data from Tushare rt_k: {e}")

            if not results:
                results = self._get_realtime_quotes_from_rt_min(ts_code_list)
                if results:
                    results['_data_quality'] = {
                        'source': 'api',
                        'fallback_used': True,
                        'note': 'Tushare rt_min fallback after rt_k failed or returned empty',
                        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    }

            return results

        data = self._fetch_with_cache('realtime', cache_key, _fetch, self.cache.TTL_REALTIME)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')
            
    def get_top_list(self, trade_date=None):
        """Get Dragon Tiger List data (龙虎榜)"""
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        def _fetch():
            logger.info(f"Fetching Dragon Tiger List for {trade_date}...")
            fields = "trade_date,ts_code,name,reason,net_amount"
            data = self._request("top_list", {"trade_date": trade_date}, fields)

            if not data or not data.get('items'):
                return []

            columns = data.get('fields', [])
            items = data.get('items', [])
            return [dict(zip(columns, item)) for item in items]

        data = self._fetch_with_cache('lhb', trade_date, _fetch, self.cache.TTL_LHB)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_top_list_recent(self, ts_code, days=5, end_date=None):
        """Get recent龙虎榜 records for a single stock (best-effort, additive).

        Returns a list (newest first) of records with at least:
        - trade_date, ts_code, name, reason, net_amount
        """
        if not ts_code:
            return []
        if not end_date:
            end_date = self._get_latest_trade_date()

        cache_key = {'ts_code': ts_code, 'days': int(days or 0), 'end': end_date}

        def _fetch():
            # Use trading calendar to get last N open days
            dates = self.get_trade_cal(
                start_date=(datetime.strptime(end_date, "%Y%m%d") - timedelta(days=int(days or 5) * 3)).strftime('%Y%m%d'),
                end_date=end_date,
            )
            open_days = [d['cal_date'] for d in dates if d.get('is_open') == 1]
            open_days.sort()
            target_days = open_days[-int(days or 5):]

            res = []
            for d in reversed(target_days):
                rows = self.get_top_list(d)
                if not rows:
                    continue
                for r in rows:
                    if r.get('ts_code') == ts_code:
                        res.append(r)
            return res

        data = self._fetch_with_cache('lhb_recent', cache_key, _fetch, getattr(self.cache, 'TTL_LHB_RECENT', self.cache.TTL_LHB))
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_stk_limit(self, trade_date=None):
        """Get daily limit-up statistics.

        Uses limit_list_d which has 'limit_times', 'fd_amount', 'first_time'.
        Returns: dict keyed by ts_code.
        """
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        def _fetch():
            logger.info(f"Fetching Limit-up Stats for {trade_date}...")
            # fd_amount is closing strength.
            fields = "ts_code,trade_date,name,industry,close,pct_chg,amount,fd_amount,limit_times,first_time,limit"
            res = self._request("limit_list_d", {"trade_date": trade_date, "limit_type": "U"}, fields)

            if not res or not res.get('items'):
                return {}

            columns = res.get('fields', [])
            items = res.get('items', [])

            result = {}
            for item in items:
                row = dict(zip(columns, item))
                ts_c = row.get('ts_code')
                if ts_c:
                    result[ts_c] = row

            return result

        cache_key = {'trade_date': trade_date, 'schema': 'no_map_dq_v1'}
        data = self._fetch_with_cache('limit_stats', cache_key, _fetch, self.cache.TTL_LIMIT)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_limit_list(self, trade_date=None):
        """Get limit up list from Tushare (limit_list_d, limit_type=U)"""
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        def _fetch():
            logger.info(f"Fetching limit list (Doc 298) for {trade_date}...")
            fields = "ts_code,trade_date,industry,name,close,pct_chg,open_times,limit_times"
            params = {"trade_date": trade_date, "limit_type": "U"}
            data = self._request("limit_list_d", params, fields)

            if not data or not data.get('items'):
                return []

            columns = data.get('fields', [])
            items = data.get('items', [])
            return [dict(zip(columns, item)) for item in items]

        data = self._fetch_with_cache('limit_list', trade_date, _fetch, self.cache.TTL_LIMIT)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_trade_cal(self, start_date=None, end_date=None):
        """Get trading calendar"""
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')

        cache_key = {'start': start_date, 'end': end_date}

        def _fetch():
            fields = "exchange,cal_date,is_open,pretrade_date"
            params = {"exchange": "SSE", "start_date": start_date, "end_date": end_date}

            data = self._request("trade_cal", params, fields)
            if not data or not data.get('items'):
                return []

            columns = data.get('fields', [])
            items = data.get('items', [])
            return [dict(zip(columns, item)) for item in items]

        data = self._fetch_with_cache('trade_cal', cache_key, _fetch, self.cache.TTL_CAL)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_sector_money_flow(self, days=10, end_date=None):
        """
        [DEPRECATED] Use get_sector_rank_by_aggregated_flow instead.
        Old method using Tushare 'moneyflow_ind_dc' (Eastmoney) has sector naming mismatches.
        """
        logger.warning("get_sector_money_flow is DEPRECATED. Use get_sector_rank_by_aggregated_flow.")
        if not end_date:
            end_date = self._get_latest_trade_date()
            
        start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days*2)).strftime('%Y%m%d')
        logger.info(f"Fetching Sector Money Flow ({start_date}-{end_date})...")
        
        # moneyflow_ind_dc: EastMoney Concept/Industry Money Flow
        # fields: trade_date, ts_code, name, net_inflow, net_inflow_rate
        # moneyflow_ind_dc: EastMoney Concept/Industry Money Flow
        # Requesting ALL fields to ensure we get net_inflow
        params = {"start_date": start_date, "end_date": end_date}
        
        data = self._request("moneyflow_ind_dc", params)
        
        if not data or not data.get('items'):
            logger.warning("No sector money flow data found.")
            return []

        columns = data.get('fields', [])
        items = data.get('items', [])
        
        results = []
        for item in items:
            d = dict(zip(columns, item))
            # Standardize keys
            d['net_inflow'] = d.get('net_amount', 0)
            d['net_inflow_rate'] = d.get('net_amount_rate', 0)
            d['pct_chg'] = d.get('pct_change', 0)
            results.append(d)
            
        return results

    def get_individual_money_flow(self, ts_code_list, days=3, end_date=None):
        """
        Get individual stock money flow (Last N days).
        ts_code_list: list of ts_codes (e.g. ['000001.SZ', ...]) (with cache)
        """
        if not end_date:
            end_date = self._get_latest_trade_date()
            
        def _fetch():
            start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days*2)).strftime('%Y%m%d')
            
            # Optimization: Fetch ALL for last N days once, then filter.
            dates = self.get_trade_cal(start_date, end_date)
            open_days = [d['cal_date'] for d in dates if d.get('is_open') == 1]
            open_days.sort()
            target_dates = open_days[-days:] # Last N days
            logger.info(f"Fetching Individual Money Flow for {target_dates}...")
            
            # Aggregated structure: {ts_code: {'net_inflow': X, 'elg_net': Y, 'inflow_days': Z, 'total_amount': W}}
            aggregated = {} 

            def add_stat(code, day_net, elg_net=0.0, source_api='moneyflow', trade_date=None):
                if not code:
                    return
                if code not in aggregated:
                    aggregated[code] = {
                        'net_inflow': 0.0,
                        'elg_net': 0.0,
                        'inflow_days': 0,
                        'source_api': source_api,
                        'latest_trade_date': trade_date,
                    }
                aggregated[code]['net_inflow'] += float(day_net or 0)
                aggregated[code]['elg_net'] += float(elg_net or 0)
                aggregated[code]['source_api'] = source_api
                if trade_date:
                    aggregated[code]['latest_trade_date'] = trade_date
                if float(day_net or 0) > 0:
                    aggregated[code]['inflow_days'] += 1

            def fetch_moneyflow_dc(d):
                fields = "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"
                data = self._request("moneyflow_dc", {"trade_date": d}, fields)
                if not data or not data.get('items'):
                    return 0
                cols = data.get('fields', [])
                for item in data.get('items', []):
                    row = dict(zip(cols, item))
                    net_amount = self._safe_float(row.get('net_amount'))
                    elg_net = self._safe_float(row.get('buy_elg_amount'))
                    add_stat(row.get('ts_code'), net_amount, elg_net, 'moneyflow_dc', d)
                return len(data.get('items', []))

            def fetch_legacy_moneyflow(d):
                mf_fields = "ts_code,trade_date,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"
                data = self._request("moneyflow", {"trade_date": d}, mf_fields)
                if not data or not data.get('items'):
                    return 0
                cols = data.get('fields', [])
                for item in data.get('items', []):
                    row = dict(zip(cols, item))
                    net_lg = self._safe_float(row.get('buy_lg_amount')) - self._safe_float(row.get('sell_lg_amount'))
                    net_elg = self._safe_float(row.get('buy_elg_amount')) - self._safe_float(row.get('sell_elg_amount'))
                    day_net = net_lg + net_elg
                    add_stat(row.get('ts_code'), day_net, net_elg, 'moneyflow', d)
                return len(data.get('items', []))

            primary = str(getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc") or "moneyflow_dc").lower()
            fallback = str(getattr(Config, "TUSHARE_MONEYFLOW_FALLBACK", "moneyflow") or "moneyflow").lower()
            logger.info(f"Using individual money flow source: {primary} (fallback: {fallback})")
            
            for d in target_dates:
                try:
                    if primary == 'moneyflow_dc':
                        rows = fetch_moneyflow_dc(d)
                    else:
                        rows = fetch_legacy_moneyflow(d)
                    if rows <= 0 and fallback and fallback != primary:
                        logger.warning(f"No {primary} data for {d}; trying {fallback}")
                        rows = fetch_legacy_moneyflow(d) if fallback == 'moneyflow' else fetch_moneyflow_dc(d)
                    if rows <= 0:
                        logger.warning(f"No moneyflow data for {d} from {primary}/{fallback}")
                except Exception as e:
                    logger.error(f"Failed to fetch moneyflow for {d}: {e}")
                    
            # Filter for requested list and return final stats
            final_res = {}
            if ts_code_list:
                 for c in ts_code_list:
                    if c in aggregated:
                        final_res[c] = aggregated[c]
                 return final_res
            else:
                 # If no list provided, return all (useful for sector aggregation)
                 # Sector agg only needs the net_inflow sum for now
                 sector_map = {}
                 for c, stats in aggregated.items():
                     sector_map[c] = stats['net_inflow']
                 return sector_map
                 
        cache_key = {
            'codes': ts_code_list,
            'days': days,
            'end': end_date,
            'primary': getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc"),
            'fallback': getattr(Config, "TUSHARE_MONEYFLOW_FALLBACK", "moneyflow"),
            'schema': 'v2',
        }
        return self._fetch_with_cache("moneyflow", cache_key, _fetch, self.cache.TTL_MONEYFLOW)

    def resolve_moneyflow_trade_date(self, preferred_date=None, lookback_days=5):
        """Return the latest open day at or before preferred_date with moneyflow rows."""
        if not preferred_date:
            preferred_date = self._get_latest_trade_date()

        cache_key = {'preferred': preferred_date, 'lookback': int(lookback_days or 0)}

        def _fetch():
            try:
                end_dt = datetime.strptime(preferred_date, "%Y%m%d")
            except Exception:
                end_dt = datetime.now()
                preferred = end_dt.strftime("%Y%m%d")
            else:
                preferred = preferred_date

            start = (end_dt - timedelta(days=max(10, int(lookback_days or 5) * 3))).strftime("%Y%m%d")
            cal = self.get_trade_cal(start, preferred)
            open_days = [d['cal_date'] for d in cal if d.get('is_open') == 1 and d.get('cal_date') <= preferred]
            open_days.sort(reverse=True)

            for d in open_days[:max(1, int(lookback_days or 5))]:
                api_name = str(getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc") or "moneyflow_dc").lower()
                if api_name == 'moneyflow_dc':
                    data = self._request("moneyflow_dc", {"trade_date": d}, "trade_date,ts_code,net_amount")
                else:
                    data = self._request("moneyflow", {"trade_date": d}, "ts_code,trade_date,net_mf_amount")
                rows = (data or {}).get('items') or []
                if rows:
                    if d != preferred:
                        logger.warning(f"Moneyflow data unavailable for {preferred}; using latest available {d}")
                    return d

            logger.warning(f"No moneyflow data found within {lookback_days} trading days before {preferred}")
            return preferred

        cache_key['primary'] = getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc")
        return self._fetch_with_cache("moneyflow_date", cache_key, _fetch, self.cache.TTL_MONEYFLOW)

    def get_sector_rank_by_aggregated_flow(self, days=10, end_date=None):
        """Aggregate individual stock money flow by industry to rank sectors.

        Cached by (days, end_date).
        """
        if not end_date:
            end_date = self._get_latest_trade_date()

        cache_key = {
            'days': int(days or 0),
            'end': end_date,
            'primary': getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc"),
            'fallback': getattr(Config, "TUSHARE_MONEYFLOW_FALLBACK", "moneyflow"),
            'schema': 'v2',
        }

        def _fetch():
            # 1. Get Stock Basics for Industry Map
            basics = self.get_stock_basic()
            industry_map = {code: info['industry'] for code, info in basics.items() if info.get('industry')}

            # 2. Get Aggregated Money Flow for ALL stocks
            aggregated_flow = self.get_individual_money_flow([], days=days, end_date=end_date)

            # 3. Aggregate by Industry
            sector_inflow = {}  # Industry -> Net Amount
            sector_stocks = {}  # Industry -> list of (code, net_amount)

            for code, net_amount in (aggregated_flow or {}).items():
                ind = industry_map.get(code)
                if not ind:
                    continue
                if ind not in sector_inflow:
                    sector_inflow[ind] = 0.0
                    sector_stocks[ind] = []
                try:
                    sector_inflow[ind] += float(net_amount or 0)
                except Exception:
                    pass
                sector_stocks[ind].append((code, net_amount))

            # 4. Sort
            sorted_sectors = sorted(sector_inflow.items(), key=lambda x: x[1], reverse=True)

            results = []
            for name, inflow in sorted_sectors:
                stocks = sector_stocks.get(name, [])
                stocks.sort(key=lambda x: (x[1] or 0), reverse=True)
                top_stocks = [{'code': s[0], 'net_inflow': s[1]} for s in stocks[:5]]

                results.append({
                    'ts_code': name,
                    'name': name,
                    'net_inflow': inflow,
                    'top_stocks': top_stocks,
                })

            return results

        return self._fetch_with_cache('sector_flow_agg', cache_key, _fetch, self.cache.TTL_MONEYFLOW)

    def get_hsgt_top10(self, trade_date=None):
        """获取北向资金买入Top10 (沪股通+深股通) (with cache)."""
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        def _fetch():
            logger.info(f"Fetching HSGT Top10 for {trade_date}...")
            try:
                # Get Top10 (actually returns Top20 by default)
                df = self.pro.hsgt_top10(trade_date=trade_date)

                result = {}

                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        code = row['ts_code']
                        market = row.get('market_type', '未知')
                        net = row.get('net_amount')
                        net = float(net) if net else 0

                        result[code] = {
                            'rank': int(row['rank']),
                            'amount': float(row.get('amount', 0)),
                            'net_amount': net,
                            'market': market,
                        }

                logger.info(f"HSGT Top: found {len(result)} stocks")
                return result

            except Exception as e:
                logger.error(f"Failed to fetch HSGT Top10: {e}")
                return {}

        cache_key = {'trade_date': trade_date, 'schema': 'no_map_dq_v1'}
        data = self._fetch_with_cache('hsgt_top10', cache_key, _fetch, self.cache.TTL_MONEYFLOW)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def check_hsgt_eligibility(self, ts_code):
        """检查个股是否属于陆股通标的 (with cache)."""
        hsgt_type = 'HK_SH' if str(ts_code).endswith('.SH') else 'HK_SZ'
        cache_key = {'ts_code': ts_code, 'type': hsgt_type}

        def _fetch():
            try:
                df = self.pro.stock_hsgt(type=hsgt_type)
                if df is not None and not df.empty and 'ts_code' in df.columns:
                    matched = df[df['ts_code'] == ts_code]
                    if not matched.empty:
                        types = matched['type'].unique().tolist() if 'type' in matched.columns else [hsgt_type]
                        return {'is_hsgt': True, 'type': types}
                return {'is_hsgt': False, 'type': []}
            except Exception as e:
                logger.debug(f"HSGT eligibility check failed for {ts_code}: {e}")
                return {'is_hsgt': False, 'type': []}

        data = self._fetch_with_cache('stock_hsgt', cache_key, _fetch, self.cache.TTL_BASIC)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_index_daily(self, ts_code, start_date=None, end_date=None, count=30):
        """
        获取指数日线数据 (用于市场环境感知) (with cache)
        """
        if not end_date:
            end_date = self._get_latest_trade_date()
        if not start_date:
            start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=count*2)).strftime('%Y%m%d')
        
        def _fetch():
            logger.info(f"Fetching index daily for {ts_code} from {start_date} to {end_date}...")
            
            try:
                params = {
                    'ts_code': ts_code,
                    'start_date': start_date,
                    'end_date': end_date
                }
                fields = "ts_code,trade_date,close,open,high,low,pct_chg,vol"
                data = self._request("index_daily", params, fields)
                
                if not data or not data.get('items'):
                    logger.warning(f"No index daily data for {ts_code}")
                    return []
                
                columns = data.get('fields', [])
                items = data.get('items', [])
                
                result = []
                for item in items:
                    row = dict(zip(columns, item))
                    # Convert types
                    row['close'] = float(row.get('close', 0))
                    row['pct_chg'] = float(row.get('pct_chg', 0))
                    row['vol'] = float(row.get('vol', 0))
                    result.append(row)
                
                # Sort by date ascending
                result.sort(key=lambda x: x['trade_date'])
                return result
                
            except Exception as e:
                logger.error(f"Failed to fetch index daily for {ts_code}: {e}")
                return []
                
        cache_key = f"{ts_code}_{start_date}_{end_date}"
        return self._fetch_with_cache("index", cache_key, _fetch, self.cache.TTL_INDEX)

    def get_industry_stats(self, trade_date=None):
        """获取全市场各行业热度统计 (with cache)."""
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        cache_key = {
            'date': trade_date,
            'moneyflow_primary': getattr(Config, "TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc"),
            'schema': 'v3',
        }

        def _fetch():
            logger.info(f"Fetching industry stats for {trade_date}...")

            try:
                daily_data = self.get_daily_data(trade_date)
                if not daily_data:
                    return []

                basics = self.get_stock_basic()
                mf_data = self.get_individual_money_flow([], days=1, end_date=trade_date)

                industry_stats = {}

                for stock in daily_data:
                    ts_code = stock.get('ts_code')
                    if not ts_code or ts_code not in basics:
                        continue

                    industry = basics[ts_code].get('industry', '其他')
                    if not industry:
                        industry = '其他'

                    if industry not in industry_stats:
                        industry_stats[industry] = {
                            'industry': industry,
                            'total_change': 0.0,
                            'total_amount': 0.0,
                            'stock_count': 0,
                            'total_mf': 0.0,
                        }

                    change = float(stock.get('pct_chg', 0) or 0)
                    amount = float(stock.get('amount', 0) or 0)

                    industry_stats[industry]['total_change'] += change
                    industry_stats[industry]['total_amount'] += amount
                    industry_stats[industry]['stock_count'] += 1

                    mf = mf_data.get(ts_code, {})
                    if isinstance(mf, dict):
                        net_inflow = mf.get('net_inflow', 0) or 0
                    else:
                        net_inflow = mf or 0
                    industry_stats[industry]['total_mf'] += float(net_inflow)

                result = []
                for ind, stats in industry_stats.items():
                    if stats['stock_count'] > 0:
                        result.append({
                            'industry': ind,
                            'avg_change': stats['total_change'] / stats['stock_count'],
                            'total_amount': stats['total_amount'],
                            'stock_count': stats['stock_count'],
                            'net_money_flow': stats['total_mf'],
                        })

                result.sort(key=lambda x: x['avg_change'], reverse=True)

                logger.info(f"Industry stats: found {len(result)} industries")
                return result

            except Exception as e:
                logger.error(f"Failed to fetch industry stats: {e}")
                return []

        # Industry stats is relatively heavy; cache it.
        data = self._fetch_with_cache('industry_stats', cache_key, _fetch, self.cache.TTL_DAILY)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_margin_data(self, ts_code, trade_date=None):
        """获取个股两融数据 (with cache)."""
        if not trade_date:
            trade_date = self._get_latest_trade_date()

        cache_key = {'ts_code': ts_code, 'date': trade_date}

        def _fetch():
            try:
                df = self.pro.margin_detail(ts_code=ts_code, trade_date=trade_date)
                if df is None or df.empty:
                    return None

                row = df.iloc[0]
                rzye = float(row.get('rzye', 0) or 0)
                rzmre = float(row.get('rzmre', 0) or 0)

                # Get daily turnover for intensity calculation
                daily_df = self.pro.daily(ts_code=ts_code, trade_date=trade_date)
                amount = float(daily_df['amount'].iloc[0]) if daily_df is not None and not daily_df.empty else 0

                rz_intensity = (rzmre / amount * 100) if amount > 0 else 0

                return {
                    'rzye': rzye,
                    'rzmre': rzmre,
                    'amount': amount,
                    'rz_intensity': rz_intensity,
                }
            except Exception as e:
                logger.debug(f"Margin data fetch failed for {ts_code}: {e}")
                return None

        data = self._fetch_with_cache('margin', cache_key, _fetch, self.cache.TTL_DAILY)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_holder_number(self, ts_code, periods=2):
        """获取股东人数变化 (with cache)."""
        cache_key = {'ts_code': ts_code, 'periods': int(periods or 2)}

        def _fetch():
            try:
                df = self.pro.stk_holdernumber(ts_code=ts_code)
                if df is None or df.empty:
                    return None

                df = df.sort_values('end_date', ascending=False)
                if len(df) < int(periods or 2):
                    return None

                current = int(df['holder_num'].iloc[0])
                previous = int(df['holder_num'].iloc[1])
                change_rate = (current - previous) / previous * 100 if previous > 0 else 0

                return {
                    'current': current,
                    'previous': previous,
                    'change_rate': change_rate,
                }
            except Exception as e:
                logger.debug(f"Holder number fetch failed for {ts_code}: {e}")
                return None

        data = self._fetch_with_cache('holder_num', cache_key, _fetch, self.cache.TTL_BASIC)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def check_state_fund(self, ts_code):
        """检测前十大股东中是否存在国家队 (with cache)."""
        cache_key = {'ts_code': ts_code}

        def _fetch():
            try:
                df = self.pro.top10_holders(ts_code=ts_code, start_date='20250101')
                if df is None or df.empty:
                    return {'has_state_fund': False, 'funds': []}

                state_keywords = ['证金', '汇金', '社保', '国资', '国家队']
                found_funds = []

                for _, row in df.iterrows():
                    holder_name = str(row.get('holder_name', '') or '')
                    holder_type = str(row.get('holder_type', '') or '')

                    for kw in state_keywords:
                        if kw in holder_name or kw in holder_type:
                            found_funds.append(holder_name)
                            break

                return {
                    'has_state_fund': len(found_funds) > 0,
                    'funds': found_funds,
                }
            except Exception as e:
                logger.debug(f"State fund check failed for {ts_code}: {e}")
                return {'has_state_fund': False, 'funds': []}

        data = self._fetch_with_cache('top10_holders', cache_key, _fetch, self.cache.TTL_BASIC)
        return self._with_data_quality(data, source='cache' if self.cache.enabled else 'api')

    def get_atr(self, ts_code, window=14, end_date=None):
        """
        [V12] 计算ATR (Average True Range) 真实波幅均值
        
        ATR = Average of True Range over N periods
        True Range = max(H-L, |H-PC|, |L-PC|)
        
        Args:
            ts_code: 股票代码
            window: ATR周期 (默认14天)
            end_date: 结束日期
        
        Returns:
            dict: {'atr': float, 'atr_percent': float, 'volatility': 'low'/'medium'/'high'}
        """
        try:
            if not end_date:
                end_date = self._get_latest_trade_date()
            
            start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=window*2)).strftime('%Y%m%d')
            
            # Get daily data
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or len(df) < window + 1:
                return None
            
            df = df.sort_values('trade_date').tail(window + 1)
            
            # Calculate True Range
            tr_list = []
            
            for i in range(1, len(df)):
                high = df['high'].iloc[i]
                low = df['low'].iloc[i]
                prev_close = df['pre_close'].iloc[i]
                
                tr1 = high - low
                tr2 = abs(high - prev_close)
                tr3 = abs(low - prev_close)
                
                tr = max(tr1, tr2, tr3)
                tr_list.append(tr)
            
            # Calculate ATR
            atr = sum(tr_list) / len(tr_list) if tr_list else 0
            current_price = df['close'].iloc[-1]
            atr_percent = (atr / current_price * 100) if current_price > 0 else 0
            
            # Classify volatility
            if atr_percent < 3:
                volatility = 'low'
            elif atr_percent < 6:
                volatility = 'medium'
            else:
                volatility = 'high'
            
            return {
                'atr': round(atr, 4),
                'atr_percent': round(atr_percent, 2),
                'current_price': current_price,
                'volatility': volatility,
                'window': window
            }
            
        except Exception as e:
            logger.debug(f"ATR calculation failed for {ts_code}: {e}")
            return None

    def get_moneyflow_hsgt(self, start_date, end_date):
        """
        [V2] 获取沪深港通资金流向
        """
        logger.info(f"Fetching HSGT money flow from {start_date} to {end_date}...")
        try:
            df = self.pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.error(f"Failed to fetch HSGT money flow: {e}")
        return None

    def get_stock_hist(self, ts_code, start_date, end_date):
        """
        [V2] 获取单只股票指定区间的历史日线数据，用于计算MACD/RSI等技术指标
        """
        try:
            fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
            # Tushare pro daily API can take ts_code + date range
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date, fields=fields)
            if df is not None and not df.empty:
                # 按照日期升序排列，方便计算技术指标
                df = df.sort_values('trade_date', ascending=True)
                return df
        except Exception as e:
            logger.error(f"Failed to fetch historical data for {ts_code}: {e}")
        return None

    def get_stock_min_data(self, ts_code, start_date, end_date, freq='1min'):
        """[V4] 获取分钟级 K 线数据

        Returns a pandas DataFrame sorted by trade_time ASC, or None.
        """
        cache_key = {'ts_code': ts_code, 'start': start_date, 'end': end_date, 'freq': freq}

        def _fetch():
            def _fetch_tencent_minute():
                if not Config.TENCENT_MINUTE_FALLBACK_ENABLED:
                    logger.info("Tencent minute fallback disabled by env: TENCENT_MINUTE_FALLBACK_ENABLED=0")
                    return None

                try:
                    import json
                    import time
                    import requests
                    import pandas as pd

                    prefix = ts_code.split('.')[1].lower()
                    code_only = ts_code.split('.')[0]
                    tencent_code = f"{prefix}{code_only}"

                    base_url = str(Config.TENCENT_MINUTE_FALLBACK_BASE_URL).rstrip('/')
                    path = str(Config.TENCENT_MINUTE_FALLBACK_MKLINE_PATH)
                    if not path.startswith('/'):
                        path = '/' + path

                    count = int(Config.TENCENT_MINUTE_FALLBACK_COUNT)
                    timeout_sec = float(Config.TENCENT_MINUTE_FALLBACK_TIMEOUT_SEC)

                    headers = {}
                    headers_json = str(Config.TENCENT_MINUTE_FALLBACK_HEADERS_JSON or '').strip()
                    if headers_json:
                        try:
                            parsed = json.loads(headers_json)
                            if isinstance(parsed, dict):
                                headers.update({str(k): str(v) for k, v in parsed.items()})
                        except Exception as e:
                            logger.warning(f"Invalid TENCENT_MINUTE_FALLBACK_HEADERS_JSON, ignored: {e}")

                    ua = str(Config.TENCENT_MINUTE_FALLBACK_USER_AGENT or '').strip()
                    if ua:
                        headers['User-Agent'] = ua

                    url = f"{base_url}{path}?param={tencent_code},m1,,{count}"

                    attempts = max(1, 1 + int(Config.TENCENT_MINUTE_FALLBACK_RETRIES))
                    delay_sec = float(Config.TENCENT_MINUTE_FALLBACK_RETRY_DELAY_SEC)

                    last_err = None
                    for i in range(attempts):
                        try:
                            resp = requests.get(url, timeout=timeout_sec, headers=headers or None)
                            res = resp.json()
                            if res.get('code') == 0:
                                data = ((res.get('data') or {}).get(tencent_code) or {}).get('m1') or []
                                if not data:
                                    return None
                                # Tencent structure: [time, open, close, high, low, vol]
                                df = pd.DataFrame(data).iloc[:, :6]
                                if df.shape[1] < 6:
                                    return None
                                df.columns = ['trade_time', 'open', 'close', 'high', 'low', 'vol']
                                # Format time from '202603050931' to '2026-03-05 09:31:00'
                                df['trade_time'] = pd.to_datetime(df['trade_time'], format='%Y%m%d%H%M').dt.strftime('%Y-%m-%d %H:%M:%S')
                                for col in ['open', 'close', 'high', 'low', 'vol']:
                                    df[col] = df[col].astype(float)
                                return df
                        except Exception as e:
                            last_err = e
                            if i < attempts - 1 and delay_sec > 0:
                                time.sleep(delay_sec)

                    if last_err is not None:
                        raise last_err
                except Exception as e:
                    logger.error(f"Fallback to Tencent API failed: {e}")
                return None

            if freq == '1min':
                try:
                    max_single = int(getattr(Config, "TUSHARE_RT_MIN_SINGLE_MAX_PER_RUN", 20) or 0)
                    if max_single > 0 and self._rt_min_single_calls >= max_single:
                        logger.info(f"Tushare rt_min single-call budget exhausted ({max_single}/run); using fallback for {ts_code}")
                        return _fetch_tencent_minute()
                    self._rt_min_single_calls += 1
                    time.sleep(float(getattr(Config, "TUSHARE_RT_MIN_SLEEP_SEC", 0.08) or 0.0))
                    df = self.pro.rt_min(ts_code=ts_code)
                    if df is not None and not df.empty:
                        if 'trade_time' not in df.columns and 'time' in df.columns:
                            df = df.rename(columns={'time': 'trade_time'})
                        if 'trade_time' in df.columns:
                            df = df.sort_values('trade_time', ascending=True)
                        return df
                except Exception as e:
                    logger.warning(f"Tushare rt_min failed for {ts_code}: {e}")
                    return _fetch_tencent_minute()

            try:
                import tushare as ts
                df = ts.pro_bar(ts_code=ts_code, freq=freq, start_date=start_date, end_date=end_date, api=self.pro)
                if df is not None and not df.empty:
                    df = df.sort_values('trade_time', ascending=True)
                    return df
            except Exception:
                logger.warning("Tushare 1min limit reached, fallback to Tencent API...")

            if freq == '1min':
                return _fetch_tencent_minute()

            return None

        # Cache minute bars very briefly to reduce repeated calls
        return self._fetch_with_cache('minute', cache_key, _fetch, self.cache.TTL_MINUTE)

    def get_crude_oil_price(self, date_str=None):
        """Get crude oil continuous contract price (SC.INE)"""
        if not date_str:
            date_str = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
        
        # In actual implementation, Tushare fut_daily for SC.INE
        try:
            fields = "ts_code,trade_date,close,settle,pre_settle,change1,change2,vol,amount"
            df = self.pro.fut_daily(ts_code='SC.INE', trade_date=date_str, fields=fields)
            if df is not None and not df.empty:
                row = df.iloc[0]
                pct_change = (row['settle'] - row['pre_settle']) / row['pre_settle'] * 100
                return {
                    'trade_date': row['trade_date'],
                    'close': float(row['close']),
                    'settle': float(row['settle']),
                    'pct_change': pct_change
                }
        except Exception as e:
            logger.error(f"Failed to fetch crude oil: {e}")
            
        return None

    def get_global_macro(self, date_str=None):
        """
        [Phase 1] 获取隔夜外盘与宏观数据: 美股(DJI/IXIC), A50期指(XIN9), 离岸人民币(USDCNH.FXCM)
        """
        if not date_str:
            date_str = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
        macro_data = {}
        try:
            # 1. 美股与A50 (index_global)
            logger.info("Fetching global indices (DJI, IXIC, XIN9)...")
            indices = ['DJI', 'IXIC', 'XIN9']
            df = self.pro.index_global(ts_code=','.join(indices), start_date=(datetime.now() - timedelta(days=5)).strftime('%Y%m%d'), end_date=datetime.now().strftime('%Y%m%d'))
            
            if df is not None and not df.empty:
                for code in indices:
                    code_df = df[df['ts_code'] == code].sort_values('trade_date', ascending=False)
                    if not code_df.empty:
                        row = code_df.iloc[0]
                        macro_data[code] = {
                            'trade_date': row['trade_date'],
                            'close': float(row['close']),
                            'pct_change': float(row['pct_chg'])
                        }
            
            # 2. 离岸人民币 (fx_daily)
            logger.info("Fetching FX (USDCNH)...")
            df_fx = self.pro.fx_daily(ts_code='USDCNH.FXCM', start_date=(datetime.now() - timedelta(days=5)).strftime('%Y%m%d'), end_date=datetime.now().strftime('%Y%m%d'))
            if df_fx is not None and not df_fx.empty:
                # Get the latest 2 days to calculate change if not provided
                df_fx = df_fx.sort_values('trade_date', ascending=False)
                latest = df_fx.iloc[0]
                prev = df_fx.iloc[1] if len(df_fx) > 1 else latest
                pchg = (latest['bid_close'] - prev['bid_close']) / prev['bid_close'] * 100 if prev['bid_close'] else 0
                
                macro_data['USDCNH'] = {
                    'trade_date': latest['trade_date'],
                    'close': float(latest['bid_close']),
                    'pct_change': float(pchg)
                }
                
        except Exception as e:
            logger.error(f"Failed to fetch global macro data: {e}")
            
        return macro_data

    # =========================================
    # V3 ENHANCEMENTS: FUNDAMENTALS, CONCEPTS, INSTITUTIONS
    # =========================================
    def get_fina_indicator(self, ts_code, periods=1):
        """
        [V3] 获取基本面财务指标 (ROE, 资产负债率等)
        ts_code: str (e.g. '000001.SZ')
        periods: int (获取最近几个财务周期)
        """
        try:
            logger.info(f"Fetching financial indicators for {ts_code}...")
            # We fetch recently published data
            df = self.pro.fina_indicator(ts_code=ts_code, limit=periods)
            if df is not None and not df.empty:
                # Return the latest period's data as a dict
                return df.iloc[0].to_dict()
        except Exception as e:
            logger.error(f"Failed to fetch financial indicators for {ts_code}: {e}")
        return None

    def get_stock_valuation(self, ts_code, trade_date=None):
        """
        [V5] 获取每日基本面指标 (PE_TTM, PB, 总市值等)
        该数据来自 Tushare daily_basic 接口，是 PE/PB 的唯一正确来源。
        """
        if not trade_date:
            trade_date = self._get_latest_trade_date()
        
        try:
            logger.info(f"Fetching daily_basic for {ts_code} on {trade_date}...")
            fields = "ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate"
            data = self._request("daily_basic", {"ts_code": ts_code, "trade_date": trade_date}, fields)
            
            if data and data.get('items'):
                cols = data.get('fields', [])
                items = data.get('items', [])
                if items:
                    row = dict(zip(cols, items[0]))
                    return row
            
            # Fallback: try without trade_date, get latest
            data = self._request("daily_basic", {"ts_code": ts_code, "limit": "1"}, fields)
            if data and data.get('items'):
                cols = data.get('fields', [])
                items = data.get('items', [])
                if items:
                    return dict(zip(cols, items[0]))
        except Exception as e:
            logger.error(f"Failed to fetch daily_basic for {ts_code}: {e}")
        return None

    def get_stk_surv(self, ts_code, days=30):
        """
        [V3] 获取近期机构调研情况
        ts_code: str (e.g. '000001.SZ')
        days: int (最近多少天)
        
        Returns a dict with summary stats
        """
        try:
            logger.info(f"Fetching institutional survey for {ts_code}...")
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            
            df = self.pro.stk_surv(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                return {
                    'survey_times': len(df), # number of survey events
                    'latest_date': df['surv_date'].max(),
                    'total_institutions': int(df['rece_place'].count()) if 'rece_place' in df.columns else 0 # approximate
                }
            return {'survey_times': 0, 'latest_date': None, 'total_institutions': 0}
        except Exception as e:
            logger.error(f"Failed to fetch institutional survey for {ts_code}: {e}")
            # If no permission, fail gracefully
            return None

    def get_concept_detail(self, ts_code):
        """\
        [V3] 获取个股概念题材明细
        ts_code: str (e.g. '000001.SZ')

        Returns a list of concept names

        [VNext] Add Redis cache to reduce repeated calls.
        """

        def _fetch():
            try:
                logger.info(f"Fetching concepts for {ts_code}...")
                df = self.pro.concept_detail(ts_code=ts_code)
                if df is not None and not df.empty:
                    # Extract concept names into a list
                    concepts = df['concept_name'].tolist()
                    return concepts
                return []
            except Exception as e:
                logger.error(f"Failed to fetch concept details for {ts_code}: {e}")
                return None

        # Cache: concept details are relatively stable, so we use TTL_BASIC.
        return self._fetch_with_cache(
            'concept_detail',
            {'ts_code': ts_code},
            _fetch,
            RedisCache.TTL_BASIC,
        )
