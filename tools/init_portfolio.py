# -*- coding: utf-8 -*-
"""
初始化持仓和资金
运行一次即可：python init_portfolio.py
"""
import pymysql
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.config import Config
from datetime import datetime

def init_portfolio():
    conn = pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASS,
        database=Config.DB_NAME,
        charset='utf8mb4'
    )
    
    try:
        with conn.cursor() as cursor:
            # 清空旧持仓
            cursor.execute("DELETE FROM positions")
            
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 3. 重置可用资金
            cursor.execute("INSERT INTO portfolio_value (date, cash) VALUES (%s, %s)", (now, 10000.0))
            
            conn.commit()
            
            print("✅ 持仓已重置!")
            print("持仓: 无")
            print(f"可用现金: ¥10,000")
            print(f"总资产: ¥10,000")
            
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    init_portfolio()
