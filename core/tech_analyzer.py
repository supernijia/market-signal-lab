# -*- coding: utf-8 -*-
"""
Technical Analysis Engine (V2)
Computes MACD, RSI, MAs, and ATR for detailed stock reports.
"""
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger("StockAnalyzer.Tech")

class TechAnalyzer:
    """Calculates quantitative technical indicators and generates trading insights"""
    
    @staticmethod
    def calculate_indicators(df):
        """
        Calculate technical indicators on historical daily DataFrame.
        Assumes df is sorted chronologically (ascending trade_date)
        """
        if df is None or len(df) < 30:
            return None
            
        try:
            # Ensure correct types
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['high'] = pd.to_numeric(df['high'], errors='coerce')
            df['low'] = pd.to_numeric(df['low'], errors='coerce')
            df['vol'] = pd.to_numeric(df['vol'], errors='coerce')
            
            # Moving Averages (Short + Long Term)
            df['MA5'] = df['close'].rolling(window=5).mean()
            df['MA10'] = df['close'].rolling(window=10).mean()
            df['MA20'] = df['close'].rolling(window=20).mean()
            df['MA30'] = df['close'].rolling(window=30).mean()
            df['MA60'] = df['close'].rolling(window=60).mean()
            df['MA120'] = df['close'].rolling(window=120).mean()
            
            # MACD (12, 26, 9)
            exp1 = df['close'].ewm(span=12, adjust=False).mean()
            exp2 = df['close'].ewm(span=26, adjust=False).mean()
            df['MACD'] = exp1 - exp2
            df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
            df['MACD_Hist'] = (df['MACD'] - df['Signal']) * 2
            
            # RSI (14)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))
            
            # ATR (14)
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['ATR'] = true_range.rolling(window=14).mean()
            
            # KDJ (9, 3, 3)
            low_n = df['low'].rolling(window=9).min()
            high_n = df['high'].rolling(window=9).max()
            rsv = (df['close'] - low_n) / (high_n - low_n) * 100
            df['K'] = rsv.ewm(com=2).mean() # com=2 is roughly span=3 (span=2*com+1)
            df['D'] = df['K'].ewm(com=2).mean()
            df['J'] = 3 * df['K'] - 2 * df['D']
            
            # BOLL (20, 2)
            df['BOLL_MID'] = df['close'].rolling(window=20).mean()
            df['BOLL_UP'] = df['BOLL_MID'] + 2 * df['close'].rolling(window=20).std(ddof=0)
            df['BOLL_DOWN'] = df['BOLL_MID'] - 2 * df['close'].rolling(window=20).std(ddof=0)
            
            # W&R (14)
            high_14 = df['high'].rolling(window=14).max()
            low_14 = df['low'].rolling(window=14).min()
            df['WR'] = (high_14 - df['close']) / (high_14 - low_14) * 100
            
            # OBV
            df['OBV'] = (np.sign(df['close'].diff()) * df['vol']).fillna(0).cumsum()
            
            # ADX (14) - [V20]
            df = TechAnalyzer.calculate_adx(df)
            
            return df
        except Exception as e:
            logger.error(f"Failed to calculate indicators: {e}")
            return None

    @staticmethod
    def calculate_adx(df, period=14):
        """
        [V20] Calculate Average Directional Index (ADX)
        ADX > 25 indicates a strong trend.
        """
        if df is None or len(df) < period * 2: # Need enough data for double smoothing
            return df
        
        try:
            high = df['high']
            low = df['low']
            close = df['close']
            
            # 1. TR (True Range)
            tr1 = high - low
            tr2 = abs(high - close.shift())
            tr3 = abs(low - close.shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            
            # 2. DM (Directional Movement)
            up_move = high.diff()
            down_move = -low.diff()
            
            plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0)
            minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0)
            
            # 3. Smoothing (Using rolling mean for speed/simplicity in A-share context)
            atr = tr.rolling(window=period).mean()
            plus_dm_smooth = plus_dm.rolling(window=period).mean()
            minus_dm_smooth = minus_dm.rolling(window=period).mean()
            
            # 4. DI (Directional Index)
            # Avoid division by zero
            df['plus_DI'] = 0.0
            df.loc[atr > 0, 'plus_DI'] = 100 * (plus_dm_smooth / atr)
            df['minus_DI'] = 0.0
            df.loc[atr > 0, 'minus_DI'] = 100 * (minus_dm_smooth / atr)
            
            # 5. DX & ADX
            di_sum = df['plus_DI'] + df['minus_DI']
            di_diff = abs(df['plus_DI'] - df['minus_DI'])
            
            dx = pd.Series(0.0, index=df.index)
            dx.loc[di_sum > 0] = 100 * (di_diff / di_sum)
            
            # Final ADX smoothing
            df['ADX'] = dx.rolling(window=period).mean()
            
            return df
        except Exception as e:
            logger.error(f"ADX calculate error: {e}")
            return df

    @staticmethod
    def get_advanced_advice(df):
        """
        Evaluate advanced indicators (KDJ, BOLL, WR, MACD) and return signals.
        Returns a dict with signals and resonance score.
        """
        if df is None or len(df) < 2:
            return {'signals': [], 'resonance': 0, 'desc': '数据不足'}
            
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        c = latest['close']
        signals = []
        resonance = 0 # Out of 4
        
        # 1. KDJ Signal
        k, d, j = latest['K'], latest['D'], latest['J']
        pk, pd_val = prev['K'], prev['D']
        if k > d and pk <= pd_val:
            signals.append("✅ **KDJ金叉**: K线上穿D线，短期看涨信号。")
            resonance += 1
        elif k < d and pk >= pd_val:
            signals.append("❌ **KDJ死叉**: K线下穿D线，短期看跌信号。")
        elif j > 100:
            signals.append("⚠️ **KDJ超买**: J值破百，短线有回调风险。")
        elif j < 0:
            signals.append("💡 **KDJ超卖**: J值跌入负值，随时可能反弹。")
            
        # 2. BOLL Signal
        up, down, mid = latest['BOLL_UP'], latest['BOLL_DOWN'], latest['BOLL_MID']
        if c > up:
            signals.append("⚠️ **BOLL突破上轨**: 强势但可能有抛压，谨慎追高。")
        elif c < down:
            signals.append("💡 **BOLL跌破下轨**: 短期严重超卖，关注反弹机会。")
            resonance += 1
        elif c > mid and prev['close'] <= prev['BOLL_MID']:
            signals.append("✅ **BOLL突破中轨**: 企稳中轨，中线看多。")
            resonance += 1
        elif c < mid and prev['close'] >= prev['BOLL_MID']:
            signals.append("❌ **BOLL跌破中轨**: 失去中轨支撑，中线转弱。")
            
        # 3. WR Signal
        wr = latest['WR']
        if wr < 20: # Overbought (WR is inverted usually 0-100, 0 is high)
            signals.append("⚠️ **W&R超买**: 威廉指标进入超买区(WR<20)，随时可能回调。")
        elif wr > 80:
            signals.append("💡 **W&R超卖**: 威廉指标进入超卖区(WR>80)，跌势衰竭可能反弹。")
            resonance += 1
            
        # 4. MACD Signal (from existing evaluate_trend concept)
        macd, hist = latest['MACD'], latest['MACD_Hist']
        pmacd, phist = prev['MACD'], prev['MACD_Hist']
        if macd > 0 and hist > 0 and phist <= 0:
            signals.append("✅ **MACD零上金叉**: 多头强烈确认。")
            resonance += 1
        elif macd < 0 and hist > 0 and phist <= 0:
            signals.append("💡 **MACD零下金叉**: 空头减弱，超跌反弹。")
            resonance += 1
        elif hist < 0 and phist >= 0:
            signals.append("❌ **MACD死叉**: 动能转弱，建议减仓。")
            
        return {
            'signals': signals,
            'resonance': resonance,
            'max_resonance': 4
        }

    @staticmethod
    def evaluate_trend(df):
        """
        Evaluate trend status based on the latest indicators.
        Returns a dict containing trend descriptions and key values.
        """
        if df is None or len(df) == 0:
            return {'status': '未知', 'desc': '数据不足'}
            
        latest = df.iloc[-1]
        c = latest['close']
        ma5, ma10, ma20 = latest['MA5'], latest['MA10'], latest['MA20']
        macd, hist = latest['MACD'], latest['MACD_Hist']
        rsi = latest['RSI']
        
        # Determine MA sequence
        if c > ma5 and ma5 > ma10 and ma10 > ma20:
            state = "强势多头"
        elif c < ma5 and ma5 < ma10 and ma10 < ma20:
            state = "极度弱势"
        elif c > ma20:
            state = "震荡偏强"
        else:
            state = "震荡偏弱"
            
        # Determine MACD state
        if macd > 0 and hist > 0:
            macd_desc = "MACD零轴上金叉发散"
        elif macd > 0 and hist < 0:
            macd_desc = "MACD高位死叉预期"
        elif macd < 0 and hist < 0:
            macd_desc = "MACD零轴下继续探底"
        elif macd < 0 and hist > 0:
            macd_desc = "MACD低位缩水(企稳迹象)"
        else:
            macd_desc = "MACD震荡偏中性"

        return {
            'state': state,
            'macd_desc': macd_desc,
            'rsi': rsi,
            'ma5': ma5, 'ma10': ma10, 'ma20': ma20,
            'desc': f"{state} ({c > ma20 and '站上' or '跌破'}20日线，{macd_desc})"
        }

    @staticmethod
    def get_actionable_advice(df, current_price=None):
        """
        Generate actionable trading conditions (Stop loss, Entry, Position limits)
        based on risk/reward and momentum.
        """
        if df is None or len(df) == 0:
            return {}
            
        latest_row = df.iloc[-1]
        cp = current_price if current_price else latest_row['close']
        atr = latest_row['ATR']
        trend = TechAnalyzer.evaluate_trend(df)
        status = trend['state']
        
        # 1. Stop loss
        # Default 1.5 ATR for short-term swing
        stop_loss = round(cp - (1.5 * atr), 2)
        
        # 2. Support / Resistance
        recent_high = df['high'].tail(15).max()
        recent_low = df['low'].tail(15).min()
        support = round(max(recent_low, latest_row['MA20']), 2) 
        resist = round(min(recent_high, latest_row['MA5'] * 1.05), 2) # Assume 5% bump is resistance
        
        # 3. Action and Max Position
        if status == "极度弱势":
            action = "🛑 规避 / 严格观望"
            pos_limit = 0
            entry_cond = f"RSI下探极度超卖(或日内企稳)且股价站稳¥{round(latest_row['MA5'], 2)}"
        elif status == "强势多头":
            action = "🟢 持股待涨 / 顺势做多"
            pos_limit = 20
            entry_cond = f"回踩¥{round(latest_row['MA5'], 2)}附近不破可低吸"
        else:
            action = "🟡 箱体震荡 / 高抛低吸"
            pos_limit = 10
            entry_cond = f"突破阻力位¥{resist} 或 踩稳支撑位¥{support} 时轻仓"

        return {
            'stop_loss': stop_loss,
            'support': support,
            'resistance': resist,
            'atr': round(atr, 2),
            'action': action,
            'max_position': f"{pos_limit}%",
            'entry_condition': entry_cond
        }

    @staticmethod
    def calculate_t0_score(df, current_price):
        """
        [Phase 20] Calculate a quantitative T0 score (0-100) based on daily chart data.
        Factors:
        1. MA Deviation (30%): Further below MA5 = higher score for positive T.
        2. Support Proximity (30%): Closer to S1/S2 = higher score.
        3. Volume Trend (20%): Shrinking volume on drop = higher score.
        4. Trend Status (20%): Overall trend context.
        """
        if df is None or len(df) < 5 or current_price is None or current_price <= 0:
            return {'score': 0, 'desc': '数据不足', 'details': {}}
            
        latest = df.iloc[-1]
        
        score = 0
        details = {}
        
        # 1. MA Deviation (Max 30)
        ma5 = latest['MA5']
        dev_ma5 = (current_price - ma5) / ma5
        if dev_ma5 < -0.05:
            score += 30
            details['ma_dev'] = "严重超卖(偏离5日线>5%) +30分"
        elif dev_ma5 < -0.02:
            score += 20
            details['ma_dev'] = "适度超卖(偏离5日线>2%) +20分"
        elif dev_ma5 > 0.05:
            # Overbought, better for reverse T
            score += 10
            details['ma_dev'] = "大幅均线偏离(可考虑倒T) +10分"
        else:
            score += 5
            details['ma_dev'] = "均线附近徘徊 +5分"
            
        # 2. Support Proximity (Max 30)
        # We need yesterday's data to calculate the Pivot Points for today
        prev_day = df.iloc[-2]
        p_high, p_low, p_close = prev_day['high'], prev_day['low'], prev_day['close']
        pivot = (p_high + p_low + p_close) / 3
        s1 = (2 * pivot) - p_high
        s2 = pivot - (p_high - p_low)
        
        if current_price <= s2 * 1.01:
            score += 30
            details['support'] = "击穿极限支撑位(S2) +30分"
        elif current_price <= s1 * 1.015:
            score += 20
            details['support'] = "靠近强支撑位(S1) +20分"
        else:
            score += 5
            details['support'] = "距离支撑位较远 +5分"
            
        # 3. Volume Trend (Max 20)
        # If today is down and volume is shrinking compared to 5-day avg, it's a good sign for dip buying
        vol_today = latest['vol']
        vol_ma5 = df['vol'].tail(5).mean()
        is_down = current_price < prev_day['close']
        
        if is_down and vol_today < vol_ma5 * 0.8:
            score += 20
            details['volume'] = "缩量下跌(恐慌盘枯竭) +20分"
        elif not is_down and vol_today > vol_ma5 * 1.2:
             score += 15
             details['volume'] = "放量上攻(有承接) +15分"
        else:
            score += 5
            details['volume'] = "量价平庸 +5分"
            
        # 4. Trend Status (Max 20)
        trend = TechAnalyzer.evaluate_trend(df)
        if trend['state'] == "强势多头":
             score += 20
             details['trend'] = "强势多头(容错率高) +20分"
        elif trend['state'] == "震荡偏强":
             score += 10
             details['trend'] = "箱体震荡(高抛低吸) +10分"
        else:
             score += 0
             details['trend'] = "弱势或空头(风险较高) +0分"
             
        score = min(max(int(score), 0), 100)
        
        if score >= 80:
            eval_desc = "极其适合做T (S级)"
        elif score >= 60:
            eval_desc = "具备做T空间 (A级)"
        elif score >= 40:
             eval_desc = "做T空间一般 (B级)"
        else:
             eval_desc = "不建议做T (观望)"
             
        return {
            'score': score,
            'desc': eval_desc,
            'details': details
        }
