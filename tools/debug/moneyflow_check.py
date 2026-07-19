import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StockAnalyzer")

def test_money_flow_fetch():
    dp = DataProvider()
    
    # Test stocks: Ping An (000001.SZ), Maotai (600519.SH)
    test_codes = ['000001.SZ', '600519.SH']
    
    print(f"\n--- Testing Money Flow Fetch for {test_codes} (Last 3 days) ---")
    
    # Fetch
    flows = dp.get_individual_money_flow(test_codes, days=3)
    
    print("\n--- Results ---")
    for code, flow in flows.items():
        flow_wan = flow / 10000
        print(f"{code}: {flow_wan:.2f} 万")
        
    if not flows:
        print("ERROR: No flows returned regarding these stocks. (Could be Tushare limit or no data)")
    else:
        print("SUCCESS: Data fetched.")

if __name__ == "__main__":
    test_money_flow_fetch()
