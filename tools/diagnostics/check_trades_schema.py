import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.portfolio import PortfolioManager

def check():
    p = PortfolioManager()
    conn = p._get_connection()
    try:
        with conn.cursor() as cursor:
            for table in ['transactions', 'trades']:
                print(f"\n--- {table} ---")
                cursor.execute(f"DESCRIBE {table}")
                columns = cursor.fetchall()
                for col in columns:
                    print(f"{col['Field']}: {col['Type']}")
    finally:
        conn.close()

if __name__ == "__main__":
    check()
