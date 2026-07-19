# 观测周期与下一步计划（本金安全优先）

本文件用于部署到 OpenClaw 后的“观测→定量→门禁”闭环执行。

> 原则：先观测，不改交易；样本足够后再开门禁。

---

## 1. 当前已落地能力（观测层）

### 1.1 时间段 × 天气（time_bucket × weather）
- 统计产生位置：`core/strategy_tracker.py`（track 模式）
- 展示位置：`core/reporter.py`（track 报告）
- 落库表：`time_bucket_weather_daily_stats`

### 1.2 时间桶定义（固定）
| bucket | label |
|---|---|
| B1 | 09:30-10:00 |
| B2 | 10:00-11:00 |
| B3 | 13:00-14:00 |
| B4 | 14:00-14:40 |
| B5 | 14:40-15:00 |

### 1.3 指标口径（与系统一致）
- **win**：`max_t1_return > 0.02`（冲高>2% 即达标）
- **avg_max_ret**：平均最大冲高
- **avg_close_ret**：隔日收盘平均收益
- **p5_close_ret**：收盘收益的 5 分位（尾部风险代理）

---

## 2. 建议观测周期（推荐）

### 2.1 最小样本阈值
- 每个 `weather × bucket`：**≥ 30**（最低门槛）
- 更稳健：**≥ 50**（建议）

> 注意：如果出现某个 bucket 长期样本不足，优先保持“只观测”，不要强行开门禁。

### 2.2 观测期要看什么（每日/每周）
每日（track 报告中查看）：
- 重点关注 `p5_close_ret` 是否显著为负（尾部是否经常亏损）
- 关注 win_rate 与 avg_close_ret 是否一致（避免“冲高吃肉但收盘亏损”的假强势）

每周（总结）：
- 用 1 周内累计样本判断：哪些 weather 下、哪些 bucket 的 `avg_close_ret` 稳定为正
- 对比：☀️晴天 vs ☁️多云 vs ⚠️暴雨 的 bucket 风险分布

---

## 3. 下一步（门禁层）启用策略（默认关闭）

配置位置：`config/strategy_config.json`

```json
"attack_window_gate": {
  "enabled": false,
  "min_samples": 30,
  "rules": {
    "☀️晴天": ["B1", "B4"],
    "☁️多云": ["B4"],
    "⚠️暴雨": []
  }
}
```

### 3.1 启用顺序（建议灰度）
1) 仅对 **⚠️暴雨** 生效（暴雨直接禁买）
2) 再对 **☁️多云** 生效（只允许 B4）
3) 最后对 **☀️晴天** 生效（允许 B1 + B4）

### 3.2 启用前的检查清单
- 对应 weather×bucket 的样本数 ≥ `min_samples`
- 该 bucket 的 `p5_close_ret` 不应长期极端为负（避免频繁大亏）
- 观察 2-4 周再决定是否扩大允许 bucket

---

## 4. OpenClaw 部署后运行建议（不含平台细节）
- 每日收盘后/15:35 执行：`python main.py --mode track`
- 观测期内：保持 `attack_window_gate.enabled=false`
- 当你确认样本足够：再置为 `true` 并按“灰度顺序”逐步放开
