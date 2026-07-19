import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StockAnalyzer")

def test_turnover_calc():
    dp = DataProvider()
    
    # Target stocks
    test_codes = ['000048.SZ', '600150.SH']
    
    print("\n--- 1. Fetching Circulating Share Map ---")
    float_map = dp.get_circulating_share_map()
    
    print(f"Map Size: {len(float_map)}")
    print(f"Sample Keys: {list(float_map.keys())[:5]}")
    
    # Check if 000001.SZ is in keys
    if '000001.SZ' in float_map:
        print("000001.SZ FOUND in map")
    else:
        print("000001.SZ NOT found in map")

    # Check 000048.SZ
    if '000048.SZ' in float_map:
        print("000048.SZ FOUND in map")
    else:
        print("000048.SZ NOT found in map")
        
    # debug specific date
    print("\n--- 3. Debug fetching specific date (20260211) ---")
    data = dp._request("daily_basic", {"trade_date": "20260211"}, "ts_code,float_share")
    if data and data.get('items'):
        print(f"Items count for 20260211: {len(data['items'])}")
        # Check if 000048 is in there
        found = False
        for item in data['items']:
            if '000048.SZ' in item:
                found = True
                print(f"Found 000048.SZ in 20260211 data: {item}")
                break
        if not found:
            print("000048.SZ NOT found in 20260211 data")
    
    for code in test_codes:
        f_share = float_map.get(code)
        print(f"Code: {code}, Float Share: {f_share}")
        
    print("\n--- 2. Fetching Realtime Quotes ---")
    rt_data = dp.get_realtime_quotes(test_codes)
    
    for code, data in rt_data.items():
        vol = data['vol']
        print(f"Code: {code}, Vol: {vol}")
        
        f_share = float_map.get(code)
        if f_share and f_share > 0:
            # Turnover = vol / (float_share * 10000) * 100
            tr = vol / (f_share * 10000) * 100
            print(f"Calculated Turnover: {tr:.2f}%")
        else:
            print("Cannot calculate turnover: Float share missing or 0")

if __name__ == "__main__":
    test_turnover_calc()
