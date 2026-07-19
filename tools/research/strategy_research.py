# 策略调研脚本
# 用途: 分析近7天各策略选股表现，输出评分报告并使用项目邮件通知

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymysql

from core.config import Config
from core.reporter import Reporter, log_report_snapshot
from core.utils import setup_logger


def build_report() -> str:
    conn = pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASS,
        database=Config.DB_NAME,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT strategy, COUNT(*) as total,
                       SUM(CASE WHEN zt_result IN ("涨停", "吃肉") THEN 1 ELSE 0 END) as zt_count,
                       AVG(COALESCE(change_pct, 0)) as avg_change
                FROM strategy_selection
                WHERE date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                GROUP BY strategy
            ''')
            rows = cursor.fetchall()

        results = []
        for row in rows:
            avg = float(row['avg_change']) if row['avg_change'] else 0.0
            total = int(row['total']) if row['total'] else 0
            zt_count = int(row['zt_count']) if row['zt_count'] else 0
            zt_rate = zt_count / total * 100 if total > 0 else 0.0
            score = zt_rate * 2 + avg * 0.5  # 简单评分
            results.append((row['strategy'] or '未知', total, zt_count, zt_rate, avg, score))

        results.sort(key=lambda x: x[5], reverse=True)

        lines = [
            '=' * 60,
            f'【策略调研报告】(近7天) {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            '=' * 60,
            '',
        ]

        if not results:
            lines.append('近7天无策略选股记录')
            return '\n'.join(lines)

        lines.extend([
            '| 策略 | 选出 | 涨停/吃肉 | 涨停率 | 平均涨幅 | 评分 |',
            '| :--- | ---: | ---: | ---: | ---: | ---: |',
        ])
        for strat, total, zt_cnt, zt_rate, avg, score in results:
            lines.append(f'| {strat} | {total} | {zt_cnt} | {zt_rate:.1f}% | {avg:.1f}% | {score:.1f} |')

        best = results[0]
        lines.extend([
            '',
            f'📈 最优策略: {best[0]} (评分: {best[5]:.1f})',
            '💡 建议: 可适当提高该策略权重，或增加选中数量；正式配置仍需结合风控审计人工确认。',
        ])
        return '\n'.join(lines)
    finally:
        conn.close()


def main() -> int:
    setup_logger()
    report = build_report()
    print(report)
    log_report_snapshot('策略调研-买入权重优化分析', report, source='strategy_research')
    sent = Reporter().send_email(f'📈【策略调研】买入权重优化分析 {datetime.now().strftime("%Y-%m-%d")}', report)
    return 0 if sent else 2


if __name__ == '__main__':
    raise SystemExit(main())
