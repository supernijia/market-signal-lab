#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly strategy weight/audit report with project email notification.

Run via:
    ./run_openclaw.sh scripts/weekly_audit.py

This script intentionally uses the project Config/Reporter so SMTP credentials are
loaded from .env.openclaw by run_openclaw.sh. No WeCom delivery is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymysql

from core.config import Config
from core.reporter import Reporter, log_report_snapshot
from core.utils import setup_logger


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_report(days: int = 7) -> str:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASS,
        database=Config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(strategy, '未知') AS strategy,
                    COUNT(*) AS total,
                    SUM(CASE WHEN zt_result IN ('吃肉','涨停') THEN 1 ELSE 0 END) AS win_count,
                    SUM(CASE WHEN zt_result IN ('吃面') THEN 1 ELSE 0 END) AS loss_count,
                    AVG(COALESCE(change_pct, 0)) AS avg_change
                FROM strategy_selection
                WHERE date >= %s
                GROUP BY COALESCE(strategy, '未知')
                ORDER BY total DESC
                """,
                (cutoff,),
            )
            rows = cursor.fetchall()

        lines = [
            f"📊【周度策略权重分析报告】{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            f"统计窗口: 最近 {days} 天（自 {cutoff} 起）",
            "",
        ]

        if not rows:
            lines.append("近7天暂无 strategy_selection 记录。")
            return "\n".join(lines)

        lines.extend([
            "| 策略 | 样本 | 吃肉/涨停 | 吃面 | 胜率 | 平均涨幅 | 平均评分 | 建议 |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
        ])

        ranked = []
        for row in rows:
            total = int(row.get("total") or 0)
            win_count = int(row.get("win_count") or 0)
            loss_count = int(row.get("loss_count") or 0)
            avg_change = float(row.get("avg_change") or 0.0)
            win_rate = win_count / total if total else 0.0
            # Conservative score: win-rate first, then avg change and sample confidence.
            weight_score = win_rate * 100 + avg_change * 0.5 + min(total, 20) * 0.5
            avg_score = weight_score
            ranked.append((weight_score, row.get("strategy"), total, win_count, loss_count, win_rate, avg_change, avg_score))

        ranked.sort(reverse=True, key=lambda x: x[0])
        for weight_score, strategy, total, win_count, loss_count, win_rate, avg_change, avg_score in ranked:
            if total < 3:
                suggestion = "样本不足，继续观察"
            elif win_rate >= 0.60 and avg_change >= 0:
                suggestion = "可关注/小幅提高权重"
            elif win_rate < 0.40 or avg_change < -1:
                suggestion = "降低权重或加强门禁"
            else:
                suggestion = "维持观察"
            lines.append(
                f"| {strategy} | {total} | {win_count} | {loss_count} | {_pct(win_rate)} | {avg_change:.2f}% | {avg_score:.1f} | {suggestion} |"
            )

        best = ranked[0]
        lines.extend([
            "",
            f"📈 当前综合最优: {best[1]}（样本 {best[2]}，胜率 {_pct(best[5])}，平均涨幅 {best[6]:.2f}%）",
            "",
            "说明: 本报告只给权重建议，不直接修改实盘配置。",
        ])
        return "\n".join(lines)
    finally:
        conn.close()


def main() -> int:
    setup_logger()
    report = build_report(days=7)
    print(report)
    log_report_snapshot("周度策略权重分析报告", report, source="weekly_audit")
    sent = Reporter().send_email(f"📊【周度策略权重分析报告】{datetime.now().strftime('%Y-%m-%d')}", report)
    return 0 if sent else 2


if __name__ == "__main__":
    raise SystemExit(main())
