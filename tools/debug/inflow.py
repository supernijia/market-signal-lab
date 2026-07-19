import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data_provider import DataProvider
dp = DataProvider()
today = dp._get_latest_trade_date()
flow = dp.get_sector_rank_by_aggregated_flow(days=1, end_date=today)
for s in flow[:5]:
    print(f"Sector: {s['name']}, Raw Inflow: {s['net_inflow']}")
    for st in s.get('top_stocks', [])[:2]:
        print(f"  {st['code']}: raw={st['net_inflow']}")
