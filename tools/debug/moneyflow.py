import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
import json

dp = DataProvider()

# 1. Test Date Generation
today = dp._get_latest_trade_date()
print(f"Latest Trade Date: {today}")

# 2. Test Sector Money Flow (Raw)
print("\nFetching Sector Money Flow (1 day)...")
try:
    data = dp.get_sector_money_flow(days=1, end_date=today)
    print(f"Result Type: {type(data)}")
    print(f"Result Count: {len(data) if data else 0}")
    if data:
        print("First Item:")
        print(json.dumps(data[0], indent=2, ensure_ascii=False))
    else:
        print("RAW DATA IS EMPTY.")
except Exception as e:
    print(f"Error: {e}")

# 3. Test Individual Money Flow (Just to check permissions)
print("\nFetching Individual Money Flow (000001)...")
try:
    data = dp.get_individual_money_flow('000001.SZ', days=5, end_date=today)
    print(f"Result Count: {len(data) if data else 0}")
except Exception as e:
    print(f"Error: {e}")
