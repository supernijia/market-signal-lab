# -*- coding: utf-8 -*-
"""Human-readable labels for report and email presentation.

Internal enums stay unchanged in DB/code paths; these helpers are only for
user-facing report text.
"""
from __future__ import annotations

import re
from typing import Any


ACTION_LABELS = {
    "AUTO": "自动放行",
    "LOW_SIZE_AUTO": "小仓位放行",
    "LOW_SIZE_CONFIRM": "小仓位确认",
    "CONFIRM_ONLY": "等待人工确认",
    "ALLOW_WITH_WARN": "带提醒放行",
    "PENDING": "等待动态入场",
    "OBSERVE": "只观察",
    "OBSERVE_ONLY": "只观察",
    "BLOCK": "禁止买入",
    "BLOCKED": "已拦截",
    "REJECTED": "已拒绝",
    "PAUSED": "已暂停",
    "PASS": "通过",
    "QUEUED": "已入队",
    "BUY": "买入",
    "SELL": "全仓卖出",
    "SELL_LADDER": "分批止盈卖出",
}

STATUS_LABELS = {
    "PENDING": "等待动态入场",
    "SHADOW": "影子审计",
    "BOUGHT": "已买入",
    "CANCELLED": "已取消",
    "EXPIRED": "已过期",
    "INFO": "信息",
    "BLOCKED": "已拦截",
    "PAUSED": "已暂停",
    "REJECTED": "已拒绝",
    "PASS": "通过",
    "QUEUED": "已入队",
}

EVENT_TYPE_LABELS = {
    "BUY_BLOCKED_PRE_TRADE_GATE": "买入前门禁拦截",
    "PRE_TRADE_BLOCK": "买入前拦截",
    "T1_BLOCKED_SELL_SIGNAL": "T+1卖出受限",
    "SELL_TRIGGER": "卖出信号触发",
    "SELL_EXECUTED": "卖出已执行",
    "UNKNOWN": "未知事件",
}

REGIME_LABELS = {
    "strong_uptrend": "强趋势",
    "normal_uptrend": "正常多头",
    "range_market": "震荡市",
    "weak_market": "弱市",
    "storm_market": "极端风险",
    "unknown": "未知",
}

RISK_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "unknown": "未知",
}

BUCKET_LABELS = {
    "B1": "早盘确认(09:30-10:00)",
    "B2": "上午延续(10:00-11:30)",
    "B3": "午后启动(13:00-14:00)",
    "B4": "午后确认(14:00-14:40)",
    "B5": "尾盘观察(14:40-15:01)",
}

ACCOUNT_LABELS = {
    "main": "主账户",
    "watchlist": "巡航子账户",
    "rescue": "T+0自救账户",
    "paper_main": "仿真主账户",
    "paper_watchlist": "仿真巡航账户",
}

KEY_LABELS = {
    "action": "动作",
    "status": "状态",
    "reason": "原因",
    "failure": "失败原因",
    "labels": "标签",
    "tags": "标签",
    "weather": "市场天气",
    "bucket": "时间窗",
    "regime": "市场状态",
    "risk_level": "风险级别",
    "strategy": "策略",
    "entry_policy": "入场策略",
    "enabled": "是否启用",
    "model": "模型",
    "default_model": "默认模型",
    "expires_at": "过期时间",
    "state": "状态",
    "change": "涨幅",
    "score": "分数",
    "source_strategy": "来源策略",
    "params_json": "参数字段",
    "signal_tags_json": "信号标签字段",
    "samples": "样本数",
    "win_rate": "胜率",
    "n": "样本数",
    "id": "编号",
}

TOKEN_LABELS = {
    **ACTION_LABELS,
    **STATUS_LABELS,
    **EVENT_TYPE_LABELS,
    "PERMISSION_OBSERVE_ONLY": "权限仅观察",
    "PERMISSION_BLOCK": "权限阻断",
    "SOURCE_NOT_ROUTED": "来源未接入真实入场流程",
    "SCHEDULE_WINDOW_MISSING": "缺少真实入场时间窗",
    "DATA_QUALITY_BAD": "数据质量不达标",
    "SECTOR_GATE_REJECT": "板块门禁拒绝",
    "PRICE_BAND_CHASE_RISK": "价格区间追高风险",
    "TRUE": "是",
    "FALSE": "否",
    "WEAK_MARKET": "弱市",
    "HIGH_GAP": "高开过高",
    "INSUFFICIENT_SAMPLES": "样本不足",
    "PRE_TRADE_GATE": "买入前门禁",
    "ENTRY_SCENARIO": "入场场景",
    "ENTRY_CONFIRM": "入场确认",
    "DATA_QUALITY": "数据质量",
    "PERMISSION": "权限",
    "GAP_UP_STRONG": "高开强",
    "MOMENTUM_STRONG": "动量强",
    "TURNOVER_HIGH": "换手偏高",
    "VOLUME_RATIO_OK": "量比达标",
    "SECTOR_HOT_BONUS": "热点板块加分",
    "SECTOR_LEADER_CONFIRMED": "板块龙头确认",
    "FIRST_BOARD_CANDIDATE": "首板候选",
    "CONTINUE_BOARD_CANDIDATE": "接力候选",
    "FIRST_BOARD_OPEN_OK": "首板开盘达标",
    "FIRST_LIMIT_MODEL": "首板模型",
    "BREAKOUT_CONFIRM_MODEL": "突破确认模型",
    "BREAKOUT_FALSE60_MODEL": "60分钟假突破风险模型",
    "BREAKOUT_NET60_MODEL": "60分钟净突破模型",
    "COLD_START_MODEL_GOOD": "冷启动优质模型",
    "COLD_START_MODEL_GOOD_SCORE": "冷启动优质分",
    "COLD_START_PROFIT_CAPTURE": "冷启动T0利润机会",
    "COLD_START_PROFIT_SCORE": "冷启动利润分",
    "COLD_START_RISK_HIGH": "冷启动风险高",
    "COLD_START_RISK_SCORE": "冷启动风险分",
    "COLD_START_EARLY_ABSORB": "冷启动早盘吸收",
    "COLD_START_DELAYED_CONFIRM": "冷启动延迟确认",
    "COLD_START_PULLBACK_ENTRY_WATCH": "冷启动低吸观察",
    "COLD_START_OBSERVE_SIGNAL": "冷启动观察信号",
    "COLD_START_CHAIN_DELAYED_CONFIRM": "冷启动链路延迟确认",
    "COLD_START_CHAIN_EARLY_ABSORB": "冷启动链路早盘吸收",
    "BREAKOUT_FALSE60_VETO_PASS": "假突破风险通过",
    "BREAKOUT_FALSE60_RISK_HIGH": "假突破风险高",
    "BREAKOUT_NET60_PASS": "净突破通过",
    "BREAKOUT_HIGH_QUALITY": "高质量突破",
    "FIRST_LIMIT_BREAKOUT_COMBO": "首板突破组合",
    "FIRST_LIMIT_BREAKOUT_PASS": "首板突破通过",
    "LHB_PRESENT": "龙虎榜上榜",
    "LHB_NET_BUY": "龙虎榜净买入",
    "LHB_NET_SELL": "龙虎榜净卖出",
    "CONCEPT_PRESENT": "概念覆盖",
    "CONCEPT_RESONANCE": "概念共振",
    "INTRADAY_VWAP_EXTENDED": "分时偏离VWAP过远",
    "INTRADAY_NEAR_VWAP": "分时贴近VWAP",
    "INTRADAY_VWAP_STRETCHED": "分时VWAP偏强延伸",
    "INTRADAY_BELOW_VWAP": "分时低于VWAP",
    "INTRADAY_VOLUME_BURST": "分时放量",
    "STRATEGY_REASON": "策略原因",
}

PHRASE_REPLACEMENTS = {
    "failure reason": "失败原因",
    "pending loader": "真实买入加载器",
    "real pending": "真实买入队列",
    "audit_only_shadow": "仅审计影子模式",
    "keep_observe_no_real_pending": "保持观察，不进入真实买入队列",
    "keep_block_no_real_pending": "保持阻断，不进入真实买入队列",
    "add_audit_reason_or_shadow_dry_run_window": "补充审计原因或影子试运行窗口",
    "route_to_failure_reason_audit_not_buy": "进入失败原因审计，不买入",
    "pre_trade_gate": "买入前门禁",
    "attack_window_gate": "进攻窗口门禁",
    "win_rate_gate": "胜率门禁",
    "entry_confirm": "入场确认",
    "permission matrix": "权限矩阵",
    "permission_matrix": "权限矩阵",
    "entry_policy": "入场策略",
    "入场策略.是否启用": "入场策略是否启用",
    "--queue-entry": "动态入场入队模式",
    "--auto-trade": "直接自动交易模式",
    "legacy_money_flow": "传统资金流",
    "below_ma20": "跌破MA20",
    "above_ma20": "站上MA20",
    "neutral_to_weak": "中性偏弱",
    "neutral": "中性",
    "DATA_QUALITY_BAD": "数据质量不达标",
    "数据质量BAD": "数据质量不达标",
    "signal_tags_json": "信号标签字段",
    "params_json": "参数字段",
    "data_quality": "数据质量",
    "realtime_fallback_used": "实时行情使用备用源",
    "fallback": "备用数据源",
    "price_invalid": "价格无效",
    "pre_close_missing": "昨收缺失",
    "pre_close_invalid": "昨收无效",
    "volume_or_amount_missing": "成交量/成交额缺失",
    "volume_ratio": "量比",
    "price/vwap": "价格/VWAP",
    "weather=": "市场天气=",
    "bucket=": "时间窗=",
    "win_rate=": "胜率=",
    "change=": "涨幅=",
    "score=": "分数=",
    "risk_level=": "风险级别=",
    "regime=": "市场状态=",
    "key=": "策略键=",
    "动态入场PENDING": "动态入场",
    "PENDING": "等待动态入场",
    "SHADOW": "影子审计",
    "B1": "早盘确认(09:30-10:00)",
    "B2": "上午延续(10:00-11:30)",
    "B3": "午后启动(13:00-14:00)",
    "B4": "午后确认(14:00-14:40)",
    "B5": "尾盘观察(14:40-15:01)",
    "BLOCK": "禁止买入",
    "OBSERVE": "只观察",
    "blocked": "已拦截",
    "not in": "不在允许范围",
    "max retries reached": "达到最大检查次数",
    "already holding": "已有持仓",
    "position cap reached": "仓位上限已满",
    "no realtime quote": "缺少实时行情",
    "invalid price": "价格无效",
    "cash unavailable": "现金不可用",
    "budget too small": "预算过小",
    "buy failed": "买入失败",
    "buy executed by sentinel": "哨兵已执行买入",
    "buy executed": "买入已执行",
    "dynamic_window": "动态时间窗",
    "pending retry": "等待队列重试",
    "dry-run": "试运行",
    "gate.price": "门禁记录价",
    "selection": "入库记录",
    "mode": "模式",
    "observe": "观察",
}


def _clean_key(value: Any) -> str:
    return str(value or "").strip()


def display_action(value: Any) -> str:
    raw = _clean_key(value)
    return ACTION_LABELS.get(raw.upper(), raw or "-")


def display_status(value: Any) -> str:
    raw = _clean_key(value)
    return STATUS_LABELS.get(raw.upper(), raw or "-")


def display_event_type(value: Any) -> str:
    raw = _clean_key(value)
    return EVENT_TYPE_LABELS.get(raw.upper(), humanize_text(raw) if raw else "-")


def display_regime(value: Any) -> str:
    raw = _clean_key(value)
    return REGIME_LABELS.get(raw, REGIME_LABELS.get(raw.lower(), raw or "-"))


def display_risk_level(value: Any) -> str:
    raw = _clean_key(value)
    return RISK_LEVEL_LABELS.get(raw.lower(), raw or "-")


def display_bucket(value: Any) -> str:
    raw = _clean_key(value)
    return BUCKET_LABELS.get(raw.upper(), raw or "-")


def display_account(value: Any) -> str:
    raw = _clean_key(value)
    return ACCOUNT_LABELS.get(raw, raw or "-")


def display_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    raw = _clean_key(value).lower()
    if raw in {"true", "1", "yes", "y"}:
        return "是"
    if raw in {"false", "0", "no", "n"}:
        return "否"
    return _clean_key(value) or "-"


def display_token(value: Any) -> str:
    raw = _clean_key(value)
    if not raw:
        return "-"
    if raw in REGIME_LABELS:
        return REGIME_LABELS[raw]
    if raw.lower() in REGIME_LABELS:
        return REGIME_LABELS[raw.lower()]
    if raw.lower() in RISK_LEVEL_LABELS:
        return RISK_LEVEL_LABELS[raw.lower()]
    if raw.upper() in BUCKET_LABELS:
        return BUCKET_LABELS[raw.upper()]
    return TOKEN_LABELS.get(raw.upper(), raw)


def humanize_text(value: Any, max_len: int | None = None) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "/").strip()
    if not text:
        return "-"

    def replace_pair(match: re.Match) -> str:
        key = match.group(1)
        sep = match.group(2)
        val = match.group(3)
        key_label = KEY_LABELS.get(key, key)
        val_label = display_token(val)
        return f"{key_label}{sep}{val_label}"

    text = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(=|:)\s*([^,\s/;，；]+)", replace_pair, text)

    for old, new in sorted(PHRASE_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(re.escape(old), new, text, flags=re.IGNORECASE)

    def replace_upper(match: re.Match) -> str:
        token = match.group(0)
        return TOKEN_LABELS.get(token, token)

    text = re.sub(r"\b[A-Z][A-Z0-9_]{2,}\b", replace_upper, text)

    # Clean the most visible internal suffixes after token translation.
    text = text.replace("_SHADOW", "影子审计")
    text = re.sub(r"\s+", " ", text).strip()
    if max_len is not None and len(text) > max_len:
        return text[:max_len]
    return text
