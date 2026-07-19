# GitHub 开源量化项目策略调研总结

> 生成时间：2026-05-29 10:58 GMT+8
> 目标：参考 GitHub 上优秀开源量化项目的策略、风控、回测和工程设计，反推 `<project-root>` 下一步可改进方向。
> 注意：本报告只做研究与系统设计参考，不构成投资建议。

---

## 1. 这次重点看的项目

| 项目 | 方向 | 可借鉴点 |
|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | AI 量化投资平台 | 完整 ML 流水线：数据处理、模型训练、回测、Alpha、风险模型、组合优化、订单执行 |
| [vnpy/vnpy](https://github.com/vnpy/vnpy) | 国内量化交易平台 | CTA、价差、期权、组合策略、算法交易、仿真账户、图形化回测 |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | 加密交易机器人 | 策略回测、参数优化、lookahead/recursive 分析、策略命令体系完整 |
| [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | 自动交易机器人 | 实盘 + 纸交易 + 内置回测，重视长期历史数据验证 |
| [ling-0729/KHunter](https://github.com/ling-0729/KHunter) | A 股量化选股 | 数据获取、策略分析、选股执行、五维评分、择时、回测一体化 |
| [shouldnotappearcalm/a-share-skill](https://github.com/shouldnotappearcalm/a-share-skill) | A 股策略 Skill 集 | 主板流动性池、趋势回调、MACD 趋势共振、二次金叉、场景化交易纪律 |
| [colinsweany/quant-trade](https://github.com/colinsweany/quant-trade) | 多市场 Agent 投研 | A 股回测显式处理 T+1、涨跌停；行业轮动、龙头筛选、业绩扫雷 |
| [ringoshinnytech/tqsdk-strategies](https://github.com/ringoshinnytech/tqsdk-strategies) | 期货策略集合 | 70 个策略样例：趋势、均值回归、Dual Thrust、海龟、VWAP 突破、统计套利、自适应波动率 |
| [yutiansut/QUANTAXIS](https://github.com/yutiansut/QUANTAXIS) | 本地量化平台 | 数据/回测/模拟/交易/可视化/多账户，本地统一账户协议与高性能回测 |
| [HustWolfzzb/easyQuantify](https://github.com/HustWolfzzb/easyQuantify) | A 股系统 | 数据、特征、策略、风控、交易、监控、情绪分析模块分层 |

---

## 2. 优秀项目的共同策略思想

### 2.1 先判断市场状态，再决定是否进攻

优秀项目一般不是“每天固定时间强制买”，而是先识别市场环境：

- 趋势市场：使用突破、动量、趋势跟踪。
- 震荡市场：使用均值回归、布林带、RSI、低吸反转。
- 高波动/弱势市场：降低仓位、减少交易、只观察或做防守策略。
- 极端市场：熔断交易，不开新仓。

对我们当前系统的启发：

今天康达新材的问题，本质不是单个买点错误，而是“市场转弱时仍允许集合竞价自动买入”。现在系统只是：

```text
市场转弱 → 仓位 50% + 分数门槛 +10%
```

但从优秀系统的做法看，更合理的是：

```text
市场转弱 + 早盘高波动窗口 → 禁止自动买入，只入库观察
```

尤其 A 股有 T+1，早盘追高一旦错了，当天不能止损，风险比加密/美股更重。

---

### 2.2 策略必须分“市场模式”，不能一套规则打全天

开源策略集合里常见的分类：

1. **趋势跟踪**
   - 双均线
   - MACD 趋势共振
   - ADX 趋势强度
   - 海龟突破

2. **突破策略**
   - Donchian 通道突破
   - Dual Thrust
   - VWAP + 放量突破
   - 高点突破 + 成交量确认

3. **均值回归**
   - Bollinger Bands 回归
   - RSI 超买超卖
   - Z-Score 偏离回归

4. **截面选股 / 多因子**
   - 动量、价值、质量、波动率、流动性
   - 行业轮动
   - 龙头筛选
   - 多因子打分排序

5. **统计套利 / 对冲**
   - 配对交易
   - 协整 + Z-Score
   - 价差交易
   - 组合中性

对我们当前系统的启发：

现在 `market-signal-lab` 已经有不少策略：集合竞价、冷启动、龙头跟踪、技术突破、午盘精选、盘后资金。但它们更像“信号来源”，还需要进一步绑定“适用市场状态”。

建议加一层：

| 市场状态 | 允许策略 | 禁止/降级策略 |
|---|---|---|
| 强趋势/晴天 | 集合竞价、龙头、技术突破、午盘精选 | 无 |
| 多云但指数在 MA20 上 | 技术突破、午盘精选、低仓龙头 | 高开接力降级 |
| 多云且指数跌破 MA20 | 只允许午后确认、盘后观察 | 禁止早盘自动买 |
| 暴雨 | 禁止新开仓 | 只做持仓风控 |

---

### 2.3 回测要显式模拟交易限制，而不是只看信号收益

优秀项目特别重视：

- T+1 限制
- 涨跌停无法买入/卖出
- 滑点
- 手续费
- 成交量/容量约束
- 最大回撤
- 夏普/Calmar/Sortino
- 样本外验证
- 参数过拟合检查
- 前视偏差检查

`colinsweany/quant-trade` 的 A 股回测示例明确提到：

```text
T+1 拒绝 N 笔
涨停无法买入 N 笔
```

这点非常适合我们。

当前系统虽然实盘有 T+1 跳过卖出，但策略评估时需要更细地记录：

- 买入当天最大浮亏
- 当天是否触发止损但因 T+1 无法卖出
- 次日开盘是否继续下杀
- 这类交易在历史上是否应禁止

建议新增指标：

| 指标 | 含义 |
|---|---|
| `intraday_max_drawdown_t0` | 买入当日最大浮亏 |
| `t1_blocked_stop_loss` | 是否当天触发止损但 T+1 阻止卖出 |
| `next_open_gap` | 次日开盘跳空幅度 |
| `limit_up_chase_fail` | 追涨/接力失败标签 |
| `market_weak_entry` | 是否弱市开仓 |

这样系统才能从今天这种康达新材案例里真正进化。

---

### 2.4 风控不只是止损，而是“开仓前风控”

很多交易系统的风控重点不是亏了再卖，而是：

- 买前判断市场是否允许交易
- 买前判断波动是否过高
- 买前判断价格是否偏离 VWAP/均线过远
- 买前判断当日高开是否过度
- 买前判断是否处于涨停板附近
- 买前判断是否有足够历史胜率样本
- 买前判断当前策略在此市场 regime 下是否有效

对我们当前系统，最该加强的是买前风控。

尤其今天日志里有两点需要警惕：

```text
市场转弱: 上证指数跌破MA20
胜率门禁样本不足(放行): 康达新材 samples=0 < 30
```

这两条叠加时，不应该继续买。

建议规则：

```text
如果 market_regime = weak
且 strategy = 集合竞价
且 win_rate_samples < 30
则禁止自动买入，只入库观察。
```

或者更严格：

```text
market_regime = weak 时：
- 禁止集合竞价自动买入
- 禁止昨日涨停接力自动买入
- 禁止高开 > 3% 自动买入
- 只允许 10:00 之后经过 VWAP/资金流确认的低仓买入
```

---

## 3. 可以借鉴的具体策略模块

### 3.1 市场 Regime Filter：行情状态过滤器

来源启发：Qlib、Freqtrade、VN.PY、TqSdk 策略集。

建议为当前系统新增统一 `market_regime`：

```text
strong_uptrend    强趋势
normal_uptrend    正常多头
range_market      震荡
weak_market       弱势
storm_market      暴雨
```

判断因子：

- 上证指数 vs MA20 / MA60
- 创业板指数 vs MA20
- 涨跌家数比
- 跌停数量
- 涨停高度
- 北向/主力资金方向
- 全市场成交额变化
- 指数日内 VWAP 位置

输出不只是天气，还要输出交易权限：

```json
{
  "regime": "weak_market",
  "allow_auto_buy": false,
  "allowed_strategies": ["post_market", "watchlist_observe"],
  "blocked_strategies": ["pre_market_auto", "limit_up_relay"],
  "max_position_pct": 0.0,
  "reason": "上证跌破MA20且早盘弱势"
}
```

---

### 3.2 Entry Window：动态买入窗口

来源启发：优秀交易系统不会只在固定时间点下单，会使用窗口和确认条件。

当前系统已有动态时间窗概念，但默认偏保守或观察。建议强化：

| 时间窗 | 建议用途 |
|---|---|
| 09:25-09:30 | 只做竞价观察，不自动买弱市接力 |
| 09:30-10:00 | 只允许强市场 + 强封单 + 低偏离 |
| 10:00-11:00 | 更适合确认型突破 |
| 13:00-14:00 | 午后修复/二次确认 |
| 14:00-14:40 | 趋势确认 + 资金回流 |
| 14:40-15:00 | 尾盘潜伏，但避免追高 |

建议：把 `09:26 pre_market --auto-trade` 改成“候选入库 + 动态窗口确认”，除非强市场。

---

### 3.3 VWAP + 成交量突破过滤

来源启发：TqSdk 策略集中的 VWAP breakout volume。

核心思想：价格突破必须同时满足：

- 站上 VWAP
- 成交量放大
- 不偏离 VWAP 过远
- 不是快速拉高后的回落

适合我们替换部分“站上 MA5 就买”的简单逻辑。

建议买入条件：

```text
price > VWAP
price / VWAP < 1.015 ~ 1.025
volume_ratio > 2
current_price < intraday_high * 0.985  # 避免最高点追入，可按策略调整
资金流为正
```

对涨停接力类策略，额外要求：

```text
开盘涨幅 <= 3% 或 开盘后回踩 VWAP 不破再上
```

---

### 3.4 自适应波动率仓位

来源启发：TqSdk 自适应波动率突破、海龟策略、风险平价思想。

不是固定买 15%、20%，而是根据 ATR/日内波动自动缩仓：

```text
position_pct = base_pct * target_volatility / realized_volatility
```

简单落地版本：

| ATR / price | 仓位系数 |
|---|---:|
| < 3% | 1.0 |
| 3%-5% | 0.7 |
| 5%-8% | 0.4 |
| > 8% | 禁止买入或 0.2 |

这对短线很关键，尤其是昨日涨停股、20cm/北交所个股。

---

### 3.5 策略分层：信号、确认、执行、复盘分离

优秀项目工程上普遍分层：

```text
Signal  信号生成
Filter  市场/风控过滤
Confirm 入场确认
Execute 下单执行
Monitor 持仓监控
Audit   复盘归因
Evolve  参数进化
```

我们当前系统已经有类似结构，但今天的问题说明 `Signal` 到 `Execute` 之间的风控还不够硬。

建议所有自动买入必须过统一 `pre_trade_gate()`：

```python
pre_trade_gate(signal, market_env, strategy_stats, intraday_state)
```

输出：

```python
ALLOW / OBSERVE_ONLY / BLOCK
reason
max_position_pct
required_confirmations
```

---

## 4. 对当前 market-signal-lab 的改造建议优先级

### P0：立刻改，防止再出现康达新材式问题

#### 规则 1：弱市禁止早盘自动买入

```text
如果 上证指数 < MA20
并且 当前时间 < 10:00
则 pre_market 自动买入改为 observe_only。
```

#### 规则 2：弱市 + 样本不足禁止放行

```text
if market_weak and win_rate_samples < 30:
    block auto buy
```

#### 规则 3：昨日涨停接力在弱市禁买

```text
if market_weak and signal_tag contains 昨日涨停/接力/高开:
    block auto buy
```

#### 规则 4：高开超过 3%-5% 不直接买

```text
weak_market: gap_open > 3% block
normal_market: gap_open > 5% require VWAP pullback confirmation
strong_market: gap_open > 7% block unless sealed limit-up strategy
```

---

### P1：本周内改，增强系统学习能力

#### 1. 给交易打风险标签

新增字段或日志标签：

- `market_regime_at_entry`
- `entry_time_bucket`
- `gap_open_pct`
- `price_vwap_ratio`
- `win_rate_samples`
- `t1_blocked_stop_loss`
- `intraday_max_drawdown_t0`

#### 2. 审计报告新增“失败模式归因”

例如：

```text
本周亏损交易中：
- 弱市早盘开仓：3 笔，平均收益 -6.2%
- 样本不足放行：2 笔，平均收益 -4.8%
- 高开追入：4 笔，平均收益 -5.1%
```

这样进化脚本才能改规则，而不是只调权重。

#### 3. 回测加入 A 股真实约束

至少模拟：

- T+1
- 涨跌停不可成交
- 买入当天不能止损
- 滑点/手续费
- 成交量容量

---

### P2：中期增强，提升策略质量

#### 1. 建立策略适用矩阵

每个策略维护：

```json
{
  "strategy": "集合竞价",
  "allowed_regimes": ["strong_uptrend", "normal_uptrend"],
  "blocked_regimes": ["weak_market", "storm_market"],
  "preferred_buckets": ["B1"],
  "min_samples": 30,
  "min_win_rate": 0.52,
  "max_t0_drawdown_allowed": -0.04
}
```

#### 2. 引入确认型买入

尤其早盘信号不直接买，而是创建 pending entry：

```text
09:26 产生候选
09:30-10:00 等待：回踩 VWAP 不破 + 再次放量上穿
满足才买，否则过期
```

#### 3. 增加“行业轮动 + 龙头确认”

参考 quant-trade 的行业轮动思路：

- 先选强行业
- 再选行业内龙头
- 再看个股资金/形态
- 最后看入场窗口

避免孤立看个股信号。

---

## 5. 我对当前系统的判断

当前 `market-signal-lab` 的优势：

- 已经有完整任务调度。
- 数据源、邮件报告、持仓监控、策略追踪、审计、进化都有雏形。
- 对 A 股 T+1 已经有实盘层面的保护。
- 策略覆盖早盘、午盘、盘后、备选池，闭环较完整。

当前最大短板：

1. **开仓前风控不够硬**
   弱市仍然自动买，尤其早盘接力风险大。

2. **样本不足时过于乐观**
   `samples=0 < 30` 仍然放行，应该在弱市至少默认拦截。

3. **T+1 风险没有反馈到开仓规则**
   系统知道当天不能卖，但买入时没有充分惩罚“当天可能大幅回撤”的候选。

4. **策略状态与市场状态未强绑定**
   集合竞价、龙头、技术突破不应该在所有天气下同权运行。

5. **进化更多是调权重，还缺少失败模式学习**
   需要把失败原因结构化记录，让系统知道“为什么亏”。

---

## 6. 推荐落地方案

### 第一阶段：安全补丁

马上加统一开仓门禁：

```text
弱市 + 早盘 = 禁止自动买
弱市 + 样本不足 = 禁止自动买
弱市 + 昨日涨停接力 = 禁止自动买
高开过大 = 必须等待 VWAP 回踩确认
```

这一步优先级最高，能直接降低今天这种亏损。

---

### 第二阶段：审计增强

让每笔交易记录这些字段：

```text
市场状态、时间桶、策略、样本数、开盘涨幅、VWAP偏离、量比、资金流、T+0最大浮亏、是否T+1阻止止损
```

然后审计报告按标签统计亏损来源。

---

### 第三阶段：动态窗口买入

把早盘固定买入改成：

```text
09:26 只产生候选
09:30-10:00 由 monitor / watchlist 做确认买入
```

强市场可以例外，弱市场必须观察。

---

### 第四阶段：多因子 + 行业轮动

从“单股技术信号”升级为：

```text
市场状态 → 行业强度 → 个股资金 → 技术形态 → 入场窗口 → 仓位
```

这也是 Qlib/KHunter/quant-trade 这类项目共同体现出的方向。

---

## 7. 结论

我的建议很明确：

**不要先加更多策略，先把开仓权限系统做硬。**

今天康达新材暴露出来的不是“缺少一个更聪明的选股模型”，而是：

```text
弱市 + 早盘 + 接力 + 样本不足 + T+1不可卖
```

这几个风险条件叠加时，系统仍然允许自动买入。

优秀开源项目给我们的共同答案是：

```text
先识别市场 regime，再决定策略权限；
先做开仓前风控，再谈止损；
先记录失败模式，再谈自进化。
```

下一步最值得做的是 `pre_trade_gate`，把自动买入从“信号驱动”升级为“信号 + 市场权限 + 风险标签 + 入场确认”驱动。
