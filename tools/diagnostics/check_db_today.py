import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.portfolio import PortfolioManager
from datetime import datetime

pm = PortfolioManager()
today = datetime.now().strftime('%Y-%m-%d')
# today = "2026-02-12" # Static for testing if needed

print(f"Checking selections for {today}...")

strategies = ['集合竞价', '午盘精选', '盘后资金流']

for strat in strategies:
    rows = pm.get_selections(today, strat)
    print(f"Strategy: {strat:<10} | Count: {len(rows)}")
    if rows:
        for r in rows[:3]:
            print(f"  - {r['code']} {r['name']} {r['change_pct']}%")
