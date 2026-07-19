import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.portfolio import PortfolioManager
import pandas as pd

def dump_recent_selections():
    pm = PortfolioManager()
    conn = pm._get_connection()
    if not conn:
        print("DB Connection Failed")
        return
        
    try:
        with conn.cursor() as cursor:
            print("\n=== Recent 20 Selections (strategy_selection) ===")
            cursor.execute("SELECT * FROM strategy_selection ORDER BY id DESC LIMIT 20")
            rows = cursor.fetchall()
            if rows:
                df = pd.DataFrame(rows)
                # Reorder cols for readability
                cols = ['date', 'strategy', 'code', 'name', 'change_pct', 'sector']
                print(df[cols].to_string(index=False))
            else:
                print("No records found in strategy_selection table.")

            print("\n\n=== Strategy Stats (strategy_stats) ===")
            cursor.execute("SELECT * FROM strategy_stats ORDER BY date DESC LIMIT 10")
            rows = cursor.fetchall()
            if rows:
                print(pd.DataFrame(rows).to_string(index=False))
            else:
                print("No records in strategy_stats.")
                
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    dump_recent_selections()
