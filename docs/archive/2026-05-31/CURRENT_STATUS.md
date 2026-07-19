# Market Signal Lab 进化进度与下周开盘检查清单

> 更新时间：2026-06-28 GMT+8
> 目的：记录主项目周末自进化后的当前口径，区分哪些链路已可上实盘确认、哪些仍只适合影子采样，以及下周开盘前后必须执行的检查项。
> 关联文档：
> - `docs/archive/2026-05-29/GITHUB_QUANT_STRATEGY_RESEARCH.md`
> - `docs/archive/2026-05-29/EVOLUTION_BLUEPRINT.md`

---

## 0. 当前总状态

截至 2026-06-28，主项目仍在持续迭代，但这轮周末训练已经明确分层：

已完成范围：

```text
实盘可上：盘后资金流确认链路
影子可上：集合竞价进入 shadow_pending / audit_only
弱市门槛：weak_max_open_change 与 weak_max_change 收紧到 2.5
```

当前核心变化：

```text
弱市/暴雨下不再乐观开仓
集合竞价在主链路里仍走 pending/确认链路，影子链路单独补样本
main.py / monitor.py 通过 core/entry_flow.py 共享入场验证编排
T+1 阻止止损会事件化并可通知
数据质量、行业轮动、仓位预算进入买入链路
真实约束 replay 可复盘当日策略伤害
paper_main / paper_watchlist 影子账户已接入
risk_dashboard 风控仪表盘已可运行
盘后资金流从纯观察提升为实盘确认链路
shadow_pending 已把集合竞价纳入 audit_only 影子采样
```

当前验证结果：

```text
python -m py_compile 通过
config/strategy_config.json JSON 校验通过
git diff --check 通过
main.py --mode risk_dashboard --no-email 通过
monitor.py --once --dry-run --no-email --force 通过
main.py --mode replay_day --date 20260529 --strategy 集合竞价 --no-email 通过
```

2026-06-28 的关键结论：

```text
盘后资金流可以进入 CONFIRM_ONLY
集合竞价继续留在影子里补样本，不直接放开自动执行
weak_market_entry_gate 已收紧到 2.5
```

## 1. 下周开盘前优先看什么

- 先确认 `strategy_config.json` 与 `docs/STRATEGY.md` 是否一致。
- 重点看 `盘后资金流` 是否真的只走确认链路。
- 重点看 `集合竞价` 是否仍只在 `shadow_pending` 里写影子样本。
- 重点看弱市门槛 2.5 是否过严，避免把有效样本也一并砍掉。

## 2. 如果还要继续扩样本

- 影子侧优先补 `集合竞价` 的可观测样本。
- 实盘侧先盯 `盘后资金流` 的确认成功率和后续表现。
- 只有当影子样本足够稳定，才考虑把新的训练结论向主链路迁移。
