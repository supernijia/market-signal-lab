import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
from core.analyzer import StockAnalyzer
from core.tech_analyzer import TechAnalyzer

logging.basicConfig(level=logging.INFO)

def test_v19():
    provider = DataProvider()
    analyzer = StockAnalyzer(provider)
    
    # Test a known stock
    ts_code = '600519.SH' # Kweichow Moutai as sample
    print(f"Testing V20 Technical Audit for {ts_code}...")
    
    # Check ADX directly
    hist = provider.get_history_data(ts_code, count=60)
    import pandas as pd
    df = pd.DataFrame(hist)
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    
    df = TechAnalyzer.calculate_adx(df)
    adx = float(df['ADX'].iloc[-1]) if 'ADX' in df.columns else 0.0
    print(f"ADX: {adx:.2f} (Target zone 25-40)")
    
    # Check RS
    rs_pass = analyzer._check_rs(ts_code)
    print(f"RS Passed (Outperformed Index): {rs_pass}")
    
    # Full Audit
    passed = analyzer._v20_technical_audit(ts_code, history=hist)
    print(f"Full V20 Technical Audit Passed: {passed}")

if __name__ == '__main__':
    test_v19()
