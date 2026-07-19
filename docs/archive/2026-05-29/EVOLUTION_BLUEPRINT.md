# Market Signal Lab 进化蓝图：从“信号驱动”到“市场状态驱动”的 A 股量化系统

> 生成时间：2026-05-29 11:51 GMT+8
> 归档位置：`<project-root>/docs/archive/2026-05-29/EVOLUTION_BLUEPRINT.md`
> 背景：结合 GitHub 优秀开源量化项目调研、当前 `market-signal-lab` 代码结构、今日康达新材弱市早盘自动买入案例，提出系统级进化方案。
> 目标：降低弱市/T+1/追高/接力导致的大回撤，把系统从“发现信号就买”升级为“市场状态 → 策略权限 → 入场确认 → 执行 → 审计 → 进化”的闭环。
> 注意：本文是工程与研究规划，不构成投资建议。

---

## 0. 一句话结论

当前系统已经不是玩具：它有数据、选股、持仓、风控哨兵、调度、审计、进化、邮件报告、动态 pending entry。真正的下一阶段，不是继续堆更多选股策略，而是：

```text
把“自动买入权限”做成系统核心。
```

也就是：

```text
先判断市场 regime，
再决定哪些策略允许自动买，
再决定是否需要动态窗口确认，
最后才允许 execute_buy。
```

今日康达新材暴露的核心问题是：

```text
弱市 + 早盘 + 接力/高开 + 样本不足 + T+1 当日不可卖
```

这些风险条件叠加时，旧系统仍可能让信号进入自动买入流程。我们已经开始补第一层 `weak_market_entry_gate`，但这只是安全补丁；完整进化还需要市场状态、策略矩阵、交易标签、审计归因、回测约束和进化机制一起升级。

---

## 1. 当前系统现状盘点

### 1.1 已具备的核心能力

当前 `<project-root>` 已有较完整闭环：

| 模块 | 文件/位置 | 当前能力 |
|---|---|---|
| 选股引擎 | `core/analyzer.py` | 集合竞价、午盘、盘后、备选池、冷启动、技术过滤、ADX/RS/MACD 等 |
| 自动执行 | `main.py` | 根据模式和候选执行自动买入，已有胜率门禁、资金/VWAP验证、动态窗口入口 |
| 哨兵监控 | `monitor.py` | 持仓卖出风控、T+1 检查、pending entry 动态买入、备选池修剪 |
| 组合管理 | `core/portfolio.py` | positions、transactions、strategy_selection、factor_snapshot、risk_event_log、pending_entry_signals |
| 报告 | `core/reporter.py` | 选股/交易/风险邮件报告 |
| 策略追踪 | `core/strategy_tracker.py` | T+1/T+2 结果追踪、按天气/时间窗统计 |
| 交易审计 | `core/trade_auditor.py` | 收益、持仓、来源、买入时段、转化率等审计 |
| 策略进化 | `core/evolve_strategy.py` | 根据选股/交易表现调权重和过滤器 |
| 配置 | `config/strategy_config.json` | 策略参数、风控、entry_policy、attack_window_gate、limit_up_gate、win_rate_gate |
| 调度 | crontab + docs | 13 个 OpenClaw 调度任务，全天闭环 |

### 1.2 已经不错的地方

这套系统的基础是好的：

1. **有真实交易约束意识**
   `monitor.py` 已有 T+1 检查：当日买入不卖出。

2. **有多账户/多来源结构**
   `main`、`watchlist`、`rescue` 等账户逻辑已出现，适合继续扩展风险隔离。

3. **有 factor snapshot 和 tags_json 雏形**
   这非常关键，说明未来可以做“每一笔交易为什么买、为什么亏”的结构化归因。

4. **有 pending entry 动态入场**
   `pending_entry_signals` 已经存在，这是从“固定时间点买入”升级到“窗口确认买入”的基础。

5. **有 risk_event_log**
   可以记录开仓拦截、卖出触发、T+1 阻止止损等事件，未来审计可以直接按事件类型统计。

6. **有策略追踪和进化脚本**
   已经有自我改良机制，只是目前还偏向调权重，缺少失败模式学习。

### 1.3 当前最大短板

| 短板 | 表现 | 后果 |
|---|---|---|
| 市场状态不够结构化 | 现在主要是 `weather/is_safe/risk_level/message` | 无法形成稳定策略权限矩阵 |
| 开仓门禁不够系统化 | 旧逻辑散在 win_rate、VWAP、limit_up、attack_window | 多条件叠加风险不容易统一拦截 |
| T+1 风险没有反哺买入 | 当天不能卖，但买入前没有充分惩罚高波动早盘追高 | 一旦错买，当天只能扛 |
| 样本不足默认偏乐观 | `samples < 30` 可放行 | 新/稀疏策略在弱市更容易误买 |
| 动态入场未成为默认主路径 | 09:26 信号仍可能直接买 | 早盘冲高回落风险大 |
| 审计还缺失败模式标签 | 有收益审计，但缺弱市/高开/T+1阻止等归因 | 进化脚本不知道“为什么亏” |
| 回测约束不够真实 | 实盘有 T+1，但回测/追踪需要更多约束 | 策略胜率容易高估 |

---

## 2. GitHub 优秀项目给出的启发

### 2.1 Microsoft Qlib：完整投资链路，而不是单点策略

调研要点：Qlib 明确覆盖完整 ML 投资链路：

```text
data processing → model training → backtesting → alpha seeking → risk modeling → portfolio optimization → order execution
```

并强调：

- 数据是基础设施。
- 市场建模后才生成交易决策。
- 多策略/多执行器可以嵌套优化。
- 最后要有综合分析与在线服务。

#### 对当前系统的启发

当前 `market-signal-lab` 已经有很多“链路节点”，但链路之间还不够清晰。建议未来把系统明确拆成：

```text
Data Layer
  ↓
Feature / Factor Layer
  ↓
Signal Layer
  ↓
Regime / Permission Layer
  ↓
Portfolio / Position Sizing Layer
  ↓
Execution Layer
  ↓
Audit / Evolution Layer
```

尤其要补足 Qlib 风格里的：

- `risk modeling`
- `portfolio optimization`
- `order execution policy`

当前系统最弱的是 `risk modeling` 和 `portfolio optimization`，不是选股信号数量。

---

### 2.2 VN.PY：前端风控、算法交易、组合策略要分层

VN.PY 的可借鉴点：

- `cta_strategy`：策略引擎。
- `cta_backtester`：策略回测与参数优化。
- `spread_trading`：价差交易。
- `option_master`：希腊值风险跟踪。
- `portfolio_strategy`：多合约组合策略。
- `algo_trading`：TWAP、Sniper、Iceberg、BestLimit 等算法交易。
- `paper_account`：本地仿真交易。
- `risk_manager`：交易流控、下单数量、委托限制、撤单限制等前端风控。

#### 对当前系统的启发

我们的系统目前把不少东西放在 `main.py` 和 `monitor.py`，容易变成“策略、风控、执行揉在一起”。下一阶段应该学习 VN.PY，把核心控制拆出来：

| VN.PY 思想 | market-signal-lab 对应进化 |
|---|---|
| risk_manager 前端风控 | `core/pre_trade_gate.py` + `core/risk_manager.py` |
| algo_trading 算法交易 | `core/execution_policy.py`：立即买、VWAP回踩买、TWAP买、尾盘买 |
| paper_account 仿真 | `paper` 账户/仿真模式，所有新规则先 paper-run |
| portfolio_strategy | 多策略统一仓位预算，而不是每个策略自己买 |
| cta_backtester | A 股真实约束回测：T+1、涨跌停、滑点、容量 |

---

### 2.3 Freqtrade：工具链完整，重视回测、参数优化和偏差检测

Freqtrade 的命令体系很值得学习：

```text
backtesting
backtesting-analysis
hyperopt
lookahead-analysis
recursive-analysis
plot-profit
show-trades
```

#### 对当前系统的启发

我们也应该形成自己的 CLI 研究工具链：

```bash
python main.py --mode backtest --strategy pre_market --from 2026-01-01 --to 2026-05-29 --t1 --limit-up-down
python main.py --mode audit-pattern --tag WEAK_MARKET_MORNING_BLOCK
python main.py --mode hyperopt-gate --gate weak_market_entry_gate
python main.py --mode lookahead-check --strategy all
python main.py --mode replay-day --date 20260529 --dry-run
```

其中最重要的是：

1. **lookahead 检查**：确保策略没用未来数据。
2. **recursive 分析**：确保指标随数据增量稳定，不因为补全历史而改变当时判断。
3. **backtesting-analysis**：不是只看收益，而是看失败模式。

---

### 2.4 KHunter：A 股选股系统要有配置化策略顺序、权重和五维评分

KHunter 的 README 显示它是 A 股量化选股解决方案，包含：

- 数据获取
- 策略分析
- 选股执行
- 五维评分
- 选股择时
- 策略回测
- 策略参数配置 `strategy_params.yaml`
- 策略顺序配置 `strategy_order.yaml`
- 策略权重配置 `strategy_weights.json`

#### 对当前系统的启发

我们现在有 `strategy_config.json`，但还可以更进一步：

```text
strategy_params.json   单策略参数
strategy_order.json    每个 regime 下策略运行顺序
strategy_weights.json  每个 regime/time_bucket 下策略权重
strategy_permission.json 每个策略自动买权限
```

尤其是策略顺序和策略权限矩阵：

```json
{
  "weak_market": {
    "pre_market": "observe_only",
    "watchlist": "confirm_only",
    "afternoon": "low_size_confirm",
    "post_market": "observe_only"
  },
  "strong_uptrend": {
    "pre_market": "auto_allowed",
    "watchlist": "auto_allowed",
    "afternoon": "auto_allowed"
  }
}
```

---

### 2.5 quant-trade：A 股必须显式处理 T+1 与涨跌停

quant-trade 的调研里明确出现：

```text
A 股回测（T+1 + 涨跌停）
T+1 拒绝 2 笔 | 涨停无法买入 1 笔
```

还包含：

- 行业轮动 + 龙头筛选
- 业绩预告/快报扫雷
- 券商研报跟踪
- 估值/财务/成长数据

#### 对当前系统的启发

A 股系统如果不把 T+1、涨跌停、流动性、基本面扫雷纳入回测和实盘，胜率会被高估。

当前需要补的关键指标：

| 指标 | 意义 |
|---|---|
| `t1_blocked_stop_loss` | 买入当日触发止损但 T+1 阻止卖出 |
| `limit_up_unfillable` | 涨停买不到/排队不可成交 |
| `limit_down_unsellable` | 跌停卖不出 |
| `intraday_max_drawdown_t0` | 买入当日最大浮亏 |
| `next_open_gap_after_t1_block` | T+1 后次日开盘跳空 |
| `weak_market_entry` | 弱市开仓 |
| `high_gap_entry` | 高开追入 |

---

### 2.6 TqSdk 策略集：策略库要从“信号”升级到“场景”

TqSdk 策略集合里能看到大量可借鉴的场景策略：

| 策略 | 核心逻辑 | 对当前系统的启发 |
|---|---|---|
| VWAP Breakout Volume | 价格突破 VWAP + 成交量放大 | 替代简单 MA5 突破，减少假突破 |
| Adaptive Volatility Breakout | 波动率越高仓位越低，止损止盈随波动调整 | ATR 不只用于止损，也用于开仓仓位 |
| Dual Thrust | 开盘价 ± Range 动态轨道 | 可用于早盘突破，但必须结合 A 股 T+1 风险 |
| Opening Range Breakout | 开盘前 30 分钟高低点作为突破区间 | 适合把 09:26 改为 09:30-10:00 观察后突破 |
| Donchian Channel | N 日高低点通道突破 | 可替代部分追高式突破 |
| ATR Channel Breakout | ATR 动态通道确认趋势 | 可过滤噪声突破 |
| Statistical Arbitrage | 协整 + Z-Score | 未来可做低风险观察，不适合当前主线 |

#### 对当前系统的启发

我们的策略名字多，但“入场场景”还不够精细。建议新增 `entry_scenario`：

```text
auction_relay        竞价接力
vwap_breakout        VWAP突破
opening_range_break  开盘区间突破
pullback_confirm     回踩确认
afternoon_flow       午后资金确认
post_market_prepare  盘后弹药
```

然后策略评价不再只按 `strategy`，还要按 `entry_scenario`。

---

## 3. 当前系统应保留、强化、废弃什么

### 3.1 应保留

| 能力 | 原因 |
|---|---|
| 13 个 OpenClaw 调度任务 | 已形成完整日内闭环 |
| `strategy_selection` | 是选股追踪基础 |
| `factor_snapshot` | 是未来归因/ML 的基础 |
| `pending_entry_signals` | 是动态窗口入场的基础 |
| `risk_event_log` | 是失败模式审计的基础 |
| `weather_risk` | 已有市场状态雏形 |
| `win_rate_gate` | 历史胜率门禁应继续保留 |
| `limit_up_gate` | A 股涨停成交风险必须保留 |
| `verify_money_flow` | VWAP/量比/追高过滤仍然必要 |
| `monitor.py` T+1 检查 | 实盘安全底线 |

### 3.2 应强化

| 模块 | 强化方向 |
|---|---|
| `check_market_environment` | 输出结构化 `regime`、`permissions`、`position_budget` |
| `pre_trade_gate` | 从弱市补丁升级为统一开仓权限引擎 |
| `entry_policy.dynamic_window` | 早盘默认 pending，而不是固定时点直接买 |
| `trade_auditor` | 增加失败模式归因：弱市、高开、T+1阻止、VWAP偏离 |
| `strategy_tracker` | 按 regime/time_bucket/entry_scenario 统计胜率 |
| `evolve_strategy` | 从调权重升级为调权限、调窗口、调仓位 |
| `factor_snapshot` | 增加入场时刻的分时结构和风险标签 |
| `portfolio` | 增加每日/每策略风险预算 |

### 3.3 应废弃或降级

| 旧思路 | 问题 | 建议 |
|---|---|---|
| 弱市仍自动早盘买 | T+1 风险极高 | 弱市早盘只观察 |
| 样本不足默认放行 | 容易被新策略误导 | 弱市默认拦截，强市可小仓试错 |
| 固定 09:26 直接买 | 容易买在冲高点 | 改为 pending + 09:30-10:00 确认 |
| 单股票信号独立判断 | 忽略行业/市场环境 | 加行业轮动与市场状态 |
| 只看胜率 | 胜率高但单次亏损大仍危险 | 加期望收益、最大回撤、T0最大浮亏 |
| 进化只调权重 | 无法解决结构性失败 | 进化要能修改权限和窗口 |

---

## 4. 目标架构：六层防线

未来自动买入应经过六层：

```text
L0 数据质量门禁
L1 市场 Regime 门禁
L2 策略权限门禁
L3 候选质量门禁
L4 入场确认门禁
L5 仓位预算门禁
L6 执行成交门禁
```

### L0 数据质量门禁

检查：

- 实时行情是否正常。
- Tushare/替代数据是否缺失或滞后。
- 当日 daily 是否为空/零值。
- 分钟线是否可用。
- 数据源是否 fallback。

输出：

```text
GOOD / DEGRADED / BAD
```

策略：

- `BAD`：禁止自动买。
- `DEGRADED`：只允许低仓或观察。
- `GOOD`：正常。

---

### L1 市场 Regime 门禁

建议新增结构：

```json
{
  "regime": "weak_market",
  "trend": "down_below_ma20",
  "sentiment": "cooling",
  "risk_level": "medium",
  "allow_auto_buy": false,
  "max_total_position_pct": 0.0,
  "allowed_entry_models": ["observe_only"],
  "reason": ["上证跌破MA20", "创业板弱势"]
}
```

Regime 建议：

| regime | 条件示例 | 自动买权限 |
|---|---|---|
| `strong_uptrend` | 双指数 MA20 上方，涨停多，成交额放大 | 可自动买 |
| `normal_uptrend` | 指数 MA20 上方，情绪正常 | 可自动买但限追高 |
| `range_market` | 指数 MA20 附近震荡 | 只允许确认型买入 |
| `weak_market` | 任一核心指数跌破 MA20 或情绪转弱 | 早盘禁止，午后低仓确认 |
| `storm_market` | 暴跌/跌停潮/极端风险 | 禁止新开仓 |

---

### L2 策略权限门禁

建议配置：

```json
"strategy_permission_matrix": {
  "strong_uptrend": {
    "集合竞价": "AUTO",
    "龙头跟踪": "AUTO",
    "技术突破": "AUTO",
    "午盘精选": "AUTO",
    "盘后资金流": "OBSERVE"
  },
  "weak_market": {
    "集合竞价": "BLOCK",
    "冷启动": "BLOCK",
    "龙头跟踪": "OBSERVE",
    "技术突破": "CONFIRM_ONLY",
    "午盘精选": "LOW_SIZE_CONFIRM",
    "盘后资金流": "OBSERVE"
  },
  "storm_market": {
    "*": "BLOCK"
  }
}
```

动作含义：

| 动作 | 含义 |
|---|---|
| `AUTO` | 可自动买 |
| `LOW_SIZE_AUTO` | 可自动买但仓位降低 |
| `CONFIRM_ONLY` | 必须 pending + 二次确认 |
| `OBSERVE` | 只入库/报告，不买 |
| `BLOCK` | 不买也不进入 pending |

---

### L3 候选质量门禁

候选必须有质量评分，而不只是策略评分。

建议增加 `candidate_quality_score`：

| 维度 | 指标 |
|---|---|
| 资金 | 主力净流入、超大单、行业资金强度 |
| 分时结构 | VWAP、回踩、冲高回落、距日内高点 |
| 波动 | ATR/price、开盘 range、是否高波动 |
| 趋势 | MA5/MA20、ADX、RS vs 指数 |
| 交易可行性 | 是否涨停附近、量能、成交额 |
| 历史表现 | 策略+场景+regime 胜率和最大回撤 |

候选质量不足时：

```text
AUTO → CONFIRM_ONLY
CONFIRM_ONLY → OBSERVE
OBSERVE → BLOCK
```

---

### L4 入场确认门禁

把“买点”从固定时间点变成场景确认：

| 场景 | 确认条件 |
|---|---|
| VWAP 突破 | price > VWAP，price/VWAP < 1.02，量比 > 2，资金流正 |
| 开盘区间突破 | 09:30-10:00 高点突破，但不能距高点过近追入 |
| 回踩确认 | 高开后回踩 VWAP/MA5 不破，再次放量上穿 |
| 午后资金确认 | 13:00 后行业资金回流，个股站上 VWAP |
| 尾盘潜伏 | 14:30 后趋势保持，不能冲高回落 |

当前 `pending_entry_signals` 已经支持这个方向，只要把 09:26 固定买入转为 pending，即可大幅降低早盘冲高回落。

---

### L5 仓位预算门禁

仓位不应只由账户现金和固定比例决定，而应由风险预算决定。

建议公式：

```text
position_pct = base_pct
             × market_regime_multiplier
             × strategy_confidence_multiplier
             × volatility_multiplier
             × drawdown_multiplier
```

示例：

| 条件 | multiplier |
|---|---:|
| strong_uptrend | 1.0 |
| normal_uptrend | 0.8 |
| range_market | 0.5 |
| weak_market | 0.0 / 0.2 |
| storm_market | 0.0 |
| ATR/price > 8% | 0.0 |
| ATR/price 5%-8% | 0.4 |
| win_samples < 30 | 0.0 in weak / 0.3 in strong |

---

### L6 执行成交门禁

在真正 `execute_buy` 前再检查：

- 是否已经持仓。
- 是否超过最大持仓数。
- 是否涨停/临停/无成交。
- 买入量是否满足 100 股。
- 当前价是否相对计划价偏离过大。
- 当前资金是否足够。
- 最近 N 分钟是否已下过同标的订单。

这层是最后保险。

---

## 5. 详细进化路线图

### Phase 0：已经完成/正在完成的安全补丁

状态：部分已落地。

已新增/调整：

- `core/pre_trade_gate.py`
- `config.strategy_config.json -> weak_market_entry_gate`
- `main.py` 自动买入前接入门禁
- `monitor.py` pending entry 动态买入前接入门禁

当前门禁覆盖：

```text
弱市 + 10:00前早盘策略 → BLOCK
弱市 + 样本不足 → BLOCK
弱市 + 接力/高开/昨日涨停标签 → BLOCK
弱市 + 高开/当前涨幅 > 3% → BLOCK
```

后续要做：

1. 清理配置命名，长期只保留 `weak_market_entry_gate` 或升级为 `pre_trade_gate`，不要双名并存。
2. 给 `weak_market_entry_gate` 加 `mode: observe_only/block`，让不同策略可配置动作。
3. 在审计报告里统计 `BUY_BLOCKED_PRE_TRADE_GATE`。

---

### Phase 1：市场 Regime 结构化

#### 目标

把现在的：

```json
{
  "weather": "☁️多云",
  "is_safe": false,
  "risk_level": "medium",
  "message": "市场转弱: 上证指数跌破MA20"
}
```

升级为：

```json
{
  "weather": "☁️多云",
  "regime": "weak_market",
  "trend_state": "below_ma20",
  "sentiment_state": "neutral_to_weak",
  "permission": {
    "allow_auto_buy": false,
    "allow_pending_entry": true,
    "max_position_mult": 0.2,
    "blocked_strategies": ["集合竞价", "冷启动"],
    "confirm_only_strategies": ["技术突破", "午盘精选"]
  },
  "risk_reasons": ["上证跌破MA20"]
}
```

#### 代码位置

- `core/analyzer.py -> check_market_environment`
- `core/portfolio.py -> save_market_sentiment`
- `core/reporter.py -> 市场环境展示`

#### 需要新增字段

`market_sentiment_daily` 可扩展：

- `regime`
- `trend_state`
- `sentiment_state`
- `permission_json`
- `risk_reasons_json`

#### 验证方式

- 用最近 60 个交易日回放，检查 regime 是否稳定。
- 人工抽样 10 天，确认分类合理。

---

### Phase 2：策略权限矩阵

#### 目标

每个策略不再自己决定是否买，而是由统一权限矩阵决定：

```text
market_regime × strategy × time_bucket → action
```

#### 配置示例

```json
"strategy_permission_matrix": {
  "weak_market": {
    "B0_auction": {
      "集合竞价": "BLOCK",
      "冷启动": "BLOCK"
    },
    "B1_0930_1000": {
      "集合竞价": "OBSERVE",
      "技术突破": "CONFIRM_ONLY"
    },
    "B4_1400_1440": {
      "午盘精选": "LOW_SIZE_CONFIRM"
    }
  }
}
```

#### 代码位置

建议新增：

- `core/strategy_permission.py`

接入：

- `main.py`
- `monitor.py`
- `core/pre_trade_gate.py`

#### 动作定义

| action | 处理 |
|---|---|
| `BLOCK` | 不进入 pending，不买 |
| `OBSERVE` | 入库/报告，不进入买入 |
| `PENDING` | 写入 pending，等待确认 |
| `CONFIRM_ONLY` | 只有确认条件满足才买 |
| `LOW_SIZE_AUTO` | 允许买，但仓位乘数降低 |
| `AUTO` | 正常买 |

---

### Phase 3：动态入场成为主路径

#### 目标

把早盘自动交易从：

```text
09:26 选出 → verify_money_flow → execute_buy
```

改为：

```text
09:26 选出 → strategy_selection + pending_entry_signals
09:30-10:00 monitor/watchlist 多次确认
确认通过 → execute_buy
未通过 → EXPIRED / OBSERVE
```

#### 重点确认条件

1. 价格没有远离 VWAP：

```text
price / vwap <= 1.015 ~ 1.025
```

2. 不是日内最高点追入：

```text
price <= intraday_high * 0.985
```

3. 回踩不破：

```text
low_since_signal >= vwap * 0.995
```

4. 再次放量：

```text
volume_ratio >= 2
```

5. 资金流为正：

```text
net_mf_amount > 0
```

#### 代码位置

- `main.py`：候选写 pending。
- `monitor.py`：pending 循环确认。
- `core/analyzer.py -> verify_money_flow`：增强 VWAP/高点/回踩判断。
- 新增 `core/entry_confirm.py`：统一入场确认。

---

### Phase 4：交易标签与失败模式审计

#### 目标

让每笔买入/拦截/卖出都有结构化标签。

#### 当前已有基础

- `transactions.signal_tags_json`
- `positions.entry_tags_json`
- `strategy_selection.tags_json`
- `risk_event_log.params_json`
- `factor_snapshot.tags_json`

#### 建议统一标签规范

```json
[
  {"tag": "WEAK_MARKET_ENTRY", "value": true, "source": "pre_trade_gate"},
  {"tag": "ENTRY_BUCKET", "value": "B1", "source": "scheduler"},
  {"tag": "GAP_OPEN_PCT", "value": 6.98, "source": "quote"},
  {"tag": "WIN_RATE_SAMPLES", "value": 0, "source": "tracker"},
  {"tag": "VWAP_RATIO", "value": 1.027, "source": "intraday"},
  {"tag": "T1_BLOCKED_STOP_LOSS", "value": true, "source": "monitor"}
]
```

#### 审计报告新增章节

`core/trade_auditor.py` 新增：

```text
## 失败模式归因

| 标签 | 交易数 | 胜率 | 平均收益 | 最大亏损 | T0最大浮亏 | 建议 |
|---|---:|---:|---:|---:|---:|---|
| WEAK_MARKET_ENTRY | 5 | 20% | -4.8% | -12.1% | -13.5% | 弱市禁用自动买 |
| HIGH_GAP_ENTRY | 7 | 28% | -3.2% | -9.4% | -10.1% | 高开>3%等待回踩 |
| INSUFFICIENT_SAMPLES | 4 | 25% | -5.1% | -12.0% | -12.0% | 样本不足只观察 |
```

#### 关键事件记录

`monitor.py` 当 T+1 阻止卖出时，不应只是 `continue`，应该记录：

```text
risk_event_log:
event_type = T1_BLOCKED_SELL_SIGNAL
code = xxx
reason = stop_loss triggered but bought today
params = {pct_change, dynamic_sl, entry_time, current_price}
```

这一步非常重要，因为它能量化 A 股 T+1 的真实风险。

---

### Phase 5：A 股真实约束回测

#### 目标

建立最小可用的真实约束回测器：

```text
T+1
涨停买不到
跌停卖不出
滑点
手续费
成交额容量
买入当日不能止损
```

#### 推荐输出

```text
策略：集合竞价
区间：2026-01-01 ~ 2026-05-29
总交易：124
胜率：48.3%
平均收益：-0.8%
最大回撤：-18.2%
T+1 阻止止损：12 笔
涨停无法买入：8 笔
跌停无法卖出：2 笔
弱市早盘交易：19 笔，平均收益 -4.7%
高开>5%交易：14 笔，平均收益 -5.3%
```

#### 代码位置

建议新增：

- `core/backtester.py`
- `main.py --mode backtest`
- `tools/replay_day.py`

#### 优先级

比继续优化打分更高。因为没有真实约束回测，所有策略进化都可能在错误目标上优化。

---

### Phase 6：进化脚本升级

#### 当前问题

`core/evolve_strategy.py` 目前主要做：

- 策略胜率统计
- 权重调整
- 部分过滤参数调整

但下一阶段需要进化：

- 哪些 regime 禁止哪些策略。
- 哪些 time_bucket 胜率差，需要降级。
- 哪些风险标签导致亏损，需要硬拦截。
- 哪些策略样本不足，只能观察。

#### 新进化目标

```text
从调权重 → 调权限、调窗口、调仓位、调确认条件
```

#### 示例规则

```text
如果 WEAK_MARKET_MORNING_BLOCK 被拦截后，候选次日表现仍差：保持拦截。
如果 HIGH_GAP_ENTRY 历史平均收益 < -2%，降低 weak_max_open_change。
如果 B1 集合竞价 T0最大浮亏 > 5%，把集合竞价改为 pending。
如果 午盘精选在 weak_market 胜率 > 55%，允许 LOW_SIZE_CONFIRM。
```

#### 输出 patch 示例

```json
{
  "strategy_patch": {
    "weak_market_entry_gate": {
      "weak_max_open_change": 2.5
    },
    "strategy_permission_matrix": {
      "weak_market": {
        "集合竞价": "BLOCK",
        "午盘精选": "LOW_SIZE_CONFIRM"
      }
    }
  },
  "reason": "过去20日弱市高开>3%平均收益-4.2%，下调高开阈值"
}
```

---

## 6. 针对当前文件的具体改造清单

### 6.1 `core/pre_trade_gate.py`

当前已新增，建议继续演进为正式 `Risk Manager`：

下一步：

- 支持 `ALLOW / BLOCK / OBSERVE / PENDING / LOW_SIZE_AUTO`。
- 支持 `position_multiplier` 输出。
- 支持 `required_confirmations` 输出。
- 支持 `strategy_permission_matrix`。
- 移除兼容性双命名，只保留一个正式配置键。

目标输出：

```python
{
  "action": "CONFIRM_ONLY",
  "allow_execute_buy": False,
  "allow_pending": True,
  "position_multiplier": 0.2,
  "required_confirmations": ["vwap_pullback", "volume_rebreak"],
  "risk_tags": [...],
  "reason": "weak_market: pre_market confirm only"
}
```

---

### 6.2 `main.py`

当前问题：自动执行逻辑仍偏重，函数较长。

建议拆分：

```text
build_candidate_pool()
apply_candidate_filters()
apply_pre_trade_gate()
create_pending_or_execute()
calculate_position_size()
execute_buy_with_attribution()
```

短期动作：

- 09:26 pre_market 默认写 pending，不直接买。
- 强市场时才允许 immediate buy。
- 将 `strategy_name`、`entry_scenario`、`time_bucket` 写入 pending payload。

---

### 6.3 `monitor.py`

当前是哨兵 + pending entry + 卖出风控混合。

建议：

- pending entry 确认逻辑拆到 `core/entry_confirm.py`。
- 卖出风控拆到 `core/exit_policy.py`。
- T+1 阻止卖出时记录 `T1_BLOCKED_SELL_SIGNAL`。
- 每轮持仓检查增加轻量 heartbeat，显示持仓、价格、止损线、是否 T+1 blocked。

---

### 6.4 `core/analyzer.py`

建议：

- `check_market_environment` 输出 `regime` 和 `permissions`。
- `verify_money_flow` 改名/拆分为 `verify_intraday_structure`。
- 增强 VWAP 回踩、日内高点追入、opening range 判断。
- 把选股信号和入场确认分离。

---

### 6.5 `core/portfolio.py`

建议：

新增/扩展字段：

`transactions`：

- `entry_regime`
- `entry_bucket`
- `entry_scenario`
- `gate_action`
- `position_multiplier`

`risk_event_log` 已够用，先不用大改表。

`pending_entry_signals.payload_json` 应写入：

- `entry_scenario`
- `open_change`
- `vwap_ratio`
- `intraday_high_ratio`
- `win_rate_samples`
- `regime`

---

### 6.6 `core/trade_auditor.py`

新增章节：

1. `audit_pre_trade_gate_blocks()`
2. `audit_failure_patterns()`
3. `audit_t1_blocked_risk()`
4. `audit_regime_strategy_matrix()`
5. `audit_entry_bucket_performance()`

---

### 6.7 `core/evolve_strategy.py`

新增输入：

- `risk_event_log`
- `transactions.signal_tags_json`
- `market_sentiment_daily.regime`
- `strategy_performance_history` by regime/time_bucket

新增输出：

- `strategy_permission_matrix` patch
- `weak_market_entry_gate` patch
- `entry_policy.dynamic_window` patch
- `position_sizing` patch

---

## 7. 优先级排序

### P0：今天/明天必须完成

1. 确认 `weak_market_entry_gate` 已接入 `main.py` 和 `monitor.py`。
2. 清理配置命名，只保留一套正式门禁。
3. T+1 阻止卖出时写入 `risk_event_log`。
4. 审计报告统计 `BUY_BLOCKED_PRE_TRADE_GATE`。
5. 09:26 弱市 pre_market 禁止 immediate buy。

### P1：本周完成

1. `check_market_environment` 输出 `regime`。
2. 新增 `strategy_permission_matrix`。
3. 早盘集合竞价默认 pending，强市场例外。
4. `trade_auditor` 增加失败模式归因。
5. `pending_entry_signals` payload 写入更多风险字段。

### P2：两周内完成

1. 建立 A 股真实约束 backtester。
2. 增加 VWAP 回踩/开盘区间突破确认。
3. 仓位 sizing 引入 ATR/波动率。
4. 进化脚本输出权限矩阵 patch。

### P3：中长期

1. 多因子模型/机器学习排序。
2. 行业轮动 + 龙头池重构。
3. Paper account 仿真。
4. Web/HTML 风控仪表盘。
5. 组合优化和资金预算器。

---

## 8. 建议的最终系统形态

最终 `market-signal-lab` 应该变成：

```text
盘前：
  更新数据 → 判断 market_regime → 生成今日策略权限

09:26：
  集合竞价候选 → 入库 → 根据 regime 决定 pending/observe/block

09:30-10:00：
  monitor 多次确认 VWAP/量能/回踩/高点距离 → 满足才小仓买

10:00-14:00：
  watchlist 巡航 → 只买确认型突破，不买冲高回落

14:30：
  午盘资金确认 → 弱市只低仓，强市正常

15:30/15:35：
  审计 + 策略追踪 → 写入失败模式

周六：
  evolve_strategy → 调整权限矩阵/门禁阈值/仓位系数
```

核心原则：

```text
弱市不抢早盘。
高开不直接追。
样本不足不乐观。
T+1 风险前置。
失败模式必须结构化记录。
进化不只调权重，也调权限。
```

---

## 9. 立即可执行的下一步任务

建议下一轮直接做这 5 件：

1. **清理门禁配置命名**
   统一为 `weak_market_entry_gate` 或 `pre_trade_gate`，不要两个并存。

2. **T+1 阻止卖出事件化**
   在 `monitor.py` 中，当 `check_t1_limit(p)` 且止损本应触发时，写入：
   ```text
   event_type = T1_BLOCKED_SELL_SIGNAL
   ```

3. **审计报告新增门禁拦截统计**
   从 `risk_event_log` 读取：
   ```text
   BUY_BLOCKED_PRE_TRADE_GATE
   T1_BLOCKED_SELL_SIGNAL
   ```

4. **market_env 增加 regime 字段**
   先用规则版：
   ```text
   high → storm_market
   medium/MA20下方 → weak_market
   low/MA20上方 → normal_uptrend
   ```

5. **早盘 immediate buy 改 pending 优先**
   只有 `strong_uptrend` 才允许 09:26 immediate buy；其他全部 pending/observe。

---

## 10. 最终判断

当前系统已经到了一个关键转折点：

```text
继续堆策略，收益不一定提高，风险会更复杂；
先把交易权限、失败归因、真实约束回测打牢，系统才会真正进化。
```

GitHub 上优秀项目的共同方向不是“某个神奇指标”，而是：

```text
完整链路 + 风控前置 + 回测真实 + 执行分层 + 审计可解释 + 持续进化。
```

对当前 `market-signal-lab` 来说，最优路径是：

```text
先成为一个不会乱买的系统，
再成为一个会稳定赚钱的系统。
```
