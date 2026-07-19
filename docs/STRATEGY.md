# Market Signal Lab（OpenClaw 执行手册 / Runbook）(VNext)

> 目标：把本项目作为 **OpenClaw 的“可调度交易系统”** 来运行。
>
> 核心原则：
> 1) **本金安全优先**（先观测、再门禁、再放大）
> 2) **全链路可审计**（选股→入库→交易→风控→追踪→审计→进化）
> 3) **默认保守**（新规则先过门禁、审计和 paper 影子账户观察）
>
> 整体框架、模块分层和关键设计决策见 [ARCHITECTURE.md](ARCHITECTURE.md)。本文只维护 OpenClaw 部署和日常运行口径。

---

## 0. 你需要知道的 5 件事（给 OpenClaw 的最短摘要）

1. **选股入库**由 `main.py` 在不同 `--mode` 下完成（集合竞价/午盘/盘后等）。
2. **自动买入执行模式**仍由 `main.py` 的 `--auto-trade` 表示（并受 regime、权限矩阵、数据质量、行业轮动、量比/VWAP、仓位预算约束）；OpenClaw AgentTurn 定时任务不直接执行该参数。
3. **自动卖出/风控哨兵**由 `monitor.py` 负责（独立进程，避免重复发卖出邮件）。
4. **动态入场**默认启用：AgentTurn 使用 `--queue-entry` 只创建 `pending_entry_signals(status=PENDING)` 和门禁审计，不执行买入；`monitor.py` 再在窗口内二次确认。
5. **paper 影子账户**默认启用：真实 `main/watchlist` 买入成功后，会同步镜像到 `paper_main/paper_watchlist`，用于观察新规则。

### 0.1 2026-07-05 训练承接部署摘要（OpenClaw 必读）

本次主项目变更来自 2026-07-02 / 2026-07-03 影子训练复盘，目标是 **提高 paper 影子系统的可买入样本采集能力**，同时保持 `main/watchlist` 主账户门禁严格。OpenClaw 部署时必须按代码和本文档一起同步，不要只更新任务课表。

部署范围：

| 文件/模块 | 变更目的 | 主账户影响 | 影子账户影响 |
|---|---|---|---|
| `config/strategy_config.json` | 新增/调整 paper 弱市门禁、paper 资金、paper 复核窗口和暴雨确认探针配置 | 主账户仍受原门禁；暴雨只允许确认队列探针，不直接买入 | 扩大 paper 采样能力，减少预算/窗口/弱市样本不足造成的无效损耗 |
| `core/pre_trade_gate.py` | 增加 account 上下文和 `paper_weak_market_gate_experiment` | `main/watchlist` 不使用 PAPER_WEAK 覆盖 | `paper_main/paper_watchlist` 可使用样本门槛 25、弱市追高观察带 5.0% |
| `main.py` | paper 训练路由可保留被行业/板块主账户过滤的候选，并写结构化审计 | 主账户过滤不变 | 写入 `PAPER_SECTOR_FILTER_BYPASS` / `PAPER_BOARD_FILTER_BYPASS` 后交给影子哨兵二次确认 |
| `monitor.py` | paper pending 使用更短冷却和更多复核次数，并传递 account 给门禁 | 主哨兵逻辑不放宽 | paper pending 更容易形成 `PENDING_CHECK / SIM_TRADE_BUY / FINAL_BLOCKER` 终态证据 |
| `core/position_sizer.py` | paper 账户允许现金足够时一手兜底 | 主账户默认不启用一手兜底 | 减少 `budget too small` 导致的训练样本流失 |
| `core/sector_rotation.py` | 行业 UNKNOWN 在弱市改为降权确认，不再直接硬拒绝 | 主账户仍经过后续门禁 | paper 训练可保留更多待验证强票 |
| `tests/` 与 `tools/diagnostics/` | 增加回归测试和影子训练日志诊断 | 不交易 | 用于部署后验收 |

OpenClaw 部署后必须检查：

```bash
cd <project-root> && ./run_openclaw.sh tools/diagnostics/check_shadow_training_preflight.py --check-db-cash --email --assert-ready
cd <project-root> && ./run_openclaw.sh tools/diagnostics/analyze_shadow_training_logs.py --date YYYY-MM-DD --expect-bypass --email --assert-healthy --strict
cd <project-root> && ./run_openclaw.sh tools/diagnostics/replay_pending_gate.py --date YYYY-MM-DD --accounts main,watchlist,paper_main,paper_watchlist --now 'YYYY-MM-DD 10:40:00'
```

部署验收日志标记：

- `PAPER_WEAK_SAMPLE_FLOOR_EXPERIMENT` / `PAPER_WEAK_SAMPLE_FLOOR_USED`
- `PAPER_WEAK_CHASE_BAND_EXPERIMENT` / `PAPER_WEAK_CHASE_BAND_USED`
- `PAPER_SECTOR_FILTER_BYPASS` / `PAPER_BOARD_FILTER_BYPASS`
- `PAPER_TRAINING_PENDING_CHECK`（新 paper 统一阈值复核）；旧日志可能只有 `PAPER_STRONG_PENDING_CHECK`
- `SIM_TRADE_BUY account=paper_main` 或 `SIM_TRADE_BUY account=paper_watchlist`
- `FINAL_BLOCKER` / `BUY_BLOCKED_PRE_TRADE_GATE` / `PENDING_SKIP` 等终态阻断

禁止事项：

- 不要把 `PAPER_WEAK_*` 作用到 `main/watchlist`。
- 不要给 OpenClaw AgentTurn 任务加 `--auto-trade`。
- 不要把邮件、SHADOW、OBSERVE、focus-only、fixture、补跑日志当作 BUY 样本。
- 不要因为本次部署放宽真实账户的 VWAP、量比、追高、涨停不可成交、权限矩阵或 T+1 卖出限制。

### 0.2 2026-06-01 开盘实战前检查（OpenClaw 必读）

> 结论先写前面：OpenClaw 正式任务一律使用 `./run_openclaw.sh ...`。
> 不要直接执行 `./venv/bin/python ...`，因为直接执行不会自动加载 `.env.openclaw`，可能使用默认数据库配置。

开盘前在项目根目录执行：

```bash
./run_openclaw.sh main.py --mode risk_dashboard --no-email
./run_openclaw.sh main.py --mode audit --no-email
./run_openclaw.sh monitor.py --once --dry-run --no-email --force
./run_openclaw.sh tools/audit_cold_start_tags.py --date 2026-06-01 --json
```

通过标准：

```text
risk_dashboard 能看到 regime/权限矩阵/pending/持仓止损线
audit 能正常输出交易质量审计
monitor dry-run 不做真实交易，只输出风控和 pending 动态入场判断
cold_start_tags 审计命令能连上正式库，并输出 pending/positions/transactions 三段 JSON
```

冷启动标签审计重点看：

```text
pending.with_base_tags_json > 0    表示待确认信号保留了训练/观察标签
positions.cold_rows > 0            表示真实持仓里能追溯冷启动标签
transactions.cold_rows > 0         表示成交流水里能追溯冷启动标签
```

如果当天尚未产生冷启动候选/成交，上述数量可以为 0；但命令必须能正常连接数据库并返回结构化结果。

本轮冷启动标签补丁是观测增强：

```text
不新增自动卖出规则
不放宽买入条件
不改变仓位预算
只保证 pending -> buy -> positions/transactions 的标签可追溯
```

---

## 1. 一键运行与目录约定

### 1.1 推荐运行方式（OpenClaw：统一用虚拟环境）
在项目根目录 `market-signal-lab/` 下执行（OpenClaw 只需要会执行命令）。

**第一次部署 / 虚拟环境丢失时，重建 venv：**

```bash
cd <project-root>
python3 -m venv venv
./venv/bin/python -m pip install -U pip
./venv/bin/pip install -r requirements.txt
```

**日常执行命令（OpenClaw 推荐：统一用 env loader 脚本 `run_openclaw.sh`）：**

1) 首次配置（只做一次）：
- `cp .env.openclaw.example .env.openclaw`（填写 DB_PASS / EMAIL_PWD / TUSHARE_TOKEN 等敏感项）

2) 日常运行（统一用脚本注入环境变量后再执行 Python）：

- 盘前宏观：
  - `./run_openclaw.sh main.py --mode macro`
- 早盘选股入队：
  - `./run_openclaw.sh main.py --mode pre_market --queue-entry`（选股/入库/PENDING入队，不直接买入）
- 午盘资金流：
  - `./run_openclaw.sh main.py --mode afternoon --queue-entry`
- 盘后备选：
  - `./run_openclaw.sh main.py --mode post_market`
- 交易质量审计：
  - `./run_openclaw.sh main.py --mode audit`
- 风控仪表盘：
  - `./run_openclaw.sh main.py --mode risk_dashboard`
- 策略追踪（T+1/T+2）：
  - `./run_openclaw.sh main.py --mode track`
- 全天重点雷达（只读观察）：
  - `./run_openclaw.sh main.py --mode focus_monitor`
- A股真实约束回放：
  - `./run_openclaw.sh main.py --mode replay_day --date 20260529 --strategy 集合竞价 --no-email`
- 全天哨兵（卖出/风控/备选池巡航）：
  - `./run_openclaw.sh monitor.py`

> 说明：`run_openclaw.sh` 会自动加载同目录的 `.env.openclaw`（如存在），从而避免在 OpenClaw 的每条任务里重复写一堆环境变量。
>
> 如需静默（不发邮件）：在命令后追加 `--no-email`。

> 注：是否发邮件由 `--no-email` 控制；OpenClaw 正式运行一般不加 `--no-email`。
>
> 本机开发调试时也可用 `python main.py ...`，但 OpenClaw 任务编排请统一使用 `./run_openclaw.sh ...`。
>
> 排查数据库时也使用 `./run_openclaw.sh tools/...`，不要直接用虚拟环境 Python 跑诊断脚本。

### 1.2 关键目录
- 配置：`config/strategy_config.json`（可被进化引擎改写）
- 日志：`logs/stock_analyzer-YYYYMMDD.log`，每天一个文件；策略进化日志为 `logs/evolution-YYYYMMDD.log`
- DB 表结构初始化：启动时 `PortfolioManager.init_tables()` 自动迁移（加法变更）

### 1.3 必须配置的环境变量（OpenClaw 必读）

> 近期已移除代码中的明文密码/Token 默认值：
> - DB_PASS / REDIS_PASS / EMAIL_PWD / TUSHARE_TOKEN **必须通过环境变量提供**
> - 否则系统会正常运行到“连接/发信”阶段，但会因为缺少凭据而跳过或失败（这是安全设计）。

**最少必配（交易/入库/通知都正常）：**

```bash
# MySQL
export DB_HOST="127.0.0.1"
export DB_PORT="3306"
export DB_USER="root"
export DB_PASS="<your_mysql_password>"
export DB_NAME="stock_analysis"

# Redis（如不使用缓存也可不配；但项目默认会连接）
export REDIS_HOST="127.0.0.1"
export REDIS_PORT="6379"
export REDIS_PASS="<your_redis_password>"  # 无密码可留空
export REDIS_DB="0"
export REDIS_SOCKET_TIMEOUT="5"
export REDIS_SOCKET_CONNECT_TIMEOUT="5"
export REDIS_HEALTH_CHECK_INTERVAL="30"
export REDIS_MAX_CONNECTIONS="20"
export REDIS_MAX_VALUE_BYTES="5000000"
export REDIS_BATCH_HISTORY_CACHE_CHUNK_SIZE="200"

# 邮件（不想发邮件也可在任务命令里加 --no-email）
export EMAIL_ENABLED="0"  # 显式改为 1 后才允许发送
export SMTP_SERVER="smtp.qq.com"
export SMTP_PORT="465"
export EMAIL_USER="your-email@example.com"
export EMAIL_PWD="<your_smtp_auth_code>"
export EMAIL_TO="your-email@example.com"

# 日志：每天一个文件，保留5天；邮件正文完整落日志，便于不能复制邮件时复盘
export LOG_RETENTION_DAYS="5"
export LOG_EMAIL_CONTENT="1"

# Tushare（如使用 tushare 接口）
export TUSHARE_TOKEN="<your_tushare_token>"
export TUSHARE_URL="https://api.tushare.pro"
export TUSHARE_REALTIME_PRIMARY="rt_k"
export TUSHARE_RT_K_ENABLED="1"
export TUSHARE_RT_MIN_CHUNK_SIZE="200"
export TUSHARE_RT_MIN_SLEEP_SEC="0.08"

# 腾讯财经 GTimg 分钟线降级（可选；tushare 1min 限频/失败时启用）
# 1=启用(默认) 0=禁用
export TENCENT_MINUTE_FALLBACK_ENABLED="1"
export TENCENT_MINUTE_FALLBACK_BASE_URL="http://ifzq.gtimg.cn"
export TENCENT_MINUTE_FALLBACK_MKLINE_PATH="/appstock/app/kline/mkline"
export TENCENT_MINUTE_FALLBACK_COUNT="500"
export TENCENT_MINUTE_FALLBACK_TIMEOUT_SEC="5"
# 可选 headers（JSON 字符串）/ UA
export TENCENT_MINUTE_FALLBACK_USER_AGENT=""
export TENCENT_MINUTE_FALLBACK_HEADERS_JSON=""
# 可选重试（默认 0，不改变当前一次请求行为）
export TENCENT_MINUTE_FALLBACK_RETRIES="0"
export TENCENT_MINUTE_FALLBACK_RETRY_DELAY_SEC="0"
```

**OpenClaw 落地建议：**
- 把上述 export 写到 OpenClaw 的任务环境里（或统一的启动脚本），确保 cron/调度执行时也能拿到。
- 如果你暂时只想验证逻辑，不想发邮件：在命令后追加 `--no-email`。
- Redis 只是加速缓存，不是交易任务硬依赖；远端 Redis 写入慢时不能阻断选股、入库、风控或邮件。
- `batch_history` 会按 `REDIS_BATCH_HISTORY_CACHE_CHUNK_SIZE` 拆块缓存，避免把全市场历史一次性写成 10MB+ 的 Redis 大对象。
- 超过 `REDIS_MAX_VALUE_BYTES` 的其他大对象会主动跳过写缓存，日志里的 `Redis SET skipped` 属于正常保护。
- 后续如果训练或策略更新增加大批量数据缓存，必须同步检查并更新这里的 Redis 上限说明。

---

## 2. OpenClaw 执行课表（唯一权威版本）

> 本节是 OpenClaw 的唯一任务源。OpenClaw 必须严格按本节创建、启用、停用任务；不要再从 README、旧 MEMORY、聊天记录或历史日志里拼接课表。
>
> 所有正式任务都必须使用完整命令：`cd <project-root> && ./run_openclaw.sh ...`。
>
> 默认发邮件；正式任务不要追加 `--no-email`。只有开盘前人工自检、回放、临时排错才允许使用 `--no-email`。
>
> `--queue-entry` 只入队和审计，不直接买入；`monitor.py` 再二次确认并执行模拟仓买卖。
>
> `--paper-trade` 创建 `paper_main/paper_watchlist` 的影子 PENDING；`monitor.py --paper-only` 只处理影子账户。
>
> `--auto-trade` 不进入 OpenClaw AgentTurn 定时任务，避免调度层拒绝高影响金融操作。

### 2.1 执行纪律

OpenClaw 按下面规则维护任务：

1. 表中 `必须启用` 的任务必须存在且启用。
2. 表中 `停用/删除` 的任务必须从 OpenClaw 里禁用或删除。
3. 同一任务名、同一命令、同一时间段只能保留一条；不要同时保留旧课表和新课表。
4. 主账户任务和影子任务要分开建，不要把 `--paper-trade` 或 `--paper-only` 混到主账户任务里。
5. 影子系统必须同时有“影子入队”和“影子哨兵”；只有 `monitor.py --paper-only` 不会自己产生买入候选。
6. 每次训练、周末进化、参数升级后，如果影响策略执行时间、命令参数、账户路由或通知口径，必须先更新本节课表，再更新 OpenClaw 任务。
7. 更新课表时必须在表格里写清楚：任务名、时间、完整命令、账户范围、是否启用、是否发邮件、变更原因。
8. OpenClaw 执行后以 `logs/stock_analyzer-YYYYMMDD.log` 里出现 `Starting Market Signal Lab in ... mode` 或 monitor 启动日志为准，不只看 OpenClaw 的 `ok` 状态。
9. 如果当天是盘中临时部署，部署完成前已经错过的 09:26/09:31 等任务不能算系统执行失败；但部署完成后仍必须按后续课表执行，并在日志中看到 `Runtime flags` 确认参数。
10. 如果 Tushare token 在早盘过期，早盘宏观/竞价数据可能降级或缺失；token 恢复后需要重点核对后续任务是否重新初始化、是否继续发邮件、是否按风控门禁写阻断审计。

### 2.2 主账户交易日课表（周一至周五必须启用）

这张表负责 `main/watchlist` 主模拟账户的完整闭环：盘前准备、候选入队、哨兵买卖、风控、审计、追踪、盘后装填。

| 序号 | 任务名 | 执行时间段 | 完整命令 | 账户范围 | 邮件 | 启用 | 说明 |
|---|---|---|---|---|---|---|---|
| M01 | StockAnalyzer 盘前宏观 | 周一至周五 09:00-09:20 内执行一次 | `cd <project-root> && ./run_openclaw.sh main.py --mode macro` | 全局缓存 | 发 | 必须启用 | 生成/刷新宏观缓存，给竞价和风控使用 |
| M02 | StockAnalyzer 开盘前风控仪表盘 | 周一至周五 09:15 | `cd <project-root> && ./run_openclaw.sh main.py --mode risk_dashboard` | 全账户看板 | 发 | 必须启用 | 检查 regime、权限矩阵、pending、持仓止损线 |
| M03 | StockAnalyzer 主哨兵 | 周一至周五 09:25 起每 10 分钟，至 15:55 结束 | `cd <project-root> && ./run_openclaw.sh monitor.py` | `main/watchlist/rescue` | 发 | 必须启用 | 主账户持仓风控卖出、PENDING 动态入场、手工持仓提醒、事件审计 |
| M04 | StockAnalyzer 早盘竞价入队 | 周一至周五 09:26 | `cd <project-root> && ./run_openclaw.sh main.py --mode pre_market --queue-entry` | `main` | 发 | 必须启用 | 集合竞价候选入库、门禁审计、PENDING 入队 |
| M05 | StockAnalyzer 备选池开盘入队 | 周一至周五 09:31 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry` | `watchlist` | 发 | 必须启用 | MA5 突破、资金确认、备选池开盘候选入队 |
| M06 | StockAnalyzer 备选池上午巡航一 | 周一至周五 10:00 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry` | `watchlist` | 发 | 必须启用 | 捕捉早盘分歧后的二次确认 |
| M07 | StockAnalyzer 备选池上午巡航二 | 周一至周五 11:20 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry` | `watchlist` | 发 | 必须启用 | 午前复核备选池确认买点，避开 11:30 午间非交易窗口 |
| M08 | StockAnalyzer 备选池下午巡航一 | 周一至周五 13:00 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry` | `watchlist` | 发 | 必须启用 | 捕捉午后修复/资金回流 |
| M09 | StockAnalyzer 备选池下午巡航二 | 周一至周五 14:00 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry` | `watchlist` | 发 | 必须启用 | 午后最后一轮备选池确认 |
| M10 | StockAnalyzer 午盘资金流入队 | 周一至周五 14:30 | `cd <project-root> && ./run_openclaw.sh main.py --mode afternoon --queue-entry` | `main` | 发 | 必须启用 | 资金流窗口候选入库、门禁审计、PENDING 入队 |
| M11 | StockAnalyzer 收盘前风控仪表盘 | 周一至周五 15:10 | `cd <project-root> && ./run_openclaw.sh main.py --mode risk_dashboard` | 全账户看板 | 发 | 必须启用 | 收盘前复核持仓、T+1 阻断、paper 镜像 |
| M12 | StockAnalyzer 交易质量审计 | 周一至周五 15:30 | `cd <project-root> && ./run_openclaw.sh main.py --mode audit` | 全账户审计 | 发 | 必须启用 | 交易归因、执行质量、风险事件 |
| M13 | StockAnalyzer 策略追踪 | 周一至周五 15:35 | `cd <project-root> && ./run_openclaw.sh main.py --mode track` | 策略样本 | 发 | 必须启用 | T+1/T+2 表现验证、进攻窗口观测 |
| M14 | StockAnalyzer 模拟仓日报 | 周一至周五 15:45 | `cd <project-root> && ./run_openclaw.sh tools/report_sim_trades.py --accounts main,watchlist,paper_main,paper_watchlist --realtime --email` | 全模拟账户 | 发 | 必须启用 | 汇总当日买卖流水、持仓浮盈亏、已实现盈亏 |
| M15 | StockAnalyzer 盘后资金复盘 | 周一至周五 18:00 | `cd <project-root> && ./run_openclaw.sh main.py --mode post_market` | `main` | 发 | 必须启用 | 盘后资金流候选入库，为后续交易日装填弹药 |

### 2.3 影子模拟仓交易日课表（周一至周五必须启用）

这张表只服务 `paper_main/paper_watchlist`。它可以触发影子买入和卖出，但不能处理主模拟账户。

影子系统不是只做审计。`--paper-trade --mode watchlist --queue-entry` 会把备选池候选写入 `paper_watchlist`；其中被主账户“涨幅超过 pending 前上限”或“盘中触板/曾涨停”挡住的强票，会在 `paper_strong_entry_experiment.enabled=true` 时进入 **影子强票实验通道**。该通道只影响 `paper_*`：

- 当前阶段影子主目标是增加真实买卖样本：`paper_all_pool_execution.enabled=true` 时，早盘 `--paper-trade --mode pre_market --queue-entry` 会把 `集合竞价 / 冷启动 / 龙头跟踪 / 技术突破` 全部写入 paper 可买 PENDING，不再只停留在 SHADOW 审计。
- 全天重点雷达也必须参与影子交易验证：`--mode focus_monitor --queue-entry --paper-trade` 会把 `强盯/重点` 且非 T+2 的标的写入 `paper_watchlist` 可买 PENDING，用于验证龙头、冷启动、技术突破等强势池的真实买卖结果。
- 主账户 `main/watchlist` 继续使用原来的 5% 备选池入队上限、7% 防追高、涨停禁买、量比 2.0 等严格门禁。
- paper-only 训练路由不能被主账户权限提前丢样本：行业轮动弱势、创业板/科创板/北交所未授权等主账户硬过滤，在 `--paper-trade` 下改为写入 `PAPER_SECTOR_FILTER_BYPASS` 或 `PAPER_BOARD_FILTER_BYPASS` 审计标签后继续进入 paper 候选；最终是否买入仍由影子哨兵的量能、VWAP、成交可行性、风控和 T+1 规则决定。该放宽只作用于 `paper_main/paper_watchlist`，不得扩散到 `main/watchlist`。
- 影子全池入队日志标记：`PAPER_ALL_POOL_PENDING_ROUTE`；重点雷达影子入队日志标记：`PAPER_FOCUS_PENDING_CREATED`、`PAPER_FOCUS_PENDING_SUMMARY`。
- 影子强票入队日志标记：`PAPER_STRONG_PENDING_ROUTE`、`PAPER_STRONG_PENDING_CREATED`。
- 影子训练过滤绕过日志标记：`PAPER_SECTOR_FILTER_BYPASS`、`PAPER_BOARD_FILTER_BYPASS`、`PAPER_TRAINING_FILTER_BYPASS`。
- 影子哨兵二次确认日志标记：`PAPER_TRAINING_PENDING_CHECK`；旧强票日志可能只有 `PAPER_STRONG_PENDING_CHECK`。买入成功仍以 `SIM_TRADE_BUY account=paper_main/paper_watchlist` 为准。
- `paper_all_pool_execution.windows` 是影子可买池专用执行窗口，默认 B1-B4；`午盘精选` 为 B2-B5，用于减少 paper-only 的 `window not allowed` 样本损耗。该窗口对所有 `paper_*` 训练 pending 生效，不放宽主账户时间窗。
- paper-only 复核节奏使用独立采样频率：`paper_all_pool_execution.scan_interval_sec=120`、`retry_cooldown_sec=120`、`max_retries=10`；主账户动态入场仍使用 `monitor.py` 默认 60 秒扫描、`entry_policy.models.dynamic_window.retry_cooldown_sec=300`、`max_retries=6`。paper-only 扫描间隔必须不低于 paper 冷却时间，避免每分钟制造 `retry cooldown` 噪声。
- 主哨兵与影子哨兵必须分工执行：`monitor.py` 默认只消费 `main/watchlist` 的 pending 和持仓；`monitor.py --paper-only` 只消费 `paper_main/paper_watchlist` 的 pending 和持仓。OpenClaw 可同时保留主哨兵和影子哨兵，但不得让主哨兵重复处理 paper 行，否则会把 `retry cooldown`、重复复核、重复卖出噪声放大。
- 影子强票默认最多入队 5 只；当前涨幅最低 5.0%；主板涨幅上限 10.2%，创业板/科创板/北交所上限 20.2%，ST 上限 5.2%。
- 所有 `paper_*` 训练 pending 的二次确认统一使用 paper-only 阈值：量比最低 0.9、VWAP 下沿 0.99、VWAP 偏离最高 1.05、冲高回落容忍 4.0%、近期过热阈值放宽；这些参数统一写在 `config/strategy_config.json` 的 `paper_strong_entry_experiment`。主账户仍使用原始入场确认阈值。
- 影子弱市门禁实验只在 `paper_weak_market_gate_experiment.enabled=true` 且账户为 `paper_main/paper_watchlist` 时生效：弱市样本门槛从 30 下探到 25，弱市当前涨幅观察带扩到 5.0%。它覆盖所有 `paper_*` 训练 pending；主账户 `main/watchlist` 仍使用 `weak_market_entry_gate` 原始门禁。
- `paper_weak_market_gate_experiment.max_per_day=2` 已在哨兵买入前生效：只有真正命中 `PAPER_WEAK_SAMPLE_FLOOR_USED / PAPER_WEAK_CHASE_BAND_USED` 的 paper 弱市放宽样本才计入每日上限；普通 paper 样本和主账户不受此限流影响。
- 影子弱市门禁实验只解决训练采样不足和弱市强票误杀复核，不绕过数据质量、权限矩阵、涨停不可成交、VWAP/量能二次确认、T+1 卖出限制。命中时必须写入 `PAPER_WEAK_SAMPLE_FLOOR_EXPERIMENT / PAPER_WEAK_SAMPLE_FLOOR_USED / PAPER_WEAK_CHASE_BAND_EXPERIMENT / PAPER_WEAK_CHASE_BAND_USED` 标签，后续以 `SIM_TRADE_BUY account=paper_main/paper_watchlist` 和卖出闭环验收。
- 影子强票仍然必须满足成交可行性：一字板涨停、开高低收几乎没有日内价格区间、现实中排队买不到的票，只记录 `PAPER_STRONG_UNFILLABLE_LIMIT_UP`，不模拟买入。
- 影子账户只为训练样本服务，`paper_*` 使用单独仓位参数：基础仓位 10%、最小订单 5000、单票上限 20%；弱市/暴雨低仓确认下允许一手兜底 `ensure_round_lot_when_cash_available=true`，用于减少 `budget too small`，主账户默认不启用该兜底。
- 2026-07-06 至 2026-07-09 日志复盘显示：影子入队、复核、买入、卖出闭环已经跑通，但 7/8、7/9 的 0 买入主要来自 `consecutive_losses=3 hard_stop` 把 paper 仓位预算压到 0。训练口径调整为：主账户连续亏损仍硬停，`paper_*` 连续亏损只降仓到 70%，并把一手兜底门槛降到 `min_order_position_pct_floor=0.001`，确保影子继续产生买卖成绩样本。
- 同轮复盘还显示：`午盘精选` paper pending 在 B2/B3 曾继承主账户 `B4/B5` 窗口，产生大量 `window not allowed`。训练口径调整为：所有 `paper_*` 训练 pending 统一优先使用 `paper_all_pool_execution.windows`；主账户仍使用 `entry_policy.models.dynamic_window.strategy_windows`。
- 同轮复盘的下一层阻断显示：部分普通 `paper_*` pending 虽然进入了 paper 窗口，但仍使用主账户 1.8 量比 / 0.995 VWAP 下沿，导致影子样本继续偏少。训练口径调整为：所有 `paper_*` 训练 pending 统一进入 `PAPER_TRAINING_PENDING_CHECK`，使用 paper-only 二次确认阈值；一字板/触板不可成交仍只记录不可成交，不模拟买入。
- 若日志出现 `Duplicate entry 'xxxx' for key 'positions.code'`，说明生产库还残留旧版 `positions(code)` 唯一键。系统启动时会自动迁移为 `PRIMARY KEY (code, account)` 并移除单列 `code` 唯一索引，保证 `paper_main` 与 `paper_watchlist` 可独立持有同一代码。
- 为避免影子刚买 1-2 笔后因现金不足停止采样，`paper_account.training_cash_target=100000`、`training_cash_floor=100000`；每天盘前执行 P10 校准 paper 模拟现金，不影响 `main/watchlist`。
- 影子卖出使用 `paper_exit_policy` 短线训练口径：盈利 2.5% 直接全平，亏损 2.5% 止损，持仓满 2 天且收益不足 1.5% 出清。主账户仍使用原来的阶梯止盈和持仓周期。
- 哨兵每次复核 paper/main pending 时必须写入 `pending_entry_check_events`，记录 `SKIP / UNFILLABLE / EXPIRED / CANCELLED / BOUGHT / ERROR`、价格、涨幅、量比、VWAP 偏离和原因；模拟仓日报会汇总“动态入场复核事件”，用于解释影子为什么买/没买。
- 如果后续训练调整影子强票买入逻辑、弱市 paper 门禁、paper 仓位、paper 现金、paper 卖出或任务课表，必须同步更新 `paper_strong_entry_experiment`、`paper_weak_market_gate_experiment`、`paper_all_pool_execution`、`paper_account`、`paper_exit_policy`、`position_sizer`、本节说明，以及 P02-P11 影子任务；不能只改代码或只改 OpenClaw 任务。

| 序号 | 任务名 | 执行时间段 | 完整命令 | 账户范围 | 邮件 | 启用 | 说明 |
|---|---|---|---|---|---|---|---|
| P01 | StockAnalyzer 影子哨兵 | 周一至周五 09:26 起每 10 分钟，至 15:56 结束 | `cd <project-root> && ./run_openclaw.sh monitor.py --paper-only` | `paper_main/paper_watchlist` | 发 | 必须启用 | 只消费 paper PENDING，只管理 paper 持仓 |
| P02 | StockAnalyzer 影子早盘全池入队 | 周一至周五 09:27 | `cd <project-root> && ./run_openclaw.sh main.py --mode pre_market --queue-entry --paper-trade` | `paper_main` | 发 | 必须启用 | 影子集合竞价、冷启动、龙头跟踪、技术突破全部入可买 PENDING，不触碰主账户 |
| P03 | StockAnalyzer 影子备选池开盘入队 | 周一至周五 09:32 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 影子备选池开盘候选入队 |
| P04 | StockAnalyzer 影子备选池上午巡航一 | 周一至周五 10:01 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 影子捕捉早盘二次确认 |
| P05 | StockAnalyzer 影子备选池上午巡航二 | 周一至周五 11:21 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 影子午前复核备选池，避开 11:30 午间非交易窗口 |
| P06 | StockAnalyzer 影子备选池下午巡航一 | 周一至周五 13:01 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 影子捕捉午后修复 |
| P07 | StockAnalyzer 影子备选池下午巡航二 | 周一至周五 14:01 | `cd <project-root> && ./run_openclaw.sh main.py --mode watchlist --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 影子午后最后一轮备选池确认 |
| P08 | StockAnalyzer 影子午盘资金流入队 | 周一至周五 14:31 | `cd <project-root> && ./run_openclaw.sh main.py --mode afternoon --queue-entry --paper-trade` | `paper_main` | 发 | 必须启用 | 影子资金流窗口候选入队 |
| P09 | StockAnalyzer 影子重点雷达入队 | 周一至周五 09:35-15:00 每 30 分钟 | `cd <project-root> && ./run_openclaw.sh main.py --mode focus_monitor --queue-entry --paper-trade` | `paper_watchlist` | 发 | 必须启用 | 全天重点雷达的强盯/重点票进入 paper 可买 PENDING，补齐龙头/冷启动/技术突破的买卖样本 |
| P10 | StockAnalyzer 影子训练现金校准 | 周一至周五 09:05 | `cd <project-root> && ./run_openclaw.sh tools/maintenance/ensure_paper_training_cash.py --email --db-timeout 5` | `paper_main/paper_watchlist` | 发 | 必须启用 | 若 paper 现金低于训练下限，则校准到目标训练现金，避免影子因预算过小停止采样；默认写入 `reports/paper_training_cash/latest.json`，`overall=OK` 才允许继续 P11；失败时不得继续把当天当成有效训练样本 |
| P11 | StockAnalyzer 影子训练盘前预检 | 周一至周五 09:06 | `cd <project-root> && ./run_openclaw.sh tools/diagnostics/check_shadow_training_preflight.py --check-db-cash --email --assert-ready --db-timeout 5` | `paper_main/paper_watchlist` | 发 | 必须启用 | 只读检查新版部署、paper 现金、冷却节奏、一手兜底和严格日志验收能力；默认写入 `reports/shadow_training_preflight/latest.json`，`overall=PASS` 才允许把后续 paper 买卖计入训练样本 |

### 2.4 观察、训练、进化课表（必须按用途启用）

这些任务不直接承担主账户买卖，但负责样本观察、策略研究、周末进化和标签链路审计。它们必须独立排班，不要伪装成主账户交易任务。

| 序号 | 任务名 | 执行时间段 | 完整命令 | 账户范围 | 邮件 | 启用 | 说明 |
|---|---|---|---|---|---|---|---|
| R01 | StockAnalyzer 全天重点雷达只读复核 | 按需或排查时执行 | `cd <project-root> && ./run_openclaw.sh main.py --mode focus_monitor` | 观察/影子审计 | 发 | 按需启用 | 只读观察重点标的，并写入影子审计样本；常规交易日应优先执行 P09，让强票进入 paper 可买验证 |
| R02 | StockAnalyzer 冷启动标签审计 | 周一至周五 16:10 | `cd <project-root> && ./run_openclaw.sh tools/audit_cold_start_tags.py --date $(date +%Y-%m-%d) --json` | 标签链路 | 不强制 | 必须启用 | 检查 pending、positions、transactions 的冷启动标签追溯 |
| R03 | StockAnalyzer 周度策略权重分析报告 | 每周一 19:00 | `cd <project-root> && ./run_openclaw.sh scripts/weekly_audit.py` | 策略样本 | 发 | 必须启用 | 输出周度策略权重建议，不直接修改实盘配置 |
| R04 | StockAnalyzer 买入权重优化调研 | 每周二、周四、周六 16:00 | `cd <project-root> && ./run_openclaw.sh tools/research/strategy_research.py` | 策略样本 | 发 | 必须启用 | 分析近 7 天策略表现；注意真实脚本路径在 `tools/research/` |
| R05 | StockAnalyzer 周末策略进化 | 每周六 10:00 | `cd <project-root> && ./run_openclaw.sh -m core.evolve_strategy` | 策略配置建议 | 发 | 必须启用 | 生成权限、门禁、窗口建议；默认不自动扩大实盘权限 |
| R06 | MarketSignalTraining L2 每日训练跟进 | 周一至周五 15:55 | `cd <training-workspace> && TRADE_DATE=$(date +%F) && .venv/bin/python training/run_daily_training_followup.py --start-date "$TRADE_DATE" --end-date "$TRADE_DATE" --main-log-dir <project-root>/logs --env-file <project-root>/.env.openclaw --output-dir "training/artifacts/outputs/$TRADE_DATE" --report-dir "training/docs/reports/generated/$TRADE_DATE" --force-audits` | 训练仓只读 | 不强制 | 必须启用 | 消费主项目当日日志和模拟仓日报，刷新 L2/L3 训练状态；不执行交易、不改门禁 |
| R07 | StockAnalyzer 影子链路诊断 | 周一至周五 15:50 | `cd <project-root> && ./run_openclaw.sh tools/diagnostics/check_pending_entry_events.py --date $(date +%F) --accounts paper_main,paper_watchlist --json` | paper 证据链 | 不强制 | 必须启用 | 检查 paper pending、复核事件、买入成交和未买原因，确认影子链路真实落库 |
| R08 | StockAnalyzer 影子日志训练复盘 | 周一至周五 15:52；或 MySQL 超时时按需执行 | `cd <project-root> && ./run_openclaw.sh tools/diagnostics/analyze_shadow_training_logs.py --date $(date +%F) --expect-bypass --email --assert-healthy --strict` | paper 日志证据 | 发 | 必须启用 | 纯日志复盘 paper 入队、复核、买卖、跳过原因、门禁拦截，并输出 P01-P11 影子任务覆盖、PASS/WARN/FAIL 训练验收和下一步建议；严格模式下 WARN/FAIL 必须返回非 0，不能当作训练完成 |
| R09 | StockAnalyzer MySQL 训练前置诊断 | P10/P11 失败时立即执行；也可周一至周五 09:04 盘前执行 | `cd <project-root> && ./run_openclaw.sh tools/diagnostics/check_mysql_connectivity.py --db-timeout 5 --email --assert-ready` | paper 数据库前置 | 发 | 建议启用 | 分层诊断 TCP、MySQL 握手、基础查询、表状态、paper 现金和可选写入探针；默认写入 `reports/mysql_preflight/latest.json`，用于区分网络不通、MySQL 握手失败、查询失败、现金不足，避免把数据库前置问题误判成策略问题 |

> 注意：R08 的影子训练验收只对交易日日志有效。周六/周日的周末进化、研究调研、策略周报日志应标记为 `N/A`，不能因为没有 P01-P11、paper 入队或 paper 买入就判定影子系统失败，也不能据此调整买入门槛、预算、冷却或任务课表。

### 2.4.1 影子链路上线验收口径

影子全池和重点雷达上线后，交易日必须同时满足以下证据，才算链路跑通：

- `PAPER_ALL_POOL_PENDING_ROUTE` 或 `PAPER_FOCUS_PENDING_CREATED` 出现在日志中，说明候选已经进入 paper 可买池。
- `pending_entry_check_events` 当天存在记录，且 `decision` 至少覆盖 `SKIP / UNFILLABLE / BOUGHT / EXPIRED / CANCELLED` 中的一类真实结果。
- 模拟仓日报邮件出现“动态入场复核事件”表，能看到主要未买原因。
- 15:30 `main.py --mode audit` 的交易质量审计出现“动态入场复核事件”章节，能按账户、策略、决策汇总最近 7 天复核结果。
- `tools/diagnostics/check_pending_entry_events.py --date YYYY-MM-DD --accounts paper_main,paper_watchlist --json` 的 `coverage.event_total > 0`；如果 `coverage.pending_total > 0` 但 `event_total = 0`，说明哨兵没有消费 pending 或事件表写入失败。
- 一字板涨停不可成交票只允许出现 `UNFILLABLE` 或 `PAPER_STRONG_UNFILLABLE_LIMIT_UP`，不能出现模拟买入。

### 2.4.2 2026-07-07 至 2026-07-10 影子训练复盘结论

这四个交易日日志显示：OpenClaw 课表不是主要问题，P01/P02/P03-P07/P08/P09/P10 影子任务覆盖均为 PASS；影子入队和哨兵复核也足够。新增 P11 后，后续交易日必须把盘前预检也纳入覆盖验收。训练未达标的核心原因是执行端样本损耗：

| 日期 | paper 入队 | 哨兵复核 | 统一阈值复核 | 影子买入 | 主要阻断 |
|---|---:|---:|---:|---:|---|
| 2026-07-07 | 20 | 95 | 0 | 0 | 窗口不允许 271、复核冷却 103、预算不足 18 |
| 2026-07-08 | 81 | 189 | 0 | 0 | 复核冷却 198、预算不足 54、一字板不可成交 20 |
| 2026-07-09 | 80 | 179 | 0 | 0 | 复核冷却 169、预算不足 94 |
| 2026-07-10 | 61 | 100 | 0 | 0 | 复核冷却 91、预算不足 67 |

训练调整方向：

- 不再继续盲目扩大选股池；当前瓶颈不在候选数量。
- 必须先验证新版 `monitor.py` 是否在运行日志里产生 `PAPER_TRAINING_PENDING_CHECK`。
- 必须验证 P10 影子训练现金校准、paper 一手兜底、paper 连续亏损不硬停、paper-only 扫描间隔跟随冷却是否真正部署生效，把 `budget too small` 压到每日不超过 3，把 `retry cooldown` 压到每日不超过 30。
- 必须在 P10 后执行 P11 盘前预检；如果 `check_shadow_training_preflight.py --check-db-cash --assert-ready --db-timeout 5` 失败，说明部署/配置/现金状态未就绪，不能把当天后续影子训练失败解释成策略失败。
- R09/P10/P11 必须全部落盘机器可读 JSON：`reports/mysql_preflight/latest.json`、`reports/paper_training_cash/latest.json`、`reports/shadow_training_preflight/latest.json`。训练仓库每日跟进会读取三者生成 `shadow_preflight_chain_acceptance_*.json/md`；只有链路 `ready=true`，当天 paper 买卖才允许抵扣训练样本。
- R09/P10/P11 必须通过 `./run_openclaw.sh ...` 执行，不能直接用 `venv/bin/python ...` 的结果作为 OpenClaw 结论；`run_openclaw.sh` 会加载 `.env.openclaw`，否则可能误用默认 DB 用户或空密码，导致诊断层级失真。
- 2026-07-12 本地实测：P11 的代码、配置、窗口、paper 一手兜底、连续亏损不硬停、短线卖出策略、严格日志验收全部 PASS；阻断层为 R09 `MYSQL_HANDSHAKE`，P11 因数据库现金读取失败而 FAIL。下一轮训练必须先让 OpenClaw 运行环境带上正确 MySQL 账号/密码并让 R09/P11 PASS，再看交易日买卖样本，不继续放宽策略门槛。
- 必须保持主账户门禁严格；本轮只修 `paper_main/paper_watchlist` 的训练样本闭环。
- R08 必须使用 `--assert-healthy --strict`，否则 WARN 会被误当成任务成功。
- 周末/非交易日日志只检查研究、进化和邮件是否执行；影子训练验收应显示 `N/A`，下一步仍然等待真实交易日运行日志确认。

### 2.5 外部独立任务（不属于本仓库主课表）

这些任务可以在 OpenClaw 里保留，但必须标成外部独立任务，不能混进 `market-signal-lab` 主课表统计。

| 序号 | 任务名 | 执行时间段 | 完整命令 | 账户范围 | 邮件 | 启用 | 说明 |
|---|---|---|---|---|---|---|---|
| X01 | 002182 宝武镁业 T+0 监控 | 交易日每 30 分钟 | `cd /home/pi/.openclaw/workspace/memory && bash check_002182.sh` | 外部脚本 | 外部决定 | 按需启用 | 单票持仓监控，卖点/买点/止损检查；不归 market-signal-lab 主链路 |

### 2.6 必须停用或删除的错误任务

OpenClaw 里如果存在下面任务，必须停用或删除：

| 错误任务 | 错误原因 | 正确处理 |
|---|---|---|
| `cd <project-root> && ./run_openclaw.sh scripts/strategy_research.py` | 仓库没有这个脚本路径 | 改成 `tools/research/strategy_research.py` |
| `[知识学习] 量化交易前沿研究` 但命令仍是 `scripts/weekly_audit.py` | 名称和实际脚本不一致，会造成任务含义错误和重复周报 | 删除，或等有真实研究脚本后再新增 |
| 旧版 `备选上午监控 11:00` | 当前权威课表使用 10:00 / 11:20 / 13:00 / 14:00 | 删除 11:00，避免重复巡航 |
| 旧版 `午间体检报告` | 当前没有独立脚本，容易和 watchlist 巡航混淆 | 删除，除非未来新增明确脚本 |
| 重复的主哨兵 `monitor.py` | 多条主哨兵会重复发邮件、重复扫描、增加锁冲突 | 只保留 M03 |
| 重复的影子哨兵 `monitor.py --paper-only` | 多条影子哨兵会重复扫描 paper pending | 只保留 P01 |
| 任意正式任务带 `--no-email` | 正式运行需要通知闭环 | 删除 `--no-email`，只在人工自检时使用 |
| 任意 OpenClaw AgentTurn 任务带 `--auto-trade` | 可能被 Agent 安全策略拒绝，Cron 显示 ok 但程序没启动 | 改成 `--queue-entry`，由 monitor 动态确认 |

### 2.7 训练更新后的课表维护规则

训练项目或周末进化只要引入以下变化，必须同步修改本节：

- 新增策略入口，例如新增 `main.py --mode xxx`。
- 改变策略运行时间，例如把备选池巡航从 10:00 改到 10:30。
- 改变账户路由，例如主账户改 paper、paper 改主账户、或新增账户。
- 改变通知口径，例如某任务从发邮件改为只写日志。
- 改变脚本路径，例如从 `scripts/` 移到 `tools/research/`。
- 改变执行风险，例如从只读观察改为可创建 PENDING，或从 PENDING 改为可买卖。

更新格式必须包含：

```text
日期：
变更原因：
新增任务：
删除任务：
修改任务：
是否影响主账户：
是否影响影子账户：
是否影响邮件通知：
OpenClaw 是否已同步：
```

如果训练更新只改模型、阈值或权重，但不改变任务时间、命令、账户、通知，也要在变更说明里写：`不涉及 OpenClaw 课表变更`。

#### 2026-07-05 训练承接变更记录

```text
日期：2026-07-05
变更原因：7/2-7/3 影子系统已经证明选股/重点雷达能发现强票，但 paper 买入样本仍不足；训练侧要求扩大 paper-only 可执行样本，同时保持主账户严格。
新增任务：无。
删除任务：无。
修改任务：无。本次不改变 M01-M15 / P01-P11 / T01-T05 的执行时间、命令和邮件口径。
是否影响主账户：不放宽真实买入；main/watchlist 仍受数据质量、权限矩阵、VWAP、量比、追高、涨停不可成交、T+1 等门禁约束。暴雨环境只允许确认队列探针，不直接买入。
是否影响影子账户：影响。paper_main/paper_watchlist 增加 paper 弱市门禁实验、行业/板块过滤绕过审计、更高复核频率、paper 一手兜底和更高训练现金下限。
是否影响邮件通知：不改变任务邮件开关；新增诊断报告可按需用 `--email` 发送。
OpenClaw 是否已同步：部署后必须用本文 0.1 的诊断命令和 3.4 的回放命令确认；未看到 PAPER_WEAK / PAPER_*_BYPASS / SIM_TRADE_BUY 或 FINAL_BLOCKER 前，不得把本次部署视为运行验收通过。
```

#### 2026-07-12 影子训练盘前预检变更记录

```text
日期：2026-07-12
变更原因：7/10 最新有效交易日日志显示 paper 入队和哨兵复核已经足够，但线上仍未出现 PAPER_TRAINING_PENDING_CHECK，且 budget too small / retry cooldown 偏高。下一轮训练需要在开盘前先确认部署、现金和冷却配置，不再等收盘后才发现旧代码或现金不足。
新增任务：P11 StockAnalyzer 影子训练盘前预检，周一至周五 09:06 执行 `./run_openclaw.sh tools/diagnostics/check_shadow_training_preflight.py --check-db-cash --email --assert-ready --db-timeout 5`。
删除任务：无。
修改任务：R08 影子日志训练复盘的覆盖验收从 P01-P10 升级为 P01-P11；P10 现金校准后必须紧跟 P11 盘前预检。
是否影响主账户：不影响。不放宽 main/watchlist 的真实买入、VWAP、量比、追高、涨停不可成交、权限矩阵或 T+1 卖出限制。
是否影响影子账户：影响。P11 只读检查 paper_main/paper_watchlist 的新版部署、训练现金、冷却节奏、一手兜底、连续亏损不硬停和严格日志验收能力；失败时先修部署/现金，不调整策略门槛。
是否影响邮件通知：影响。P11 必须发送 PASS/WARN/FAIL 邮件，用于开盘前确认影子训练是否具备采样条件。
OpenClaw 是否已同步：部署后必须看到 P10 现金校准邮件和 P11 盘前预检 PASS 邮件；若 P11 失败或 paper 现金不可读，当天影子训练失败不能解释成策略失败。
```

#### 2026-07-12 影子训练现金校准实测记录

```text
日期：2026-07-12
变更原因：7/8-7/10 日志显示影子入队和哨兵复核足够，但 budget too small 分别达到 54 / 94 / 67，是影子买入样本不足的主要阻断之一。P10 需要在 MySQL 偶发断连时更清楚地重试、报告和返回失败，避免静默失败。
新增任务：无。
删除任务：无。
修改任务：P10 时间不变，命令增加 `--db-timeout 5`；工具内部改为短连接读写，不再先跑完整 `PortfolioManager.init_tables()`，读现金失败时仍尝试写入训练目标现金，写入失败则返回 ERROR 并在邮件中标明失败账户、尝试次数和原因。
是否影响主账户：不影响。P10 只允许处理 paper_* 账户，非 paper 账户会 SKIP。
是否影响影子账户：影响。P10 同时校准 accounts.initial_capital 和 portfolio_value.cash/total_value，目标为 paper_account.training_cash_target=100000，下限为 training_cash_floor=100000。
是否影响邮件通知：影响。P10 邮件表格必须显示 状态 / 尝试 / 原因；若任一 paper 账户 ERROR，命令返回非 0。P11 如果 paper 现金不可读或低于下限，必须返回 FAIL。
OpenClaw 是否已同步：下一交易日 09:05 必须先看到 P10 OK 邮件，09:06 必须看到 P11 PASS 邮件。若 P10/P11 因 MySQL 连接、现金不可读或现金不足失败，当天不算有效影子训练日，不继续调大买入门槛、不关闭影子哨兵、不解释为策略选股失败。
```

#### 2026-07-12 MySQL 训练前置诊断记录

```text
日期：2026-07-12
变更原因：P10/P11 本地实测显示 paper 现金不可读，直接影响影子买入预算和训练样本入账；需要在继续调策略前先区分 TCP、MySQL 握手、基础查询、表状态和 paper 现金问题。
新增任务：R09 StockAnalyzer MySQL 训练前置诊断，命令为 `./run_openclaw.sh tools/diagnostics/check_mysql_connectivity.py --db-timeout 5 --email --assert-ready`，P10/P11 失败时立即执行，也可放在 09:04 盘前预诊断。
删除任务：无。
修改任务：无。不改变 M01-M15 / P01-P11 / R01-R08 的既有时间和账户路由。
是否影响主账户：不影响。R09 只读诊断为主，默认不写入交易数据；可选 `--write-probe` 也只做回滚写入探针。
是否影响影子账户：影响训练前置判断。R09 若 FAIL，当天不能把 `budget too small` 或 P11 现金失败解释为策略失败。
是否影响邮件通知：影响。R09 使用 `--email` 发送 PASS/FAIL 诊断邮件，作为 OpenClaw 盘前排障依据。
OpenClaw 是否已同步：部署后若 P10/P11 失败，必须先看 R09 邮件；TCP PASS 但 MySQL 握手 FAIL 时优先修 MySQL 服务/连接/权限/白名单，不调买入门槛。
```

### 2.8 开盘前额外校验（人工执行，不是日常 cron）

开盘前需要人工确认时，才执行下面四条；它们不加入日常定时任务：

```bash
cd <project-root> && ./run_openclaw.sh main.py --mode risk_dashboard --no-email
cd <project-root> && ./run_openclaw.sh main.py --mode audit --no-email
cd <project-root> && ./run_openclaw.sh main.py --mode replay_day --date 20260529 --strategy 集合竞价 --no-email
cd <project-root> && ./run_openclaw.sh monitor.py --once --dry-run --no-email --force
```

### 2.9 OpenClaw AgentTurn 与 `--auto-trade`

2026-06-04 和 2026-06-05 的 09:26 任务暴露出一个调度层问题：如果 OpenClaw 以 AgentTurn 方式执行带 `--auto-trade` 的命令，Agent 可能按高影响金融操作策略拒绝执行。此时 Cron 任务可能仍显示 `ok`，但主项目日志不会出现 `pre_market` 启动记录。

业务代码已经拆分成两个意图：
- `--queue-entry`：只做候选入库、pre-trade gate、PENDING 入队、风险事件审计；写完后直接返回，不会加载 pending、不做实时确认、不做仓位预算、不调用 `execute_buy`。
- `--auto-trade`：完整执行模式，既会创建/消费 PENDING，也可能在验证通过后买入；不放入 AgentTurn cron。
- 极端高风险天气或 `attack_window_gate` 禁止当前窗口时，`--queue-entry` 不再整轮静默退出：仍跑候选并逐只写 `BUY_BLOCKED_PRE_TRADE_GATE`，但不创建 PENDING，用于保留训练/审计样本。

直接去掉 `--auto-trade` 仍只是只读观察方案：
- `main.py` 会继续选股、写 `strategy_selection`、发报告。
- `execute_automated_strategies()` 会在入口处直接返回，不会执行 pre-trade gate、不会创建 `pending_entry_signals(status=PENDING)`、不会写候选级 `BUY_BLOCKED_PRE_TRADE_GATE`。
- `monitor.py` 只轮询 `pending_entry_signals` 中的 PENDING，不会从 `strategy_selection` 自动补建 pending。

因此，要保留“09:26 候选入池 → 09:30 后哨兵动态确认”的业务意图，AgentTurn cron 使用 `--queue-entry`：

```bash
./run_openclaw.sh main.py --mode pre_market --queue-entry
./run_openclaw.sh main.py --mode watchlist --queue-entry
./run_openclaw.sh main.py --mode afternoon --queue-entry
```

### 2.5 课表同步检查

每次 OpenClaw 调整任务后，都要先检查本节是否仍然一致，再去更新调度器。判定标准：

- 新增/删除任务是否已经同步到表格
- 时间是否唯一且不冲突
- 主账户、影子账户、外部任务是否分开
- 邮件通知是否和文档一致
- 旧任务是否已经删除

如果要验证某条命令是否真的跑起来，以 `logs/stock_analyzer-YYYYMMDD.log` 里是否出现对应 `Starting Market Signal Lab in ... mode` 或 monitor 启动日志为准，不只看 Cron 的 `ok` 状态。

只读观察和人工校验命令仍然保留在后文“开盘前额外校验”里，不作为日常 cron。

---

## 3. 系统的“进攻/防守”总开关（必须理解）

### 3.1 自动交易开关
- 是否自动买入：由命令行 `--auto-trade` 控制；OpenClaw AgentTurn cron 不配置该参数
- 是否只创建动态入场 PENDING：由 `--queue-entry` 控制；该模式不会调用 `execute_buy`
- 如果两个参数同时出现，`--queue-entry` 优先生效，本次只入队不买入
- 是否发邮件：默认发；如需静默，用 `--no-email`

### 3.2 市场天气（weather）与风控矩阵
系统会在交易执行前调用 `check_market_environment()` 生成市场环境：
- ☀️晴天 / ☁️多云 / ⚠️暴雨（会归一化）
- 暴雨（high risk）停止直接买入；主账户只允许进入“小仓确认 PENDING”探针，每轮最多 1 只，仍必须由哨兵二次确认后才可能成交。

对应参数在 `strategy_config.json`：
- `weather_risk`（止损、止盈、回撤锁定）

天气/窗口门禁执行位置：
- `--queue-entry` 入队前先看 `market_env`、`attack_window_gate` 和权限矩阵；`risk_level=high` 时默认只写阻断审计；若 `weak_market_entry_gate.allow_storm_pending_probe=true`，则按 `strategy_permission_matrix.storm_market` 仅允许确认类 PENDING，且主账户每轮最多 `storm_pending_probe_max_per_run` 只。
- `monitor.py` 消费 PENDING 前会重新获取最新 `market_env`，再跑一次 pre-trade gate。
- 如果 `attack_window_gate.enabled=true`，`monitor.py` 会按 `weather × bucket` 二次拦截；被拦截的 PENDING 只记录原因，不执行买入。
- 仓位预算会吃到 `market_env.adjustments.max_position_mult`，再叠加 regime、波动率、权限动作、持仓数量、日内亏损/连亏等乘数。

### 3.3 买入前的硬门槛（资金/追高/数据质量）
自动买入前会经过多层门禁：

- `strategy_permission_matrix`：按 regime × 策略决定 AUTO / CONFIRM / OBSERVE / BLOCK。
- `normal_uptrend` 当前已把 `盘后资金流` 从 `OBSERVE` 提升为 `CONFIRM_ONLY`，表示这条链路已可进入实盘确认，不再只是纯观察。
- `weak_market_entry_gate`：弱市早盘、样本不足、接力高开、追高候选禁止直接自动买入；高开阈值仍保守，当前涨幅阈值放宽用于减少强票误杀；冷启动/龙头/午盘强票保留低仓确认链路。
- `data_quality_gate`：价格/昨收缺失禁止自动买，量额缺失或实时 fallback 只允许确认链路。
- `sector_rotation`：弱市优先强行业，行业内前排龙头加标签；行业 UNKNOWN 不再一刀切拒绝，改为降权后进入确认链路。
- `entry_confirm`：pending 动态入场时检查 VWAP、开盘区间、冲高回落、二次放量，以及执行前近期过热复核。
- `position_sizer`：按 regime、ATR、权限动作、持仓上限、当日亏损和连续亏损决定仓位。

### 3.4 周末自进化后的执行口径（2026-06-28）

这轮周末训练不再“一股脑全上”，而是拆成两类：

- 可上实盘：`盘后资金流` 的确认链路，已允许进入 `CONFIRM_ONLY`。
- 可上影子：`集合竞价` 进入 `shadow_pending`，继续走 `audit_only`，只写影子样本，不进真实买入队列。

2026-07-03 根据 7/2、7/3 日志修正“选出强票但执行端过窄”的问题：

- `weak_market_entry_gate.weak_max_open_change = 2.5`
- `weak_market_entry_gate.weak_max_change = 4.5`
- `weak_market_entry_gate.allow_storm_pending_probe = true`
- `weak_market_entry_gate.storm_pending_probe_max_per_run = 1`
- `paper_weak_market_gate_experiment.sample_floor_override = 25`
- `paper_weak_market_gate_experiment.weak_chase_override_pct = 5.0`
- `sector_rotation.weak_market_unknown_sector_action = PENALTY`

当前结论很简单：

- 影子继续扩大买卖样本，主账户只做强票确认探针。
- 暴雨不再“选出来也完全不排队”，但仍不允许直接买入。
- 周末进化不得把 `weak_max_change` 机械回落到 2.5；若收益/胜率或 T+1 风险偏高，只收紧高开阈值，当前涨幅阈值不低于配置里的 `weak_max_change_floor`。

离线验证命令：

```bash
cd <project-root> && ./run_openclaw.sh tools/diagnostics/replay_pending_gate.py --date YYYY-MM-DD --accounts main,watchlist,paper_main,paper_watchlist --now 'YYYY-MM-DD 10:40:00'
```

用途：把当日 `pending_entry_check_events` 的最后一次有效检查行情，按当前 `pre_trade_gate` / 权限矩阵重算，判断旧阻断是否会变成新确认。它只做复盘，不改数据库、不下单。

如果 MySQL 连接不稳定，先不要停止训练，改用纯日志复盘：

```bash
cd <project-root> && ./run_openclaw.sh tools/diagnostics/analyze_shadow_training_logs.py --date YYYY-MM-DD
```

用途：从 `logs/stock_analyzer-YYYYMMDD.log` 汇总 `PAPER_FOCUS_PENDING_CREATED`、`PAPER_STRONG_PENDING_CREATED`、`PENDING_CHECK`、`PENDING_SKIP`、`SIM_TRADE_BUY`、`SIM_TRADE_SELL`、`PAPER_SECTOR_FILTER_BYPASS`、`PAPER_BOARD_FILTER_BYPASS` 和主要阻断原因。它不依赖数据库，不改任何交易状态。

下一交易日验收命令：

```bash
cd <project-root> && ./run_openclaw.sh tools/diagnostics/check_shadow_training_preflight.py --check-db-cash --email --assert-ready --db-timeout 5
cd <project-root> && ./run_openclaw.sh tools/diagnostics/analyze_shadow_training_logs.py --date YYYY-MM-DD --expect-bypass --email --assert-healthy --strict
```

验收含义：

- `PASS`：paper 入队、复核、买入样本和主要卡点都达到当前阈值，严格验收命令返回 0。
- `WARN`：基础链路已跑通，但买入数、预算不足、窗口不允许或复核冷却仍未达训练目标；严格验收命令必须返回非 0。
- `FAIL`：paper 入队或哨兵复核不足，说明影子链路本身没有跑通；严格验收命令必须返回非 0。

当前默认阈值：paper 入队不少于 10、复核不少于 10、买入目标不少于 1、`budget too small` 不超过 3、`窗口不允许` 不超过 10、`复核冷却` 不超过 30，并要求 P01/P02/P03-P07/P08/P09/P10/P11 至少在日志中出现一次。`--expect-bypass` 用于检查新版 `PAPER_SECTOR_FILTER_BYPASS / PAPER_BOARD_FILTER_BYPASS` 是否真的在交易日出现。

验收邮件会附带“下一步建议”。OpenClaw 处理口径：

- `paper 入队不足`：先检查 P02-P09 是否启用，不要改买入门槛。
- `影子任务覆盖不足`：先修 OpenClaw 课表或日志同步，不要把“任务没跑”误判成“策略没买”。
- `哨兵复核不足`：先检查 P01 paper-only 哨兵频率和运行时间。
- `影子买入不足`：先看量比、VWAP、近期过热、一字板不可成交，不直接放宽主账户。
- `预算不足偏高`：先确认 P10 影子现金校准、paper 一手兜底、`paper_*` 连续亏损降仓是否生效；不要把 paper 连续亏损硬停回滚成 0 仓位。
- `P10 现金校准 ERROR`：先修 MySQL 连接或数据库写入权限，并确认 `paper_main/paper_watchlist` 现金能被写到 100,000；P10 未 OK 前，不要把当天 `budget too small` 当成策略问题。
- `盘前预检失败`：先确认 OpenClaw 已部署新版 `monitor.py`、`config/strategy_config.json`、P10/P11 任务，以及 paper 现金可读且已达训练下限；不要继续调大 paper 买入门槛或关闭影子哨兵。
- `MySQL 连接异常`：立即执行 `tools/diagnostics/check_mysql_connectivity.py --db-timeout 5 --email --assert-ready`。如果 TCP PASS 但 MySQL 握手登录 FAIL，优先检查 MySQL 服务状态、连接数、账号权限、bind-address、SSL/认证插件和来源 IP 白名单；不要继续改策略阈值。
- `复核冷却偏高`：先确认 `paper_all_pool_execution.scan_interval_sec` 不低于 `retry_cooldown_sec`，并确认线上运行的是新版 `monitor.py`；不要通过关闭 paper 哨兵来消除噪声。
- `窗口不允许偏高`：先检查 `paper_all_pool_execution.windows`，尤其 `午盘精选` 是否覆盖 B2-B5。
- `未看到 paper 过滤绕过标签`：确认新版 `main.py` 已部署，并检查当天是否有行业/板块权限被主账户挡住的候选。

---

## 4. 选股策略与数据源（你在报告里看到的内容从哪里来）

### 4.1 早盘集合竞价（pre_market）
特点：**最进攻**，同时波动也最大。

数据与加分链路（优先级）：
- 官方竞价：`stk_auction`（仅当 `TUSHARE_STK_AUCTION_ENABLED=1` 且 token 有权限时启用）
- 昨日涨停/连板基因：`stk_limit`
- 个股资金流（主力/机构/游资）：`moneyflow_dc`（`moneyflow` 兜底）
- 实时补充：`rt_k` 实时日线聚合（`rt_min` 仅作分钟级/兜底）

当前生产口径：
- `stk_auction` 默认关闭；仅在 Token 具备对应权限时设置 `TUSHARE_STK_AUCTION_ENABLED=1`。
- 早盘集合竞价优先走官方竞价数据；若未来再次失去权限，系统会自动退回 `rt_k` 实时行情口径，不会停摆。
- 以后如果你要临时切回降级，只需把 `TUSHARE_STK_AUCTION_ENABLED=0`。

### 4.2 午盘资金流（afternoon）
特点：**确认型进攻**。

核心链路：
- 行业资金聚合（近 N 日）：`get_sector_rank_by_aggregated_flow()`
- 个股实时确认：`rt_k` 实时日线聚合
- 个股资金流过滤：`moneyflow_dc`（`moneyflow` 兜底）
- 叠加：龙虎榜（LHB）/概念（concept）标签（best-effort）

当前执行口径：
- `午盘精选` 暂不作为无脑进攻入口；弱市/震荡市走 `LOW_SIZE_CONFIRM`，继续观察候选质量。
- 候选可以进入 pending 动态确认，必须通过实时资金、VWAP、开盘区间/回落结构等检查后才允许买入。

### 4.2.1 冷启动（爆点观察）
特点：抓早盘突然起爆的个股，适合先发现、再确认，不适合弱市直接追入。

当前执行口径：
- 强势市场：按权限矩阵执行，仍受数据质量、资金、VWAP、仓位限制。
- 正常/震荡市场：默认 `CONFIRM_ONLY`，先进入动态确认。
- 弱势市场：`LOW_SIZE_CONFIRM`，保留 600869、603319 这类起涨点观察价值，但禁止直接自动买入。
- 暴雨/极端市场：`BLOCK`。

### 4.3 盘后资金复盘（post_market）
特点：次日博弈用的“弹药装填”，多为 T+2 验证。

核心链路：
- 今日行业资金聚合：`moneyflow_dc` → `get_sector_rank_by_aggregated_flow(days=1)`
- 收盘价/成交额过滤：`rt_k` 实时日线聚合
- 叠加：龙虎榜/概念标签（best-effort）

### 4.4 备选池巡航（watchlist）
特点：从入库候选里找“形态确认 + 资金确认”的买点。

核心链路：
- MA5/MA20 结构判断
- 资金流确认（净流入必须为正）
- 概念共振（best-effort）

#### 4.4.1 入库票观察生命周期（必须明确）

入库票的“表现验证”和“是否继续观察”是两条线，不能混用：

| 维度 | 字段/表 | 作用 | 是否决定继续观察 |
|---|---|---|---|
| T+1/T+2 表现验证 | `strategy_selection.zt_result` / `strategy_performance_history` | 记录涨停、吃肉、震荡、吃面，用于训练和策略权重复盘 | 否 |
| 观察生命周期 | `strategy_selection.observe_status` | 记录是否仍在观察、待入场、已买入、已剔除、已到期 | 是 |
| 观察原因轨迹 | `selection_observation_events` | 记录入库、继续观察、触发买点、剔除、到期、买入、验证结果等事件 | 用于审计和邮件解释 |

当前观察状态：

| 状态 | 中文含义 | 触发场景 |
|---|---|---|
| `ACTIVE` | 新入库待观察 | 候选写入 `strategy_selection` 时默认状态 |
| `WATCHING` | 继续观察 | 未触发买点，但结构尚未走坏 |
| `PENDING` | 已触发买点，等待动态入场 | 站上 MA5 且资金/量比确认，进入动态入场链路 |
| `BOUGHT` | 已买入 | 备选池候选买入成功 |
| `REMOVED` | 技术剔除 | 跌破 MA20、首板承接转弱等逻辑走坏 |
| `EXPIRED` | 观测期满停止观察 | 超过观察天数仍未触发买点，但不等价于技术走坏 |

“超出观测时限”的具体口径：
- 当前配置：`config/strategy_config.json -> watchlist.observe_days = 5`。
- 口径：超过 5 个自然日仍未触发动态入场，标记 `EXPIRED`，原因写入 `observe_end_reason`，并写事件表。
- 这只是时间到期，不代表个股已经进入下跌通道；它表示“这次入库逻辑没有在有效窗口内兑现买点”。
- 如果个股跌破 MA20 或首板相对入选价回撤超过阈值，则标记 `REMOVED`，这是“逻辑走坏/技术剔除”。
- `track` 任务只做 T+1/T+2 表现验证，不能因为写了“吃肉/震荡/吃面”就让票从观察池无声消失。

OpenClaw 任务要求：
- `09:31` 备选池开盘入队必须执行 `./run_openclaw.sh main.py --mode watchlist --queue-entry`。
- 备选池巡航邮件必须展示：买入信号、破位剔除、到期停止观察、继续观察。
- 后续训练或策略更新只要改变观察期、剔除条件、买点触发条件，必须同步更新本节和 `config/strategy_config.json`，不能只改代码或只改 OpenClaw 课表。

---

### 4.5 全天重点雷达（focus_monitor）
特点：把“值得盯的票”和“自动买入是否放行”拆开，避免选股表现很好但执行门禁过严时，邮件只剩“没买/被拦截”的结论。

执行周期：
- 默认不纳入日常常驻任务；需要补影子样本时再手动启用。
- 命令：`./run_openclaw.sh main.py --mode focus_monitor`
- 不加 `--auto-trade`；该模式不买入、不卖出、不改持仓/选股状态。
- 为补齐训练审计链路，该模式会按配置写入 `pending_entry_signals` 的 `SHADOW` 行：
  - `status=SHADOW`
  - `entry_model=audit_only_shadow`
  - `source_strategy=原策略_SHADOW`
  - 真实买入流程只读取 `status=PENDING`，因此 SHADOW 不会触发自动买。
- 当前 `shadow_pending` 额外包含 `集合竞价`，它仍只走 `audit_only`，用途是补样本、补审计，不是放行实盘。

如果需要临时补样本，直接手动跑一次即可，不建议把它重新铺进日常 cron。

监测范围：
- 昨日 `T+1` 入库票；
- 前一交易日 `T+2` 盘后资金流票；
- 近 5 日仍处于 `ACTIVE/WATCHING/PENDING` 的重点观测池。

报告用途：
- 输出“强盯/重点/观察/降级/风险”分层；
- 展示入库价、现价、今日涨幅、相对入库涨幅、量比、`60m标签` 和盯盘动作；
- `60m标签` 来自当天分钟结构，只做解释：
  - `60m冲高回落风险` / `60m强冲高回落风险`：早盘冲高后 60m 收不住，VWAP 承接弱，邮件提示“不追/隔夜降级”；
  - `60m持续强势观察` / `60m强势延续`：60m 收盘强且 VWAP 承接好，邮件提示“重点观察/盯收盘质量”；
  - `收盘持有闸门`：当前收盘位置和尾盘 VWAP 承接较强，只解释隔夜质量，不反推盘中买点；
- 当前 P0 SHADOW 策略：
  - `集合竞价`（`shadow_pending / audit_only`）
  - `龙头跟踪`
  - `冷启动`
  - `技术突破`
- 这用于为后续训练区分“选股问题”和“入场/买卖执行问题”。

---

## 5. 本金安全：进攻窗口观测与门禁（重点）

### 5.1 为什么必须做“时间段×天气”的观测
A股并不是全天等价。很多亏损不是策略本身错，而是：
- 在 **不适合进攻的时间段** 或 **不适合进攻的天气** 硬上。

所以我们先观测：
- `weather × time_bucket` 的胜率、均值、尾部（P5）
再决定是否开门禁。

### 5.2 观测在哪里看
每日运行 `track` 后，报告会新增：
- `⏱️【进攻窗口验证(时间段×天气)】(观测，不改交易)`

并且会落库到：
- `time_bucket_weather_daily_stats`

### 5.3 指标口径
- win：`max_t1_return > 2%`
- avg_max_ret：平均最大冲高
- avg_close_ret：平均收盘收益
- p5_close_ret：收盘收益 5 分位（尾部风险代理，越负越伤本金）

### 5.4 时间桶定义（固定）
| bucket | label |
|---|---|
| B1 | 09:30-10:00 |
| B2 | 10:00-11:30 |
| B3 | 13:00-14:00 |
| B4 | 14:00-14:40 |
| B5 | 14:40-15:00 |

### 5.5 进攻窗口门禁（默认关闭）
配置：`config/strategy_config.json` → `attack_window_gate`

- `enabled=false`：仅观测，不影响自动买入
- `enabled=true`：自动买入只允许发生在 `rules[weather]` 的 bucket 内

默认规则（可按观测结果迭代）：
- ☀️晴天：B1 + B4
- ☁️多云：仅 B4
- ⚠️暴雨：不允许直接买入；如 `attack_window_gate.enabled=true` 且暴雨规则为空，则连确认探针也会被窗口门禁挡住。当前默认 `enabled=false`，暴雨探针由 `weak_market_entry_gate` 与权限矩阵控制。

启用前先保持只观测，不改交易；每个 `weather × bucket` 至少累计 30 个样本，建议 50 个以上。灰度顺序：先只对 ⚠️暴雨 禁买，再限制 ☁️多云 只允许 B4，最后才放开 ☀️晴天 的 B1 + B4；观察 2-4 周后再扩大允许窗口。

### 5.6 动态时间窗入场（减少“固定时刻买高”，当前已开启）
配置：`config/strategy_config.json` → `entry_policy`

当前正式配置已经开启：

```text
entry_policy.enabled = true
entry_policy.default_model = dynamic_window
```

这个功能解决的问题：
- 以前 OpenClaw 任务在 09:26 / 14:30 等固定时刻触发，容易出现“那一刻价格偏高就买了”。
- 现在可以把候选先写入 **待入场信号池**，在允许的 time_bucket 窗口里（例如 B1 或 B4）多次复核资金/VWAP/量比门槛，择机入场。

当前代码注意：动态入场的“可执行 PENDING”由 `--queue-entry` 或 `--auto-trade` 进入 `execute_automated_strategies()` 后创建；`--queue-entry` 创建后直接返回，绝不执行买入。

**方式A：OpenClaw AgentTurn PENDING 入队（当前推荐）**
1) 09:26 / 09:31 / 14:30 等任务运行 `--queue-entry`：
   - `./run_openclaw.sh main.py --mode pre_market --queue-entry`
   - `./run_openclaw.sh main.py --mode watchlist --queue-entry`
   - `./run_openclaw.sh main.py --mode afternoon --queue-entry`
2) 当任务产生候选（集合竞价 / 午盘精选 / 备选池）时，如果 `entry_policy.enabled=true` 且 `default_model=dynamic_window`：
   - 系统写 `strategy_selection`、报告、门禁审计和 `pending_entry_signals(status=PENDING)`。
   - 如果权限矩阵是 BLOCK/OBSERVE，则不创建 PENDING，并写 `BUY_BLOCKED_PRE_TRADE_GATE`。
3) AgentTurn cron 不携带 `--auto-trade`，避免调度层安全策略直接拒绝；实际买入只由哨兵消费 PENDING 后触发。

**方式B：动态入场哨兵（推荐）**
- 直接运行：`./run_openclaw.sh monitor.py`
- 当 `entry_policy.enabled=true` 且 `default_model=dynamic_window` 时：
  - `monitor.py` 会像“卖出哨兵”一样，轮询 `pending_entry_signals(status=PENDING)`，在允许的 bucket 窗口内按系统内部风控/确认流程处理。
  - 这样你不必依赖 09:26/14:30 等固定任务时刻，动态询盘更连续。
  - 如果最新 pre-trade gate 返回 `BLOCK` 或 `OBSERVE`，PENDING 会直接标记 `CANCELLED`，避免硬拦截信号反复重试。
  - 如果只是量比/VWAP/开盘区间等确认不足，则只更新 `last_reason/check_count`，在冷却时间后继续尝试，直到过期或达到最大重试次数。

常用配置项：
- `entry_policy.enabled`：是否开启（当前正式配置为 true）
- `entry_policy.models.dynamic_window.retry_cooldown_sec`：每次失败后最短重试间隔（默认 300 秒）
- `entry_policy.models.dynamic_window.max_retries`：最多重试次数（默认 6）
- `entry_policy.models.dynamic_window.strategy_windows`：每个策略允许在哪些 bucket 内重试入场
- `entry_policy.models.dynamic_window.max_price_vwap_ratio`：可选“更严格的VWAP追高阈值”（留空表示沿用现有 1.03）
- `entry_confirm.recent_overheat_gate`：pending 买入前再次检查近 5/10 日涨幅、MA20 偏离和近 5 日涨停次数，防止候选阶段的过热过滤被动态入场绕过。

注意：
- 动态时间窗入场 **不等于放宽买入**。它仍然受权限矩阵、数据质量、VWAP/量比/开盘区间确认、weather 风控、仓位上限约束。
- 该功能依赖 DB：确保 `PortfolioManager.init_tables()` 可执行并成功创建 `pending_entry_signals`。
- 判断 AgentTurn 任务是否真正跑起来，以 `logs/stock_analyzer-YYYYMMDD.log` 中的 `Starting Market Signal Lab in ... mode` 为准，不只看 Cron `ok`。

### 5.7 Paper 影子账户（默认开启）

配置：`config/strategy_config.json` → `paper_account`

当前支持：

```text
main 买入成功 → 镜像到 paper_main
watchlist 买入成功 → 镜像到 paper_watchlist
monitor.py 动态 pending 入场成功 → 同步镜像到 paper
--paper-trade 创建独立 paper pending，monitor.py --paper-only 可独立模拟买入
paper 仓位不扣 main/watchlist 主模拟资金
paper 仓位会扣减/回补 paper_* 自己的模拟现金，并写入 portfolio_value
paper 仓位不计入 main/watchlist 主模拟账户持仓上限
paper 仓位进入报告/审计，并触发模拟仓专用买入/卖出邮件
```

用途：

```text
新规则先观察 paper 表现
下周至少跑 1 个完整交易日后，再决定是否继续训练或放大真实仓位
```

### 5.8 A 股真实约束回放

命令：

```bash
./run_openclaw.sh main.py --mode replay_day --date 20260529 --strategy 集合竞价 --no-email
```

当前回放器支持：

```text
T+1
涨停买不到
跌停卖不出
滑点
手续费
买入当日不能止损
T0最大浮亏
当天日线未落盘时使用 rt_min 信号后分钟数据
```

---

## 6. 深套自救（rescue 账户）

用于保护“深套底仓”不被风控系统误伤：
- 将持仓账户标为 `rescue`
- 哨兵会对 `rescue` 账户免疫多数自动止损/止盈逻辑
- 结合 T+0 做T建议邮件，人工执行摊低成本

### 6.1 手工实盘持仓跟踪

如果用户在券商账户里手工买入，但希望 market-signal-lab 盘中提醒，不要写入 `main` 自动账户，应登记到 `rescue`：

```bash
cd <project-root>
./run_openclaw.sh tools/register_manual_position.py --code 600579 --name 中化装备 --price 8.35 --quantity 100 --created-at "2026-06-30 14:30:00"
```

登记后由 M03 主哨兵统一监控，触发邮件但不自动处理真实券商账户。提醒覆盖：

```text
系统入库价/信号修复线
成本/回本线
突破确认线
日内多空枢轴失守
浮亏 3% / 5% / 8% 警戒
放量异动
原有 rescue 止损/止盈触发拦截提醒
```

执行边界：
- `rescue` 只用于真实手工持仓的提醒和自救，不参与主模拟账户仓位上限。
- 哨兵不会自动卖出真实券商账户；邮件只是提醒，最终操作由用户手动完成。
- 后续如果手工加仓/减仓，必须重新执行登记命令，用真实最新成本和数量覆盖原记录。

---

## 7. 审计、仪表盘与追踪（每天必跑）

日志文件在 `logs/stock_analyzer-YYYYMMDD.log`。系统启动时会滚动删除超过 `LOG_RETENTION_DAYS` 的日期日志，默认 5 天。所有 `Reporter.send_email()` 发送的邮件主题、正文、发送/失败结果都会写入当天日志，便于从服务器日志直接复盘邮件内容。

1) `risk_dashboard`：风控仪表盘（开盘前、盘中、收盘前）
- `./run_openclaw.sh main.py --mode risk_dashboard`

看这些内容：

```text
今日 regime
策略权限矩阵
pending signals
门禁拦截
T+1 阻断事件
持仓止损线
当日风险预算使用情况
paper_main / paper_watchlist
```

2) `audit`：交易质量审计（归因、执行偏差、风险事件）
- `./run_openclaw.sh main.py --mode audit`

3) `track`：策略追踪（T+1/T+2）+ 进攻窗口观测
- `./run_openclaw.sh main.py --mode track`

最低可信标准：

```text
股票名称不为空
策略、模型、标签不为空
买入/拦截/止损事件能在 risk_event_log 中追溯
paper 账户与真实账户同步关系明确
```

---

## 8. 常见故障处理（OpenClaw 需要会自救）

### 8.1 monitor lock 导致哨兵无法启动
`monitor.py` 使用 lock 防多开，但主账户和影子账户分开：
- 主哨兵：`monitor.lock`
- 影子哨兵：`monitor.paper.lock`

如果 OpenClaw 因异常退出留下 lock，下一次同账户范围的哨兵会拒绝运行；主哨兵不应阻挡 `--paper-only`，影子哨兵也不应阻挡主哨兵。
处理方式：确认没有对应的 `monitor.py` 进程后，只删除对应 lock 文件；不要为了恢复影子哨兵误删主哨兵 lock。

### 8.2 Track 模式拿不到 daily（收盘前/接口延迟）
track 会优先用 daily；拿不到时会自动 fallback 到 `rt_k` 实时日线行情近似。

### 8.3 控制台编码（Windows）
系统日志已尽量用 UTF-8 + replace，避免 emoji 导致崩溃；若仍异常，建议只看邮件或日志文件。

---

## 9. 风险声明
- 本系统输出与交易执行仅用于研究与模拟验证，不构成投资建议。
- OpenClaw 部署时请确保：数据库、缓存、邮箱、Tushare 权限与限频策略都已满足。
