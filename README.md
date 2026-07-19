# Market Signal Lab

面向 A 股研究的自动化选股、回测与风险监控工具。支持多模式策略运行，默认以模拟交易和只读分析为主。

## 项目结构

```
├── main.py                # 主入口 (多模式分析)
├── monitor.py             # 哨兵进程 (持仓守护与止盈止损)
├── core/                  # 核心业务模块
│   ├── trade_auditor.py   # 交易质量审计 [V16]
│   ├── evolve_strategy.py # 策略自动化进化
│   ├── analyzer.py        # 选股分析引擎
│   ├── entry_flow.py      # 共享入场验证编排 (main/monitor)
│   ├── data_provider.py   # 数据接口 (Tushare/Sina)
│   └── ...                # 其他核心组件
├── config/                # 策略配置文件 (JSON)
├── data/                  # 系统数据与MACD缓存
├── logs/                  # 统一日志存放
├── docs/                  # 策略说明与开发文档
├── scripts/               # 部署/迁移/平台快捷脚本
├── tools/                 # 稳定运维工具、诊断脚本、旧研究归档
│   ├── debug/             # 临时数据源与指标排查
│   ├── diagnostics/       # 人工检查脚本
│   └── research/          # 研究辅助与旧训练残留
```

## 运行方式

**首次部署：**

```bash
cd /path/to/market-signal-lab
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.openclaw.example .env.openclaw   # 填写 DB_PASS / EMAIL_PWD 等敏感值
```

**日常运行（统一用 `run_openclaw.sh`）：**

```bash
./run_openclaw.sh main.py --mode pre_market --queue-entry
./run_openclaw.sh monitor.py
```

> 生产部署和开盘检查请先阅读 `docs/STRATEGY.md`。不要把包含真实凭据的环境文件提交到 Git。
> 邮件通知默认关闭；只有设置 `EMAIL_ENABLED=1` 并完整配置发件人、密码和收件人后才会发送。

**开盘实战前快速检查：**

```bash
./run_openclaw.sh main.py --mode risk_dashboard --no-email
./run_openclaw.sh monitor.py --once --dry-run --no-email --force
./run_openclaw.sh tools/audit_cold_start_tags.py --date 2026-06-01 --json
```

> 详见 `docs/STRATEGY.md` 的完整任务课表和冷启动标签审计；文档入口见 `docs/README.md`。
> 如果要了解整体框架、模块分层和关键设计决策，先看 `docs/ARCHITECTURE.md`。

## ⏰ 定时任务入口

OpenClaw 的权威任务课表只看 `docs/STRATEGY.md`。README 不再维护独立时间表，避免和主 Runbook 冲突。

推荐从这里进入：

- [OpenClaw 执行课表](docs/STRATEGY.md#2-openclaw-执行课表唯一权威版本)
- [开盘前检查](docs/STRATEGY.md#28-开盘前额外校验人工执行不是日常-cron)

## 依赖

```bash
pip install -r requirements.txt
```
