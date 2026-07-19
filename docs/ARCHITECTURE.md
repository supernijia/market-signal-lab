# Market Signal Lab 整体框架与设计决策

> 目标：给接手开发、排查问题、评估新策略是否能上线的人一个总览。
> 运行命令和部署细节仍以 [STRATEGY.md](STRATEGY.md) 为准。

---

## 1. 系统定位

`market-signal-lab` 不是单纯的选股脚本，而是一个围绕 A 股自动化选股、动态入场、持仓风控、审计追踪和策略进化构建的轻量交易系统。

当前系统的核心判断是：

```text
先判断市场状态，
再决定策略权限，
再做入场确认和仓位控制，
最后才允许执行买卖。
```

因此，项目的重点不只是“找到强票”，而是把每次选股、拦截、买入、卖出、追踪和进化都留下可审计证据。

---

## 2. 分层框架

```text
调度层
  OpenClaw / cron / run_openclaw.sh
    ↓
入口层
  main.py                         选股、报告、自动买入、审计、追踪、回放
  monitor.py                      持仓哨兵、动态入场哨兵
    ↓
数据层
  core/data_provider.py           Tushare、Sina/Tencent fallback、缓存、数据质量标记
  core/data_cache.py              Redis 缓存
    ↓
信号层
  core/analyzer.py                集合竞价、午盘、盘后、备选池、冷启动、个股诊断
  core/tech_analyzer.py           技术指标
  core/cold_start_model.py        冷启动观察模型
  core/combo_signal_adapter.py    训练侧组合信号适配
    ↓
风控与执行决策层
  core/pre_trade_gate.py          策略权限、弱市门禁、数据质量门禁、风险标签
  core/entry_flow.py              main/monitor 共用的入场确认编排
  core/entry_confirm.py           VWAP、量比、开盘区间、回落结构确认
  core/sector_rotation.py         行业轮动和强行业过滤
  core/position_sizer.py          仓位预算
    ↓
组合与执行层
  core/portfolio.py               持仓、现金、交易流水、选股入库、pending、risk events
  core/paper_account.py           paper 影子账户镜像
    ↓
报告、审计、进化层
  core/reporter.py                邮件和文本报告
  core/risk_dashboard.py          风控仪表盘
  core/trade_auditor.py           交易质量审计
  core/strategy_tracker.py        T+1/T+2 追踪、时间桶和天气统计
  core/evolve_strategy.py         进化建议
  core/backtester.py              A 股约束回放
```

---

## 3. 主流程

### 3.1 选股到买入

```text
OpenClaw 定时任务
  ↓
run_openclaw.sh 加载 .env.openclaw
  ↓
main.py --mode pre_market / afternoon / post_market / watchlist
  ↓
StockAnalyzer 生成候选
  ↓
PortfolioManager.save_selection 写入 strategy_selection
  ↓
如果既不带 --auto-trade，也不带 --queue-entry：
  AgentTurn cron 到这里结束：入库 / 报告 / SHADOW / 审计
  注意：不会创建 status=PENDING 的可执行动态入场信号

如果带 --queue-entry：
  pre-trade gate / 权限矩阵 / 数据质量门禁
    ↓
  允许动态确认：写入 pending_entry_signals(status=PENDING)
  BLOCK / OBSERVE / risk_level=high：只写风险事件和审计
    ↓
  直接返回，不加载 pending，不执行 execute_buy
```

完整执行分支由 --auto-trade 进入；AgentTurn cron 不使用：

```text
执行候选
  ↓
  市场天气 / 策略权限矩阵 / 弱市门禁 / 数据质量门禁
    ↓
  AUTO：进入仓位计算和 execute_buy
  CONFIRM / LOW_SIZE_CONFIRM：写入 pending_entry_signals 等待动态确认
  OBSERVE：只观察和审计
  BLOCK：拦截并写 risk_event_log
```

### 3.2 动态入场

```text
pending_entry_signals
  ↓
monitor.py 哨兵轮询
  ↓
重新获取 market_env，并执行 attack_window_gate / pre-trade gate / 仓位调整
  ↓
BLOCK 或 OBSERVE：CANCELLED；确认不足：保留 PENDING 等下次重试
  ↓
core/entry_flow.py
  ↓
core/entry_confirm.py 复核实时行情、VWAP、量比、开盘区间、冲高回落
  ↓
通过后 execute_buy，失败则记录重试和原因，过期则 EXPIRED
```

这条链路的意义是把“固定时刻买入”改成“候选先入池，窗口内确认后再入场”。

### 3.3 持仓风控到卖出

```text
monitor.py
  ↓
读取 positions
  ↓
刷新市场天气和实时价格
  ↓
检查 T+1、止损、移动止盈、分段止盈、持仓过久、rescue 账户豁免
  ↓
execute_sell 或发送提醒
  ↓
risk_event_log / transactions 留痕
```

### 3.4 审计与进化

```text
track                 追踪 T+1/T+2 选股表现
audit                 审计真实交易、来源、买入时段、风险门禁、标签表现
risk_dashboard        看 pending、持仓、权限矩阵、风险事件
evolve_strategy       生成策略权限、门禁和窗口建议，默认不直接改实盘行为
```

---

## 4. 关键数据表与文件

| 类型 | 位置 | 作用 |
| :--- | :--- | :--- |
| 策略配置 | `config/strategy_config.json` | 权限矩阵、门禁、入场策略、仓位、paper、天气风控 |
| 环境变量 | `.env.openclaw` | 数据库、Redis、邮箱、Tushare 等敏感配置 |
| 选股记录 | `strategy_selection` | 候选、策略、标签、周期、入库价，用于 T+1/T+2 追踪 |
| 待确认入场 | `pending_entry_signals` | 动态入场池，区分 PENDING、BOUGHT、EXPIRED、SHADOW |
| 持仓 | `positions` | 真实账户、paper 账户、rescue 账户持仓 |
| 交易流水 | `transactions` | 买卖成交、来源策略、标签、天气、快照 |
| 风险事件 | `risk_event_log` | 买入拦截、T+1 阻断、止损止盈、门禁原因 |
| 进化审计 | `evolution_audit_log` | 进化建议和指标，不作为自动上线依据 |
| 宏观缓存 | `data/macro_cache.json` | 盘前宏观结果，给后续选股加权 |
| 日志 | `logs/stock_analyzer-YYYYMMDD.log` | 每日运行日志和邮件快照 |

表结构由 `PortfolioManager.init_tables()` 在启动时做加法迁移。

---

## 5. 重要设计决策

### 5.1 OpenClaw 正式任务统一走 `run_openclaw.sh`

正式调度不要直接跑 `python main.py ...` 或 `./venv/bin/python ...`。
`run_openclaw.sh` 负责加载 `.env.openclaw`，避免任务连错数据库、缺邮箱凭据或缺 Tushare Token。

### 5.2 `main.py` 和 `monitor.py` 分工

`main.py` 是批处理入口，负责选股、报告、入库、自动买入、审计、追踪和回放。
`monitor.py` 是哨兵入口，负责盘中循环检查持仓卖出风险，也负责 pending 动态入场。

共享入场逻辑放在 `core/entry_flow.py`，避免 main 和 monitor 各自实现一套不同标准。

### 5.3 自动买入权限是系统核心

策略信号不能直接等价于买入。买入前必须经过：

```text
market regime / weather
strategy_permission_matrix
weak_market_entry_gate
data_quality_gate
sector_rotation
entry_confirm
position_sizer
```

这样做是为了处理 A 股里最伤本金的组合风险：弱市、早盘、追高、高开接力、样本不足、T+1 当日不可卖。

### 5.4 pending 动态入场优先于固定时刻追入

集合竞价、午盘等模式可以先把候选放入 `pending_entry_signals`，由后续窗口多次确认。
这比在 09:26 或 14:30 看到信号就立刻买更稳，也能把失败原因记录下来供审计。

### 5.5 新规则先观察，再进入真实权限

新策略、新标签、新门禁默认应走观察链路：

```text
SHADOW / OBSERVE
  ↓
paper 影子账户
  ↓
track + audit
  ↓
样本足够后再调整权限矩阵
```

`evolve_strategy` 当前定位是生成建议和审计材料，不应未经人工确认就扩大真实自动买入权限。

### 5.6 数据质量是门禁的一部分

实时行情、昨收、成交额、量比、fallback 来源都会影响自动买入动作。
价格/昨收缺失应阻断自动买入；量额缺失或实时 fallback 更适合走确认链路。

### 5.7 所有影响交易的判断都要可追溯

候选标签、冷启动标签、风险门禁、入场确认结果和交易来源要尽量写入：

```text
strategy_selection.tags_json
pending_entry_signals.payload_json / tags_json
positions.signal_tags_json
transactions.signal_tags_json
risk_event_log.params_json
```

这样后续才能区分：

```text
选股错了
入场时机错了
市场状态不适合
门禁太松或太紧
卖出风控执行不好
```

---

## 6. 文档分工

| 文档 | 用途 |
| :--- | :--- |
| [README.md](../README.md) | 项目入口、目录和常用命令 |
| [docs/README.md](README.md) | 文档导航 |
| [docs/STRATEGY.md](STRATEGY.md) | OpenClaw 部署和日常运行 Runbook |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | 整体框架、模块分层、关键设计决策 |
| [docs/archive/2026-05-29/EVOLUTION_BLUEPRINT.md](archive/2026-05-29/EVOLUTION_BLUEPRINT.md) | 历史系统进化蓝图和决策来源 |
| [docs/archive/2026-05-29/EVOLUTION_GUIDE.md](archive/2026-05-29/EVOLUTION_GUIDE.md) | 进化引擎的历史设计说明 |

新内容放置建议：

```text
部署命令、任务课表、开盘检查       → docs/STRATEGY.md
模块分层、主流程、系统边界         → docs/ARCHITECTURE.md
阶段性研究、长篇方案、外部调研     → docs/archive/YYYY-MM-DD/
工具脚本说明                       → tools/README.md
```
