# Tools

这里放人工运维、诊断和研究辅助脚本。OpenClaw 正式定时任务只依赖根目录入口和少数稳定工具。

## 稳定运维工具

- `audit_cold_start_tags.py`：冷启动标签链路审计，OpenClaw 盘后任务会用。
- `replay_day.py`：单日选股约束回放。
- `init_portfolio.py`：初始化持仓和资金，谨慎手动执行。
- `diagnostics/check_pending_entry_events.py`：检查 pending 入场复核事件、影子买入样本和未买原因，部署后验收影子链路使用。

## 子目录

- `debug/`：临时排查数据源、资金流、换手率、板块匹配等问题。
- `diagnostics/`：数据库 schema、今日入库、V19 技术审计等人工检查脚本。
- `research/`：研究辅助脚本；`research/legacy_training/` 是主项目旧训练残留，只做追溯，不作为当前训练入口。

正式部署和任务编排仍以 `docs/STRATEGY.md` 为准。
