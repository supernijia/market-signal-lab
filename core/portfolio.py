# -*- coding: utf-8 -*-
"""
Portfolio Management using MySQL
"""
from __future__ import annotations

import pymysql
import logging
import json
from core.config import Config
from datetime import datetime

logger = logging.getLogger("StockAnalyzer.Portfolio")


def is_virtual_account(account: str | None) -> bool:
    account = str(account or "").strip().lower()
    return account == "rescue" or account.startswith("paper_")


def is_paper_account(account: str | None) -> bool:
    return str(account or "").strip().lower().startswith("paper_")


def _index_columns(rows, key_name: str) -> list[str]:
    parts = [
        row for row in (rows or [])
        if str(row.get("Key_name") or "") == key_name
    ]
    parts.sort(key=lambda row: int(row.get("Seq_in_index") or 0))
    return [str(row.get("Column_name") or "") for row in parts]


class PortfolioManager:
    def __init__(self):
        self.host = Config.DB_HOST
        self.port = Config.DB_PORT
        self.user = Config.DB_USER
        self.password = Config.DB_PASS
        self.db_name = Config.DB_NAME

        # in-memory cache for computed stats during one run
        self._win_rate_cache = {}

    def _get_connection(self):
        """Create database connection with retry [V17]"""
        import time
        max_retries = 3
        for i in range(max_retries):
            try:
                conn = pymysql.connect(
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    database=self.db_name,
                    charset='utf8mb4',
                    cursorclass=pymysql.cursors.DictCursor,
                    connect_timeout=15,
                    read_timeout=60,
                    write_timeout=60
                )
                return conn
            except pymysql.MySQLError as e:
                if i < max_retries - 1:
                    logger.warning(f"Database connection failed, retrying ({i+1}/{max_retries}): {e}")
                    time.sleep(2)
                else:
                    logger.error(f"Database connection failed after {max_retries} attempts: {e}")
        return None

    def _ensure_positions_account_key(self, cursor):
        """Upgrade legacy positions(code) uniqueness to account-aware positions(code, account)."""
        try:
            cursor.execute("UPDATE positions SET account='main' WHERE account IS NULL OR account=''")
            cursor.execute("ALTER TABLE positions MODIFY account VARCHAR(20) NOT NULL DEFAULT 'main'")
        except Exception as exc:
            logger.debug("Skip positions account normalization: %s", exc)

        try:
            cursor.execute("SHOW INDEX FROM positions")
            rows = cursor.fetchall() or []
        except Exception as exc:
            logger.debug("Skip positions index inspection: %s", exc)
            return

        primary_cols = _index_columns(rows, "PRIMARY")
        unique_single_code_keys = []
        for key_name in sorted({str(row.get("Key_name") or "") for row in rows if int(row.get("Non_unique") or 0) == 0}):
            cols = _index_columns(rows, key_name)
            if key_name != "PRIMARY" and cols == ["code"]:
                unique_single_code_keys.append(key_name)

        if primary_cols and primary_cols != ["code", "account"]:
            try:
                cursor.execute("ALTER TABLE positions DROP PRIMARY KEY")
                logger.info("POSITIONS_INDEX_MIGRATION dropped legacy PRIMARY KEY columns=%s", primary_cols)
                primary_cols = []
            except Exception as exc:
                logger.warning("POSITIONS_INDEX_MIGRATION failed to drop legacy PRIMARY KEY columns=%s error=%s", primary_cols, exc)

        if not primary_cols:
            try:
                cursor.execute("ALTER TABLE positions ADD PRIMARY KEY (code, account)")
                logger.info("POSITIONS_INDEX_MIGRATION added PRIMARY KEY (code, account)")
            except Exception as exc:
                logger.debug("POSITIONS_INDEX_MIGRATION add primary skipped: %s", exc)

        for key_name in unique_single_code_keys:
            try:
                cursor.execute(f"ALTER TABLE positions DROP INDEX `{key_name}`")
                logger.info("POSITIONS_INDEX_MIGRATION dropped legacy unique index %s(code)", key_name)
            except Exception as exc:
                logger.warning("POSITIONS_INDEX_MIGRATION failed to drop legacy unique index %s error=%s", key_name, exc)

    def load_positions(self, account='main'):
        """Load current positions from database for specific account"""
        conn = self._get_connection()
        if not conn:
            return []
            
        try:
            with conn.cursor() as cursor:
                # Based on legacy code: "SELECT * FROM positions"
                # Assuming schema: code, name, buy_price, current_price, quantity, ...
                # Build positions with their actual purchase date for T+1 rule
                sql = """SELECT p.*, 
                         (SELECT MIN(date) FROM transactions t WHERE t.code = p.code AND t.account = p.account AND t.type = 'BUY') AS created_at 
                         FROM positions p WHERE p.account=%s"""
                cursor.execute(sql, (account,))
                result = cursor.fetchall()
                # If result is list of dicts (DictCursor), great.
                # Legacy returns list of tuples because of sqlite default.
                # We will adapt to return standardized dicts.
                return result
        except Exception as e:
            logger.error(f"Failed to load positions: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def load_all_positions(self):
        """Load current positions from database for ALL accounts"""
        conn = self._get_connection()
        if not conn:
            return []
            
        try:
            with conn.cursor() as cursor:
                sql = """SELECT p.*, 
                         (SELECT MIN(date) FROM transactions t WHERE t.code = p.code AND t.account = p.account AND t.type = 'BUY') AS created_at 
                         FROM positions p"""
                cursor.execute(sql)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to load all positions: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def count_paper_weak_buys(self, account='paper_watchlist', trade_date=None):
        """Count today's paper weak-gate BUY samples for per-day experiment caps."""
        conn = self._get_connection()
        if not conn:
            return 0

        try:
            date_str = str(trade_date or datetime.now().strftime("%Y-%m-%d"))[:10]
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM transactions
                    WHERE account=%s
                      AND type='BUY'
                      AND DATE(date)=%s
                      AND signal_tags_json LIKE %s
                    """,
                    (account, date_str, "%PAPER_WEAK_%"),
                )
                row = cursor.fetchone() or {}
                return int(row.get("cnt") or 0)
        except Exception as e:
            logger.warning("Failed to count paper weak buys account=%s: %s", account, e)
            return 0
        finally:
            if conn:
                conn.close()

    def load_cash(self, account='main'):
        """Load latest cash balance for specific account"""
        if is_virtual_account(account) and not is_paper_account(account):
            return 0.0

        conn = self._get_connection()
        if not conn:
            return float(self.get_initial_capital(account))

        try:
            with conn.cursor() as cursor:
                sql = "SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1"
                cursor.execute(sql, (account,))
                result = cursor.fetchone()
                if result:
                    return float(result['cash'])
                return float(self.get_initial_capital(account))
        except Exception as e:
            logger.error(f"Failed to load cash: {e}")
            return float(self.get_initial_capital(account))
        finally:
            if conn:
                conn.close()

    def load_cash_for_trading(self, account='main'):
        """Load cash for buy sizing. Fail closed if DB/cash row is unavailable."""
        if is_virtual_account(account):
            return float(self.load_cash(account) or self.get_initial_capital(account))

        conn = self._get_connection()
        if not conn:
            logger.error(f"[{account}] Cannot load trading cash: database connection failed")
            return None

        try:
            with conn.cursor() as cursor:
                sql = "SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1"
                cursor.execute(sql, (account,))
                result = cursor.fetchone()
                if not result:
                    logger.error(f"[{account}] Cannot load trading cash: no portfolio_value row")
                    return None
                return float(result['cash'])
        except Exception as e:
            logger.error(f"[{account}] Cannot load trading cash: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_strategy_win_rate(self, *, strategy: str, analysis_cycle: str = 'T+1', lookback_days: int = 20, win_statuses: list[str] | None = None) -> dict:
        """Compute rolling win-rate for a strategy from strategy_performance_history.

        Returns a dict like:
        {"cnt": int, "win_cnt": int, "win_rate": float, "lookback_days": int, "analysis_cycle": str}
        """
        if not strategy:
            return {"cnt": 0, "win_cnt": 0, "win_rate": 0.0, "lookback_days": int(lookback_days), "analysis_cycle": str(analysis_cycle)}

        try:
            lookback_days = int(lookback_days)
        except Exception:
            lookback_days = 20

        cache_key = (str(strategy), str(analysis_cycle), int(lookback_days), tuple((win_statuses or [])))
        if cache_key in self._win_rate_cache:
            return self._win_rate_cache[cache_key]

        if not win_statuses:
            win_statuses = ['涨停', '吃肉']

        conn = self._get_connection()
        if not conn:
            res = {"cnt": 0, "win_cnt": 0, "win_rate": 0.0, "lookback_days": int(lookback_days), "analysis_cycle": str(analysis_cycle)}
            self._win_rate_cache[cache_key] = res
            return res

        try:
            with conn.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(win_statuses))
                sql = f"""
                    SELECT
                        COUNT(1) AS cnt,
                        SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END) AS win_cnt
                    FROM strategy_performance_history
                    WHERE strategy=%s
                      AND analysis_cycle=%s
                      AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                """
                params = tuple(list(win_statuses) + [strategy, analysis_cycle, lookback_days])
                cursor.execute(sql, params)
                row = cursor.fetchone() or {}
                cnt = int(row.get('cnt', 0) or 0)
                win_cnt = int(row.get('win_cnt', 0) or 0)
                win_rate = (win_cnt / cnt) if cnt > 0 else 0.0
                res = {"cnt": cnt, "win_cnt": win_cnt, "win_rate": float(win_rate), "lookback_days": int(lookback_days), "analysis_cycle": str(analysis_cycle)}
                self._win_rate_cache[cache_key] = res
                return res
        except Exception as e:
            logger.warning(f"Failed to compute win rate for {strategy}: {e}")
            res = {"cnt": 0, "win_cnt": 0, "win_rate": 0.0, "lookback_days": int(lookback_days), "analysis_cycle": str(analysis_cycle)}
            self._win_rate_cache[cache_key] = res
            return res
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_initial_capital(self, account='main'):
        """Get initial capital for an account from the accounts table"""
        if is_virtual_account(account) and not is_paper_account(account):
            return 0.0
            
        conn = self._get_connection()
        if not conn:
            return Config.RISK_MANAGEMENT.get('INITIAL_CAPITAL', {}).get(account, 20000.0)
        
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT initial_capital FROM accounts WHERE account=%s", (account,))
                result = cursor.fetchone()
                if result:
                    return float(result['initial_capital'])
                return Config.RISK_MANAGEMENT.get('INITIAL_CAPITAL', {}).get(account, 20000.0)
        except Exception as e:
            logger.warning(f"Failed to get initial capital from DB, using config fallback: {e}")
            return Config.RISK_MANAGEMENT.get('INITIAL_CAPITAL', {}).get(account, 20000.0)
        finally:
            if conn:
                conn.close()
                
    def update_cash(self, cash_value, account='main'):
        """Update cash record for specific account"""
        conn = self._get_connection()
        if not conn:
            return
            
        try:
            with conn.cursor() as cursor:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # [Bug Fix] Use REPLACE INTO to handle multiple updates within the same second (Primary Key: date, account)
                sql = "REPLACE INTO portfolio_value (date, account, cash) VALUES (%s, %s, %s)"
                cursor.execute(sql, (timestamp, account, cash_value))
                conn.commit()
                logger.info(f"Updated cash: {cash_value}")
        except Exception as e:
            logger.error(f"Failed to update cash: {e}")
        finally:
            if conn:
                conn.close()

    def calculate_risk_size(self, code, price, atr_data, account='main', risk_percent=0.02, max_position_pct=0.30):
        """
        [V13] 动态仓位计算 - 基于2%风险准则
        
        公式: 买入金额 = (总资产 * 单笔风险比例) / 止损空间
        
        Args:
            code: 股票代码
            price: 当前价格
            atr_data: ATR数据 dict {'atr': float, 'atr_percent': float, 'volatility': str}
            account: 账户
            risk_percent: 单笔风险比例 (默认2%)
            max_position_pct: 单票最大仓位比例 (默认30%)
        
        Returns:
            dict: {'quantity': int, 'amount': float, 'risk_amount': float, 'stop_loss_pct': float}
        """
        try:
            # Get total asset
            cash = float(self.load_cash(account))
            
            # Get position value if any
            positions = self.load_all_positions()
            current_pos_value = sum(float(p.get("market_value", 0) or 0) for p in positions if p.get("account") == account)
            total_asset = cash + current_pos_value
            
            # Calculate stop loss percentage based on ATR
            if atr_data and atr_data.get('atr_percent'):
                vol = atr_data.get('volatility', 'medium')
                # Get N multiplier from config
                cfg = {}
                try:
                    from core.config import Config
                    Config.load_strategy_config()
                    cfg = Config.STRATEGY.get('atr_volatility', {}).get('multipliers', {})
                except:
                    pass
                
                n_multiplier = cfg.get(vol, 2.0)
                atr_pct = atr_data.get('atr_percent', 4.0)
                stop_loss_pct = (atr_pct * n_multiplier) / 100  # Convert to decimal
            else:
                # Default 5% stop loss if no ATR
                stop_loss_pct = 0.05
            
            # Ensure minimum stop loss
            stop_loss_pct = max(stop_loss_pct, 0.03)  # At least 3%
            
            # Calculate risk amount (2% of total asset)
            risk_amount = total_asset * risk_percent
            
            # Calculate position size
            position_value = risk_amount / stop_loss_pct
            
            # Apply max position cap
            max_amount = total_asset * max_position_pct
            position_value = min(position_value, max_amount)
            
            # Calculate quantity (round to 100 shares)
            quantity = int(position_value / price / 100) * 100
            
            return {
                'quantity': quantity,
                'amount': round(quantity * price, 2),
                'risk_amount': round(risk_amount, 2),
                'stop_loss_pct': round(stop_loss_pct * 100, 1),
                'total_asset': round(total_asset, 2),
                'volatility': atr_data.get('volatility', 'unknown') if atr_data else 'unknown'
            }
            
        except Exception as e:
            logger.error(f"Risk size calculation failed: {e}")
            return None

    def init_tables(self):
        """Initialize database tables"""
        conn = self._get_connection()
        if not conn: return
        
        try:
            with conn.cursor() as cursor:
                # 1. Strategy Selection
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS strategy_selection (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        date VARCHAR(20),
                        strategy VARCHAR(50),
                        code VARCHAR(20),
                        name VARCHAR(50),
                        sel_price FLOAT,
                        change_pct FLOAT,
                        turnover FLOAT,
                        sector VARCHAR(50),
                        zt_result VARCHAR(20),
                        observe_status VARCHAR(20) DEFAULT 'ACTIVE',
                        observe_reason VARCHAR(255),
                        observe_updated_at DATETIME,
                        observe_end_at DATETIME,
                        observe_end_reason VARCHAR(255),
                        analysis_cycle VARCHAR(10) DEFAULT 'T+1',
                        snapshot_id BIGINT,
                        score_total FLOAT,
                        tags_json LONGTEXT,
                        data_quality VARCHAR(20),
                        created_at DATETIME
                    )
                """)
                
                # [V18] Upgrade schema: Add analysis_cycle if missing
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN analysis_cycle VARCHAR(10) DEFAULT 'T+1' AFTER zt_result")
                except Exception:
                    pass

                # [V9] Upgrade schema if needed
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN sel_price FLOAT AFTER name")
                except Exception:
                    pass # Column exists or other error

                # [VNext] Attribution & factor snapshot columns
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN snapshot_id BIGINT AFTER analysis_cycle")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN score_total FLOAT AFTER snapshot_id")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN tags_json LONGTEXT AFTER score_total")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE strategy_selection ADD COLUMN data_quality VARCHAR(20) AFTER tags_json")
                except Exception:
                    pass
                observe_cols_to_add = [
                    ("observe_status", "VARCHAR(20) DEFAULT 'ACTIVE' AFTER zt_result"),
                    ("observe_reason", "VARCHAR(255) AFTER observe_status"),
                    ("observe_updated_at", "DATETIME AFTER observe_reason"),
                    ("observe_end_at", "DATETIME AFTER observe_updated_at"),
                    ("observe_end_reason", "VARCHAR(255) AFTER observe_end_at"),
                ]
                for col_name, col_def in observe_cols_to_add:
                    try:
                        cursor.execute(f"ALTER TABLE strategy_selection ADD COLUMN {col_name} {col_def}")
                    except Exception:
                        pass

                # [V17] Add unique index for (date, strategy, code) to prevent duplicates
                # and enable ON DUPLICATE KEY UPDATE while preserving created_at
                try:
                    cursor.execute("CREATE UNIQUE INDEX idx_date_strat_code ON strategy_selection (date, strategy, code)")
                except Exception:
                    pass # Index already exists

                try:
                    cursor.execute("""
                        UPDATE strategy_selection
                        SET observe_status='REMOVED',
                            observe_end_at=COALESCE(observe_end_at, NOW()),
                            observe_end_reason=COALESCE(observe_end_reason, '历史记录: zt_result=已剔除')
                        WHERE zt_result='已剔除'
                          AND (observe_status IS NULL OR observe_status IN ('ACTIVE', 'WATCHING'))
                    """)
                    cursor.execute("""
                        UPDATE strategy_selection
                        SET observe_status='BOUGHT',
                            observe_end_at=COALESCE(observe_end_at, NOW()),
                            observe_end_reason=COALESCE(observe_end_reason, '历史记录: zt_result=已买入')
                        WHERE zt_result='已买入'
                          AND (observe_status IS NULL OR observe_status IN ('ACTIVE', 'WATCHING', 'PENDING'))
                    """)
                    cursor.execute("""
                        UPDATE strategy_selection
                        SET observe_status='ACTIVE',
                            observe_reason=COALESCE(observe_reason, '历史记录: 默认仍在观察期'),
                            observe_updated_at=COALESCE(observe_updated_at, created_at)
                        WHERE observe_status IS NULL
                    """)
                    cursor.execute("""
                        UPDATE strategy_selection
                        SET observe_reason=COALESCE(observe_reason, '历史记录: 默认仍在观察期'),
                            observe_updated_at=COALESCE(observe_updated_at, created_at)
                        WHERE observe_status='ACTIVE'
                          AND observe_reason IS NULL
                    """)
                except Exception:
                    pass

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS selection_observation_events (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        selection_id INT,
                        date VARCHAR(20),
                        strategy VARCHAR(50),
                        code VARCHAR(20),
                        name VARCHAR(50),
                        event_type VARCHAR(40),
                        from_status VARCHAR(20),
                        to_status VARCHAR(20),
                        reason VARCHAR(255),
                        metrics_json LONGTEXT,
                        created_at DATETIME,
                        INDEX idx_selection (selection_id),
                        INDEX idx_date_strategy_code (date, strategy, code),
                        INDEX idx_event_created (event_type, created_at),
                        INDEX idx_code_created (code, created_at)
                    )
                """)
                
                # 2. Strategy Stats
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS strategy_stats (
                        date VARCHAR(20) PRIMARY KEY,
                        strategy VARCHAR(50),
                        total INT,
                        zt_count INT,
                        success_rate FLOAT,
                        updated_at DATETIME
                    )
                """)

                # 3. Portfolio Value
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_value (
                        date VARCHAR(20),
                        account VARCHAR(20) DEFAULT 'main',
                        total_value FLOAT,
                        cash FLOAT,
                        market_value FLOAT,
                        return_rate FLOAT,
                        PRIMARY KEY (date, account)
                    )
                """)

                # 3.5 Accounts (initial capital tracking)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        account VARCHAR(20) PRIMARY KEY,
                        initial_capital FLOAT NOT NULL DEFAULT 20000,
                        created_at DATETIME
                    )
                """)
                # Seed default accounts if not exist
                cursor.execute("SELECT COUNT(*) as cnt FROM accounts")
                if cursor.fetchone()['cnt'] == 0:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("INSERT INTO accounts (account, initial_capital, created_at) VALUES (%s, %s, %s)", ('main', 20000.0, now))
                    cursor.execute("INSERT INTO accounts (account, initial_capital, created_at) VALUES (%s, %s, %s)", ('watchlist', 10000.0, now))
                    cursor.execute("INSERT INTO accounts (account, initial_capital, created_at) VALUES (%s, %s, %s)", ('paper_main', 20000.0, now))
                    cursor.execute("INSERT INTO accounts (account, initial_capital, created_at) VALUES (%s, %s, %s)", ('paper_watchlist', 10000.0, now))
                    logger.info("Seeded default accounts: main=20000, watchlist=10000, paper_main=20000, paper_watchlist=10000")
                else:
                    try:
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        defaults = {
                            'paper_main': Config.RISK_MANAGEMENT.get('INITIAL_CAPITAL', {}).get('paper_main', 20000.0),
                            'paper_watchlist': Config.RISK_MANAGEMENT.get('INITIAL_CAPITAL', {}).get('paper_watchlist', 10000.0),
                        }
                        for acc, capital in defaults.items():
                            cursor.execute(
                                "INSERT IGNORE INTO accounts (account, initial_capital, created_at) VALUES (%s, %s, %s)",
                                (acc, float(capital), now),
                            )
                    except Exception:
                        pass

                # 4. Positions (Enhanced for V9 Trailing Stop)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        code VARCHAR(20),
                        account VARCHAR(20) DEFAULT 'main',
                        name VARCHAR(50),
                        buy_price FLOAT,
                        avg_price FLOAT,
                        current_price FLOAT,
                        quantity INT,
                        market_value FLOAT,
                        highest_price FLOAT,
                        pnl FLOAT,
                        pnl_pct FLOAT,
                        cost FLOAT,
                        sell_stage INT DEFAULT 0,
                        update_time DATETIME,
                        created_at DATETIME,
                        entry_snapshot_id BIGINT,
                        entry_strategy VARCHAR(50),
                        entry_tags_json LONGTEXT,
                        PRIMARY KEY (code, account)
                    )
                """)

                # Upgrade positions table if columns are missing
                columns_to_add = [
                    ("avg_price", "FLOAT AFTER buy_price"),
                    ("market_value", "FLOAT AFTER quantity"),
                    ("highest_price", "FLOAT AFTER market_value"),
                    ("pnl", "FLOAT AFTER highest_price"),
                    ("pnl_pct", "FLOAT AFTER pnl"),
                    ("sell_stage", "INT DEFAULT 0 AFTER pnl_pct"),
                    ("update_time", "DATETIME AFTER cost"),
                    ("entry_snapshot_id", "BIGINT AFTER created_at"),
                    ("entry_strategy", "VARCHAR(50) AFTER entry_snapshot_id"),
                    ("entry_tags_json", "LONGTEXT AFTER entry_strategy")
                ]
                for col_name, col_def in columns_to_add:
                    try:
                        cursor.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_def}")
                    except Exception:
                        pass # Column likely exists
                self._ensure_positions_account_key(cursor)

                # 5. Transactions
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        date DATETIME,
                        account VARCHAR(20) DEFAULT 'main',
                        type VARCHAR(10),
                        code VARCHAR(20),
                        name VARCHAR(50),
                        price FLOAT,
                        quantity INT,
                        amount FLOAT,
                        balance FLOAT,
                        reason VARCHAR(100),
                        snapshot_id BIGINT,
                        source_strategy VARCHAR(50),
                        weather VARCHAR(10),
                        signal_tags_json LONGTEXT,
                        selection_id INT
                    )
                """)
                
                # [V17] Upgrade transactions table if reason is missing
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN reason VARCHAR(100) AFTER balance")
                except Exception:
                    pass

                # [VNext] Attribution columns for post-trade audit  evolver alignment
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN snapshot_id BIGINT AFTER reason")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN source_strategy VARCHAR(50) AFTER snapshot_id")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN weather VARCHAR(10) AFTER source_strategy")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN signal_tags_json LONGTEXT AFTER weather")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN selection_id INT AFTER signal_tags_json")
                except Exception:
                    pass

                # 6. Strategy Performance History (T+1 Backtest data)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS strategy_performance_history (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        date VARCHAR(20),
                        strategy VARCHAR(50),
                        code VARCHAR(20),
                        name VARCHAR(50),
                        buy_price FLOAT,
                        max_price FLOAT,
                        close_price FLOAT,
                        max_ret FLOAT,
                        close_ret FLOAT,
                        status VARCHAR(20),
                        analysis_cycle VARCHAR(10) DEFAULT 'T+1',
                        snapshot_id BIGINT,
                        tags_json LONGTEXT,
                        weather VARCHAR(10),
                        created_at DATETIME,
                        INDEX idx_date (date),
                        INDEX idx_strategy (strategy),
                        INDEX idx_snapshot (snapshot_id)
                    )
                """)

                # Upgrade strategy_performance_history table if columns are missing (additive)
                perf_cols_to_add = [
                    ("analysis_cycle", "VARCHAR(10) DEFAULT 'T+1' AFTER status"),
                    ("snapshot_id", "BIGINT AFTER analysis_cycle"),
                    ("tags_json", "LONGTEXT AFTER snapshot_id"),
                    ("weather", "VARCHAR(10) AFTER tags_json"),
                ]
                for col_name, col_def in perf_cols_to_add:
                    try:
                        cursor.execute(f"ALTER TABLE strategy_performance_history ADD COLUMN {col_name} {col_def}")
                    except Exception:
                        pass

                # [VNext] New tables for full-chain analytics
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS factor_snapshot (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        trade_date VARCHAR(20),
                        strategy VARCHAR(50),
                        analysis_cycle VARCHAR(10) DEFAULT 'T+1',
                        code VARCHAR(20),
                        ts_code VARCHAR(20),
                        name VARCHAR(50),
                        snapshot_version VARCHAR(20),
                        score_total FLOAT,
                        factors_json LONGTEXT,
                        tags_json LONGTEXT,
                        data_quality VARCHAR(20),
                        created_at DATETIME,
                        INDEX idx_trade_date (trade_date),
                        INDEX idx_strategy (strategy),
                        INDEX idx_code_date (code, trade_date)
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS market_sentiment_daily (
                        trade_date VARCHAR(20) PRIMARY KEY,
                        weather VARCHAR(10),
                        regime VARCHAR(32),
                        trend_state VARCHAR(32),
                        sentiment_state VARCHAR(32),
                        risk_level VARCHAR(20),
                        is_safe TINYINT(1) DEFAULT 1,
                        message VARCHAR(255),
                        permission_json LONGTEXT,
                        risk_reasons_json LONGTEXT,
                        limit_up INT,
                        limit_down INT,
                        limit_up_height INT,
                        ladder_json LONGTEXT,
                        sector_top_json LONGTEXT,
                        ecosystem_json LONGTEXT,
                        created_at DATETIME
                    )
                """)

                # Upgrade market_sentiment_daily for structured regime/permission fields
                for col_name, col_def in [
                    ("regime", "VARCHAR(32) AFTER weather"),
                    ("trend_state", "VARCHAR(32) AFTER regime"),
                    ("sentiment_state", "VARCHAR(32) AFTER trend_state"),
                    ("permission_json", "LONGTEXT AFTER message"),
                    ("risk_reasons_json", "LONGTEXT AFTER permission_json"),
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE market_sentiment_daily ADD COLUMN {col_name} {col_def}")
                    except Exception:
                        pass

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS risk_event_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        event_time DATETIME,
                        account VARCHAR(20),
                        code VARCHAR(20),
                        event_type VARCHAR(30),
                        weather VARCHAR(10),
                        reason VARCHAR(255),
                        params_json LONGTEXT,
                        INDEX idx_event_time (event_time),
                        INDEX idx_code_time (code, event_time)
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS evolution_audit_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        run_time DATETIME,
                        dry_run TINYINT(1) DEFAULT 0,
                        window_days INT,
                        changes_json LONGTEXT,
                        metrics_json LONGTEXT,
                        created_at DATETIME,
                        INDEX idx_run_time (run_time)
                    )
                """)

                # [VNext] Concept heat daily stats (additive)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS concept_heat_daily (
                        trade_date VARCHAR(20) PRIMARY KEY,
                        heat_json LONGTEXT,
                        top_json LONGTEXT,
                        total_candidates INT DEFAULT 0,
                        created_at DATETIME
                    )
                """)

                # [VNext] Tag performance aggregation (observation-only, supports evolver)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS factor_tag_daily_stats (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        date VARCHAR(20),
                        tag VARCHAR(64),
                        cnt INT,
                        win_cnt INT,
                        win_rate FLOAT,
                        avg_max_ret FLOAT,
                        avg_close_ret FLOAT,
                        max_max_ret FLOAT,
                        min_close_ret FLOAT,
                        created_at DATETIME,
                        UNIQUE KEY uk_date_tag (date, tag),
                        INDEX idx_date (date),
                        INDEX idx_tag (tag)
                    )
                """)

                # [VNext] Time-bucket × weather aggregation (observation-only, supports capital safety)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS time_bucket_weather_daily_stats (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        date VARCHAR(20),
                        analysis_cycle VARCHAR(10) DEFAULT 'T+1',
                        weather VARCHAR(10),
                        time_bucket VARCHAR(16),
                        bucket_label VARCHAR(24),
                        cnt INT,
                        win_cnt INT,
                        win_rate FLOAT,
                        avg_max_ret FLOAT,
                        avg_close_ret FLOAT,
                        max_max_ret FLOAT,
                        min_close_ret FLOAT,
                        p5_close_ret FLOAT,
                        created_at DATETIME,
                        UNIQUE KEY uk_date_cycle_weather_bucket (date, analysis_cycle, weather, time_bucket),
                        INDEX idx_date (date),
                        INDEX idx_weather (weather),
                        INDEX idx_bucket (time_bucket)
                    )
                """)

                # [VNext] Pending entry signals (dynamic window entry, default disabled)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pending_entry_signals (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        trade_date VARCHAR(20),
                        code VARCHAR(20),
                        name VARCHAR(50),
                        ts_code VARCHAR(20),
                        source_strategy VARCHAR(50),
                        target_account VARCHAR(32) DEFAULT 'main',
                        signal_time DATETIME,
                        expires_at DATETIME,
                        weather VARCHAR(10),
                        signal_bucket VARCHAR(16),
                        entry_model VARCHAR(32),
                        payload_json LONGTEXT,
                        status VARCHAR(16) DEFAULT 'PENDING',
                        last_checked_at DATETIME,
                        check_count INT DEFAULT 0,
                        last_reason VARCHAR(255),
                        created_at DATETIME,
                        updated_at DATETIME,
                        UNIQUE KEY uk_trade_code_strat_account (trade_date, code, source_strategy, target_account),
                        INDEX idx_trade_status (trade_date, status),
                        INDEX idx_expires_at (expires_at),
                        INDEX idx_code_trade (code, trade_date)
                    )
                """)
                try:
                    cursor.execute("ALTER TABLE pending_entry_signals ADD COLUMN target_account VARCHAR(32) DEFAULT 'main' AFTER source_strategy")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE pending_entry_signals DROP INDEX uk_trade_code_strat")
                except Exception:
                    pass
                try:
                    cursor.execute(
                        "ALTER TABLE pending_entry_signals "
                        "ADD UNIQUE KEY uk_trade_code_strat_account (trade_date, code, source_strategy, target_account)"
                    )
                except Exception:
                    pass

                # Pending check events: full audit trail for every sentinel re-check.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pending_entry_check_events (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        pending_id BIGINT,
                        trade_date VARCHAR(20),
                        code VARCHAR(20),
                        account VARCHAR(32),
                        strategy VARCHAR(50),
                        check_time DATETIME,
                        bucket VARCHAR(16),
                        price FLOAT,
                        pre_close FLOAT,
                        change_pct FLOAT,
                        volume_ratio FLOAT,
                        price_vwap_ratio FLOAT,
                        decision VARCHAR(16),
                        reason VARCHAR(255),
                        status_before VARCHAR(16),
                        status_after VARCHAR(16),
                        check_count INT,
                        payload_json LONGTEXT,
                        created_at DATETIME,
                        INDEX idx_pending_id (pending_id),
                        INDEX idx_trade_account_strategy (trade_date, account, strategy),
                        INDEX idx_trade_decision (trade_date, decision),
                        INDEX idx_code_trade (code, trade_date),
                        INDEX idx_check_time (check_time)
                    )
                """)

                conn.commit()
                logger.info("Tables initialized.")
        except Exception as e:
            logger.error(f"Failed to init tables: {e}")
        finally:
            conn.close()

    def execute_buy(self, code, name, price, quantity, account='main', *, snapshot_id=None, source_strategy=None, weather=None, signal_tags_json=None, selection_id=None):
        """Execute Buy Transaction

        [VNext] Optional attribution fields are written into:
        - positions.entry_snapshot_id/entry_strategy/entry_tags_json (on first entry only)
        - transactions.snapshot_id/source_strategy/weather/signal_tags_json/selection_id
        """
        conn = self._get_connection()
        if not conn:
            return False, "DB Connection Failed"
        
        is_virtual = is_virtual_account(account)
        
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 1. Check Cash atomically within this transaction.
                # Paper accounts keep their own simulated cash ledger.
                if is_paper_account(account):
                    cursor.execute("SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1", (account,))
                    cash_row = cursor.fetchone()
                    cash = float(cash_row['cash']) if cash_row else float(self.get_initial_capital(account))
                    cost = price * quantity * 1.0003 # 0.03% Comm
                    if cash < cost:
                        logger.warning(f"[{account}] Insufficient simulated funds: {cash} < {cost}")
                        return False, f"Insufficient simulated funds: {cash:.2f} < {cost:.2f}"
                    new_cash = cash - cost
                elif not is_virtual:
                    cursor.execute("SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1", (account,))
                    cash_row = cursor.fetchone()
                    if not cash_row:
                        msg = f"[{account}] Missing cash row; buy blocked"
                        logger.error(msg)
                        return False, msg
                    cash = float(cash_row['cash'])
                    
                    cost = price * quantity * 1.0003 # 0.03% Comm
                    if cash < cost:
                        logger.warning(f"[{account}] Insufficient funds: {cash} < {cost}")
                        return False, f"Insufficient funds: {cash:.2f} < {cost:.2f}"
                    # 2. Update Cash
                    new_cash = cash - cost
                else:
                    # 虚拟账户：不扣现金，记录成本用于计算
                    cost = price * quantity * 1.0003
                    new_cash = 0  # 不更新现金
                
                # 3. Add/Update Position
                cursor.execute("SELECT * FROM positions WHERE code=%s AND account=%s", (code, account))
                exist = cursor.fetchone()
                
                if exist:
                    # Update existing
                    old_q = int(exist['quantity'])
                    # float conversion for safety
                    try: old_avg = float(exist['avg_price'])
                    except: old_avg = float(exist.get('buy_price', 0))
                    
                    new_q = old_q + quantity
                    total_cost = (old_q * old_avg) + cost
                    new_avg = total_cost / new_q
                    
                    # Update fields: quantity, avg_price, market_value (approx), highest_price, update_time
                    sql = "UPDATE positions SET quantity=%s, avg_price=%s, market_value=%s, highest_price=GREATEST(IFNULL(highest_price, 0), %s), update_time=%s WHERE code=%s AND account=%s"
                    cursor.execute(sql, (new_q, new_avg, price * new_q, price, now, code, account))
                else:
                    # Insert new
                    sql = """INSERT INTO positions 
                             (code, account, name, buy_price, quantity, avg_price, market_value, highest_price, pnl, pnl_pct, update_time, created_at) 
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s, %s)"""
                    cursor.execute(sql, (code, account, name, price, quantity, price, price * quantity, price, now, now))

                    # [VNext] Store entry attribution on first entry only
                    try:
                        if snapshot_id is not None or source_strategy or signal_tags_json:
                            cursor.execute(
                                "UPDATE positions SET entry_snapshot_id=%s, entry_strategy=%s, entry_tags_json=%s WHERE code=%s AND account=%s",
                                (snapshot_id, source_strategy, signal_tags_json, code, account)
                            )
                    except Exception:
                        pass
                
                # 4. Securely Update Cash via ON DUPLICATE.
                # Paper accounts persist simulated cash; rescue remains cashless.
                if (not is_virtual) or is_paper_account(account):
                    sql_cash = "INSERT INTO portfolio_value (date, account, cash) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE cash=VALUES(cash)"
                    cursor.execute(sql_cash, (now, account, new_cash))

                # 5. Log Transaction
                sql_trans = """INSERT INTO transactions
                               (date, account, type, code, name, price, quantity, amount, balance,
                                snapshot_id, source_strategy, weather, signal_tags_json, selection_id)
                               VALUES (%s, %s, 'BUY', %s, %s, %s, %s, %s, %s,
                                       %s, %s, %s, %s, %s)"""
                # 虚拟账户记录成本为0，不影响真实余额
                balance = new_cash if ((not is_virtual) or is_paper_account(account)) else 0
                cursor.execute(
                    sql_trans,
                    (
                        now, account, code, name, price, quantity, -cost, balance,
                        snapshot_id, source_strategy, weather, signal_tags_json, selection_id,
                    )
                )
                
                conn.commit()
                msg = f"[{account}] 买入成功: {code} {name}, {quantity}股 @ {price}, 金额: {cost:.2f}"
                logger.info(msg)
                return True, msg
        except Exception as e:
            try:
                conn.rollback()
            except: pass
            msg = f"买入失败 {code}: {e}"
            logger.error(msg)
            return False, msg
        finally:
            conn.close()

    def update_highest_price(self, code, account, highest_price):
        """Update highest price for tracking trailing stop"""
        conn = self._get_connection()
        if not conn: return
        try:
            with conn.cursor() as cursor:
                sql = "UPDATE positions SET highest_price=%s WHERE code=%s AND account=%s"
                cursor.execute(sql, (highest_price, code, account))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to update highest price for {code} [{account}]: {e}")
        finally:
            conn.close()

    def update_sell_stage(self, code, account, stage):
        """Update sell stage for laddered take profit"""
        conn = self._get_connection()
        if not conn: return
        try:
            with conn.cursor() as cursor:
                sql = "UPDATE positions SET sell_stage=%s WHERE code=%s AND account=%s"
                cursor.execute(sql, (stage, code, account))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to update sell stage for {code} [{account}]: {e}")
        finally:
            conn.close()

    def execute_sell(self, code, price, reason="By User", account='main', percentage=1.0, *, snapshot_id=None, source_strategy=None, weather=None, signal_tags_json=None, selection_id=None):
        """Execute Sell Transaction - Supports Partial Selling (V18)

        [VNext] Optional attribution fields are written to transactions.
        """
        conn = self._get_connection()
        if not conn: return False, "DB Connection Failed"
        
        is_virtual = is_virtual_account(account)
        
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 1. Check Position
                cursor.execute("SELECT * FROM positions WHERE code=%s AND account=%s", (code, account))
                pos = cursor.fetchone()
                if not pos: 
                    return False, f"[{account}] 无持仓: {code}"
                
                total_qty = int(pos['quantity'])
                name = pos['name']
                
                # Determine sell quantity
                sell_qty = int(total_qty * percentage)
                # Round to nearest 100 for A-shares
                if percentage < 1.0:
                    sell_qty = (sell_qty // 100) * 100
                    if sell_qty <= 0:
                        return False, f"[{account}] 减仓失败: {code} 数量过少"
                else:
                    sell_qty = total_qty 

                try: avg_price = float(pos['avg_price'])
                except: avg_price = float(pos.get('buy_price', 0))
                
                amount = price * sell_qty * 0.999 # 0.1% Tax + Comm (Approx)
                
                # P&L
                buy_cost_part = avg_price * sell_qty
                pnl = amount - buy_cost_part
                pnl_pct = (pnl / buy_cost_part) * 100 if buy_cost_part > 0 else 0
                
                # 2. Update Cash Atomically.
                # Paper accounts keep their own simulated cash ledger.
                if is_paper_account(account):
                    cursor.execute("SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1", (account,))
                    cash_row = cursor.fetchone()
                    cash = float(cash_row['cash']) if cash_row else float(self.get_initial_capital(account))
                    new_cash = cash + amount
                elif not is_virtual:
                    cursor.execute("SELECT cash FROM portfolio_value WHERE account=%s ORDER BY date DESC LIMIT 1", (account,))
                    cash_row = cursor.fetchone()
                    cash = float(cash_row['cash']) if cash_row else 10000.0
                    new_cash = cash + amount
                else:
                    cash = 0
                    new_cash = 0  # 虚拟账户不更新现金
                
                # 3. Update/Remove Position
                if sell_qty >= total_qty:
                    cursor.execute("DELETE FROM positions WHERE code=%s AND account=%s", (code, account))
                else:
                    new_qty = total_qty - sell_qty
                    cursor.execute("UPDATE positions SET quantity=%s, update_time=%s WHERE code=%s AND account=%s", 
                                 (new_qty, now, code, account))
                
                # 4. Securely Update Cash via ON DUPLICATE.
                # Paper accounts persist simulated cash; rescue remains cashless.
                if (not is_virtual) or is_paper_account(account):
                    sql_cash = "INSERT INTO portfolio_value (date, account, cash) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE cash=VALUES(cash)"
                    cursor.execute(sql_cash, (now, account, new_cash))

                # 5. Log Transaction
                sql_trans = """INSERT INTO transactions
                               (date, account, type, code, name, price, quantity, amount, balance, reason,
                                snapshot_id, source_strategy, weather, signal_tags_json, selection_id)
                               VALUES (%s, %s, 'SELL', %s, %s, %s, %s, %s, %s, %s,
                                       %s, %s, %s, %s, %s)"""
                balance = new_cash if ((not is_virtual) or is_paper_account(account)) else 0
                cursor.execute(
                    sql_trans,
                    (
                        now, account, code, name, price, sell_qty, amount, balance, reason,
                        snapshot_id, source_strategy, weather, signal_tags_json, selection_id,
                    )
                )
                
                conn.commit()
                status_str = "全平" if sell_qty >= total_qty else f"减仓{percentage:.0%}"
                msg = f"[{account}] {status_str}成功: {code} {name}, {sell_qty}股 @ {price:.2f}, 盈亏: {pnl:.2f}元 ({pnl_pct:.2f}%), 原因: {reason}"
                logger.info(msg)
                return True, msg
        except Exception as e:
            try:
                conn.rollback()
            except:
                pass
            msg = f"卖出失败 {code}: {e}"
            logger.error(msg)
            return False, msg
        finally:
            conn.close()

    def log_risk_event(self, *, account: str, code: str, event_type: str, weather: str = None, reason: str = "", params: dict = None):
        """Write an auditable risk/ops event into risk_event_log (best-effort)."""
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                params_json = json.dumps(params or {}, ensure_ascii=False)
                cursor.execute(
                    "INSERT INTO risk_event_log (event_time, account, code, event_type, weather, reason, params_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (now, account, code, event_type, weather, reason, params_json)
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"risk_event_log write failed: {e}")
        finally:
            conn.close()

    def save_evolution_audit(self, *, dry_run: bool, window_days: int, changes, metrics):
        """Persist evolver run details into evolution_audit_log (best-effort, additive)."""
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    """INSERT INTO evolution_audit_log (run_time, dry_run, window_days, changes_json, metrics_json, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        now,
                        1 if dry_run else 0,
                        int(window_days or 0),
                        json.dumps(changes or [], ensure_ascii=False),
                        json.dumps(metrics or {}, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"evolution_audit_log write failed: {e}")
        finally:
            conn.close()

    def save_market_sentiment(self, trade_date, market_env):
        """Save daily market environment/sentiment snapshot into DB (upsert)."""
        import json

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    from core.utils import normalize_weather
                    weather = normalize_weather(market_env.get('weather'))
                except Exception:
                    weather = market_env.get('weather')
                risk_level = market_env.get('risk_level')
                regime = market_env.get('regime')
                trend_state = market_env.get('trend_state')
                sentiment_state = market_env.get('sentiment_state')
                permission = market_env.get('permission', {}) or {}
                risk_reasons = market_env.get('risk_reasons', []) or []
                is_safe = 1 if market_env.get('is_safe', True) else 0
                message = market_env.get('message', '')

                sentiment = market_env.get('sentiment', {}) or {}
                limit_up = sentiment.get('limit_up')
                limit_down = sentiment.get('limit_down')

                ecosystem = market_env.get('ecosystem', {}) or {}
                limit_up_height = ecosystem.get('limit_up_height')
                ladder_json = json.dumps(ecosystem.get('ladder_distribution', {}), ensure_ascii=False)
                sector_top_json = json.dumps(ecosystem.get('sector_top', []), ensure_ascii=False)
                ecosystem_json = json.dumps(ecosystem, ensure_ascii=False)
                permission_json = json.dumps(permission, ensure_ascii=False)
                risk_reasons_json = json.dumps(risk_reasons, ensure_ascii=False)

                sql = """REPLACE INTO market_sentiment_daily
                         (trade_date, weather, regime, trend_state, sentiment_state, risk_level, is_safe, message,
                          permission_json, risk_reasons_json,
                          limit_up, limit_down, limit_up_height,
                          ladder_json, sector_top_json, ecosystem_json, created_at)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

                cursor.execute(sql, (
                    trade_date, weather, regime, trend_state, sentiment_state, risk_level, is_safe, message,
                    permission_json, risk_reasons_json,
                    limit_up, limit_down, limit_up_height,
                    ladder_json, sector_top_json, ecosystem_json,
                    now
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save market sentiment: {e}")
        finally:
            conn.close()

    def save_concept_heat(self, trade_date, concept_heat):
        """Persist concept heat stats (best-effort).

        concept_heat: dict like {
          'total_candidates': int,
          'heat': {concept: count, ...},
          'top': [{'concept': str, 'count': int}, ...]
        }
        """
        import json

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                heat = (concept_heat or {}).get('heat', {}) or {}
                top = (concept_heat or {}).get('top', []) or []
                total_candidates = int((concept_heat or {}).get('total_candidates', 0) or 0)

                sql = """REPLACE INTO concept_heat_daily
                         (trade_date, heat_json, top_json, total_candidates, created_at)
                         VALUES (%s, %s, %s, %s, %s)"""

                cursor.execute(sql, (
                    trade_date,
                    json.dumps(heat, ensure_ascii=False),
                    json.dumps(top, ensure_ascii=False),
                    total_candidates,
                    now,
                ))
                conn.commit()
        except Exception as e:
            logger.debug(f"concept_heat_daily write failed: {e}")
        finally:
            conn.close()

    def save_factor_tag_daily_stats(self, date: str, stats: list[dict]):
        """Upsert aggregated tag performance stats (observation-only).

        stats item format:
          {
            'tag': str,
            'cnt': int,
            'win_cnt': int,
            'win_rate': float (0-1),
            'avg_max_ret': float,
            'avg_close_ret': float,
            'max_max_ret': float,
            'min_close_ret': float,
          }
        """

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO factor_tag_daily_stats
                         (date, tag, cnt, win_cnt, win_rate, avg_max_ret, avg_close_ret, max_max_ret, min_close_ret, created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                         ON DUPLICATE KEY UPDATE
                           cnt=VALUES(cnt),
                           win_cnt=VALUES(win_cnt),
                           win_rate=VALUES(win_rate),
                           avg_max_ret=VALUES(avg_max_ret),
                           avg_close_ret=VALUES(avg_close_ret),
                           max_max_ret=VALUES(max_max_ret),
                           min_close_ret=VALUES(min_close_ret),
                           created_at=VALUES(created_at)"""

                data = []
                for s in stats or []:
                    tag = (s or {}).get('tag')
                    if not tag:
                        continue
                    data.append((
                        date,
                        str(tag)[:64],
                        int((s or {}).get('cnt', 0) or 0),
                        int((s or {}).get('win_cnt', 0) or 0),
                        float((s or {}).get('win_rate', 0.0) or 0.0),
                        float((s or {}).get('avg_max_ret', 0.0) or 0.0),
                        float((s or {}).get('avg_close_ret', 0.0) or 0.0),
                        float((s or {}).get('max_max_ret', 0.0) or 0.0),
                        float((s or {}).get('min_close_ret', 0.0) or 0.0),
                        now,
                    ))

                if data:
                    cursor.executemany(sql, data)
                conn.commit()
        except Exception as e:
            logger.debug(f"factor_tag_daily_stats write failed: {e}")
        finally:
            conn.close()

    def save_time_bucket_weather_daily_stats(self, date: str, stats: list[dict]):
        """Upsert time-bucket × weather performance stats (observation-only).

        Note:
        - This table is used both for observation reports and for later entry-policy evolution.
        - Weather/time_bucket keys should be normalized to the canonical config keys.


        stats item format:
          {
            'analysis_cycle': 'T+1'|'T+2',
            'weather': str,
            'time_bucket': str,  # e.g. 'B1'
            'bucket_label': str, # e.g. '09:30-10:00'
            'cnt': int,
            'win_cnt': int,
            'win_rate': float,
            'avg_max_ret': float,
            'avg_close_ret': float,
            'max_max_ret': float,
            'min_close_ret': float,
            'p5_close_ret': float,
          }
        """

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO time_bucket_weather_daily_stats
                         (date, analysis_cycle, weather, time_bucket, bucket_label, cnt, win_cnt, win_rate,
                          avg_max_ret, avg_close_ret, max_max_ret, min_close_ret, p5_close_ret, created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                         ON DUPLICATE KEY UPDATE
                           cnt=VALUES(cnt),
                           win_cnt=VALUES(win_cnt),
                           win_rate=VALUES(win_rate),
                           avg_max_ret=VALUES(avg_max_ret),
                           avg_close_ret=VALUES(avg_close_ret),
                           max_max_ret=VALUES(max_max_ret),
                           min_close_ret=VALUES(min_close_ret),
                           p5_close_ret=VALUES(p5_close_ret),
                           created_at=VALUES(created_at)"""

                data = []
                for s in stats or []:
                    try:
                        weather = (s or {}).get('weather')
                        time_bucket = (s or {}).get('time_bucket')
                        if not weather or not time_bucket:
                            continue

                        analysis_cycle = (s or {}).get('analysis_cycle') or 'T+1'
                        bucket_label = (s or {}).get('bucket_label') or ''

                        data.append((
                            date,
                            str(analysis_cycle)[:10],
                            str(weather)[:10],
                            str(time_bucket)[:16],
                            str(bucket_label)[:24],
                            int((s or {}).get('cnt', 0) or 0),
                            int((s or {}).get('win_cnt', 0) or 0),
                            float((s or {}).get('win_rate', 0.0) or 0.0),
                            float((s or {}).get('avg_max_ret', 0.0) or 0.0),
                            float((s or {}).get('avg_close_ret', 0.0) or 0.0),
                            float((s or {}).get('max_max_ret', 0.0) or 0.0),
                            float((s or {}).get('min_close_ret', 0.0) or 0.0),
                            float((s or {}).get('p5_close_ret', 0.0) or 0.0),
                            now,
                        ))
                    except Exception:
                        continue

                if data:
                    cursor.executemany(sql, data)
                conn.commit()
        except Exception as e:
            logger.debug(f"time_bucket_weather_daily_stats write failed: {e}")
        finally:
            conn.close()

    # --- Pending entry signals (dynamic window entry) ---

    def upsert_pending_entry_signal(
        self,
        *,
        trade_date: str,
        code: str,
        name: str | None = None,
        ts_code: str | None = None,
        source_strategy: str,
        signal_time=None,
        expires_at=None,
        weather: str | None = None,
        signal_bucket: str | None = None,
        entry_model: str = 'dynamic_window',
        payload: dict | None = None,
        status: str = 'PENDING',
    ) -> None:
        """Insert/update a pending entry signal (best-effort).

        This enables flexible entry within a time bucket window by re-checking entry gates
        across multiple OpenClaw scheduled runs.
        """

        if not trade_date or not code or not source_strategy:
            return

        # Normalize datetimes
        try:
            if signal_time is None:
                signal_time = datetime.now()
            if isinstance(signal_time, str):
                signal_time = datetime.fromisoformat(signal_time.replace('Z', ''))
        except Exception:
            signal_time = datetime.now()

        try:
            if expires_at is None:
                expires_at = datetime.now()
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace('Z', ''))
        except Exception:
            expires_at = datetime.now()

        try:
            if isinstance(payload, str):
                payload_json = payload
                try:
                    payload_obj = json.loads(payload_json or '{}')
                    if not isinstance(payload_obj, dict):
                        payload_obj = {}
                except Exception:
                    payload_obj = {}
            else:
                payload_obj = payload or {}
                payload_json = json.dumps(payload or {}, ensure_ascii=False)
        except Exception:
            payload_obj = {}
            payload_json = "{}"
        target_account = str((payload_obj or {}).get('target_account') or 'main')[:32]

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO pending_entry_signals
                         (trade_date, code, name, ts_code, source_strategy, target_account, signal_time, expires_at,
                          weather, signal_bucket, entry_model, payload_json, status,
                          last_checked_at, check_count, last_reason, created_at, updated_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,0,NULL,%s,%s)
                         ON DUPLICATE KEY UPDATE
                          name=COALESCE(VALUES(name), name),
                          ts_code=COALESCE(VALUES(ts_code), ts_code),
                          target_account=VALUES(target_account),
                          expires_at=GREATEST(expires_at, VALUES(expires_at)),
                          weather=COALESCE(VALUES(weather), weather),
                          signal_bucket=COALESCE(VALUES(signal_bucket), signal_bucket),
                          entry_model=COALESCE(VALUES(entry_model), entry_model),
                          payload_json=COALESCE(VALUES(payload_json), payload_json),
                          status=IF(status IN ('BOUGHT','CANCELLED'), status, VALUES(status)),
                          updated_at=VALUES(updated_at)"""
                cursor.execute(
                    sql,
                    (
                        trade_date,
                        str(code)[:20],
                        (name or '')[:50],
                        (ts_code or '')[:20],
                        str(source_strategy)[:50],
                        target_account,
                        signal_time.strftime("%Y-%m-%d %H:%M:%S"),
                        expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                        (weather or '')[:10],
                        (signal_bucket or '')[:16],
                        str(entry_model)[:32],
                        payload_json,
                        str(status)[:16],
                        now,
                        now,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"pending_entry_signals upsert failed: {e}")
        finally:
            conn.close()

    def upsert_shadow_pending_signal(
        self,
        *,
        trade_date: str,
        code: str,
        name: str | None = None,
        ts_code: str | None = None,
        source_strategy: str,
        signal_time=None,
        expires_at=None,
        weather: str | None = None,
        signal_bucket: str | None = None,
        payload: dict | None = None,
        reason: str | None = None,
    ) -> None:
        """Insert/update an audit-only shadow pending row.

        Shadow rows are deliberately isolated from executable pending rows:
        - status is SHADOW, while load_pending_entry_signals only loads PENDING.
        - source_strategy is suffixed with _SHADOW to avoid unique-key collisions.
        """

        if not trade_date or not code or not source_strategy:
            return

        try:
            if signal_time is None:
                signal_time = datetime.now()
            if isinstance(signal_time, str):
                signal_time = datetime.fromisoformat(signal_time.replace('Z', ''))
        except Exception:
            signal_time = datetime.now()

        try:
            if expires_at is None:
                expires_at = signal_time
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace('Z', ''))
        except Exception:
            expires_at = signal_time

        try:
            if isinstance(payload, str):
                payload_json = payload
                try:
                    payload_obj = json.loads(payload_json or '{}')
                    if not isinstance(payload_obj, dict):
                        payload_obj = {}
                except Exception:
                    payload_obj = {}
            else:
                payload_obj = payload or {}
                payload_json = json.dumps(payload or {}, ensure_ascii=False)
        except Exception:
            payload_obj = {}
            payload_json = "{}"
        target_account = str((payload_obj or {}).get('target_account') or 'shadow')[:32]

        shadow_strategy = f"{str(source_strategy)[:42]}_SHADOW"

        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO pending_entry_signals
                         (trade_date, code, name, ts_code, source_strategy, target_account, signal_time, expires_at,
                          weather, signal_bucket, entry_model, payload_json, status,
                          last_checked_at, check_count, last_reason, created_at, updated_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'audit_only_shadow',%s,'SHADOW',
                                 %s,1,%s,%s,%s)
                         ON DUPLICATE KEY UPDATE
                          name=COALESCE(VALUES(name), name),
                          ts_code=COALESCE(VALUES(ts_code), ts_code),
                          target_account=VALUES(target_account),
                          expires_at=GREATEST(expires_at, VALUES(expires_at)),
                          weather=COALESCE(VALUES(weather), weather),
                          signal_bucket=COALESCE(VALUES(signal_bucket), signal_bucket),
                          entry_model='audit_only_shadow',
                          payload_json=VALUES(payload_json),
                          status=IF(status IN ('BOUGHT','CANCELLED'), status, 'SHADOW'),
                          last_checked_at=VALUES(last_checked_at),
                          check_count=IFNULL(check_count,0)+1,
                          last_reason=VALUES(last_reason),
                          updated_at=VALUES(updated_at)"""
                cursor.execute(
                    sql,
                    (
                        trade_date,
                        str(code)[:20],
                        (name or '')[:50],
                        (ts_code or '')[:20],
                        shadow_strategy[:50],
                        target_account,
                        signal_time.strftime("%Y-%m-%d %H:%M:%S"),
                        expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                        (weather or '')[:10],
                        (signal_bucket or '')[:16],
                        payload_json,
                        now,
                        (reason or '')[:255],
                        now,
                        now,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"shadow pending upsert failed: {e}")
        finally:
            conn.close()

    def load_pending_entry_signals(
        self,
        *,
        trade_date: str,
        now_dt=None,
        limit: int = 50,
        target_accounts: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict]:
        """Load active pending entry signals for a trade date.

        Returns list of dict rows.
        """
        if not trade_date:
            return []

        if now_dt is None:
            now_dt = datetime.now()
        try:
            now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = self._get_connection()
        if not conn:
            return []

        try:
            with conn.cursor() as cursor:
                account_filter = ""
                params = [trade_date, now_str]
                if target_accounts:
                    accounts = [str(a)[:32] for a in target_accounts if a]
                    if accounts:
                        account_filter = " AND target_account IN (" + ",".join(["%s"] * len(accounts)) + ")"
                        params.extend(accounts)
                sql = """SELECT *
                         FROM pending_entry_signals
                         WHERE trade_date=%s
                           AND status='PENDING'
                           AND (expires_at IS NULL OR expires_at >= %s)
                           {account_filter}
                         ORDER BY updated_at DESC
                         LIMIT %s""".format(account_filter=account_filter)
                params.append(int(limit or 0))
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall() or []
                return rows
        except Exception as e:
            logger.debug(f"pending_entry_signals load failed: {e}")
            return []
        finally:
            conn.close()

    def log_pending_entry_check_event(
        self,
        *,
        pending_id: int | None,
        trade_date: str | None = None,
        code: str | None = None,
        account: str | None = None,
        strategy: str | None = None,
        check_time=None,
        bucket: str | None = None,
        price: float | None = None,
        pre_close: float | None = None,
        change_pct: float | None = None,
        volume_ratio: float | None = None,
        price_vwap_ratio: float | None = None,
        decision: str,
        reason: str | None = None,
        status_before: str | None = None,
        status_after: str | None = None,
        check_count: int | None = None,
        payload: dict | None = None,
    ) -> None:
        """Write one sentinel pending re-check decision (best-effort)."""
        if not pending_id or not decision:
            return
        try:
            pending_id = int(pending_id)
        except Exception:
            return
        try:
            if check_time is None:
                check_time = datetime.now()
            if isinstance(check_time, str):
                check_time = datetime.fromisoformat(check_time.replace('Z', ''))
        except Exception:
            check_time = datetime.now()
        try:
            payload_json = json.dumps(payload or {}, ensure_ascii=False)
        except Exception:
            payload_json = "{}"

        def _num(value):
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except Exception:
                return None

        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO pending_entry_check_events
                         (pending_id, trade_date, code, account, strategy, check_time, bucket,
                          price, pre_close, change_pct, volume_ratio, price_vwap_ratio,
                          decision, reason, status_before, status_after, check_count, payload_json, created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
                cursor.execute(
                    sql,
                    (
                        pending_id,
                        (trade_date or '')[:20],
                        (code or '')[:20],
                        (account or '')[:32],
                        (strategy or '')[:50],
                        check_time.strftime("%Y-%m-%d %H:%M:%S"),
                        (bucket or '')[:16],
                        _num(price),
                        _num(pre_close),
                        _num(change_pct),
                        _num(volume_ratio),
                        _num(price_vwap_ratio),
                        str(decision)[:16],
                        (reason or '')[:255],
                        (status_before or '')[:16],
                        (status_after or '')[:16],
                        int(check_count or 0),
                        payload_json,
                        now,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"pending_entry_check_events write failed: {e}")
        finally:
            conn.close()

    def mark_pending_entry_status(self, *, signal_id: int, status: str, reason: str | None = None) -> None:
        """Update status + last_reason (best-effort)."""
        if not signal_id:
            return
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """UPDATE pending_entry_signals
                         SET status=%s,
                             last_reason=%s,
                             updated_at=%s
                         WHERE id=%s"""
                cursor.execute(sql, (str(status)[:16], (reason or '')[:255], now, int(signal_id)))
                conn.commit()
        except Exception as e:
            logger.debug(f"pending_entry_signals status update failed: {e}")
        finally:
            conn.close()

    def touch_pending_entry_check(self, *, signal_id: int, reason: str | None = None) -> None:
        """Increment check_count and update last_checked_at/last_reason (best-effort)."""
        if not signal_id:
            return
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """UPDATE pending_entry_signals
                         SET last_checked_at=%s,
                             check_count=IFNULL(check_count,0)+1,
                             last_reason=%s,
                             updated_at=%s
                         WHERE id=%s"""
                cursor.execute(sql, (now, (reason or '')[:255], now, int(signal_id)))
                conn.commit()
        except Exception as e:
            logger.debug(f"pending_entry_signals touch failed: {e}")
        finally:
            conn.close()

    def expire_old_pending_entries(
        self,
        *,
        trade_date: str,
        now_dt=None,
        target_accounts: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        """Mark expired pending entries as EXPIRED (best-effort)."""
        if not trade_date:
            return 0
        if now_dt is None:
            now_dt = datetime.now()
        try:
            now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                account_filter = ""
                params = [now_str, trade_date, now_str]
                if target_accounts:
                    accounts = [str(a)[:32] for a in target_accounts if a]
                    if accounts:
                        account_filter = " AND target_account IN (" + ",".join(["%s"] * len(accounts)) + ")"
                        params.extend(accounts)
                sql = """UPDATE pending_entry_signals
                         SET status='EXPIRED',
                             last_reason=COALESCE(last_reason, 'window expired'),
                             updated_at=%s
                         WHERE trade_date=%s
                           AND status='PENDING'
                           AND expires_at IS NOT NULL
                           AND expires_at < %s
                           {account_filter}""".format(account_filter=account_filter)
                cursor.execute(sql, tuple(params))
                cnt = int(cursor.rowcount or 0)
                conn.commit()
                return cnt
        except Exception as e:
            logger.debug(f"pending_entry_signals expire failed: {e}")
            return 0
        finally:
            conn.close()

    def save_selection(self, selections, date, strategy="集合竞价", cycle='T+1', market_env=None):
        """Save strategy selections with timing control and created_at protection [V18]

        [VNext] Also persists factor snapshots (best-effort) and links snapshot_id/tags back to strategy_selection.

        Returns:
            dict: {'saved': int, 'blocked': bool, 'reason': str}
        """
        # 1. Timing Control Check (STRICT)
        # Normalize date to YYYY-MM-DD for DB consistency
        try:
            date = str(date)
            if len(date) == 8 and date.isdigit():
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        except Exception:
            pass

        def resolve_data_quality(selection: dict | None, snapshot_meta: dict | None = None) -> str:
            """Return a compact, non-empty data lineage label for reports/DB."""
            selection = selection or {}
            snapshot_meta = snapshot_meta or {}
            candidates = [
                snapshot_meta.get('data_quality'),
                selection.get('data_quality'),
            ]
            dq = selection.get('_data_quality') if isinstance(selection, dict) else None
            if isinstance(dq, dict):
                src = dq.get('source') or 'api'
                note = dq.get('note') or ''
                fallback = bool(dq.get('fallback_used'))
                if note:
                    candidates.append(f"{src}:fallback" if fallback else str(note))
                candidates.append(str(src))
            for value in candidates:
                value = str(value or '').strip()
                if value and value.lower() != 'unknown':
                    return value[:20]
            return 'api'

        from core.config import Config
        timing = Config.TIMING_CONTROL.get(strategy)

        # If no timing defined, we allow it (for custom analysis).
        # If defined, we enforce window ONLY when writing for *today*.
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
        except Exception:
            today_str = None

        if timing and today_str and date == today_str:
            now_time = datetime.now().strftime("%H:%M")
            if not (timing['start'] <= now_time <= timing['end']):
                msg = f"[Strict Protection] 策略 '{strategy}' 禁入库: 时间 {now_time} 不在允许窗口 {timing['start']}-{timing['end']} 内"
                logger.warning(msg)
                return {'saved': 0, 'blocked': True, 'reason': msg}

        conn = self._get_connection()
        if not conn:
            return {'saved': 0, 'blocked': False, 'reason': 'no db connection'}

        try:
            with conn.cursor() as cursor:
                # [V18] Optimized: Added analysis_cycle support
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = """INSERT INTO strategy_selection
                         (date, strategy, code, name, sel_price, change_pct, turnover, sector, zt_result,
                          observe_status, observe_reason, observe_updated_at,
                          analysis_cycle,
                          snapshot_id, score_total, tags_json, data_quality,
                          created_at)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                         ON DUPLICATE KEY UPDATE
                         name=VALUES(name),
                         sel_price=VALUES(sel_price),
                         change_pct=VALUES(change_pct),
                         turnover=VALUES(turnover),
                         sector=VALUES(sector),
                         zt_result=IF(zt_result='待验证', VALUES(zt_result), zt_result),
                         observe_status=IF(observe_status IN ('REMOVED','EXPIRED','BOUGHT'), observe_status, VALUES(observe_status)),
                         observe_reason=IF(observe_status IN ('REMOVED','EXPIRED','BOUGHT'), observe_reason, VALUES(observe_reason)),
                         observe_updated_at=IF(observe_status IN ('REMOVED','EXPIRED','BOUGHT'), observe_updated_at, VALUES(observe_updated_at)),
                         analysis_cycle=VALUES(analysis_cycle),
                         snapshot_id=COALESCE(VALUES(snapshot_id), snapshot_id),
                         score_total=COALESCE(VALUES(score_total), score_total),
                         tags_json=COALESCE(VALUES(tags_json), tags_json),
                         data_quality=COALESCE(VALUES(data_quality), data_quality)"""
                         
                # [VNext] 1) Build & persist factor snapshots (best-effort)
                snapshot_meta_by_code = {}
                try:
                    from core.factor_engine import FactorEngine

                    engine = FactorEngine()
                    # Attach concept heat into each candidate (best-effort) so FactorEngine can emit resonance tags
                    if market_env and isinstance(market_env, dict) and isinstance(market_env.get('concept_heat'), dict):
                        ch = market_env.get('concept_heat')
                        for s in selections or []:
                            if isinstance(s, dict) and 'concept_heat' not in s:
                                s['concept_heat'] = ch

                    # Attach intraday vwap threshold into each candidate so FactorEngine tags align with the gate.
                    try:
                        intraday_cfg = Config.STRATEGY.get('intraday_structure', {}) if isinstance(getattr(Config, 'STRATEGY', {}), dict) else {}
                        max_ratio = float(intraday_cfg.get('max_price_vwap_ratio', 1.03) or 1.03)
                    except Exception:
                        max_ratio = 1.03
                    for s in selections or []:
                        if isinstance(s, dict) and 'intraday_max_price_vwap_ratio' not in s:
                            s['intraday_max_price_vwap_ratio'] = max_ratio

                    snapshots = engine.build_snapshots(
                        selections or [],
                        trade_date=date,
                        strategy=strategy,
                        analysis_cycle=cycle,
                        market_env=market_env,
                    )

                    snap_sql = """INSERT INTO factor_snapshot
                                 (trade_date, strategy, analysis_cycle, code, ts_code, name,
                                  snapshot_version, score_total, factors_json, tags_json, data_quality, created_at)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

                    for snap in snapshots:
                        factors_json = json.dumps(snap.get('factors', {}), ensure_ascii=False)
                        tags_json = json.dumps(snap.get('tags', []), ensure_ascii=False)
                        code_key = str(snap.get('code') or '')
                        source_selection = next(
                            (
                                s for s in (selections or [])
                                if isinstance(s, dict)
                                and str(s.get('code') or s.get('ts_code', '')[:6]) == code_key
                            ),
                            {},
                        )
                        data_quality = resolve_data_quality(source_selection, snap)

                        cursor.execute(
                            snap_sql,
                            (
                                snap.get('trade_date'),
                                snap.get('strategy'),
                                snap.get('analysis_cycle'),
                                snap.get('code'),
                                snap.get('ts_code'),
                                snap.get('name'),
                                snap.get('snapshot_version'),
                                float(snap.get('score_total', 0.0) or 0.0),
                                factors_json,
                                tags_json,
                                data_quality,
                                now,
                            )
                        )
                        snapshot_id = cursor.lastrowid
                        if code_key:
                            snapshot_meta_by_code[code_key] = {
                                'snapshot_id': snapshot_id,
                                'score_total': float(snap.get('score_total', 0.0) or 0.0),
                                'tags_json': tags_json,
                                'data_quality': data_quality,
                            }
                except Exception as e:
                    logger.warning(f"Factor snapshot save skipped: {e}")

                # 2) Upsert selections (and link snapshot fields when available)
                data = []
                for s in selections or []:
                    code = s.get('code', s.get('ts_code', '')[:6])
                    code = str(code or '')

                    sel_price = float(s.get('price', s.get('close', s.get('trade', 0.0))) or 0)
                    turnover = float(s.get('turnover', s.get('turnover_rate', 0.0)) or 0)
                    change_pct = float(s.get('open_change', s.get('change', s.get('pct_chg', 0))) or 0)

                    snap_meta = snapshot_meta_by_code.get(code, {})
                    snapshot_id = snap_meta.get('snapshot_id')
                    score_total = snap_meta.get('score_total')
                    tags_json = snap_meta.get('tags_json')

                    data_quality = resolve_data_quality(s, snap_meta)

                    data.append((
                        date,
                        strategy,
                        code,
                        s.get('name'),
                        sel_price,
                        change_pct,
                        turnover,
                        s.get('industry', ''),
                        '待验证',
                        'ACTIVE',
                        '新入库待观察',
                        now,
                        cycle,
                        snapshot_id,
                        score_total,
                        tags_json,
                        data_quality,
                        now,
                    ))

                if data:
                    cursor.executemany(sql, data)
                    for row in data:
                        try:
                            self._log_selection_observation_event_cursor(
                                cursor,
                                date=row[0],
                                code=row[2],
                                strategy=row[1],
                                event_type='SELECTED',
                                to_status='ACTIVE',
                                reason='新入库待观察',
                                metrics={'sel_price': row[4], 'change_pct': row[5], 'turnover': row[6]},
                            )
                        except Exception:
                            pass
                conn.commit()
                logger.info(f"Successfully saved {len(data)} selections for {strategy} ({cycle})")
                return {'saved': len(data), 'blocked': False, 'reason': ''}
        except Exception as e:
            logger.error(f"Failed to save selections: {e}")
            return {'saved': 0, 'blocked': False, 'reason': str(e)}
        finally:
            conn.close()

    def get_selections(self, date, strategy="集合竞价"):
        """Get selections for a date"""
        conn = self._get_connection()
        if not conn: return []
        
        try:
            with conn.cursor() as cursor:
                sql = "SELECT * FROM strategy_selection WHERE date=%s AND strategy=%s"
                cursor.execute(sql, (date, strategy))
                return cursor.fetchall()
        finally:
            conn.close()

    def get_latest_selection_for_code(self, code, days=10):
        """Get the latest strategy_selection row for a code.

        Used by individual stock diagnosis to connect a live holding back to the
        exact strategy signal and stored reference price.
        """
        code = str(code or '').strip()[:6]
        if not code:
            return None
        conn = self._get_connection()
        if not conn:
            return None

        try:
            with conn.cursor() as cursor:
                sql = """
                    SELECT *
                    FROM strategy_selection
                    WHERE code=%s
                      AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                """
                cursor.execute(sql, (code, days))
                return cursor.fetchone()
        except Exception as e:
            logger.warning(f"Failed to fetch latest selection for {code}: {e}")
            return None
        finally:
            conn.close()

    def upsert_manual_position(
        self,
        *,
        code,
        name,
        price,
        quantity,
        account='rescue',
        source_strategy='手工实盘跟踪',
        signal_tags_json=None,
        created_at=None,
    ):
        """Create/update an exact manual holding for sentinel-only tracking.

        This does not touch cash or create a BUY transaction. It is intended for
        externally executed real holdings that should be watched by monitor.py.
        The rescue account is alert-only: monitor will not auto-remove the
        position when a sell signal is detected.
        """
        code = str(code or '').strip()[:6]
        name = str(name or code or '').strip()[:50]
        try:
            price = float(price)
            quantity = int(quantity)
        except Exception:
            return False, "invalid price or quantity"
        if not code or price <= 0 or quantity <= 0:
            return False, "code, price and quantity are required"

        conn = self._get_connection()
        if not conn:
            return False, "DB Connection Failed"

        try:
            with conn.cursor() as cursor:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                created_at = str(created_at or now)
                market_value = price * quantity
                sql = """
                    INSERT INTO positions
                        (code, account, name, buy_price, avg_price, current_price,
                         quantity, market_value, highest_price, pnl, pnl_pct, cost,
                         sell_stage, update_time, created_at,
                         entry_strategy, entry_tags_json)
                    VALUES
                        (%s, %s, %s, %s, %s, %s,
                         %s, %s, %s, 0, 0, %s,
                        0, %s, %s,
                         %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name=VALUES(name),
                        buy_price=VALUES(buy_price),
                        avg_price=VALUES(avg_price),
                        current_price=VALUES(current_price),
                        quantity=VALUES(quantity),
                        market_value=VALUES(market_value),
                        highest_price=GREATEST(IFNULL(highest_price, 0), VALUES(highest_price)),
                        cost=VALUES(cost),
                        update_time=VALUES(update_time),
                        created_at=VALUES(created_at),
                        entry_strategy=VALUES(entry_strategy),
                        entry_tags_json=VALUES(entry_tags_json)
                """
                cursor.execute(
                    sql,
                    (
                        code,
                        account,
                        name,
                        price,
                        price,
                        price,
                        quantity,
                        market_value,
                        price,
                        market_value,
                        now,
                        created_at,
                        source_strategy,
                        signal_tags_json,
                    ),
                )
                conn.commit()
                return True, f"{account} manual position tracked: {code} {quantity}@{price:.2f}"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"Failed to upsert manual position: {e}")
            return False, str(e)
        finally:
            conn.close()

    # --- [Phase 20] T+0 Statistics & Tracking ---
    
    def save_t0_record(self, date_str, account, code, name, buy_price, sell_price, quantity, pnl, reason=""):
        """
        Record a T0 transaction directly into the transactions table
        This is separated from regular BUY/SELL to track T0 success rate independently
        """
        conn = self._get_connection()
        if not conn: return False
        
        try:
            with conn.cursor() as cursor:
                logger.info(f"Saving T0 Record: {name}({code}) | Buy:{buy_price:.2f} Sell:{sell_price:.2f} PnL:{pnl:.2f}")
                sql = """INSERT INTO transactions 
                         (date, account, type, code, name, price, quantity, amount, balance, reason) 
                         VALUES (%s, %s, 'T0', %s, %s, %s, %s, %s, %s, %s)"""
                # For T0, we record the 'sell_price' in the price column, but we can store the 'buy_price' in the reason for reference.
                enriched_reason = f"【T0】收:{sell_price:.2f} 支:{buy_price:.2f} 盈:{pnl:.2f} | {reason}"
                cursor.execute(sql, (date_str, account, code, name, sell_price, quantity, pnl, 0, enriched_reason))
                conn.commit()
                return True
        except Exception as e:
            try: conn.rollback()
            except: pass
            logger.error(f"Failed to save T0 record: {e}")
            return False
        finally:
            conn.close()

    def get_t0_stats(self, code=None, account='main'):
        """
        Get historical T0 statistics (win rate, total pnl, average return per trade)
        If code is None, returns account-wide stats.
        """
        conn = self._get_connection()
        if not conn: return {}
        
        try:
            with conn.cursor() as cursor:
                if code:
                    cursor.execute("SELECT * FROM transactions WHERE type='T0' AND account=%s AND code=%s", (account, code))
                else:
                    cursor.execute("SELECT * FROM transactions WHERE type='T0' AND account=%s", (account,))
                records = cursor.fetchall()
                
                if not records:
                    return {'count': 0, 'win_rate': 0.0, 'total_pnl': 0.0, 'avg_pnl': 0.0}
                    
                wins = 0
                total_pnl = 0.0
                for r in records:
                    # Amount column stores the PnL for T0 rows
                    pnl = float(r.get('amount', 0))
                    total_pnl += pnl
                    if pnl > 0:
                        wins += 1
                        
                return {
                    'count': len(records),
                    'win_rate': (wins / len(records)) * 100,
                    'total_pnl': total_pnl,
                    'avg_pnl': total_pnl / len(records)
                }
        except Exception as e:
            logger.error(f"Failed to get T0 stats: {e}")
            return {}
        finally:
            conn.close()

    def get_selections_by_cycle(self, date, cycle='T+1'):
        """[V18] Get all selections for a specific date and cycle (T+1 or T+2)"""
        conn = self._get_connection()
        if not conn: return []
        try:
            with conn.cursor() as cursor:
                sql = "SELECT * FROM strategy_selection WHERE date=%s AND analysis_cycle=%s"
                cursor.execute(sql, (date, cycle))
                return cursor.fetchall()
        finally:
            conn.close()

    def _log_selection_observation_event_cursor(
        self,
        cursor,
        *,
        date,
        code,
        strategy=None,
        event_type,
        from_status=None,
        to_status=None,
        reason='',
        metrics=None,
    ):
        selection = None
        if strategy:
            cursor.execute(
                "SELECT id, name, observe_status FROM strategy_selection WHERE date=%s AND strategy=%s AND code=%s ORDER BY id DESC LIMIT 1",
                (date, strategy, code),
            )
        else:
            cursor.execute(
                "SELECT id, name, strategy, observe_status FROM strategy_selection WHERE date=%s AND code=%s ORDER BY id DESC LIMIT 1",
                (date, code),
            )
        selection = cursor.fetchone()
        if not selection:
            return
        metrics_json = json.dumps(metrics or {}, ensure_ascii=False) if metrics else None
        cursor.execute(
            """INSERT INTO selection_observation_events
               (selection_id, date, strategy, code, name, event_type, from_status, to_status, reason, metrics_json, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                selection.get('id'),
                date,
                strategy or selection.get('strategy'),
                code,
                selection.get('name'),
                event_type,
                from_status if from_status is not None else selection.get('observe_status'),
                to_status,
                str(reason or '')[:255],
                metrics_json,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    def log_selection_observation_event(self, date, code, strategy=None, event_type='NOTE', from_status=None, to_status=None, reason='', metrics=None):
        """Append an observation lifecycle event without changing selection state."""
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                self._log_selection_observation_event_cursor(
                    cursor,
                    date=date,
                    code=code,
                    strategy=strategy,
                    event_type=event_type,
                    from_status=from_status,
                    to_status=to_status,
                    reason=reason,
                    metrics=metrics,
                )
                conn.commit()
                return True
        except Exception as e:
            logger.debug(f"selection observation event failed: {e}")
            return False
        finally:
            conn.close()

    def update_selection_observe_status(self, date, code, status, strategy=None, reason='', metrics=None):
        """Update the independent observation lifecycle status for a selected stock."""
        status = str(status or '').strip().upper()
        if not status:
            return False
        terminal = status in {'REMOVED', 'EXPIRED', 'BOUGHT'}
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                if strategy:
                    cursor.execute(
                        "SELECT id, observe_status FROM strategy_selection WHERE date=%s AND strategy=%s AND code=%s ORDER BY id DESC LIMIT 1",
                        (date, strategy, code),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return False
                    sql = """UPDATE strategy_selection
                             SET observe_status=%s,
                                 observe_reason=%s,
                                 observe_updated_at=%s,
                                 observe_end_at=IF(%s, %s, observe_end_at),
                                 observe_end_reason=IF(%s, %s, observe_end_reason)
                             WHERE id=%s"""
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(sql, (status, str(reason or '')[:255], now, terminal, now, terminal, str(reason or '')[:255], row['id']))
                else:
                    cursor.execute(
                        "SELECT id, observe_status FROM strategy_selection WHERE date=%s AND code=%s ORDER BY id DESC LIMIT 1",
                        (date, code),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return False
                    sql = """UPDATE strategy_selection
                             SET observe_status=%s,
                                 observe_reason=%s,
                                 observe_updated_at=%s,
                                 observe_end_at=IF(%s, %s, observe_end_at),
                                 observe_end_reason=IF(%s, %s, observe_end_reason)
                             WHERE id=%s"""
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(sql, (status, str(reason or '')[:255], now, terminal, now, terminal, str(reason or '')[:255], row['id']))
                event_type = status if status in {'REMOVED', 'EXPIRED', 'BOUGHT', 'PENDING'} else 'OBSERVE_KEEP'
                self._log_selection_observation_event_cursor(
                    cursor,
                    date=date,
                    code=code,
                    strategy=strategy,
                    event_type=event_type,
                    from_status=row.get('observe_status'),
                    to_status=status,
                    reason=reason,
                    metrics=metrics,
                )
                conn.commit()
                return True
        except Exception as e:
            logger.debug(f"selection observe status update failed: {e}")
            return False
        finally:
            conn.close()

    def update_zt_result(self, date, code, status, strategy=None, metrics=None):
        """Update selection status (zt_result).

        IMPORTANT:
          - When strategy is provided, update is scoped to (date, strategy, code) to avoid cross-strategy overwrites.
          - When strategy is None, falls back to legacy behavior (date, code).
        """
        conn = self._get_connection()
        if not conn:
            return

        try:
            with conn.cursor() as cursor:
                if strategy:
                    sql = "UPDATE strategy_selection SET zt_result=%s WHERE date=%s AND strategy=%s AND code=%s"
                    cursor.execute(sql, (status, date, strategy, code))
                else:
                    # Legacy fallback
                    sql = "UPDATE strategy_selection SET zt_result=%s WHERE date=%s AND code=%s"
                    cursor.execute(sql, (status, date, code))
                if metrics is not None:
                    try:
                        self._log_selection_observation_event_cursor(
                            cursor,
                            date=date,
                            code=code,
                            strategy=strategy,
                            event_type='TRACK_RESULT',
                            to_status=None,
                            reason=f"T+验证结果: {status}",
                            metrics=metrics,
                        )
                    except Exception:
                        pass
                conn.commit()
        finally:
            conn.close()

    def get_watchlist(self, days=5):
        """[V9] Get recent watchlist candidates for monitoring"""
        conn = self._get_connection()
        if not conn: return []
        try:
            with conn.cursor() as cursor:
                sql = """
                    SELECT * FROM strategy_selection 
                    WHERE (
                        observe_status IN ('ACTIVE', 'WATCHING', 'PENDING')
                        OR (
                            observe_status IS NULL
                            AND zt_result IN ('待验证', '继续观察')
                        )
                    )
                    AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    ORDER BY created_at DESC
                """
                cursor.execute(sql, (days,))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to get watchlist: {e}")
            return []
        finally:
            conn.close()

    def get_recent_selections(self, days=5):
        """[V9] Get all strategy selections from recent days for lifecycle tracking"""
        conn = self._get_connection()
        if not conn: return []
        try:
            with conn.cursor() as cursor:
                sql = """SELECT * FROM strategy_selection 
                         WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                         ORDER BY created_at DESC"""
                cursor.execute(sql, (days,))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to get recent selections: {e}")
            return []
        finally:
            conn.close()

    def save_stats(self, date, stats, strategy_name="集合竞价"):
        """Save daily stats"""
        conn = self._get_connection()
        if not conn: return
        
        try:
            with conn.cursor() as cursor:
                sql = """REPLACE INTO strategy_stats 
                         (date, strategy, total, zt_count, success_rate, updated_at)
                         VALUES (%s, %s, %s, %s, %s, %s)"""
                cursor.execute(sql, (
                    date, strategy_name, 
                    stats['total'], stats['zt_count'], stats['success_rate'],
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ))
                conn.commit()
        finally:
            conn.close()
            
    def get_history_stats(self, limit=10):
        """Get history stats"""
        conn = self._get_connection()
        if not conn: return []
        
        try:
            with conn.cursor() as cursor:
                sql = "SELECT * FROM strategy_stats ORDER BY date DESC LIMIT %s"
                cursor.execute(sql, (limit,))
                return cursor.fetchall()
        finally:
            conn.close()
