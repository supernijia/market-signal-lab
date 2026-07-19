# OpenClaw 策略进化手册 (VNext)

本文档用于说明 OpenClaw 如何运行 `core/evolve_strategy.py`。当前版本的进化引擎默认只生成建议，不自动改写实盘配置；任何会改变自动买入行为的参数，都需要先经过 `risk_dashboard + audit + track + paper` 观察。

## 1. 核心进化逻辑

系统通过 `core/evolve_strategy.py` 对历史数据、实盘交易、门禁事件和 T+1 阻断事件进行对齐。

- **三位一体分析**:
  - **选股端**: `strategy_selection` (统计哪些标的“看准了”)。
  - **交易端**: `transactions` (统计哪些标的“买到了/赚到了”)。
  - **周期端**: 区分 **T+1 (盘中)** 与 **T+2 (盘后)** 两个生命周期。
- **建议输出**:
  - **一致性审计**: 如果选股胜率高但实盘亏损，提示执行磨损或买点过差。
  - **门禁建议**: 根据 `risk_event_log` 输出弱市门禁、权限矩阵、动态入场窗口建议。
  - **仓位建议**: 根据连续亏损、T+1 阻断和策略表现提示是否降仓。
  - **不自动应用**: 当前只把 `strategy_patch` 写入 `evolution_audit_log.metrics_json` 和邮件报告。

## 2. OpenClaw 部署命令

在 OpenClaw 的任务调度中，建议设置**每周六上午 10:00** 执行一次进化。下周开盘前先不要依赖进化结果自动调参，先观察正式链路。

**调度指令**:

```bash
cd <project-root>
./run_openclaw.sh -m core.evolve_strategy
```

## 3. 监控与交付指标

当任务执行完后，您会收到一份报告。请重点关注：

| 指标 | 意义 | 当前处理 |
| :--- | :--- | :--- |
| 实盘胜率 | 真实交易的最终战果 | 只作为建议依据 |
| 选股胜率 | 策略逻辑的理论上限 | 与实盘成交对齐分析 |
| 门禁拦截后表现 | 被拦截候选后来是否继续下跌 | 判断门禁是否过严 |
| T+1 阻断事件 | 当日买入触发止损但不可卖 | 判断是否要压低早盘/高开策略权限 |
| strategy_patch | 建议修改项 | 写入审计日志，不自动应用 |

## 4. 人工干预与配置

1. **配置路径**: `/config/strategy_config.json`。当前进化引擎不应自动改写该文件。
2. **日志路径**: `/logs/evolution-YYYYMMDD.log` (记录了所有参数跳变的因果关系)。
3. **数据备份**: `/data/evolution_history.json` (保存了胜率的历史曲线)。
4. **建议落库**: `evolution_audit_log.metrics_json.strategy_patch`。

### 4.1 进攻窗口观测（本金安全）
系统新增“时间段×天气”的观测统计，用于决定是否要开启自动买入的进攻窗口门禁。

- 产出方式：每日运行 `main.py --mode track`
- 报告中新增段落：`⏱️【进攻窗口验证(时间段×天气)】(观测，不改交易)`
- 观测周期与启用顺序：已合并进 `docs/STRATEGY.md`，旧稿归档于 `docs/archive/2026-05-31/OBSERVATION_CYCLE.md`
- 门禁开关（默认关闭）：`strategy_config.json` → `attack_window_gate.enabled`


### 4.2 进化前必须先看

```bash
./run_openclaw.sh main.py --mode risk_dashboard
./run_openclaw.sh main.py --mode audit
./run_openclaw.sh main.py --mode track
```

如果这三份报告中股票名称、策略、模型、标签为空，或者 Tushare 数据明显延迟，不要采用进化建议。

## 5. 给 OpenClaw 的部署指令

> 请执行以下任务：
>
> 1. 每周六 10:00 运行一次 `./run_openclaw.sh -m core.evolve_strategy`。
> 2. 只发送进化建议报告，不自动应用 `strategy_patch`。
> 3. 重点监控门禁建议、T+1 阻断、paper 账户表现和策略权限矩阵建议。
> 4. 如果 `T1_BLOCKED_SELL_SIGNAL` 或 `BUY_BLOCKED_PRE_TRADE_GATE` 异常增多，先发通知，不要自动放开买入权限。
