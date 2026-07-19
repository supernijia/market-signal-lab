# 冷启动观察模型接入记录 2026-05-30

## 接入范围

本次只接入观察链路，不改变主项目选股排序，不触发自动交易。

接入内容：

- 冷启动候选补充模型分数字段。
- 邮件中展示冷启动模型分数和观察标签。
- factor_snapshot / strategy_selection 的 tags_json 中记录冷启动观察模型标签。

## 模型文件

模型文件来自 `optional-training-workspace`：

- `data/models/cold_start_quality_good_10m.json`
- `data/models/cold_start_quality_profit_capture_10m.json`
- `data/models/cold_start_quality_risk_10m.json`

## 新增/修改文件

- `core/cold_start_model.py`
- `core/analyzer.py`
- `core/reporter.py`
- `core/factor_engine.py`
- `config/strategy_config.json`

## 输出字段

候选中新增：

- `cold_start_good_score`
- `cold_start_profit_score`
- `cold_start_risk_score`
- `cold_start_score_10m`
- `cold_start_observe_score`
- `cold_start_score_60m`
- `cold_start_early_absorb`
- `cold_start_delayed_confirm`
- `cold_start_vwap_support_observe`
- `cold_start_pullback_entry_candidate`
- `cold_start_pullback_window_min`
- `cold_start_pullback_entry_vs_signal`
- `cold_start_pullback_above_vwap_prefix`
- `cold_start_entry_mode`
- `cold_start_model_tags`

## 使用原则

- `core_top` 适合核心观察。
- `vwap_hold_delayed_confirm_60m` 不能理解为 60 分钟后追买，而应展示为 VWAP 承接 / 轻回踩观察。
- `cold_start_pullback_entry_candidate` 只表示不追高观察窗口，不表示自动买入授权。
- `early_high_trigger_absorb_10m` 只作为观察补捞，不提高核心排序。
- 高风险分数只做风险提示/降权参考，不做自动交易决策。

## 2026-05-31 训练结论更新

训练侧 `cold_start_pullback_entry_2026-05-31.md` 显示：

- 严格 3 分钟不追高条件下，`core_top + delayed_confirm` 覆盖率 72.38%，T0 收盘 +3.37%，T+1 风险 9.02%。
- 机械等待 5/10 分钟确认后追买会明显损失收益。
- 因此主项目只展示观察字段，不把确认标签解释成追买授权。

## 本地验证

已通过：

```bash
./venv/bin/python -m py_compile core/cold_start_model.py core/analyzer.py core/reporter.py core/factor_engine.py
./run_openclaw.sh main.py --mode pre_market --no-email --monitor
```

验证结果：

- Tushare 初始化正常。
- Redis 初始化正常。
- pre_market 报告生成正常。
- 冷启动邮件表格已展示模型列。
- `--monitor` 模式未写入 `strategy_selection`。

2026-05-31 补充验证：

```bash
./venv/bin/python -m py_compile core/cold_start_model.py core/reporter.py core/analyzer.py core/entry_flow.py main.py monitor.py
./venv/bin/python -m json.tool config/strategy_config.json
```

并用模拟冷启动候选验证：

- `cold_start_vwap_support_observe = True`
- `cold_start_pullback_entry_candidate = True`
- 邮件模型列展示为 `承接 低吸5m`
- 业务标签展示为 `VWAP承接/低吸观察`
