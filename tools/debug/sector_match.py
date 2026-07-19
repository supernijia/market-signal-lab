import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StockAnalyzer")

def test_industry_match():
    dp = DataProvider()
    
    print("\n--- 1. Fetching Sector Money Flow Names ---")
    sector_flow = dp.get_sector_money_flow(days=1)
    # Get unique sector names
    mf_sectors = set(item['name'] for item in sector_flow)
    print(f"MoneyFlow Sectors (Sample 10): {list(mf_sectors)[:10]}")
    
    print("\n--- 2. Fetching Stock Basic Industries ---")
    basics = dp.get_stock_basic()
    basic_industries = set(info['industry'] for info in basics.values() if info['industry'])
    print(f"Basic Industries (Sample 10): {list(basic_industries)[:10]}")
    
    print("\n--- 3. Checking Overlap ---")
    overlap = mf_sectors.intersection(basic_industries)
    print(f"Total MoneyFlow Sectors: {len(mf_sectors)}")
    print(f"Total Basic Industries: {len(basic_industries)}")
    print(f"Overlapping Sectors: {len(overlap)}")
    print(f"Overlap Ratio: {len(overlap)/len(mf_sectors):.2%}")
    
    print("\n--- Mismatch Examples ---")
    only_in_mf = list(mf_sectors - basic_industries)[:10]
    print(f"In MoneyFlow but not Basic: {only_in_mf}")

if __name__ == "__main__":
    test_industry_match()
