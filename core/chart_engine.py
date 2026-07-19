# -*- coding: utf-8 -*-
import os
import glob
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import mplfinance as mpf
import matplotlib as mpl
from core.tech_analyzer import TechAnalyzer

# Set Chinese font support for matplotlib
mpl.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
mpl.rcParams['axes.unicode_minus'] = False

logger = logging.getLogger("StockAnalyzer.ChartEngine")

class ChartEngine:
    def __init__(self, provider):
        self.provider = provider
        # Ensure images directory exists
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.image_dir = os.path.join(base_dir, 'reports', 'images')
        os.makedirs(self.image_dir, exist_ok=True)
        
        # Cleanup old charts right on initialization
        self.cleanup_old_charts(days=7)

    def cleanup_old_charts(self, days=7):
        """Automatically delete charts older than specific days"""
        try:
            current_time = time.time()
            cutoff = current_time - (days * 86400)
            
            files = glob.glob(os.path.join(self.image_dir, "*.png"))
            deleted = 0
            for f in files:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
                    deleted += 1
            if deleted > 0:
                logger.debug(f"GC Cleaned {deleted} old chart files.")
        except Exception as e:
            logger.error(f"Failed to cleanup old charts: {e}")

    def generate_t0_charts(self, ts_code, name, buy_price=None, buy_time=None, hold_vol=None):
        """
        Generate Daily and Intraday (1min) charts.
        Returns a dict of file paths and a string of T+0 advice.
        """
        today_str = datetime.now().strftime('%Y%m%d')
        daily_path = os.path.join(self.image_dir, f"{today_str}-{ts_code}_daily.png")
        intraday_path = os.path.join(self.image_dir, f"{today_str}-{ts_code}_intraday.png")
        
        advice_lines = []
        paths = {'_meta': {'intraday_points': 0, 'intraday_quality': 'missing'}}
        
        # 1. Fetch Daily Data (~60 days)
        start_date_daily = (datetime.now() - timedelta(days=90)).strftime('%Y%m%d')
        df_daily = self.provider.get_stock_hist(ts_code, start_date_daily, today_str)
        
        if df_daily is not None and not df_daily.empty:
            df_curr = df_daily.copy()
            df_curr['trade_date'] = pd.to_datetime(df_curr['trade_date'])
            df_curr.set_index('trade_date', inplace=True)
            df_curr.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'vol': 'Volume'}, inplace=True)
            
            # Daily Plot
            alines = []
            if buy_price and buy_price > 0:
                # Add horizontal line for buy price
                alines.append((buy_price, buy_price))
            
            try:
                # Setup kwargs
                kwargs = dict(type='candle', mav=(5,10,20), volume=True, figratio=(12, 6), figscale=1.2, 
                              title=f"{ts_code} Daily Chart", style='yahoo')
                if buy_price and buy_price > 0:
                    kwargs['hlines'] = dict(hlines=[buy_price], colors=['r'], linestyle='--', linewidths=[1.5])
                
                mpf.plot(df_curr.tail(60), datetime_format='%Y-%m-%d', **kwargs, savefig=daily_path)
                paths['daily'] = daily_path
            except Exception as e:
                logger.error(f"Failed to generate daily chart: {e}")
                
        # 2. Fetch Intraday 1min Data
        start_date_min = (datetime.now() - timedelta(days=2)).strftime('%Y%m%d')
        df_min = self.provider.get_stock_min_data(ts_code, start_date_min, today_str)
        
        if df_min is not None and not df_min.empty:
            df_min = df_min.copy()
            # trade_time format is typically '2026-03-05 09:31:00'
            df_min['trade_time'] = pd.to_datetime(df_min['trade_time'])
            df_min.set_index('trade_time', inplace=True)
            df_min.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'vol': 'Volume'}, inplace=True)
            
            # Filter solely for the most recent day in the data
            today_date_str = df_min.index[-1].strftime('%Y-%m-%d')
            df_today = df_min.loc[df_min.index.strftime('%Y-%m-%d') == today_date_str]
            
            if not df_today.empty:
                # Calculate VWAP
                df_today = df_today.copy()
                paths['_meta']['intraday_points'] = int(len(df_today))
                paths['_meta']['intraday_date'] = today_date_str
                paths['_meta']['intraday_last_time'] = df_today.index[-1].strftime('%H:%M:%S')
                if len(df_today) >= 30:
                    paths['_meta']['intraday_quality'] = 'ok'
                else:
                    paths['_meta']['intraday_quality'] = 'insufficient'
                df_today['amount'] = df_today.get('amount', df_today['Close'] * df_today['Volume'])
                df_today['cum_amount'] = df_today['amount'].cumsum()
                df_today['cum_volume'] = df_today['Volume'].cumsum()
                df_today['VWAP'] = df_today['cum_amount'] / df_today['cum_volume'] / 100 # Adjust if amount is in 1000s or native
                
                # If amount isn't accurate, fallback to typical price VWAP
                typical_price = (df_today['High'] + df_today['Low'] + df_today['Close']) / 3
                df_today['VWAP'] = (typical_price * df_today['Volume']).cumsum() / df_today['Volume'].cumsum()
                
                # Pivot Points calculation based on previous day's daily data
                pivot, r1, r2, s1, s2 = 0, 0, 0, 0, 0
                if df_daily is not None and len(df_daily) >= 2:
                    prev_day = df_daily.iloc[-2] # Assuming last is today if during market, -2 is yesterday
                    p_high, p_low, p_close = prev_day['high'], prev_day['low'], prev_day['close']
                    pivot = (p_high + p_low + p_close) / 3
                    r1 = (2 * pivot) - p_low
                    r2 = pivot + (p_high - p_low)
                    s1 = (2 * pivot) - p_high
                    s2 = pivot - (p_high - p_low)
                    
                    advice_lines.append(f"- **日内多空枢轴 (Pivot)**: {pivot:.2f} (企稳其上做多，跌破看空)")
                    advice_lines.append(f"- **超买做T抛压区**: 强阻 {r1:.2f} / 极限 {r2:.2f}")
                    advice_lines.append(f"- **超卖做T低吸区**: 强撑 {s1:.2f} / 极限 {s2:.2f}")
                
                    vwap_val = df_today['VWAP'].iloc[-1]
                    curr_price = df_today['Close'].iloc[-1]
                    advice_lines.append(f"- **最新均价(VWAP)**: {vwap_val:.2f}")
                    if len(df_today) < 30:
                        advice_lines.append(f"- **分时数据质量**: 当前仅 {len(df_today)} 个分钟点，分时量能判断只能作弱参考")
                    
                    # [Phase 20] T+0 深度解套与做T辅助提示
                    if buy_price and buy_price > 0:
                        underwater_pct = (curr_price - buy_price) / buy_price
                        
                        if underwater_pct < -0.05:
                            # User is trapped (loss > 5%)
                            advice_lines.append(f"\n> [!WARNING]\n> **【自救做T窗口】** 当前仓位已浮亏 {abs(underwater_pct)*100:.1f}%，具备摊低成本的做T空间。")
                            
                            # Check for buy conditions (near support or far below VWAP)
                            if curr_price <= s1 * 1.01 or curr_price < vwap_val * 0.98:
                                advice_lines.append("> 🚨 **【低吸做正T】** 股价已杀跌至极度超卖区(或贴近强支撑)，可考虑**逢低买入相同股数(做正T)**。")
                                t_out_price = min(curr_price * 1.02, r1) if r1 > 0 else curr_price * 1.02
                                advice_lines.append(f"> 🎯 **【建议 T出价(降本点)】** 买入后，挂单目标 **¥{t_out_price:.2f}** 抛出今日买入部分，赚取差价摊低成本。")
                            else:
                                advice_lines.append("> ⏳ 暂未进入极度超卖击杀区间，请耐心等待分时急跌再进行正T低吸。")
                        
                        elif underwater_pct > 0.05:
                             advice_lines.append(f"\n> [!TIP]\n> **【顺势做T窗口】** 当前仓位浮盈 {underwater_pct*100:.1f}%，安全垫丰厚。")
                             if curr_price >= r1 * 0.99 or curr_price > vwap_val * 1.02:
                                 advice_lines.append("> 🚨 **【高抛做倒T】** 股价已脉冲至强阻力位/均价线上方，建议**逢高抛出部分筹码(锁利润)**。")
                                 t_back_price = max(curr_price * 0.98, s1) if s1 > 0 else curr_price * 0.98
                                 advice_lines.append(f"> 🎯 **【建议 接回价】** 抛出后，挂单目标 **¥{t_back_price:.2f}** 重新接回以增加持仓数量。")
                                 
                    if curr_price > vwap_val:
                        advice_lines.append("\n  💡 *当前股价运行于均价线上方，多头控盘，势头良好。*")
                    else:
                        advice_lines.append("\n  ⚠️ *当前股价运行于均价线下方，承压状态，谨慎接飞刀。*")

                try:
                    # Append VWAP to plot
                    apdict = mpf.make_addplot(df_today['VWAP'], color='orange', width=1.5)
                    
                    # Draw support/resist lines if exists
                    hlines_vals = []
                    hlines_colors = []
                    if pivot > 0:
                        hlines_vals.extend([r2, r1, pivot, s1, s2])
                        hlines_colors.extend(['red', 'red', 'purple', 'green', 'green'])
                        
                    if buy_price and buy_price > 0:
                        hlines_vals.append(buy_price)
                        hlines_colors.append('blue')
                        
                    kwargs = dict(type='line', volume=True, figratio=(12, 6), figscale=1.2,
                                  title=f"{ts_code} 1-Min Intraday", style='yahoo', addplot=apdict)
                    
                    if hlines_vals:
                        kwargs['hlines'] = dict(hlines=hlines_vals, colors=hlines_colors, linestyle='--', linewidths=[1.0]*len(hlines_vals))

                    if buy_time:
                        try:
                            # Parse absolute time if it has a slash/dash, else assume today
                            if '-' in buy_time or '/' in buy_time:
                                bt_dt = pd.to_datetime(buy_time)
                            else:
                                bt_dt = pd.to_datetime(f"{today_date_str} {buy_time.strip()}")
                            
                            # Only draw vertical line if buy_time is actually TODAY
                            # (Since the intraday chart only plots today's X-axis bounds)
                            if bt_dt.date() == pd.to_datetime(today_date_str).date():
                                kwargs['vlines'] = dict(vlines=bt_dt, colors='blue', linestyle='-.', linewidths=2.0)
                        except Exception as e:
                            logger.warning(f"Could not parse buy_time '{buy_time}': {e}")
                    
                    # Pad to 15:00 if market is still open, to show 'future' space
                    last_time = df_today.index[-1]
                    end_time = pd.to_datetime(f"{today_date_str} 15:00:00")
                    if last_time < end_time:
                        idx_future = pd.date_range(start=last_time + pd.Timedelta(minutes=1), end=end_time, freq='1min')
                        df_future = pd.DataFrame(index=idx_future, columns=df_today.columns)
                        df_padded = pd.concat([df_today, df_future])
                        
                        # Forward fill VWAP and plot on padded
                        apdict = mpf.make_addplot(df_padded['VWAP'].ffill(), color='orange', width=1.5)
                        kwargs['addplot'] = apdict
                        mpf.plot(df_padded, datetime_format='%H:%M', **kwargs, savefig=intraday_path)
                    else:
                        mpf.plot(df_today, datetime_format='%H:%M', **kwargs, savefig=intraday_path)
                        
                    paths['intraday'] = intraday_path
                except Exception as e:
                    logger.error(f"Failed to generate intraday chart: {e}")
        else:
            advice_lines.append("未能获取分钟级数据，请检查Tushare权限或网络。")
            
        return paths, "\n".join(advice_lines)
        
    def generate_tech_indicators(self, ts_code, period=60):
        """
        Generate a comprehensive 5-panel technical indicator chart.
        """
        # 1. Fetch data
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=period*2)).strftime('%Y%m%d')
        
        # We need raw Tushare daily data (list of dicts), then convert to DataFrame for tech_analyzer
        raw_data = self.provider.get_history_data(ts_code, count=period)
        if not raw_data:
            return None, "获取历史数据失败"
            
        df = pd.DataFrame(raw_data)
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df.set_index('trade_date', inplace=True)
        # Ensure numeric
        for col in ['close', 'open', 'high', 'low', 'vol']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 2. Calculate Indicators
        df = TechAnalyzer.calculate_indicators(df)
        
        # Add basic RSI and BOLL for plotting if missing
        if 'RSI' not in df.columns:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))
            
        # Add simple KDJ approximation for plotting
        if 'K' not in df.columns:
            low_min = df['low'].rolling(9).min()
            high_max = df['high'].rolling(9).max()
            rsv = (df['close'] - low_min) / (high_max - low_min) * 100
            df['K'] = rsv.ewm(com=2).mean()
            df['D'] = df['K'].ewm(com=2).mean()
            df['J'] = 3 * df['K'] - 2 * df['D']

        # BOLL
        df['BOLL_MID'] = df['close'].rolling(20).mean()
        std = df['close'].rolling(20).std()
        df['BOLL_UP'] = df['BOLL_MID'] + 2 * std
        df['BOLL_DOWN'] = df['BOLL_MID'] - 2 * std
        
        # Trim to requested period
        df = df.tail(period)
        
        # 3. Plotting
        fig, axes = plt.subplots(5, 1, figsize=(14, 16), sharex=True, gridspec_kw={'height_ratios': [3, 1, 1, 1, 1]})
        
        # Panel 1: Price + BOLL
        ax1 = axes[0]
        ax1.plot(df.index, df['close'], 'b-', linewidth=1.5, label='Close')
        ax1.plot(df.index, df['BOLL_UP'], 'purple', linestyle='--', alpha=0.5, label='BOLL')
        ax1.plot(df.index, df['BOLL_DOWN'], 'purple', linestyle='--', alpha=0.5)
        ax1.fill_between(df.index, df['BOLL_UP'], df['BOLL_DOWN'], color='purple', alpha=0.05)
        ax1.set_ylabel('Price')
        ax1.set_title(f"{ts_code} 核心技术指标体系 (Technical Indicators)", fontsize=14)
        ax1.legend(loc='upper left')
        ax1.grid(True, alpha=0.3)
        
        # Panel 2: MACD
        ax2 = axes[1]
        colors = ['red' if v > 0 else 'green' for v in df.get('MACD_Hist', [0]*len(df))]
        ax2.bar(df.index, df.get('MACD_Hist', [0]*len(df)), color=colors, alpha=0.6)
        ax2.plot(df.index, df.get('MACD', [0]*len(df)), 'b-', label='DIF', linewidth=1)
        ax2.plot(df.index, df.get('Signal', [0]*len(df)), 'orange', label='DEA', linewidth=1)
        ax2.axhline(0, color='black', linewidth=0.5, linestyle='--')
        ax2.set_ylabel('MACD')
        ax2.legend(loc='upper left')
        ax2.grid(True, alpha=0.3)
        
        # Panel 3: KDJ
        ax3 = axes[2]
        ax3.plot(df.index, df.get('K', [0]*len(df)), 'b-', label='K', linewidth=1)
        ax3.plot(df.index, df.get('D', [0]*len(df)), 'orange', label='D', linewidth=1)
        ax3.plot(df.index, df.get('J', [0]*len(df)), 'purple', label='J', linewidth=1)
        ax3.axhline(80, color='r', linestyle='--', alpha=0.3)
        ax3.axhline(20, color='g', linestyle='--', alpha=0.3)
        ax3.fill_between(df.index, 80, 100, color='red', alpha=0.05)
        ax3.fill_between(df.index, 0, 20, color='green', alpha=0.05)
        ax3.set_ylabel('KDJ')
        ax3.legend(loc='upper left')
        ax3.grid(True, alpha=0.3)
        
        # Panel 4: RSI
        ax4 = axes[3]
        ax4.plot(df.index, df.get('RSI', [0]*len(df)), 'b-', linewidth=1.5, label='RSI(14)')
        ax4.axhline(70, color='r', linestyle='--', alpha=0.5)
        ax4.axhline(30, color='g', linestyle='--', alpha=0.5)
        ax4.fill_between(df.index, 70, 100, color='red', alpha=0.05)
        ax4.fill_between(df.index, 0, 30, color='green', alpha=0.05)
        ax4.set_ylabel('RSI')
        ax4.set_ylim(0, 100)
        ax4.legend(loc='upper left')
        ax4.grid(True, alpha=0.3)
        
        # Panel 5: Volume
        ax5 = axes[4]
        vcolors = ['red' if df['close'].iloc[i] >= df['open'].iloc[i] else 'green' for i in range(len(df))]
        ax5.bar(df.index, df['vol'], color=vcolors, alpha=0.7)
        ax5.set_ylabel('Volume')
        ax5.grid(True, alpha=0.3)
        
        plt.tight_layout()
        today_str = datetime.now().strftime('%Y%m%d')
        path = os.path.join(self.image_dir, f"{today_str}-{ts_code}_indicators.png")
        plt.savefig(path, dpi=120)
        plt.close()
        
        return path, "技术指标体系图表已生成。"

    def generate_money_flow(self, ts_code, days=20):
        """Generate Individual Money Flow Chart"""
        mf_data = self.provider.get_individual_money_flow([ts_code], days=days)
        if not mf_data or ts_code not in mf_data:
            return None, "无资金流数据"
            
        stats = mf_data[ts_code] # Notice: Provider currently returns aggregated sums if we passed ts_code_list. 
        # Wait, get_individual_money_flow actually aggregates over the period! 
        # We need daily breakdown for charting!
        return None, "资金流详表暂未实现日历图绘"
        
    def generate_comparison_chart(self, ts_code, period=60):
        """Generate Relative Strength Comparison with SH Index"""
        stock_data = self.provider.get_history_data(ts_code, count=period)
        index_data = self.provider.get_index_daily('000001.SH', count=period)
        
        if not stock_data or not index_data:
            return None, "数据不足无法对比"
            
        df_stk = pd.DataFrame(stock_data)
        df_idx = pd.DataFrame(index_data)
        
        df_stk['trade_date'] = pd.to_datetime(df_stk['trade_date'])
        df_idx['trade_date'] = pd.to_datetime(df_idx['trade_date'])
        
        df_stk.set_index('trade_date', inplace=True)
        df_idx.set_index('trade_date', inplace=True)
        
        # Align dates (intersection)
        common_dates = df_stk.index.intersection(df_idx.index)
        df_stk = df_stk.loc[common_dates]
        df_idx = df_idx.loc[common_dates]
        
        if df_stk.empty or df_idx.empty:
            return None, "无有效交易日重叠"
            
        # Normalize to Base=100
        df_stk['norm_close'] = (pd.to_numeric(df_stk['close']) / pd.to_numeric(df_stk['close'].iloc[0])) * 100
        df_idx['norm_close'] = (pd.to_numeric(df_idx['close']) / pd.to_numeric(df_idx['close'].iloc[0])) * 100
        
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(df_stk.index, df_stk['norm_close'], 'r-', linewidth=2, label=f'{ts_code} (Target)')
        ax.plot(df_idx.index, df_idx['norm_close'], 'b--', linewidth=1.5, alpha=0.7, label='000001.SH (Index)')
        
        ax.axhline(100, color='gray', linestyle='-.', alpha=0.5)
        
        # Fill areas where stock out-performs index
        ax.fill_between(df_stk.index, df_stk['norm_close'], df_idx['norm_close'], 
                        where=(df_stk['norm_close'] >= df_idx['norm_close']), 
                        interpolate=True, color='red', alpha=0.1, label='Alpha (Outperform)')
                        
        ax.set_title(f"{ts_code} 个股 vs 上证指数 相对强弱 (Base=100)", fontsize=14)
        ax.set_ylabel('Relative Price')
        ax.legend(loc='upper left')
        ax.grid(True, alpha=0.3)
        
        plt.tight_layout()
        today_str = datetime.now().strftime('%Y%m%d')
        path = os.path.join(self.image_dir, f"{today_str}-{ts_code}_comparison.png")
        plt.savefig(path, dpi=120)
        plt.close()
        
        return path, "相对强弱对比图已生成。"
