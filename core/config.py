# -*- coding: utf-8 -*-
"""
Configuration settings for Market Signal Lab
"""

import json
import os
from copy import deepcopy


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return bool(default)
    val = str(val).strip().lower()
    if val in {"1", "true", "yes", "y", "on"}:
        return True
    if val in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


class Config:
    # Load Strategy Config
    _STRATEGY_CONFIG_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "strategy_config.json",
    )
    STRATEGY = {}

    @classmethod
    def load_strategy_config(cls):
        """Load strategy parameters from JSON"""
        try:
            if os.path.exists(cls._STRATEGY_CONFIG_FILE):
                with open(cls._STRATEGY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cls.STRATEGY = json.load(f)
            else:
                # Default fallback
                cls.STRATEGY = {
                    "auction": {"min_open_change": 2.0, "max_open_change": 5.0, "min_turnover": 0.5},
                    "afternoon": {"min_change": 2.0, "max_change": 5.0, "min_turnover": 2.0, "max_turnover": 8.0, "trend_factor": 1.01},
                    "macd": {"fast": 12, "slow": 26, "signal": 9}
                }
            cls.apply_training_snapshot_if_exists()
        except Exception as e:
            print(f"Error loading strategy config: {e}")

    @staticmethod
    def _deep_merge_dict(base: dict, override: dict) -> dict:
        """Return merged dict (override wins, recursive)."""
        if not isinstance(base, dict):
            base = {}
        merged = deepcopy(base)
        for k, v in (override or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = Config._deep_merge_dict(merged.get(k, {}), v)
            else:
                merged[k] = v
        return merged

    @classmethod
    def apply_training_snapshot_if_exists(cls):
        """Overlay safe strategy fields from training snapshot if enabled and valid."""
        try:
            snapshot_path = os.environ.get(
                "TRAINING_SNAPSHOT_PATH",
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data",
                    "strategy_snapshot.json",
                ),
            )
            if not snapshot_path or not os.path.exists(snapshot_path):
                return

            with open(snapshot_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            if not isinstance(snap, dict):
                return

            if str(snap.get("schema_version", "")).strip() != "1.0":
                return

            publish = snap.get("publish", {}) if isinstance(snap.get("publish"), dict) else {}
            if publish.get("enabled") is not True:
                return

            gate = snap.get("safety_gate", {}) if isinstance(snap.get("safety_gate"), dict) else {}
            metrics = snap.get("metrics", {}) if isinstance(snap.get("metrics"), dict) else {}

            min_win_rate = float(gate.get("min_win_rate", 0.0) if gate.get("min_win_rate") is not None else 0.0)
            max_drawdown = float(gate.get("max_drawdown", 1.0) if gate.get("max_drawdown") is not None else 1.0)
            min_sample_size = int(gate.get("min_sample_size", 0) if gate.get("min_sample_size") is not None else 0)
            observed_win_rate = float(metrics.get("win_rate", 0.0) if metrics.get("win_rate") is not None else 0.0)
            observed_drawdown = float(metrics.get("max_drawdown", 1.0) if metrics.get("max_drawdown") is not None else 1.0)
            observed_sample_size = int(metrics.get("sample_size", 0) if metrics.get("sample_size") is not None else 0)

            if observed_win_rate < min_win_rate or observed_drawdown > max_drawdown or observed_sample_size < min_sample_size:
                return

            patch = snap.get("strategy_patch", {}) if isinstance(snap.get("strategy_patch"), dict) else {}
            if not patch:
                return

            cls.STRATEGY = cls._deep_merge_dict(cls.STRATEGY, patch)
        except Exception:
            # Snapshot is best-effort; never break runtime.
            return

    # Tushare Settings (Higher-tier API)
    # NOTE: Per docs/STRATEGY.md, secrets/tokens must come from environment variables.
    # If unset, leave empty so the caller can decide to skip or fail safely.
    TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
    TUSHARE_URL = os.environ.get("TUSHARE_URL", "https://api.tushare.pro")

    # Tencent/GTimg minute fallback (best-effort)
    # When tushare minute bars are rate-limited/unavailable, we can fallback to GTimg.
    # Defaults preserve the current hardcoded behavior.
    TENCENT_MINUTE_FALLBACK_ENABLED = _env_bool("TENCENT_MINUTE_FALLBACK_ENABLED", True)
    TENCENT_MINUTE_FALLBACK_BASE_URL = os.environ.get(
        "TENCENT_MINUTE_FALLBACK_BASE_URL",
        "http://ifzq.gtimg.cn",
    )
    TENCENT_MINUTE_FALLBACK_MKLINE_PATH = os.environ.get(
        "TENCENT_MINUTE_FALLBACK_MKLINE_PATH",
        "/appstock/app/kline/mkline",
    )
    TENCENT_MINUTE_FALLBACK_COUNT = _env_int("TENCENT_MINUTE_FALLBACK_COUNT", 500)
    TENCENT_MINUTE_FALLBACK_TIMEOUT_SEC = _env_float("TENCENT_MINUTE_FALLBACK_TIMEOUT_SEC", 5.0)
    TENCENT_MINUTE_FALLBACK_USER_AGENT = os.environ.get(
        "TENCENT_MINUTE_FALLBACK_USER_AGENT",
        "",
    )
    TENCENT_MINUTE_FALLBACK_HEADERS_JSON = os.environ.get(
        "TENCENT_MINUTE_FALLBACK_HEADERS_JSON",
        "",
    )

    # Optional retries (defaults preserve the current single-attempt behavior)
    TENCENT_MINUTE_FALLBACK_RETRIES = _env_int("TENCENT_MINUTE_FALLBACK_RETRIES", 0)
    TENCENT_MINUTE_FALLBACK_RETRY_DELAY_SEC = _env_float("TENCENT_MINUTE_FALLBACK_RETRY_DELAY_SEC", 0.0)

    # Logging
    LOG_RETENTION_DAYS = _env_int("LOG_RETENTION_DAYS", 5)
    LOG_EMAIL_CONTENT = _env_bool("LOG_EMAIL_CONTENT", True)

    # Tushare request pacing. Keep conservative defaults because OpenClaw may run
    # main/monitor/sentinel concurrently.
    TUSHARE_MIN_REQUEST_INTERVAL_SEC = _env_float("TUSHARE_MIN_REQUEST_INTERVAL_SEC", 0.12)
    TUSHARE_BATCH_HISTORY_SIZE = _env_int("TUSHARE_BATCH_HISTORY_SIZE", 50)
    TUSHARE_BATCH_HISTORY_SLEEP_SEC = _env_float("TUSHARE_BATCH_HISTORY_SLEEP_SEC", 0.15)
    TUSHARE_STK_AUCTION_ENABLED = _env_bool("TUSHARE_STK_AUCTION_ENABLED", False)
    TUSHARE_RT_K_ENABLED = _env_bool("TUSHARE_RT_K_ENABLED", True)
    TUSHARE_REALTIME_PRIMARY = os.environ.get("TUSHARE_REALTIME_PRIMARY", "rt_k").lower()
    if TUSHARE_REALTIME_PRIMARY == "rt_k" and not TUSHARE_RT_K_ENABLED:
        TUSHARE_REALTIME_PRIMARY = "rt_min"
    TUSHARE_REALTIME_CHUNK_SIZE = _env_int("TUSHARE_REALTIME_CHUNK_SIZE", 200)
    TUSHARE_REALTIME_SLEEP_SEC = _env_float("TUSHARE_REALTIME_SLEEP_SEC", 0.08)
    TUSHARE_RT_MIN_CHUNK_SIZE = _env_int("TUSHARE_RT_MIN_CHUNK_SIZE", 200)
    TUSHARE_RT_MIN_SLEEP_SEC = _env_float("TUSHARE_RT_MIN_SLEEP_SEC", 0.08)
    TUSHARE_RT_MIN_SINGLE_MAX_PER_RUN = _env_int("TUSHARE_RT_MIN_SINGLE_MAX_PER_RUN", 10)
    FOCUS_MONITOR_MINUTE_MAX_PER_RUN = _env_int("FOCUS_MONITOR_MINUTE_MAX_PER_RUN", 20)
    TUSHARE_MONEYFLOW_PRIMARY = os.environ.get("TUSHARE_MONEYFLOW_PRIMARY", "moneyflow_dc").lower()
    TUSHARE_MONEYFLOW_FALLBACK = os.environ.get("TUSHARE_MONEYFLOW_FALLBACK", "moneyflow").lower()

    # MySQL Database Settings
    DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.environ.get("DB_PORT", "3306"))
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASS = os.environ.get("DB_PASS", "")
    # Assuming a default database name, can be changed if needed
    DB_NAME = os.environ.get("DB_NAME", "stock_analysis")

    # Redis Cache Settings [V18]
    REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_PASS = os.environ.get("REDIS_PASS", "")
    REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
    REDIS_SOCKET_TIMEOUT = _env_float("REDIS_SOCKET_TIMEOUT", 5.0)
    REDIS_SOCKET_CONNECT_TIMEOUT = _env_float("REDIS_SOCKET_CONNECT_TIMEOUT", 5.0)
    REDIS_HEALTH_CHECK_INTERVAL = _env_int("REDIS_HEALTH_CHECK_INTERVAL", 30)
    REDIS_MAX_CONNECTIONS = _env_int("REDIS_MAX_CONNECTIONS", 20)
    REDIS_MAX_VALUE_BYTES = _env_int("REDIS_MAX_VALUE_BYTES", 5_000_000)
    REDIS_BATCH_HISTORY_CACHE_CHUNK_SIZE = _env_int("REDIS_BATCH_HISTORY_CACHE_CHUNK_SIZE", 200)

    # Email Settings
    EMAIL_ENABLED = _env_bool("EMAIL_ENABLED", False)
    SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.qq.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
    EMAIL_USER = os.environ.get("EMAIL_USER", "")
    EMAIL_PWD = os.environ.get("EMAIL_PWD", "")
    EMAIL_TO = [addr.strip() for addr in os.environ.get("EMAIL_TO", "").split(",") if addr.strip()]

    # [V17] 标的入库时间窗控制 (Timing Control)
    TIMING_CONTROL = {
        "集合竞价": {"start": "09:25", "end": "09:30"},
        "龙头跟踪": {"start": "09:25", "end": "09:30"},
        "技术突破": {"start": "09:25", "end": "10:00"},
        "午盘精选": {"start": "14:30", "end": "15:05"},
        "盘后资金流": {"start": "18:00", "end": "19:00"}
    }

    # Analysis Parameters
    MIN_TURNOVER = 2.0  # Minimum turnover rate (%)
    MAX_TURNOVER = 8.0  # Maximum turnover rate (%)
    MIN_CHANGE = 2.0    # Minimum change (%)
    MAX_CHANGE = 5.0    # Maximum change (%)
    INDIVIDUAL_FLOW_DAYS = 3 # Check last 3 days money flow for individual stocks

    # Retry Settings
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # Seconds

    # Risk Management
    BLACKLIST = [] # List of stock codes to exclude

    # Auto-Trade Constraints
    RISK_MANAGEMENT = {
        "STOP_LOSS": -0.03,       # [V21] Stop loss at -3% (收紧)
        "TAKE_PROFIT": 0.08,      # Take profit at +8%

        # [V19] Broadened Laddered Take Profit (Profit %, Sell %)
        # [V21] 优化: 盈利3%锁住保本线，5%锁3%，8%锁5%
        "LADDERED_TAKE_PROFIT": [
            (0.03, 0.3),  # Profit 3%, sell 30%
            (0.05, 0.3),  # Profit 5%, sell 30%
            (0.08, 0.3),  # Profit 8%, sell 30%
            (0.10, 1.0)   # Profit 10%, sell remaining 100%
        ],

        # [V18] Admission Validation
        "MIN_VOLUME_RATIO": 2.0,     # Mandatory Volume Ratio for Buying
        "MAX_BUY_CHANGE": 7.0,      # Don't chase if already > 7%

        # [V18] Position Management
        "MAX_TOTAL_POSITIONS": 5,    # Global cap on number of stocks
        "MAX_POSITION_PER_STOCK": 0.20, # Dynamic cap 20%

        "MAX_HOLD_DAYS": 5,       # Force sell if held > 5 days
        "MIN_HOLD_RETURN": 0.01,  # If held > 5 days and return < 1%, sell
        "INITIAL_CAPITAL": {
            "main": 20000.0,
            "watchlist": 10000.0,
            "paper_main": 50000.0,
            "paper_watchlist": 50000.0
        }
    }

    @staticmethod
    def get_allowed_boards():
        """Return enabled A-share board permissions from strategy_config."""
        mp = Config.STRATEGY.get('market_permission', {}) if isinstance(Config.STRATEGY, dict) else {}
        if not isinstance(mp, dict):
            return ['main']

        current = mp.get('current_boards')
        if isinstance(current, list) and current:
            return current

        profile = str(mp.get('current_profile') or 'MAIN').upper()
        profile_cfg = mp.get(profile)
        if isinstance(profile_cfg, dict):
            boards = profile_cfg.get('allowed_boards')
            if isinstance(boards, list) and boards:
                return boards

        main_cfg = mp.get('MAIN')
        if isinstance(main_cfg, dict):
            boards = main_cfg.get('allowed_boards')
            if isinstance(boards, list) and boards:
                return boards

        return ['main']

# Load on module import
Config.load_strategy_config()
