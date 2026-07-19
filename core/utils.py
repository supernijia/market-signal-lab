# -*- coding: utf-8 -*-
"""
Utility functions for Market Signal Lab
"""
import logging
import sys
import io
from datetime import datetime, timedelta

logger = logging.getLogger("StockAnalyzer.Utils")


_WEATHER_NORMALIZE_MAP = {
    "晴天": "☀️晴天",
    "sunny": "☀️晴天",
    "☀️": "☀️晴天",
    "多云": "☁️多云",
    "cloudy": "☁️多云",
    "☁️": "☁️多云",
    "暴雨": "⚠️暴雨",
    "rain": "⚠️暴雨",
    "storm": "⚠️暴雨",
    "⚠️": "⚠️暴雨",
}


def _get_log_retention_days(default=5):
    try:
        from core.config import Config
        return max(1, int(getattr(Config, "LOG_RETENTION_DAYS", default) or default))
    except Exception:
        return default


def cleanup_old_logs(log_dir=None, retention_days=None):
    """Delete date-stamped project logs older than the retention window."""
    try:
        import os
        import re

        if log_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_dir, "logs")
        if retention_days is None:
            retention_days = _get_log_retention_days(5)
        if not os.path.isdir(log_dir):
            return 0

        # Keep today plus the previous retention_days-1 calendar dates.
        cutoff_date = (datetime.now() - timedelta(days=max(0, int(retention_days) - 1))).date()
        patterns = (
            re.compile(r"^stock_analyzer-(\d{8})\.log$"),
            re.compile(r"^evolution-(\d{8})\.log$"),
        )
        removed = 0
        for filename in os.listdir(log_dir):
            log_date = None
            for pattern in patterns:
                match = pattern.match(filename)
                if match:
                    try:
                        log_date = datetime.strptime(match.group(1), "%Y%m%d").date()
                    except Exception:
                        log_date = None
                    break
            if log_date is None or log_date >= cutoff_date:
                continue
            try:
                os.remove(os.path.join(log_dir, filename))
                removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


def normalize_weather(weather: str) -> str:
    """Normalize weather labels to the canonical keys used in config.

    Canonical values are currently emoji-prefixed Chinese strings:
    - ☀️晴天 / ☁️多云 / ⚠️暴雨

    This keeps internal DB/config compatibility, while allowing callers to pass
    plain labels like "晴天".
    """

    if not weather:
        return "☀️晴天"
    w = str(weather).strip()
    if w in ("☀️晴天", "☁️多云", "⚠️暴雨"):
        return w
    return _WEATHER_NORMALIZE_MAP.get(w, w)


def setup_logger(name="StockAnalyzer"):
    """Setup logger configuration"""

    # Best-effort: avoid Windows console UnicodeEncodeError (e.g. emoji in logs)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root_logger = logging.getLogger("StockAnalyzer")
    root_logger.setLevel(logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Check root business logger handlers to avoid duplicate logs while ensuring
    # sibling loggers such as StockAnalyzer.Reporter share the same file.
    if not root_logger.handlers:
        # Console handler (force UTF-8 + replace to avoid crashes on GBK consoles)
        stream = sys.stdout
        try:
            if hasattr(sys.stdout, "buffer"):
                stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            stream = sys.stdout

        handler = logging.StreamHandler(stream)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

        # One file per day. OpenClaw tasks are short-lived, so a date-stamped
        # file is easier to inspect than size-based rotation fragments.
        try:
            import os
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_dir, "logs")
            log_file = os.path.join(log_dir, f"stock_analyzer-{datetime.now().strftime('%Y%m%d')}.log")

            # Ensure logs directory exists
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            removed = cleanup_old_logs(log_dir=log_dir)

            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            if removed:
                root_logger.info("Log cleanup removed %s old log file(s), retention_days=%s", removed, _get_log_retention_days(5))
        except Exception:
            pass  # Graceful fallback if file logging fails

    return logger

def format_currency(value):
    """Format value as currency"""
    if value is None:
        return "¥0.00"
    return f"¥{value:,.2f}"

def format_pct(value):
    """Format value as percentage"""
    if value is None:
        return "0.00%"
    return f"{value:+.2f}%"

def get_level_description(stock):
    """Get rating description for a stock"""
    rise_from_avg = stock.get('rise_from_avg', 0)
    turnover = stock.get('turnover', 0)
    amount = stock.get('amount', 0) # Amount in hundreds of millions usually

    if rise_from_avg > 2 and turnover > 4:
        return "⭐⭐⭐ 强量价齐升"
    elif rise_from_avg > 1 and turnover > 3:
        return "⭐⭐ 量价配合良好"
    elif amount > 5: # Assuming unit is Yi (100 million)
        return "⭐ 大资金关注"
    else:
        return ""

def get_dynamic_stop_loss(max_pct_change, original_stop_loss, weather='☀️晴天', trailing_config=None, is_state_fund=False):
    """
    [V10] High-Water Mark Trailing stop loss based on tiered profit levels AND market weather.
    [V11] Added state_fund parameter - if True, widen SL to -7%

    In good weather (☀️), let profits run. In bad weather (⚠️), be aggressive with stops.
    For state fund holdings, allow more volatility tolerance.

    Args:
        max_pct_change: Current max profit percentage
        original_stop_loss: Base stop loss (e.g., -0.05)
        weather: Current market weather ('☀️晴天', '☁️多云', '⚠️暴雨')
        trailing_config: Dict with 'trailing_retrace' for each weather
        is_state_fund: If True (has 证金/汇金/社保), widen SL to -7%

    Returns:
        float: Dynamic stop loss percentage
    """
    # Default trailing configs by weather
    if trailing_config is None:
        trailing_config = {
            '☀️晴天': 0.03,   # 晴天回撤3% (15%锁住12%)
            '☁️多云': 0.08,   # Allow 8% retrace
            '⚠️暴雨': 0.05    # Allow 5% retrace (tight)
        }

    # Get trailing retrace for current weather
    trailing_retrace = trailing_config.get(weather, 0.15)

    # [V10] Weather-adjusted base stop loss (收紧版: 控制最大亏损)
    weather_stop_loss = {
        '☀️晴天': -0.03,   # 晴天最多亏3%
        '☁️多云': -0.025,  # 多云最多亏2.5%
        '⚠️暴雨': -0.015   # 暴雨最多亏1.5%
    }
    base_sl = weather_stop_loss.get(weather, original_stop_loss)

    # [V11] State Fund Adjustment: If has 证金/汇金/社保, widen SL to -7%
    if is_state_fund:
        base_sl = -0.07  # More tolerance for state-backed stocks
        logger.info(f"State fund detected, SL widened to -7%")

    # Apply trailing stop based on profit level
    if max_pct_change >= 0.40:
        return max_pct_change - 0.10  # 赚40%+, 回撤10%止盈
    elif max_pct_change >= 0.20:
        return max_pct_change - 0.08  # 赚20%+, 回撤8%止盈
    elif max_pct_change >= 0.10:
        return max_pct_change - 0.05  # 赚10%+, 回撤5%止盈
    elif max_pct_change >= 0.05:
        return max_pct_change - 0.03  # 赚5%+, 回撤3%止盈
    elif max_pct_change >= 0.02:
        if is_state_fund:
            return base_sl
        return 0.01  # [V19] 保1%利润 (覆盖交易成本)
    elif max_pct_change >= 0.01:
        if is_state_fund:
            return base_sl
        return base_sl
    return base_sl


def get_weather_risk_params(weather='☀️晴天', config_dict=None):
    """
    [V10] Get risk parameters based on market weather.

    Args:
        weather: Current market weather
        config_dict: Optional custom config dict

    Returns:
        dict: {'stop_loss', 'take_profit', 'trailing_retrace'}
    """
    if config_dict is None:
        # Default weather risk matrix
        return {
            '☀️晴天': {'stop_loss': -0.06, 'take_profit': 0.25, 'trailing_retrace': 0.15},
            '☁️多云': {'stop_loss': -0.04, 'take_profit': 0.12, 'trailing_retrace': 0.08},
            '⚠️暴雨': {'stop_loss': -0.03, 'take_profit': 0.08, 'trailing_retrace': 0.05}
        }.get(weather, {'stop_loss': -0.05, 'take_profit': 0.08, 'trailing_retrace': 0.10})

    return config_dict.get(weather, {'stop_loss': -0.05, 'take_profit': 0.08, 'trailing_retrace': 0.10})

def is_trading_hours(allow_auction=False):
    """Check if current time is within CN market trading hours"""
    from datetime import datetime
    now = datetime.now()

    # Check weekday (0=Mon, 6=Sun)
    if now.weekday() > 4:
        return False

    hour = now.hour
    minute = now.minute

    # Morning: 09:30 - 11:30 (Auction includes 09:15-09:30)
    if allow_auction:
        if (hour == 9 and minute >= 15) or (hour == 10) or (hour == 11 and minute <= 30):
            return True
    else:
        if (hour == 9 and minute >= 30) or (hour == 10) or (hour == 11 and minute <= 30):
            return True

    # Afternoon: 13:00 - 15:00
    if (13 <= hour < 15) or (hour == 15 and minute == 0):
        return True

    return False

def calculate_max_drawdown(nav_list):
    """
    Calculate Maximum Drawdown from a list of cumulative returns or Net Asset Values (NAV).
    Returns the maximum percentage drop from a peak to a trough.
    """
    if not nav_list or len(nav_list) < 2:
        return 0.0

    import numpy as np
    nav_array = np.array(nav_list)
    # Calculate the cumulative maximum
    running_max = np.maximum.accumulate(nav_array)
    # Ensure no division by zero
    running_max[running_max == 0] = 1
    # Calculate drawdowns
    drawdowns = (nav_array - running_max) / running_max

    return float(np.min(drawdowns))

def calculate_volatility(daily_returns, annual_factor=252):
    """
    Calculate annualized volatility from a list of daily returns (percentages).
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0

    import numpy as np
    # Assume returns are decimals (e.g., 0.01 for 1%)
    std_dev = np.std(daily_returns, ddof=1)
    return float(std_dev * np.sqrt(annual_factor))

def calculate_sharpe_ratio(daily_returns, risk_free_rate=0.02, annual_factor=252):
    """
    Calculate annualized Sharpe Ratio.
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0

    import numpy as np
    returns_array = np.array(daily_returns)
    mean_return = np.mean(returns_array) * annual_factor
    volatility = calculate_volatility(daily_returns, annual_factor)

    if volatility == 0:
        return 0.0

    return float((mean_return - risk_free_rate) / volatility)

def calculate_expected_return(win_rate, avg_win_pct, avg_loss_pct):
    """
    Calculate expected return per trade based on win rate and average win/loss.
    win_rate: 0.0 to 1.0
    avg_win_pct: positive float (e.g., 0.05 for 5%)
    avg_loss_pct: negative or positive float representing the loss (e.g., -0.03 for -3%)
    """
    loss_rate = 1.0 - win_rate
    # Ensure avg_loss_pct is negative for the formula
    if avg_loss_pct > 0:
        avg_loss_pct = -avg_loss_pct

    expected_value = (win_rate * avg_win_pct) + (loss_rate * avg_loss_pct)
    return float(expected_value)

def calculate_t0_buy_size(current_qty, t0_score, available_cash, current_price):
    """
    [Phase 20] Dynamic Position Sizing for T+0 Trades
    Returns recommended buy quantity based on score, constrained by A-share rules (multiples of 100).
    Max T+0 size equals current_qty to ensure full roll-over capability.
    """
    if t0_score >= 80:
        target_qty = current_qty # Full size T0 for S grade
    elif t0_score >= 60:
        target_qty = current_qty * 0.5 # Half size T0 for A grade
    else:
        target_qty = 0

    # Constrain by cash
    max_affordable_qty = int(available_cash / current_price)
    target_qty = min(target_qty, max_affordable_qty)

    # Round down to nearest 100
    target_qty = (int(target_qty) // 100) * 100

    return max(0, target_qty)
