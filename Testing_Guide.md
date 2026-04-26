# Testing_Guide.md — Round 3 云端验证判准

> **重要**：本档「Testing」**仅指 IMC 云端 console 验证**，不含任何本地测试。
> PnL 数据 / 胜负判定 / 策略可行性 —— 唯一权威来源是 IMC 云端返回的 log + console。

---

## 1. 上传流程（用户操作）

1. 将 `round3_trader.py` 上传至 IMC 网站的 Round 3 Upload & Log 页面。
2. 等待沙盒跑完（~1-3 分钟）。
3. 下载 log 文件夹（包含 activity log、sandbox log、trade log）。
4. 截图：Performance 页面的 PnL 卡片 + 资金曲线。
5. 将「log 文件夹路径 + PnL 数字 + 截图」反馈给 Agent。

---

## 2. Agent 对云端数据的分析步骤

### 2.1 第一层：健康检查（Smoke Test）

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | Sandbox 是否正常跑完所有 10000 ticks | 无 "Algorithm crashed" 或 timeout 错误 |
| 2 | 是否出现 position limit violation | log 中无 "POSITION_LIMIT_EXCEEDED" 类错误 |
| 3 | 是否出现订单格式错误 | log 中无 "INVALID_ORDER" |
| 4 | traderData 大小是否稳定 | 无 "traderData too long" 类警告 |
| 5 | 异常捕获是否触发 | log 中 `[MODA ERR]` / `[MODB ERR]` 条数 < 5 |

**若任何一项 Fail → 立即回滚**（不评估 PnL）。

### 2.2 第二层：策略行为合理性（Behavior Inspection）

#### A. Voucher 模块

| 预期行为 | 观测方法 |
|---|---|
| 前 ~10-50 ticks：**无 voucher 订单**（rolling_std warmup） | 看 activity log 前段是否只有 UND/HYD 订单 |
| VEV_6000 / VEV_6500 **仓位恒 0**（pin 0.5 被过滤） | 查 position history |
| VEV_4000 / VEV_4500 **仓位恒 0**（deep ITM 被过滤） | 查 position history |
| 真正下单集中在 VEV_5000 ~ VEV_5500 | 查 trade log 按 symbol 分布 |
| 每次 voucher 下单后，看到同 tick 或下 tick 的 UND 对冲单 | 时序对齐检查 |

#### B. Delta Hedge 模块

| 预期行为 | 观测方法 |
|---|---|
| VELVETFRUIT_EXTRACT 仓位在 `[-180, +180]` 范围内浮动 | position time-series |
| 无连续 20+ ticks 净 delta > 20 未对冲 | log 中 `UND BUY/SELL` 频次 |
| Hedge 订单不会一次性挪 > 20 手 | trade log 单笔 qty 分布 |

#### C. HYDROGEL 模块

| 预期行为 | 观测方法 |
|---|---|
| HYDROGEL 仓位在 `[-150, +150]` 温和震荡（库存惩罚起作用） | position time-series |
| 每 tick 双边挂单 | activity log 持续有 HYD 订单 |
| Opportunistic taker 偶发（`HYD TAKE/HIT` log） | 扫 log 关键词频次 |

#### D. Log 健康

| 预期 | 检查 |
|---|---|
| 无 `[TRIM]` 频繁触发（说明状态大小失控） | 扫 log |
| 每 tick log 字节 < 3750 | 抽样 |

### 2.3 第三层：PnL 归因分析（仅在第一/二层全部通过后）

按 symbol 拆分 PnL 贡献：
- **Voucher 总 PnL**（Σ VEV_*）：预期 **≥ 0**，若 < 0 需分析是哪个 strike 亏
- **UND PnL**：预期 **接近 0**（对冲不应赚也不应大亏）
- **HYDROGEL PnL**：预期 **≥ 0 或小幅负**（纯做市）

### 2.4 决策门槛

| 场景 | 触发决策 |
|---|---|
| 第一层 Fail | → **决策 B**（回滚 + 失败深度剖析） |
| 第一层 Pass，第二层有异常行为 | → **决策 B**（回滚 + 剖析异常来源） |
| 全三层通过，总 PnL > 0 | → **决策 A**（推送 baseline，准备 v1.1 迭代） |
| 全三层通过，总 PnL ≈ 0 | → **决策 A**（保守版不亏即成功，v1.1 研究如何加收益） |
| 全三层通过，总 PnL < 0 | → **决策 B**（虽无逻辑错但策略不适配市场，深度分析） |

---

## 3. 向用户反馈后的 Agent 输出模板

Agent 收到云端 log + PnL 后，必须按以下结构回复：

```
【云端结果分析 v1.0】

1) 健康检查：
   - 沙盒完成：是/否
   - 订单错误：N 条
   - 异常捕获：N 条
   - 判定：Pass / Fail

2) 行为检查：
   - Voucher 仓位分布：...
   - Delta hedge 频次：...
   - HYDROGEL 表现：...
   - 判定：正常 / 异常（列出异常点）

3) PnL 归因：
   - 总 PnL：X
   - Voucher 子模块：X
   - UND 对冲：X
   - HYDROGEL：X

4) 决策建议：
   - [决策 A / 决策 B]
   - 下一步行动项 (v1.1 候选改进点 或 回滚并剖析)
```

---

## 4. 绝对禁止条例

1. ❌ 禁止基于任何本地模拟数字判断"策略是否有效"。本地不能生成 PnL 预测。
2. ❌ 禁止在未收到用户提供的云端 log 前推测结果。
3. ❌ 禁止跳过第一/二层直接看 PnL 做决策——行为异常但 PnL 偶发正的版本不可推送（样本偏差）。

---

## 版本历史

| 版本 | 日期 | 适用策略版本 |
|---|---|---|
| v1.0 | 2026-04-24 | round3_trader.py v1.0 |

---

## 5. v3.0 Cloud Validation Criteria (Inventory-State Refactor)

### Required Log Signals
1. `HYD TARGET BUY` appears with reason tags (`rs=`) and target (`tgt=`)
2. `HYD HIGH RELOAD SELL` still appears in late HIGH regime
3. No exception logs from Module B

### Segment-Level Acceptance Checks
1. Early pressure segment (25.8k-32.7k): drawdown should shrink vs run 420388 baseline
2. Mid over-cover segment (49k-53k): remove large one-way cover overshoot behavior
3. Late downswing segment (76.8k-91k): retain more downside capture after reload
4. Tail rebound segment (91k-99.9k): avoid excessive give-back

### Decision Rule
- Pass: PnL >= 1437 and at least 2/3 pressure segments improve
- Strong pass: PnL >= 1500 with clear improvement in all 3 primary segments
- Fail: PnL regression or pressure segments worsen
