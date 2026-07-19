# Market Signal Lab Docs

这目录只保留少量常用入口，避免部署和复盘时不知道该看哪份。

## 该看哪份

| 文档 | 给谁看 | 什么时候看 |
| :--- | :--- | :--- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 开发 / 接手维护 | 了解整体框架、模块分层、主流程和关键设计决策 |
| [STRATEGY.md](STRATEGY.md) | OpenClaw / 部署执行 | 配置任务、部署、开盘前检查、排查正式环境运行问题 |
| [HEARTBEAT.md](HEARTBEAT.md) | cron / 健康检查 | 调度系统探活文件，不作为人工阅读入口 |

## 归档文档

历史研究、蓝图和阶段性说明放在归档目录：

- [archive/2026-05-29](archive/2026-05-29/)
- [archive/2026-05-31](archive/2026-05-31/)

这些文档用于追溯“为什么这么改”，不是日常部署入口：

- [GitHub 开源量化项目调研](archive/2026-05-29/GITHUB_QUANT_STRATEGY_RESEARCH.md)
- [系统进化蓝图](archive/2026-05-29/EVOLUTION_BLUEPRINT.md)
- [策略进化手册](archive/2026-05-29/EVOLUTION_GUIDE.md)
- [个股深度体检分析手册](archive/2026-05-29/ANALYZE.md)
- [阶段进度与开盘前检查](archive/2026-05-31/CURRENT_STATUS.md)
- [冷启动观察模型接入记录](archive/2026-05-31/COLD_START_OBSERVE_MODEL_2026-05-30.md)

## 当前约定

- `STRATEGY.md` 是 OpenClaw 唯一部署 Runbook。
- `ARCHITECTURE.md` 是当前整体框架和设计决策入口；不要把部署课表和敏感配置样例搬进去。
- 2026-06-28 这轮周末自进化的结论，已经写进 `STRATEGY.md` 和 `archive/2026-05-31/CURRENT_STATUS.md`；当前分层是“盘后资金流可上实盘确认、集合竞价留在影子采样、弱市门槛收紧到 2.5”。
- 训练研究文档不放在本仓库；当前入口在相邻仓库 `../optional-training-workspace/training/docs/README.md`，最新状态见 `../optional-training-workspace/training/docs/project/CURRENT_TRAINING_STATUS.md`。
- 新的长研究文档先放入 `archive/YYYY-MM-DD/`，避免污染日常入口。
- 正式部署命令统一通过项目根目录的 `./run_openclaw.sh ...` 执行，确保 `.env.openclaw` 被加载。
