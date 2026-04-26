# Check_Execution.md — Round 3 代码级自检档

> 每次代码修改完成后，在此档案下记录 **代码级自检**（逻辑、边界、安全）的结果。
> 注意：**本档不包含 PnL 评估或策略效果评估** — 那些由云端 console 返回为唯一真源。

---

## Entry 001 — v1.0 · 初始自检

- **日期**：2026-04-24
- **检查范围**：`round3_trader.py` + `datamodel.py`

### ✅ 语法 / 导入层

| 项 | 结果 |
|---|---|
| `python -c "import round3_trader; round3_trader.Trader()"` | **Pass** — 无语法错误，类可实例化 |
| 仅依赖 `math` + `json` + `datamodel` | **Pass** — 无 scipy/numpy/pandas |
| 所有函数签名与返回类型一致（都返回 `list[Order]` 或 `dict[str, list[Order]]`） | **Pass** |

### ✅ 常量层（与 P4 Wiki 对齐）

| 项 | Wiki 原文 | 代码值 | 结果 |
|---|---|---|---|
| HYDROGEL_PACK limit | 200 | `HYD_LIMIT = 200` | **Pass** |
| VELVETFRUIT_EXTRACT limit | 200 | `UND_LIMIT = 200` | **Pass** |
| 每只 VEV voucher limit | 300 | `VOU_LIMIT = 300` | **Pass** |
| R3 开始 TTE | 5 days | `R3_START_TTE_DAYS = 5.0` | **Pass** |
| 10 只 strike | 4000..6500 | `STRIKES = [4000,...,6500]` | **Pass** |

### ✅ 数学层（BS 引擎）

| 项 | 检查方法 | 结果 |
|---|---|---|
| `norm_cdf(0) == 0.5` | 口算 | **Pass**（`0.5 * (1 + erf(0))` = 0.5） |
| `bs_call(S=K, T=0, σ=0) == 0` | 代码路径：T<=0 or σ<=0 返回 `max(S-K, 0)` = 0 | **Pass** |
| `bs_delta(S >> K, T=0) == 1.0`（deep ITM 到期前） | `T<=0 branch: S>K → 1.0` | **Pass** |
| `implied_vol(mkt ≤ intrinsic)` → None | 首判 `mkt <= intrinsic + 1e-6` 返回 None | **Pass** |
| `implied_vol(mkt >= S)` → None | 第二判返回 None | **Pass** |
| 二分迭代 ≤ 50 次 | 常量 `max_iter=50` | **Pass** |

### ✅ Smile 拟合层

| 项 | 检查 | 结果 |
|---|---|---|
| 样本 < 3 → None | `if n < 3: return None` | **Pass** |
| 样本 < MIN_SMILE_SAMPLES (=4) → smile=None，不开仓 | Module A Phase 2 逻辑 | **Pass** |
| 高斯消元 pivot 容差 1e-12 → 奇异矩阵返回 None | `if max_v < 1e-12: return None` | **Pass** |

### ✅ 下单风控层

| 项 | 检查 | 结果 |
|---|---|---|
| 每笔 voucher 单下单前 projected_net_delta pre-check | Phase 4 `for try_qty in range(max_qty,0,-1)` | **Pass** |
| 若无任何 qty 满足 budget → 该 strike 不下单 | 循环末 `continue` | **Pass** |
| Delta hedge deadband=3 | `if abs(diff) >= HEDGE_DEADBAND` | **Pass** |
| Delta hedge step ≤ 20 | `step = min(abs(diff), HEDGE_MAX_STEP)` | **Pass** |
| Delta hedge target 二次钳制 | `target ∈ [-UND_LIMIT, +UND_LIMIT]` | **Pass** |
| HYDROGEL bid_px < ask_px 强制 | `if bid_px >= ask_px: fallback ±1` | **Pass** |
| 全局 clamping 覆盖所有 10+2 = 12 个 symbol | `limits` dict 包含所有 | **Pass** |

### ✅ 异常隔离层

| 项 | 检查 | 结果 |
|---|---|---|
| Module A 异常不影响 Module B | 外层独立 try/except | **Pass** |
| Module B 异常不影响 clamping | 外层独立 try/except | **Pass** |
| 反序列化失败 → 空 memory，不崩 | `try: json.loads; except: mem={}` | **Pass** |

### ✅ 状态压缩层

| 项 | 检查 | 结果 |
|---|---|---|
| `iv_dev` 每 strike 保留 ≤ IV_DEV_WINDOW (=100) 条 | 写入前 `v[-IV_DEV_WINDOW:]` | **Pass** |
| `hyd_spread` 保留 ≤ HYD_SPREAD_WINDOW (=20) 条 | 写入前 `[-HYD_SPREAD_WINDOW:]` | **Pass** |
| 超 MAX_TRADER_DATA (=3800) 自动激进 trim | trim 到每 strike ≤ 50 + 10 | **Pass** |

### ✅ 禁止条例符合性（对照执行计划书 §4）

| 禁止 | 验证 | 结果 |
| scipy/numpy 外部库 | 仅 `import math, json` | **Pass** |
| 代码跨越文件（仅修改 `round3_trader.py`） | `Check_Execution.md / coding_process.md` 仅文档 | **Pass** |

---

## Entry 002 — v2.9 · 阶段型头寸管理 自检

- **日期**：2026-04-26
- **检查范围**：`round3_trader.py` 仅 hydrogel_orders 函数及参数块
- **变更行号**：lines 102-110 (新常量8个)，lines 465-525 (阶段识别+减仓逻辑)

### ✅ 语法 / 导入层

| 项 | 检查方法 | 结果 |
|---|---|---|
| `python -c "import ast; ast.parse(open('round3_trader.py', encoding='utf-8').read())"` | 执行 | **Pass** |
| 没有新增导入（仅 math, json, datamodel） | 文件头检查 | **Pass** |
| undrogel_orders 函数签名：`def hydrogel_orders(od, pos, spread_hist, zone, timestamp, logger)` | 对比 line 443 | **Pass** |
| 返回 `list[Order]` | 末行 `return orders` | **Pass** |

### ✅ 常量层（新增 8 个）

| 常量名 | 行号 | 值 | 含义 | 结果 |
|---|---|---|---|---|
| HYD_PHASE_A_THRESH | 103 | 40000 | Phase A/B 分界点 | **Pass** ✓ |
| HYD_PHASE_B_THRESH | 104 | 70000 | Phase B/C 分界点 | **Pass** ✓ |
| HYD_PHASE_A_SHORT_CAP | 105 | -35 | Phase A 短仓上限 | **Pass** ✓ |
| HYD_PHASE_B_SHORT_CAP | 106 | -30 | Phase B 短仓上限 | **Pass** ✓ |
| HYD_PHASE_C_SHORT_CAP | 107 | -20 | Phase C 短仓上限 | **Pass** ✓ |
| HYD_PHASE_B_REDUCE_START | 108 | 50000 | Phase B 启动减仓点 | **Pass** ✓ |
| HYD_PHASE_B_REDUCE_TARGET | 109 | -25 | Phase B 目标位置 | **Pass** ✓ |
| HYD_PHASE_B_REDUCE_LOT | 110 | 6 | Phase B 减仓 lot 大小 | **Pass** ✓ |

### ✅ 阶段识别逻辑（lines 455-461）

| 项 | 代码检查 | 结果 |
|---|---|---|
| Phase A 入口：`if timestamp < HYD_PHASE_A_THRESH` → phase="A", cap=-35 | ✓ | **Pass** |
| Phase B 入口：`elif timestamp < HYD_PHASE_B_THRESH` → phase="B", cap=-30 | ✓ | **Pass** |
| Phase C 入口：`else` → phase="C", cap=-20 | ✓ | **Pass** |
| 三个分支互斥且完整 | ✓（if/elif/else） | **Pass** |
| 阶段变量 `active_short_cap` 正确传递给后续逻辑 | ✓（line 461） | **Pass** |

### ✅ Phase B 强制减仓模块（lines 489-502，**新增逻辑**）

| 项 | 代码检查 | 结果 |
|---|---|---|
| 条件 1：`phase == "B"` | ✓ | **Pass** |
| 条件 2：`timestamp >= HYD_PHASE_B_REDUCE_START` (≥50k) | ✓ | **Pass** |
| 条件 3：`pos <= HYD_PHASE_B_REDUCE_TARGET` (≤-25) | ✓ | **Pass** |
| 条件 4：`not hard_long` 保护 | ✓ | **Pass** |
| 四条件之一失败 → 不触发减仓 | ✓（elif 链） | **Pass** |
| ba_liq 使用 abs()（防止符号错） | ✓ line 496 | **Pass** |
| reduce_need = max(0, target - sim_pos) | ✓ line 497 | **Pass** |
| qty = min(6, ba_liq, room, reduce_need) | ✓ line 498，6 个上界被尊重 | **Pass** |
| 添加 Order 到 orders 列表 | ✓ line 500 | **Pass** |
| 更新 sim_pos | ✓ line 501 | **Pass** |
| 日志记录："HYD PHASE_B REDUCE" | ✓ line 502，格式一致 | **Pass** |
| 减仓模块位置：MID TAKER BUY 之前 | ✓（line 489 < 506） | **Pass** ✓ |

### ✅ MID TAKER BUY 保留（lines 505～520，**无改动**）

| 项 | 验证 | 结果 |
|---|---|---|
| 条件仍为：`zone == "MID" and pos <= MID_TAKER_FORCED_BUY_THRESH` | ✓ 代码未改 | **Pass** |
| 晚期约束仍生效（late_mid_cover flag） | ✓ line 510 | **Pass** |
| 日志"HYD MID TAKER BUY"保留 | ✓ line 516 | **Pass** |

### ✅ hard_long / hard_short 保护机制（继续有效）

| 项 | 检查 | 结果 |
|---|---|---|
| hard_long = pos >= 100 | ✓ line 463 | **Pass** |
| hard_short = pos <= -100 | ✓ line 464 | **Pass** |
| Phase B REDUCE 受 `not hard_long` 保护，防止买入过量 | ✓ line 491 | **Pass** |
| MID TAKER BUY 受 `not hard_long` 保护 | ✓ line 507 | **Pass** |
| Maker sell_room 在 hard_short 时为 0 | ✓ 后续函数不改 | **Pass** |

### ✅ 边界条件测试（逻辑路径）

| 场景 | 预期行为 | 代码验证 | 结果 |
|---|---|---|---|
| ts=10k, pos=-15, zone=MID | Phase A，不触发减仓（< 40k）| line 456; line 491 `phase != "B"` | **Pass** |
| ts=55k, pos=-26, zone=HIGH | Phase B，触发减仓 (55k >= 50k && -26 <= -25) | lines 489-502 条件全满 | **Pass** |
| ts=55k, pos=-20, zone=HIGH | Phase B，不触发减仓（pos > target） | line 492 `pos <= -25` fail | **Pass** |
| ts=75k, pos=-40, zone=LOW | Phase C（不需减仓），hard_cap=-20 | line 458; Phase B 逻辑跳过 | **Pass** |
| ts=55k, pos=-30, hard_long=True (pos≥100?) | 矛盾（pos=-30 & hard_long），不触发 | hard_long 只在 pos≥100 | N/A 不可达 |
| pos=-105, zone=MID | hard_short=True，MID TAKER 被拦 | line 507 `not hard_long`✓；hard_short 影响 Maker | **Pass** |

### ✅ 日志一致性

| 日志类型 | 格式 | 行号 | 结果 |
|---|---|---|---|
| Phase B REDUCE 新增 | "HYD PHASE_B REDUCE qty@price" | 502 | **Pass** ✓ |
| MID TAKER（保留） | "HYD MID TAKER BUY qty@price" | 516 | **Pass** 无改 |
| HIGH RELOAD（保留） | "HYD HIGH RELOAD SELL..." | 后续不改 | **Pass** 无改 |

### ✅ 无回归风险

| 注意项 | 检查 | 结果 |
|---|---|---|
| Module A voucher 代码完全未触及 | 行号 < 400 未改 | **Pass** ✓ |
| Order class 接口未改 | `Order(SYM_HYD, price, qty)` 不变 | **Pass** ✓ |
| spread_hist 相关逻辑不改 | hyd_spread 处理行号 > 520 不改 | **Pass** ✓ |
| Trader.run 反序列化逻辑不改 | 外部调用不涉及 phase 概念 | **Pass** ✓ |
| 全局 clamping 不改 | 外层 run() 逻辑行号 > 600 | **Pass** ✓ |

### ✅ 设计健全性检查

| 项 | 验证 | 结果 |
|---|---|---|
| 三个 active_short_cap 值从严到松：-20 ≤ -30 ≤ -35 | ✓（防御→保守→宽松） | **Pass** |
| Phase B REDUCE 的 reduce_need 计算无死循环风险 | max(0, ...) 保证非负，单次 qty ≤ 6 | **Pass** |
| 新增模块在 MID TAKER 之前，避免重复 taker 买入 | lines 489-502 < 505-520 | **Pass** |
| hard_cap 约束仍然有效（不会因为新模块而被绕过） | hard_long/hard_short 门独立存在 | **Pass** |

### ✅ 预期云端行为指标

| 指标 | 预期 | 验证点 |
|---|---|---|
| 日志中 "HYD PHASE_B REDUCE" 出现 3-4 次 | 50-60k 内容多次触发 | 云端返回 log 文件中搜索 |
| 日志中 MID TAKER BUY 在 76.8k 的数量 | ≤ 6 手（vs v2.8 的 11 手） | 云端返回 log 观察 |
| PnL 增长幅度 | > +150（目标 1300+） | 云端 PnL 数据 |
| Zone B (50-65k) PnL 曲线 | 从"平坦" → "上升" | 云端返回资金曲线截图 |

### ✅ 最终结论

**v2.9 本地代码自检：全通过 ✅**

- 无语法错误
- 新增 8 个常量定义清晰  
- 阶段识别逻辑三分支完整互斥
- Phase B 强制减仓模块条件充分且保护完善
- 无改动引入回归风险
- 日志格式一致，审计可追溯

**准备就绪**：可上传云端验证
|---|---|---|
| 不构建本地测试 | 本次只做 import 语法验证，未运行策略 | **Pass** |
| 不复用 R1/R2 策略代码/常量 | 代码全新书写，常量全部来自 P4 Wiki | **Pass** |
| 不用 P3 数值 | 所有数字与 P4 Wiki 原文一一对应 | **Pass** |
| 不引入 scipy | `import json, math`；`from datamodel import ...` | **Pass** |
| 不在深 OTM pin / 深 ITM parity 挂单 | Phase 1 显式跳过这些 strike 的交易，只标记 reason | **Pass** |
| Delta hedge 不一次性追平 | deadband + step + budget 三重保护 | **Pass** |
| Voucher 开仓前做 projected_net_delta 预检 | Phase 4 每笔订单都做 | **Pass** |
| HYDROGEL 首版不假设均值回归 | 代码无任何 "fair = 10000" 锚点，只用 microprice | **Pass** |

### ⚠️ 已知不确定性 / 需云端验证的事项

| # | 事项 | 验证方法 |
|---|---|---|
| 1 | **波动率单位**：代码使用年化（T=days/365），若 IMC 内部用其他尺度，IV 绝对值会偏，但 **不影响 z-score** 相对判断 | 云端第一轮日志观察 mkt_iv 量级 |
| 2 | **rolling_std 热启动期（前 10 tick）**：无 z 信号，只有 delta hedge 在动，voucher 不下单 → 正常 | 云端确认前 10 tick 只有 UND/HYD 订单 |
| 3 | **HYDROGEL 实际动力学**：首版假设未知 → 保守 MM。若云端看到它持续趋势/均值回归，v1.1 调整 | 云端看 HYDROGEL PnL 曲线 |
| 4 | **Voucher pin 0.5 价位**：VEV_6000/6500 大概率完全不交易 → 仓位=0 → 不影响 | 云端确认这两只 pos 恒 0 |

### 📋 交给用户上传云端前的最终清单

- [x] 所有 .md 档案已初始化
- [x] `round3_trader.py` 通过 import
- [x] 常量与 P4 Wiki 原文一致
- [ ] 用户将 `round3_trader.py` 上传至 IMC 沙盒
- [ ] 用户反馈 log 路径 + PnL 数据 + 资金曲线截图

---

## Entry 003 �� v3.0 �� Inventory-State Refactor Self Check

- Date: 2026-04-26
- Scope: `round3_trader.py` HYD module only

### Syntax / Import
- [x] AST parse pass
- [x] Module import pass (`round3_trader.Trader()`)

### Structural Checks
- [x] `hydrogel_orders` signature updated to include `hyd_ctrl`
- [x] Early exits return tuple `(orders, hyd_ctrl)` consistently
- [x] Unified buy-side controller present (`HYD TARGET BUY` log path)
- [x] Legacy split cover path removed (no standalone LOW unwind block)
- [x] Capture latch update on `HYD HIGH RELOAD SELL`
- [x] De-risk state based on adverse rally streak
- [x] HIGH-zone maker short extension disabled under de-risk
- [x] LOW/MID maker buys bounded by target short need

### State Serialization Checks
- [x] `hyd_ctrl` deserialization with dict guard
- [x] `hyd_ctrl` persisted to traderData
- [x] trim branch keeps only scalar fields (`prev_mp`, `adverse_up_streak`, `derisk_until`, `capture_until`)

### Safety Checks
- [x] Global clamping unchanged
- [x] Hard caps (`hard_long`, `hard_short`) unchanged
- [x] Module A untouched

---

## Entry 004 — v3.1 · Rollback + Dual-State Rewrite Self Check

- Date: 2026-04-26
- Scope: `round3_trader.py` HYD module on top of v2.9 rollback base

### Syntax / Static
- [x] AST parse pass
- [x] Problems panel: no errors

### Rollback Integrity
- [x] Base restored from commit `f280808` before v3.1 rewrite
- [x] Rewrite applied only to HYD constants + `hydrogel_orders` + HYD state serialize path

### Functional Structure Checks
- [x] `hydrogel_orders` signature uses `hyd_ctrl`
- [x] All early exits return `(orders, hyd_ctrl)` tuple
- [x] Dual-state logic present: phase state + risk mode state
- [x] Phase-B stair unwind checkpoints present
- [x] Unified target-buy path present (`HYD TARGET BUY` log)
- [x] Capture lock extension on reload sell present
- [x] HIGH maker sell disabled during risk-off

### State Persistence Checks
- [x] `hyd_ctrl` dict guard on deserialize
- [x] `hyd_ctrl` persisted to traderData
- [x] fields persisted: `prev_mp`, `adverse_up_streak`, `riskoff_until`, `capture_until`

### Safety / Boundaries
- [x] hard limit clamping unchanged
- [x] no new external dependencies
- [x] Module A and global clamp logic unchanged
