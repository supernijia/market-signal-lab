import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
import logging
from datetime import datetime, timedelta

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StockAnalyzer")

def test_turnover_calc():
    dp = DataProvider()
    
    print("\n--- Debugging Data Fetch ---")
    latest_date = dp._get_latest_trade_date()
    print(f"Latest Trade Date (from DP): {latest_date}")
    
    # Check Tushare connection first
    print("Checking Tushare connectivity...")
    try:
        cal = dp._request("trade_cal", {"start_date": latest_date, "end_date": latest_date})
        print(f"Trade Cal for {latest_date}: {cal}")
    except Exception as e:
        print(f"Tushare connection failed: {e}")
        return

    # Try 3 days back
    dates_to_try = []
    curr = datetime.strptime(latest_date, "%Y%m%d")
    for i in range(5):
        d = (curr - timedelta(days=i)).strftime('%Y%m%d')
        dates_to_try.append(d)
        
    print(f"Will try dates: {dates_to_try}")
    
    valid_map = {}
    
    for d in dates_to_try:
        print(f"\nFetching daily_basic for {d}...")
        try:
            res = dp._request("daily_basic", {"trade_date": d}, "ts_code,float_share")
            if res and res.get('items'):
                count = len(res['items'])
                print(f"  SUCCESS: Found {count} items")
                # Check for 000048.SZ
                sample_found = False
                for item in res['items']:
                    if '000048.SZ' in item:
                        print(f"  Found 000048.SZ: {item}")
                        sample_found = True
                        break
                if not sample_found:
                    print("  000048.SZ NOT found in this batch")
                
                if count > 1000:
                    print("  Data looks good. Stopping search.")
                    break
            else:
                print("  FAILED/EMPTY")
        except Exception as e:
            print(f"  Exception: {e}")

    # Test the actual method
    print("\n--- Testing get_circulating_share_map() ---")
    float_map = dp.get_circulating_share_map()
    print(f"Map Size: {len(float_map)}")

if __name__ == "__main__":
    test_turnover_calc()
