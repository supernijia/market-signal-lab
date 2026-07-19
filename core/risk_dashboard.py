# -*- coding: utf-8 -*-
"""Read-only risk dashboard snapshot."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pymysql

from core.config import Config
from core.display_labels import (
    display_account,
    display_action,
    display_bool,
    display_event_type,
    display_regime,
    display_risk_level,
    humanize_text,
)
from core.paper_account import account_position_count
from core.portfolio import is_virtual_account


class RiskDashboard:
    def __init__(self, portfolio, provider, analyzer):
        self.portfolio = portfolio
        self.provider = provider
        self.analyzer = analyzer

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:+.2f}%"
        except Exception:
            return "-"

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _format_ts_code(code: str) -> str:
        c = str(code or "").strip()
        if not c:
            return ""
        if "." in c:
            return c
        return f"{c}.SH" if c.startswith("6") else f"{c}.SZ"

    @staticmethod
    def _position_entry_datetime(position: dict) -> datetime | None:
        entry_time_str = str(position.get("created_at") or position.get("update_time") or "").strip()
        if not entry_time_str:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(entry_time_str.split(".")[0], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(entry_time_str.split(".")[0])
        except Exception:
            return None

    @classmethod
    def _effective_position_high(cls, position: dict, quote: dict, curr_price: float, buy_price: float, now: datetime | None = None) -> float:
        highest = max(cls._safe_float(position.get("highest_price"), 0.0), buy_price)
        entry_dt = cls._position_entry_datetime(position)
        if entry_dt and entry_dt.date() == (now or datetime.now()).date():
            return max(highest, curr_price)
        high = cls._safe_float((quote or {}).get("high"), curr_price)
        return max(highest, high)

    @classmethod
    def _exit_stop_settings(cls, account: str) -> dict:
        settings = {
            "stop_loss": Config.RISK_MANAGEMENT.get("STOP_LOSS", -0.03),
            "min_profit_lock_trigger": 0.02,
            "min_profit_lock_pct": 0.01,
        }
        if not str(account or "").lower().startswith("paper_"):
            return settings
        try:
            cfg = Config.STRATEGY.get("paper_exit_policy", {}) if isinstance(Config.STRATEGY, dict) else {}
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", True)):
            return settings
        settings["stop_loss"] = cls._safe_float(cfg.get("stop_loss"), settings["stop_loss"])
        settings["min_profit_lock_trigger"] = cls._safe_float(
            cfg.get("min_profit_lock_trigger"),
            settings["min_profit_lock_trigger"],
        )
        settings["min_profit_lock_pct"] = cls._safe_float(cfg.get("min_profit_lock_pct"), settings["min_profit_lock_pct"])
        return settings

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self.portfolio._get_connection()
        if not conn:
            return []
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall() or []
        except Exception:
            return []
        finally:
            conn.close()

    def _pending_signals(self, trade_date: str) -> list[dict]:
        return self._query(
            """
            SELECT id, trade_date, code, name, source_strategy, weather, signal_bucket,
                   entry_model, status, check_count, last_reason, signal_time, expires_at
            FROM pending_entry_signals
            WHERE trade_date=%s
              AND status='PENDING'
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (trade_date,),
        )

    def _shadow_signals(self, trade_date: str) -> list[dict]:
        return self._query(
            """
            SELECT id, trade_date, code, name, source_strategy, signal_bucket,
                   entry_model, status, check_count, last_reason, payload_json, updated_at
            FROM pending_entry_signals
            WHERE trade_date=%s
              AND (status='SHADOW' OR entry_model='audit_only_shadow' OR source_strategy LIKE '%%_SHADOW')
            ORDER BY updated_at DESC
            LIMIT 40
            """,
            (trade_date,),
        )

    @staticmethod
    def _safe_json(value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _audit_reason_label(reason: Any) -> str:
        reason = str(reason or "").strip()
        mapping = {
            "PERMISSION_OBSERVE_ONLY": "权限观察",
            "PERMISSION_BLOCK": "权限阻断",
            "SOURCE_NOT_ROUTED": "来源未路由",
            "SCHEDULE_WINDOW_MISSING": "窗口缺失",
            "DATA_QUALITY_BAD": "数据质量",
            "SECTOR_GATE_REJECT": "行业拒绝",
            "PRICE_BAND_CHASE_RISK": "追高风险",
        }
        return mapping.get(reason, humanize_text(reason, 24) if reason else "-")

    @staticmethod
    def _infer_shadow_reason(strategy: str, payload: dict) -> str:
        reason = str((payload or {}).get("failure_reason_primary") or "").strip()
        if reason:
            return reason
        strategy = str(strategy or "").replace("_SHADOW", "")
        regime = str((payload or {}).get("regime_assumption") or "weak_market")
        try:
            matrix = Config.STRATEGY.get("strategy_permission_matrix", {}) if isinstance(Config.STRATEGY, dict) else {}
            rules = matrix.get(regime) or {}
            permission = str((rules or {}).get(strategy) or (rules or {}).get("*") or "")
            entry_policy = Config.STRATEGY.get("entry_policy", {}) if isinstance(Config.STRATEGY, dict) else {}
            windows = (((entry_policy.get("models") or {}).get("dynamic_window") or {}).get("strategy_windows") or {})
            real_windows = windows.get(strategy) or []
        except Exception:
            permission = ""
            real_windows = []
        if permission in ("OBSERVE", "OBSERVE_ONLY"):
            return "PERMISSION_OBSERVE_ONLY"
        if permission == "BLOCK":
            return "PERMISSION_BLOCK"
        if not real_windows:
            return "SCHEDULE_WINDOW_MISSING"
        return "SOURCE_NOT_ROUTED"

    def _recent_events(self, since: str) -> list[dict]:
        return self._query(
            """
            SELECT event_time, account, code, event_type, weather, reason, params_json
            FROM risk_event_log
            WHERE event_time >= %s
            ORDER BY event_time DESC
            LIMIT 30
            """,
            (since,),
        )

    def _position_rows(self) -> list[dict]:
        positions = self.portfolio.load_all_positions()
        if not positions:
            return []

        ts_codes = [self._format_ts_code(p.get("code")) for p in positions if p.get("code")]
        quotes = self.provider.get_realtime_quotes(ts_codes) if ts_codes else {}

        rows = []
        for p in positions:
            code = str(p.get("code") or "")
            ts_code = self._format_ts_code(code)
            quote = quotes.get(ts_code, {}) if isinstance(quotes, dict) else {}
            buy_price = self._safe_float(p.get("avg_price") or p.get("buy_price"), 0.0)
            curr_price = self._safe_float(quote.get("price") or p.get("current_price") or buy_price, 0.0)
            highest = self._effective_position_high(p, quote, curr_price, buy_price)
            pnl_pct = ((curr_price - buy_price) / buy_price) if buy_price > 0 and curr_price > 0 else 0.0
            max_pct = ((highest - buy_price) / buy_price) if buy_price > 0 else 0.0
            try:
                from core.utils import get_dynamic_stop_loss
                weather = "☀️晴天"
                stop_settings = self._exit_stop_settings(p.get("account") or "")
                dynamic_sl = get_dynamic_stop_loss(max_pct, stop_settings["stop_loss"], weather)
                dynamic_sl = max(dynamic_sl, stop_settings["stop_loss"])
                if max_pct >= stop_settings["min_profit_lock_trigger"]:
                    dynamic_sl = max(dynamic_sl, stop_settings["min_profit_lock_pct"])
            except Exception:
                dynamic_sl = Config.RISK_MANAGEMENT.get("STOP_LOSS", -0.03)

            rows.append(
                {
                    "account": p.get("account") or "",
                    "code": code,
                    "name": quote.get("name") or p.get("name") or "",
                    "quantity": int(p.get("quantity") or 0),
                    "buy_price": buy_price,
                    "curr_price": curr_price,
                    "pnl_pct": pnl_pct,
                    "dynamic_sl": dynamic_sl,
                    "entry_strategy": p.get("entry_strategy") or "",
                    "virtual": is_virtual_account(p.get("account")),
                }
            )
        return rows

    def format_markdown(self, *, date: str | None = None) -> str:
        now = datetime.now()
        trade_date = date or now.strftime("%Y-%m-%d")
        if len(str(trade_date)) == 8 and str(trade_date).isdigit():
            trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        today_start = f"{trade_date} 00:00:00"

        market_env = self.analyzer.check_market_environment(str(trade_date).replace("-", ""))
        permission = market_env.get("permission") or {}
        matrix = Config.STRATEGY.get("strategy_permission_matrix", {}) if isinstance(Config.STRATEGY, dict) else {}
        regime = market_env.get("regime") or "unknown"

        pending = self._pending_signals(trade_date)
        shadow_rows = self._shadow_signals(trade_date)
        events = self._recent_events(today_start)
        positions = self._position_rows()
        all_positions = self.portfolio.load_all_positions()

        max_total = int(Config.RISK_MANAGEMENT.get("MAX_TOTAL_POSITIONS", 5) or 5)
        lines = []
        lines.append(f"📊【风控仪表盘】{trade_date} {now.strftime('%H:%M')}")
        lines.append("")
        lines.append("## 市场状态")
        lines.append(f"- 天气: {market_env.get('weather') or '-'}")
        lines.append(f"- 市场状态: {display_regime(regime)}")
        lines.append(f"- 趋势/情绪: {humanize_text(market_env.get('trend_state') or '-')} / {humanize_text(market_env.get('sentiment_state') or '-')}")
        lines.append(f"- 风险级别: {display_risk_level(market_env.get('risk_level') or '-')}")
        if market_env.get("risk_reasons"):
            lines.append(f"- 风险原因: {'; '.join([humanize_text(x) for x in market_env.get('risk_reasons') or []])}")
        if permission:
            lines.append(
                f"- 权限: 自动买入={display_bool(permission.get('allow_auto_buy'))} "
                f"动态入场队列={display_bool(permission.get('allow_pending_entry'))} "
                f"仓位倍率={permission.get('max_position_mult')}"
            )

        lines.append("")
        lines.append("## 策略权限矩阵")
        active_matrix = matrix.get(regime, {}) if isinstance(matrix, dict) else {}
        if active_matrix:
            lines.append("| 策略 | 动作 |")
            lines.append("| :--- | :--- |")
            for k, v in active_matrix.items():
                if k == "description":
                    continue
                lines.append(f"| {k} | {display_action(v)} |")
        else:
            lines.append("暂无当前市场状态的权限矩阵。")

        lines.append("")
        lines.append("## 账户风险预算")
        lines.append("| 账户 | 持仓数 | 上限 | 状态 |")
        lines.append("| :--- | ---: | ---: | :--- |")
        for account in ("main", "watchlist", "paper_main", "paper_watchlist"):
            cnt = account_position_count(all_positions, account)
            status = "满仓" if cnt >= max_total else "可用"
            lines.append(f"| {display_account(account)} | {cnt} | {max_total} | {status} |")

        lines.append("")
        lines.append("## 动态入场队列")
        if pending:
            lines.append("| ID | 代码 | 名称 | 策略 | 检查 | 过期 | 最近原因 |")
            lines.append("| ---: | :--- | :--- | :--- | ---: | :--- | :--- |")
            for row in pending[:12]:
                reason = humanize_text(row.get("last_reason") or "", 40)
                lines.append(
                    f"| {row.get('id')} | {row.get('code')} | {row.get('name') or ''} | "
                    f"{row.get('source_strategy') or ''} | {row.get('check_count') or 0} | "
                    f"{row.get('expires_at') or '-'} | {reason or '-'} |"
                )
        else:
            lines.append("暂无等待动态入场的信号。")

        lines.append("")
        lines.append("## 影子审计")
        if shadow_rows:
            reason_counts = {}
            strategy_counts = {}
            enriched_shadow = []
            for row in shadow_rows:
                payload = self._safe_json(row.get("payload_json"))
                strategy = str(row.get("source_strategy") or "").replace("_SHADOW", "")
                reason = self._infer_shadow_reason(strategy, payload)
                reason_counts[reason or "UNKNOWN"] = reason_counts.get(reason or "UNKNOWN", 0) + 1
                strategy_counts[strategy or "-"] = strategy_counts.get(strategy or "-", 0) + 1
                enriched_shadow.append((row, payload, reason, strategy))

            lines.append(f"- 影子审计行: {len(shadow_rows)} | 不进入真实买入加载器")
            lines.append("- 原因分布: " + "；".join([f"{self._audit_reason_label(k)} {v}" for k, v in sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)]))
            lines.append("- 策略分布: " + "；".join([f"{k} {v}" for k, v in sorted(strategy_counts.items(), key=lambda kv: kv[1], reverse=True)]))
            lines.append("")
            lines.append("| 代码 | 名称 | 策略 | 审计原因 | 级别 | 动作 | 更新 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for row, payload, reason, strategy in enriched_shadow[:12]:
                lines.append(
                    f"| {row.get('code') or '-'} | {row.get('name') or '-'} | {strategy or '-'} | "
                    f"{self._audit_reason_label(reason)} | {payload.get('level') or '-'} | "
                    f"{display_action(payload.get('action') or '-')} | {row.get('updated_at') or '-'} |"
                )
        else:
            lines.append("暂无影子审计行。")

        lines.append("")
        lines.append("## 持仓止损线")
        if positions:
            lines.append("| 账户 | 代码 | 名称 | 成本 | 现价 | 盈亏% | 动态止损线 | 策略 |")
            lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |")
            for row in positions[:20]:
                lines.append(
                    f"| {display_account(row['account'])} | {row['code']} | {row['name']} | {row['buy_price']:.2f} | "
                    f"{row['curr_price']:.2f} | {row['pnl_pct']*100:+.2f}% | {row['dynamic_sl']*100:+.2f}% | "
                    f"{row['entry_strategy'] or '-'} |"
                )
        else:
            lines.append("暂无持仓。")

        event_types = {}
        for event in events:
            event_types[event.get("event_type") or "UNKNOWN"] = event_types.get(event.get("event_type") or "UNKNOWN", 0) + 1

        lines.append("")
        lines.append("## 今日风控事件")
        if event_types:
            lines.append("| 类型 | 次数 |")
            lines.append("| :--- | ---: |")
            for event_type, cnt in sorted(event_types.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"| {display_event_type(event_type)} | {cnt} |")
            lines.append("")
            lines.append("| 时间 | 账户 | 代码 | 类型 | 原因 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for event in events[:12]:
                reason = humanize_text(event.get("reason") or "", 60)
                lines.append(
                    f"| {event.get('event_time')} | {display_account(event.get('account'))} | {event.get('code') or '-'} | "
                    f"{display_event_type(event.get('event_type'))} | {reason or '-'} |"
                )
        else:
            lines.append("今日暂无风控事件。")

        return "\n".join(lines)
