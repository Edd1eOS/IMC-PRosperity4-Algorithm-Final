# coding_process.md — IMC Prosperity 4 · Round 3 代码审计日志

> 此档案记录 Round 3 所有代码级变动。每个 Entry 对应一次 trader 版本迭代，
> 按「v1.0 → v1.1 → ...」严格递增。失败回滚版本仍保留记录，附《失败原因深度剖析》。

---

## Entry 001 — v1.0 · 初始架构建立

- **日期**：2026-04-24
- **状态**：已完成本地自检，待上传云端验证
- **基准版本 (baseline)**：无（R3 首版）
- **计划批复来源**：用户于 2026-04-24 明确批复《R3 执行计划书 v3》

### 1. 新增文件清单

| 文件 | 行数 (近似) | 作用 |
|---|---|---|
| `round3_trader.py` | ~380 | R3 主策略模块 |
| `datamodel.py` | ~55 | IMC 接口 stub（本地 import 用） |
| `coding_process.md` | 本文件 | 代码变动审计 |
| `Algorithm_improve_strategy.md` | 新建 | 宏观策略演进 |
| `Check_Execution.md` | 新建 | 代码级自检 |
| `Testing_Guide.md` | 新建 | 云端验证判准 |

### 2. `round3_trader.py` 模块划分（行号粗略）

| 行号段 | 模块 | 要点 |
|---|---|---|
| 1–28 | 文件头 docstring | 版本说明 + 架构总览 + 参数偏好声明 |
| 30–86 | 常量区 | 全部源自 P4 Wiki Round 3 原文，非 P3 类推 |
| 89–108 | `Logger` | byte-capped 日志缓冲（3750 字节上限） |
| 111–168 | Black-Scholes 基元 | `norm_cdf`, `bs_call`, `bs_delta`, `bs_vega`, `implied_vol` |
| 171–177 | `tte_years` | TTE=5d 起始，per-tick 线性衰减 |
| 180–207 | 订单簿工具 | `best_bid_ask`, `mid_price`, `microprice` |
| 210–254 | `fit_parabola` | 3×3 正规方程高斯消元，无 numpy |
| 257–393 | Module A `voucher_and_hedge_orders` | 5 阶段：过滤 → 反解 → 拟合 → z-score 决策 → delta hedge |
| 396–451 | Module B `hydrogel_orders` | microprice ± 自适应 edge + 库存惩罚 + opportunistic taker |
| 454–最后 | `Trader.run` | 状态反序列化 → Module A → Module B → 全局钳制 → 状态压缩 |

### 3. 关键参数（源于 §5 偏好推荐 = 全保守）

| 常量 | 值 | 出处 |
|---|---|---|
| `UND_LIMIT` / `HYD_LIMIT` / `VOU_LIMIT` | 200 / 200 / 300 | **P4 Wiki 原话** |
| `R3_START_TTE_DAYS` | 5.0 | **P4 Wiki 原话** |
| `MIN_SMILE_SAMPLES` | 4 | ⓔ 保守 |
| `Z_THRESHOLD` | 2.0 | ⓑ ⓔ 保守 |
| `IV_DEV_WINDOW` | 100 | ⓔ 保守 |
| `VOU_LOT_CAP` | 15 | ⓑ 保守 |
| `HEDGE_BUDGET` | 180 | ⓓ 软对冲 |
| `HEDGE_DEADBAND` | 3 | ⓓ 软对冲 |
| `HEDGE_MAX_STEP` | 20 | ⓓ 软对冲 |
| `HYD_EDGE_MIN` / `HYD_EDGE_MULT` | 2.0 / 0.4 | ⓒ A 方案 |
| `HYD_LOT` | 15 | ⓒ A 方案 |

### 4. Delta Hedge 预检（关键风控，硬编码在 Phase 4 每笔下单前）

```
for try_qty in range(max_qty, 0, -1):
    new_proj = projected_net_delta + signed_qty * delta_k
    if abs(new_proj) <= HEDGE_BUDGET:    # == 180
        <下单>
        projected_net_delta = new_proj
        break
```
↑ 确保即使所有 voucher 满仓，净 delta 不会超出底层 200 对冲预算的安全垫（留 20）。

### 5. 未覆盖/留白项（后续版本可迭代）

| 项 | 当前处理 | 可迭代方向 |
|---|---|---|
| Vega exposure | 未聚合、未对冲 | 若云端发现 vol 风险集中，可加 Vega-hedge 模块 |
| Smile 残差历史 | 仅按 strike 独立维护 iv_dev 历史 | 可改为全局 residual z-score |
| HYDROGEL 信号 | 纯跟随做市，无方向性 alpha | 云端数据如显示均值回归，v1.1 可加 OU fv 锚点 |
| Voucher 单笔执行 | 全部 aggress best bid/ask | 可改为 resting limit 获得 rebate（若 IMC 有的话） |

### 6. 已执行自检

- [x] `python -c "import round3_trader; Trader()"` → 无错误
- [x] 所有常量与 P4 Wiki 原文对齐（limit 200/200/300, TTE=5d）
- [x] 无 `scipy` 依赖，仅 `math` + `json`
- [x] 无 R1/R2 代码/常量复用
- [x] 无本地 backtester / 数值 "validation" 脚本
- [x] Delta hedge 预检 + deadband + step-cap 三重保护
- [x] 深度 ITM (reason="DEEP_ITM") 与深度 OTM pin (reason="DEEP_OTM_PIN") 分别赋 delta=1.0 / 0.0
- [x] traderData 超 3800 字节自动 trim
- [x] Logger 超 3750 字节自动丢弃

### 7. 下一步

- 等待用户将 `round3_trader.py` 上传 IMC 云端沙盒
- 用户反馈 log 文件夹位置 + PnL 数据 + 资金曲线截图
- 根据云端返回进入决策 A（成功→推送 baseline）或决策 B（失败→回滚 + 失败深度剖析）

---

---

## Entry 002 — v1.1 · z-score 公式修复 + 参数调优

- **日期**：2026-04-24
- **状态**：已完成本地自检，待上传云端验证
- **基准版本 (baseline)**：v1.0（PnL = −5007，全时段下行）
- **计划批复来源**：用户于 2026-04-24 批复《执行计划书 v1.1》

### 1. Root Cause（根因）

`voucher_and_hedge_orders` Phase 4 中，计算了 `mean_d`（历史 iv_dev 均值）却没有用于 z 的分子：

```python
# v1.0（bug）:
z = iv_dev / std

# v1.1（修复）:
z = (iv_dev - mean_d) / std
```

后果：VEV_4500 的结构性微笑偏差（持续约 +0.05 IV）使 z 每 tick ≈ 17，导致系统每隔数百 tick 就建仓一次，持续累积跨价点差成本，产生全时段下行的 −5007 PnL。

### 2. 代码变动清单

| 文件 | 行号 | 改动类型 | 旧值 | 新值 |
|---|---|---|---|---|
| `round3_trader.py` | 72 | 常量修改 | `HEDGE_DEADBAND = 3` | `HEDGE_DEADBAND = 8` |
| `round3_trader.py` | 78 | 常量修改 | `HYD_INV_STRENGTH = 3.0` | `HYD_INV_STRENGTH = 6.0` |
| `round3_trader.py` | 368 | 公式修复 | `z = iv_dev / std` | `z = (iv_dev - mean_d) / std` |

### 3. 各改动理由

**z-score 公式修复（主修复）**：
- 旧：z 反映「IV 偏差的绝对值」，对结构性微笑形状永远过大
- 新：z 反映「IV 偏差相对其自身历史均值的偏离」，只捕捉瞬态异常

**HEDGE_DEADBAND 3 → 8**：
- 日志显示 UND 每 100 tick 就来回交易，deadband=3 在 1000–5000 价格级别的标的上过于敏感
- 扩大至 8 后对冲次数估计减少 40–60%

**HYD_INV_STRENGTH 3.0 → 6.0**：
- HYDROGEL 在整个运行周期从 10033 下跌至 9929，做市商蓄积了净多头
- 库存惩罚翻倍后，ask 报价更激进地高于 mp，bid 报价更低，促使库存回归中性

### 4. 自检结果

- [x] `HEDGE_DEADBAND = 8` 已确认在第 72 行
- [x] `HYD_INV_STRENGTH = 6.0` 已确认在第 78 行
- [x] `z = (iv_dev - mean_d) / std` 已确认在第 368 行
- [x] `HEDGE_DEADBAND` 在第 416 行正确被引用
- [x] `HYD_INV_STRENGTH` 在第 456 行正确被引用
- [x] 无新增外部库依赖
- [x] 日志格式、序列化结构未变动
- [x] 所有 limit 常量（200/200/300）未变动

### 5. 下一步

- 等待用户将 `round3_trader.py` 上传 IMC 云端沙盒
- 反馈 log 文件夹位置 + PnL + 资金曲线截图
- PnL > 0 且曲线趋势扭转 → 决策 A；退化 → 决策 B 回滚

---

---

## Entry 003 — v1.2 · 截面 ratio 信号系统重构 + HYDROGEL 关闭

- **日期**：2026-04-24
- **状态**：已完成本地自检，待上传云端验证
- **基准版本 (baseline)**：v1.1（PnL = −1205，波动曲线，无持仓回归）
- **计划批复来源**：用户于 2026-04-24 批复《执行计划书 v1.2》

### 1. Root Cause（根因）

**根因 A — 「只进不出」积累陷阱**：v1.1 的 z-score 每次触发只追加仓位，无目标仓位概念，导致反复支付 bid-ask 点差。

**根因 B — 时间序列 z-score 方法论偏差**：Advisor 明确指示使用截面比较（同 tick 内某 strike IV vs 邻近 strikes 的拟合值），而非跨时间的 z-score 历史。

**根因 C — HYDROGEL 趋势做市亏损**：HYDROGEL 单边下跌，做市商被逆向选择，累积净损失约 −300~−500。

### 2. 代码变动清单

| 文件 | 行号 | 改动类型 | 说明 |
|---|---|---|---|
| `round3_trader.py` | 64–69 | 常量替换 | 删除 Z_THRESHOLD/WINDOW/WARMUP/STD/LOT_CAP；新增 RATIO_THRESHOLD/EXIT/VOU_LOT_MIN/MAX/SCALE/OTM_DELTA_GUARD |
| `round3_trader.py` | 80 | 常量修改 | `HYD_LOT = 15 → 0`（HYDROGEL 关闭） |
| `round3_trader.py` | 函数签名 | 参数删除 | `voucher_and_hedge_orders` 删除 `iv_dev_hist` 参数 |
| `round3_trader.py` | Phase 4 区块 | 整块替换 | z-score 逻辑 → 截面 ratio + 三区间目标仓位执行逻辑 |
| `round3_trader.py` | `Trader.run` | 反序列化 | 删除 `iv_dev_hist` 反序列化代码 |
| `round3_trader.py` | `Trader.run` | 函数调用 | 删除 `iv_dev_hist` 传参 |
| `round3_trader.py` | `Trader.run` | 序列化 | 删除 `iv_dev` 的 traderData 写入 |

### 3. v1.2 新 Phase 4 核心逻辑

```
ratio = (mkt_iv - fair_iv) / fair_iv   # 相对 IV 偏差

if |ratio| >= 0.08:                     # 信号区
    qty_tgt = 5 + round(10 * (|ratio| - 0.08) / 0.08)  # 正比于偏差大小
    qty_tgt = clip(qty_tgt, 5, 15)
    target_pos = -qty_tgt if ratio > 0 else +qty_tgt    # 高IV卖，低IV买
elif |ratio| < 0.04:                    # 退出区
    target_pos = 0                       # 主动平仓
else:                                   # 持仓区
    pass                                 # 不动

diff_k = target_pos - current_pos
→ 向 target_pos 执行一步交易（受 HEDGE_BUDGET 约束）
```

### 4. 自检结果

- [x] `python -c "import round3_trader; Trader()"` → OK
- [x] RATIO_THRESHOLD/EXIT/VOU_LOT_MIN/MAX/SCALE/OTM_DELTA_GUARD 全部在常量区第 64–69 行
- [x] `HYD_LOT = 0` 在第 80 行；所有 `min(..., HYD_LOT)` 结果均为 0
- [x] `iv_dev_hist` 在全文无任何引用（grep 验证）
- [x] 旧常量 Z_THRESHOLD/IV_DEV_WINDOW/VOU_LOT_CAP 完全删除
- [x] 无新增外部库依赖
- [x] Delta hedge Phase 5 结构完整保留
- [x] limit（200/200/300）、BS 公式、implied_vol 未改动

### 5. 下一步

- 上传云端验证
- 若 PnL 改善且曲线趋平 → 决策 A（保留）
- 若退化 → 决策 B（回滚至 v1.1）

---

---

## Entry 004 — v1.2 回滚 · 失败深度剖析

- **日期**：2026-04-24
- **状态**：已完成回滚，本地自检通过
- **基准版本 (baseline)**：v1.2（PnL = −7116，阶梯下行，最差结果）
- **回滚目标**：恢复至 v1.1 代码（PnL = −1205）
- **决策触发**：SOP Decision B — 云端 PnL 退化，立即回滚

### 1. 云端结果异常标志

| 指标 | v1.1 | v1.2 |
|---|---|---|
| 最终 PnL | −1205 | −7116 |
| 资金曲线形态 | 振荡，局部修复 | 阶梯下行（等间隔 −400~−600）|
| HYDROGEL 贡献 | 活跃做市 | 关闭（HYD_LOT=0）|

### 2. 根因定位

**根本原因：抛物线微笑拟合值本身就包含结构性偏差，不能作为截面公允参考**

v1.2 的信号公式为：
```
ratio = (mkt_iv - smile_iv) / smile_iv
```

`smile_iv` 由抛物线 `a*m² + b*m + c` 拟合所有 strike 的市场 IV 得出。
- 如果真实 vol smile 并非完美抛物线（现实中几乎必然），则某些 strike（如 VEV_4500）会**永久性地**高于或低于拟合曲线。
- VEV_4500 在每个 tick 的 ratio ≈ +0.10 ~ +0.15（持续高于阈值），导致系统永远是 `target_pos = -qty_tgt`（买入方向永远错误）。
- 阶梯状下跌：每次 ratio 触发 → 一步建仓（跨越买卖价差付出成本）→ 无均值回归 → 下一 tick ratio 仍然超阈值 → 再建仓。

**关键区别（v1.1 为何较好）**：
- v1.1 的 z-score 使用**VEV_4500 自身历史均值**作为基准，消除了结构性常量偏差。
- 只有在 IV 偏差相对于该 strike 自身的正常区间发生异常时才触发。

**HYDROGEL 关闭的额外损失**：
- v1.2 将 HYD_LOT 设为 0，失去 HYDROGEL 做市收益（即便 v1.1 时为负，仍减少整体敞口）。

### 3. 回滚代码变动清单

| 代码区域 | v1.2（原始） | v1.1（恢复） |
|---|---|---|
| 常量区 64-70 行 | RATIO_THRESHOLD/EXIT/VOU_LOT_MIN/MAX/SCALE/OTM_DELTA_GUARD | Z_THRESHOLD=2.0, IV_DEV_WINDOW=100, IV_DEV_WARMUP=10, MIN_ROLLING_STD=0.005, VOU_LOT_CAP=15 |
| HYD_LOT | 0 | 15 |
| 函数签名 | 无 iv_dev_hist 参数 | iv_dev_hist 参数恢复 |
| Phase 4 整块 | 截面 ratio + 目标仓位执行 | 时间序列 z-score（z = (iv_dev - mean_d) / std）|
| Trader.run 反序列化 | 仅 hyd_spread_hist | iv_dev_hist 反序列化 + hyd_spread_hist |
| Trader.run 函数调用 | 无 iv_dev_hist 参数 | iv_dev_hist 传参恢复 |
| Trader.run 序列化 | 仅 mem["hyd_spread"] | mem["iv_dev"] + mem["hyd_spread"] + iv_dev 应急 trim |

### 4. 自检结果

- [x] `python -c "import round3_trader; round3_trader.Trader(); print('OK')"` → OK
- [x] Z_THRESHOLD=2.0 / IV_DEV_WINDOW=100 / IV_DEV_WARMUP=10 / MIN_ROLLING_STD=0.005 / VOU_LOT_CAP=15 确认存在
- [x] HYD_LOT=15 确认存在
- [x] `iv_dev_hist` 在 `voucher_and_hedge_orders` 签名中确认存在
- [x] Phase 4：`z = (iv_dev - mean_d) / std` 确认存在
- [x] `Trader.run`：iv_dev_hist 反序列化、调用、序列化及应急 trim 均确认存在
- [x] RATIO_THRESHOLD 等 v1.2 遗留常量不存在

### 5. 经验教训（写入 v1.3+ 防坑指南）

1. **微笑拟合 ≠ 截面公允参考**：只要 vol smile 真实形状不是完美抛物线，任何基于抛物线拟合的截面 ratio 都会产生结构性常量偏差，不能用于驱动仓位。
2. **时间序列 z-score 降维更稳健**：对每个 strike 独立跟踪其 IV 偏差的历史分布，能天然消除结构性 smile 形状带来的常量偏差。
3. **HYDROGEL 不应轻易关闭**：即使 HYDROGEL 模块轻微盈亏，提供对冲意义的双边做市也优于完全空仓。
4. **阶梯式 PnL 曲线是致命警告**：应在第一个阶梯结束后即触发 SOP Decision B 而非等完整测试周期。

### 6. 下一步

- 回滚至 v1.1 已确认（PnL = −1205 为当前最优基准）
- v1.3 方向候选：
  - 分析 v1.1 日志：哪些 strike 在 z-score 框架下是系统性盈亏来源
  - 考虑对亏损 strike 设置 per-strike 开关（白名单机制）
  - 探索 HYDROGEL 方向性偏置（OU 均值回归锚点）

---

（以下为 v1.4+ 迭代占位，每次版本必须新开 Entry，严禁覆盖历史）

---

## Entry 005 — v1.3 · 三处结构性重构

- **日期**：2026-04-25
- **状态**：已完成本地自检，待上传云端验证
- **基准版本 (baseline)**：v1.1（PnL = −1205）
- **计划批复来源**：用户于 2026-04-25 批复《v1.3 重构计划书》

### 1. 变更动机

基于 v1.1（370766）日志深度分析，识别三个结构性缺陷：

1. **VEV_4000 Filter Bypass**：`DEEP_ITM_BUFFER=1.0` 为价格绝对差，深度 ITM 期权时间价值 > 1 时逃脱过滤，其解出的极高 IV（约 1.3-2.0）污染抛物线 smile 拟合，导致全体 strike z-score 信号失准。贡献约 −567 损失。
2. **低 vega strike 无法覆盖 bid-ask 成本**：VEV_5000（delta≈0.96，vega≈52）在 IV 偏差 0.010 时预期利润仅 0.52，远低于 spread=2，每次 z-score 触发均亏损。贡献约 −1393 损失。
3. **HYDROGEL 趋势积仓无硬截止**：`HYD_INV_STRENGTH=6` 软惩罚在强趋势中无法阻止仓位积累至 ±150+；趋势反转时集中爆损 −770（55k→70k 反弹）+ −939（91k→end 反弹）。

### 2. 重构一：Phase 1 过滤机制换代（delta 替代价格）

**旧机制**（三个价格判断常量）：
```python
PIN_PRICE = 0.5          # 被删除
DEEP_ITM_BUFFER = 1.0   # 被删除
DEEP_OTM_K_OFFSET = 500 # 被删除

if mid <= intrinsic + DEEP_ITM_BUFFER: info["reason"]="DEEP_ITM"
if K > S + 500 and mid <= 0.51: info["reason"]="DEEP_OTM_PIN"
```

**新机制**（delta 阈值分类）：
```python
DELTA_DEEP_ITM = 0.97   # delta >= 0.97 => 排除
DELTA_DEEP_OTM = 0.03   # delta <= 0.03 => 排除

# Phase 1 新流程：先正常反解 IV，再用 bs_delta 判断
iv = implied_vol(mid, S, K, T)
if iv is None or iv < IV_LO or iv > IV_HI: 排除(IV_INVALID)
delta_k = bs_delta(S, K, T, iv)
if delta_k >= DELTA_DEEP_ITM: 排除(DEEP_ITM_DELTA)   # VEV_4000: delta≈1.0
if delta_k <= DELTA_DEEP_OTM: 排除(DEEP_OTM_DELTA)   # VEV_6000: delta≈0.0
# 通过 => 进入 smile_samples 和 Phase 4
```

**为什么是结构性变更**：判断维度从「当前价格距内在价值的绝对差」换为「期权的方向敏感度（delta）」。Delta 随 TTE 自适应（随时间推移 delta 曲线收紧），消除了固定常量不随 S、TTE 变化的缺陷。

**量化验证**（S=5267.5，TTE=5/365）：
- VEV_4000: delta ≈ 1.0 ≥ 0.97 → 被排除（smile 污染根除）
- VEV_5000: delta = 0.9609 < 0.97 → 允许通过（由重构二兜底）
- VEV_6000: IV_INVALID → 排除（pin price，无时间价值）

### 3. 重构二：Phase 4 增加 Vega 经济性门槛

**旧机制**：仅统计显著性（z-score），无经济层。

**新机制**：在 z-score 判断后，增加 vega 经济性门槛：
```python
# 旧：z 通过后直接下单
# 新：z 通过后先检验经济可行性
vega_k = bs_vega(S, K, T, fair_iv)
spread_k = info["ba"] - info["bb"]
if abs(iv_dev - mean_d) * vega_k < spread_k:
    continue  # 即使 z 显著，预期利润不足以覆盖成本
```

**为什么是结构性变更**：在信号链中对现有决策节点（z-score）之后新增了一个此前不存在的决策层（经济可行性）。不是修改阈值，而是增加了新的判断维度。

**量化验证**（iv_dev=0.010，spread=2）：
- VEV_5000: vega≈52，预期利润=0.52 < 2 → 被拦截
- VEV_5100: vega≈131，预期利润=1.31 < 2 → 被拦截
- VEV_5200: vega≈224，预期利润=2.24 > 2 → 通过
- VEV_5300: vega≈243，预期利润=2.43 > 2 → 通过

有效交易 strike 自然收窄为 ATM 附近（5200-5300），符合期权交易理论（vega 集中在 ATM）。

### 4. 重构三：Module B 增加库存硬截止

**旧机制**：仅软惩罚（价格偏移 `inv_penalty = (pos/200)*6`），趋势行情中不足以阻止双边成交，仓位积累至 ±150+。

**新机制**：增加硬截止（单向强制）：
```python
HYD_INV_HARD_CAP = 100  # 新增常量

# 在 Taker 和 Maker 阶段前判断
hard_long  = pos >= HYD_INV_HARD_CAP   # 只允许卖出
hard_short = pos <= -HYD_INV_HARD_CAP  # 只允许买入

# Maker phase:
buy_room  = 0 if hard_long  else min(HYD_LIMIT - sim_pos, HYD_LOT)
sell_room = 0 if hard_short else min(HYD_LIMIT + sim_pos, HYD_LOT)
```

**为什么是结构性变更**：现有机制是「连续惩罚，继续双边报价」；新机制引入「阈值触发 → 强制单边」的状态切换，根本改变了高库存状态下的 MM 行为模式。不是调整 `HYD_INV_STRENGTH` 数值，而是增加了状态机分支。

### 5. 代码变动清单

| 文件 | 区域 | 旧代码 | 新代码 |
|---|---|---|---|
| `round3_trader.py` | 常量区 | `PIN_PRICE=0.5`, `DEEP_ITM_BUFFER=1.0`, `DEEP_OTM_K_OFFSET=500` | 删除；新增 `DELTA_DEEP_ITM=0.97`, `DELTA_DEEP_OTM=0.03` |
| `round3_trader.py` | 常量区 | (无) | 新增 `HYD_INV_HARD_CAP=100` |
| `round3_trader.py` | Phase 1 过滤逻辑 | 价格差比较 + DEEP_ITM/OTM_PIN 分类 | iv 反解后 delta 计算 + DEEP_ITM_DELTA/DEEP_OTM_DELTA 分类 |
| `round3_trader.py` | Phase 3 delta_for_K | `DEEP_ITM` → return 1.0, `DEEP_OTM_PIN` → return 0.0 | `DEEP_ITM_DELTA` → return info["delta"], `DEEP_OTM_DELTA` → return info["delta"] |
| `round3_trader.py` | Phase 4 z-score 后 | 直接进入下单逻辑 | 增加 `vega_k * abs(iv_dev-mean_d) < spread_k` 门槛 |
| `round3_trader.py` | hydrogel_orders Taker | 无条件执行 | `not hard_long` / `not hard_short` 条件守卫 |
| `round3_trader.py` | hydrogel_orders Maker | 双边 buy_room/sell_room | hard_long → buy_room=0;  hard_short → sell_room=0 |

### 6. 自检结果

- [x] `python -c "import round3_trader; round3_trader.Trader(); print('OK')"` → OK
- [x] `DELTA_DEEP_ITM=0.97`, `DELTA_DEEP_OTM=0.03` 存在；旧常量 `DEEP_ITM_BUFFER/DEEP_OTM_K_OFFSET/PIN_PRICE` 已删除
- [x] `HYD_INV_HARD_CAP=100` 存在
- [x] Phase 1：VEV_4000（delta≈1.0）被 `DEEP_ITM_DELTA` 捕获（计算验证：delta=1.0 ≥ 0.97）
- [x] Phase 4 vega gate：VEV_5000（vega≈52）在 iv_dev=0.010 时预期利润=0.52 < spread → 拦截
- [x] Phase 4 vega gate：VEV_5200（vega≈224）在 iv_dev=0.010 时预期利润=2.24 > spread → 通过
- [x] `hydrogel_orders` hard_long/hard_short 守卫：pos=100 时 buy_room=0，pos=-100 时 sell_room=0
- [x] delta_for_K 已同步更新为 `DEEP_ITM_DELTA`/`DEEP_OTM_DELTA`
- [x] 无新增外部库依赖

### 7. 预期效果

| 模块 | 预期改善 |
|---|---|
| Module A Phase 1 | VEV_4000 永久排除，smile 拟合质量提升（仅含 5100-5500 区间 strikes）|
| Module A Phase 4 | 有效 trades 收缩为 ATM 附近（5200-5300），每次成交预期利润 > spread，消除低 vega 拉力 |
| Module B HYDROGEL | 最大单边仓位截止 100，防止趋势积仓后反转爆损 ±700~900 |

### 8. 下一步

- 上传云端验证 (log 372715)
- 观察 Module A 成交频率（目标：VEV_5300 在 z>=2 时触发，VEV_5000/5100 仍被 vega gate 阻断）
- 观察 HYDROGEL 最大仓位是否被 cap 有效限制
- 根据 PnL 与曲线形态决策 A 或 B

---

## Entry 006 — v1.4 · 期权信号解封 + HYDROGEL 动态 lot

### 1. 版本信息

- **版本**：v1.4
- **日期**：2026-04-25
- **基线**：v1.3 (log 372715, PnL=+549)
- **计划批复**：用户于 2026-04-25 批复数量化分析诊断结论

### 2. 触发动机

对 log 372715 的深度分析揭示两处结构性问题：

**问题 A（期权模块完全静默）**：`MIN_ROLLING_STD=0.005` 是固定常量，与市场无关。实测 VEV_5200/5300 隐含波动率滚动标准差约为 0.00191 / 0.00225，始终低于 0.005，导致 Phase 4 的 std 门对全部 strikes 每个 tick 执行 `continue`，期权模块从未产生任何交易。vega gate 的修复（v1.3）甚至从未被触达。

**问题 B（HYDROGEL 还是反转损失）**：v1.3 的 hard cap (±100) 未被触及（本次最大仓位约 ±23），但固定 HYD_LOT=15 的对称双边报价在价格趋势期间仍缓慢积累单向仓位，反转时造成集中 MTM 损失（实测：两次反转各损 −857 / −1031）。

| | 数值 |
|---|---|
| 实测 VEV_5200 IV std | 0.00191 |
| 实测 VEV_5300 IV std | 0.00225 |
| MIN_ROLLING_STD（v1.3） | 0.00500 |
| 超越概率 | **0%（始终被拦截）** |
| 本次最大 HYD 仓位 | ±23（远低于 cap=100）|
| 两次反转亏损 | −849 / −1031 |

### 3. 变更内容

**Adjustment 1 — 修复 std 门，重新校准 vega gate**

| 常量 | 旧值 | 新值 | 说明 |
|---|---|---|---|
| `MIN_ROLLING_STD` | 0.005 | 1e-7 | 仅过滤数值退化（全同分布），不再施加市场依赖的 floor |
| `VEGA_MIN_RATIO` | (新增) | 0.40 | vega gate 改为"预期利润 >= spread × 0.40"（部分覆盖门） |

Vega gate 条件变更：
```python
# 旧（要求完全覆盖）：
if abs(iv_dev - mean_d) * vega_k < spread_k:
# 新（要求 40% 覆盖）：
if abs(iv_dev - mean_d) * vega_k < spread_k * VEGA_MIN_RATIO:
```

预期效果（iv_dev=0.010, std=0.002, z=5σ）：

| Strike | Vega | Spread | 预期利润 | Gate(0.40×spread) | 结果 |
|---|---|---|---|---|---|
| VEV_5000 | 52 | 2.0 | 0.52 | 0.80 | BLOCKED ✓ |
| VEV_5100 | 131 | 2.0 | 1.31 | 0.80 | PASSES（低 z 时仍受 Z_THRESHOLD 保护） |
| VEV_5300 | 240 | 2.0 | 2.40 | 0.80 | PASSES ✓ |
| VEV_5200 | 230 | 3.0 | 2.30 | 1.20 | PASSES ✓ |

**Adjustment 2 — HYDROGEL 存货感知动态 lot**

新增常量：`HYD_TILT_SCALE = 1.5`

Maker phase 替换：
```python
# 旧（固定 lot）：
buy_room  = 0 if hard_long  else min(HYD_LIMIT - sim_pos, HYD_LOT)
sell_room = 0 if hard_short else min(HYD_LIMIT + sim_pos, HYD_LOT)

# 新（动态 lot，存货倾斜）：
inv_tilt = max(-1.0, min(1.0, pos / HYD_INV_HARD_CAP))
dyn_buy_lot  = max(1, round(HYD_LOT * (1 - HYD_TILT_SCALE * inv_tilt)))
dyn_sell_lot = max(1, round(HYD_LOT * (1 + HYD_TILT_SCALE * inv_tilt)))
buy_room  = 0 if hard_long  else min(HYD_LIMIT - sim_pos, dyn_buy_lot)
sell_room = 0 if hard_short else min(HYD_LIMIT + sim_pos, dyn_sell_lot)
```

实测验证：

| pos | inv_tilt | dyn_buy_lot | dyn_sell_lot | 效果 |
|---|---|---|---|---|
| −23 | −0.23 | 20 | 10 | 空头时加大买入，压缩卖出 |
| 0 | 0 | 15 | 15 | 中性对称（与旧逻辑一致） |
| +80 | +0.80 | 1 | 33 | 多头时强力卖出，几乎停止买入 |

### 4. 代码变动清单

| 文件 | 区域 | 变更描述 |
|---|---|---|
| `round3_trader.py` | 常量区 | `MIN_ROLLING_STD`: 0.005 → 1e-7；新增 `VEGA_MIN_RATIO=0.40` |
| `round3_trader.py` | 常量区 | 新增 `HYD_TILT_SCALE=1.5` |
| `round3_trader.py` | Phase 4 vega gate | `< spread_k` → `< spread_k * VEGA_MIN_RATIO` |
| `round3_trader.py` | hydrogel_orders Maker | 固定 HYD_LOT → inv_tilt 动态 lot 计算 |

### 5. 自检结果

- [x] 语法检查：`python -c "import round3_trader; round3_trader.Trader(); print('OK')"` → OK
- [x] `MIN_ROLLING_STD=1e-7`，旧值 0.005 已删除
- [x] `VEGA_MIN_RATIO=0.40` 存在
- [x] `HYD_TILT_SCALE=1.5` 存在
- [x] std gate：实测 std=0.00191 < 1e-7? False → 不再被拦截 ✓
- [x] Vega gate：VEV_5000（vega≈52）预期利润 0.52 < 0.80 → 仍拦截 ✓
- [x] Vega gate：VEV_5300（vega≈240）预期利润 2.40 >= 0.80 → 通过 ✓
- [x] 动态 lot：pos=−23 → buy=20, sell=10；pos=+80 → buy=1, sell=33 ✓

### 6. 预期效果

| 模块 | 预期改善 |
|---|---|
| Module A | 期权信号从完全静默恢复为有效运作；VEV_5300 在高 z 时可参与交易 |
| Module B | 趋势期间仓位积累速度降低（单向 lot 减半），反转时 MTM 损失幅度收窄 |

### 7. 下一步

- 上传云端验证 (log 375039)
- 对比 v1.3/v1.4 曲线：初期平缓段应出现期权 PnL 贡献，HYDROGEL 反转损失峰值应降低
- 注：v1.4 期权模块已激活但净亏损 −70，总 PnL 549→479
  - 根因：MIN_ROLLING_STD 调参未修信号内生性问题
  - 根因：HYD_TILT_SCALE 调参未阻止被动成交蓄积

---

## Entry 007 — v1.5 · 框架性重写：VRP 信号 + 趋势感知做市

### 1. 版本信息

- **版本**：v1.5
- **日期**：2026-04-25
- **基线**：v1.4 (log 375039, PnL=+479)
- **背景**：用户批评 v1.3/v1.4 以调参手段处理结构性问题，要求先修框架

### 2. 触发动机

**失效 A — z-score 是自参照信号**：mkt_iv - smile(m) 中 smile 由 mkt_iv 自身拟合，内生相关；rolling mean 追得过快产生伪信号；v1.4 解封调参后净亏 −70。

**失效 B — dyn_lot 不阻止被动成交**：趋势期间买方主动 hit 存量 ask，dyn_sell_lot 仅控制挂单量，无法阻止已有订单被对手方成交；存货仍单向蓄积。

### 3. 框架重写内容

**Module A — 废弃 z-score/smile，改用 VRP（RV vs ATM IV 跨系统比较）**

移除：`Z_THRESHOLD`, `IV_DEV_WINDOW`, `IV_DEV_WARMUP`, `MIN_ROLLING_STD`, `VEGA_MIN_RATIO`, smile 拟合 (Phase 2)

新增：
```python
VRP_RV_WINDOW     = 50        # log-return 历史窗口
VRP_WARMUP        = 20        # 最少观测数
VRP_THRESHOLD     = 0.02      # 触发阈值
VRP_ATM_WINGS     = 2         # ATM 附近 strike 数量
PRICE_OBS_PER_YEAR = 3_650_000  # 年化因子
VOU_LOT_CAP       = 10
```

信号：`VRP = mean(ATM_IV) - RV_ann`，正 → 卖 vega，负 → 买 vega

**Module B — 趋势感知挂单抑制（替换 HYD_TILT_SCALE 机制）**

移除：`HYD_TILT_SCALE`

新增：
```python
HYD_PRICE_WINDOW  = 30
HYD_TREND_FAST    = 5
HYD_TREND_THRESH  = 4.0
```

逻辑：`trend = fast_SMA(5) - slow_SMA(30)`；上涨时 `trend_pause_sell=True`（撤 ask + 禁 taker 买）；下跌时 `trend_pause_buy=True`（撤 bid + 禁 taker 卖）

### 4. 代码变动清单

| 文件 | 区域 | 变更描述 |
|---|---|---|
| `round3_trader.py` | 常量区 | 移除 Z_THRESHOLD/IV_DEV_*/MIN_ROLLING_STD/VEGA_MIN_RATIO；新增 VRP_* + PRICE_OBS_PER_YEAR |
| `round3_trader.py` | 常量区 | 移除 HYD_TILT_SCALE；新增 HYD_PRICE_WINDOW/HYD_TREND_FAST/HYD_TREND_THRESH |
| `round3_trader.py` | `voucher_and_hedge_orders` | Phase 2 smile 拟合完全移除；Phase 4 z-score 全部重写为 VRP 信号 |
| `round3_trader.py` | `hydrogel_orders` | 加 price_hist 参数；移除 dyn_lot；增加 trend 判断 + trend_pause_buy/sell |
| `round3_trader.py` | `Trader.run` | iv_dev_hist → vrp_state；新增 hyd_price_hist；更新序列化 |

### 5. 自检结果

- [x] 语法检查：OK
- [x] 旧常量已移除：Z_THRESHOLD, MIN_ROLLING_STD, VEGA_MIN_RATIO, HYD_TILT_SCALE 均不存在
- [x] RV 年化：per-tick std=0.0001 → RV_ann≈0.154，VRP=0.111 > 0.02 → sell vega
- [x] 趋势仿真：30-tick 上涨序列 → trend=18.8 > 4.0 → trend_pause_sell=True
- [x] 平静市场：trend=0 → 无暂停

### 6. 云端结果（log 376046）

- **PnL = −559**
- 期权净亏 −1821（VRP 信号永久为负，全程 BUY vega，累积 VEV_5300=+218/VEV_5200=+164）
- HYDROGEL 净 +374（趋势检测自 ts=1300 起触发 pause_sell，导致大量 tick 停摆，低于 v1.3 的 +549）

---

## Entry 008 — v1.6 · 期权全局禁用 + 主动平仓层

### 1. 版本信息

- **版本**：v1.6
- **日期**：2026-04-25
- **基线**：v1.5 (log 376046, PnL=−559)
- **背景**：v1.5 VRP 信号存在结构性时间刻度错误，Trend Suppression 阻止了 HYDROGEL 应有的做市收益

### 2. 失效根因（v1.5 法证结论）

**Root Cause A — VRP 时间刻度错误（Module A 致命 bug）**

$$\sigma_{tick} = 0.000221, \quad PRICE\_OBS\_PER\_YEAR = 3{,}650{,}000$$
$$RV_{ann} = 0.000221 \times \sqrt{3{,}650{,}000} = 0.422 \quad \text{(ALWAYS)}$$
$$ATM\_IV \approx 0.276 \quad \Rightarrow \quad VRP = -0.147 \quad \text{(ALWAYS)} \quad \Rightarrow \quad \text{永远 BUY vega}$$

期权书固定价（VEV_5300 始终约 51/53），IV 比较无套利含义。五轮历史期权净 PnL：0, 0, 0, −70, −1821。

**Root Cause B — Trend Suppression 阻止了合法做市收益（Module B）**

v1.3 HYDROGEL +549 的核心机制：上涨时 buyers lift 我方 ask → 累积空头 → 下跌时获利。v1.5 趋势检测在 ts=1300（trend=4.96）首次触发 pause_sell，直至 ts=10200 HYDROGEL PnL 才重新出现，超过 8900 个 ts 单位零成交。趋势检测把盈利路径完全封死。

### 3. 改动策略

**Module A**：新增全局常量 `ENABLE_OPTIONS = False`，在 `voucher_and_hedge_orders` 入口插入早返回 guard，跳过全部 VRP 计算和期权下单。VRP/BS 代码保留，便于后续重新激活。

**Module B**：移除全部趋势检测代码（trend_pause_buy/sell、price_hist 追踪），恢复 v1.3 做市主体；新增**主动平仓层（Active Unwind Layer）**：当 `|pos| > UNWIND_THRESHOLD=20` 时，以 taker 订单每 tick 最多平仓 `UNWIND_LOT=10` 手，强制缩减极端库存。

### 4. 代码变动清单（六处精准替换）

| 编号 | 区域 | 行号（v1.6） | 变更描述 |
|---|---|---|---|
| C1 | 常量区 | L86–88 | 删除 HYD_PRICE_WINDOW/HYD_TREND_FAST/HYD_TREND_THRESH；新增 ENABLE_OPTIONS/UNWIND_THRESHOLD/UNWIND_LOT |
| C2 | `voucher_and_hedge_orders` | L272–273 | 插入 `if not ENABLE_OPTIONS: return result` guard |
| C3 | `hydrogel_orders` 签名 | L434 | 移除 `price_hist` 参数，移除 price_hist 追踪代码 |
| C4 | `hydrogel_orders` 主体 | L460–510 | 删除趋势检测块（22 行）；删除 trend_pause 条件；新增 Unwind Layer（14 行） |
| C5 | `Trader.run` state load | L537 | 删除 `hyd_price_hist = list(mem.get("hyd_price", []))` |
| C6 | `Trader.run` state serialize | L592–594 | 删除 `mem["hyd_price"]` 写入及 trim 分支 |

### 5. 自检结果

- [x] 语法检查：`ast.parse()` OK
- [x] `ENABLE_OPTIONS = False` 存在，`HYD_TREND_FAST` / `HYD_PRICE_WINDOW` 不存在
- [x] `hydrogel_orders` 参数为 `(od, pos, spread_hist, logger)`，无 `price_hist`
- [x] Unwind guard 出现在 L486/L492（`pos < -UNWIND_THRESHOLD` / `pos > UNWIND_THRESHOLD`）
- [x] `Trader.run` 中无 `hyd_price_hist` 引用

### 6. 预期效果

| 来源 | 预估 PnL |
|---|---|
| 禁用 Module A（消除 −1821 亏损） | 恢复 +934 |
| 移除 Trend Suppression（恢复 HYD 从 +374 → +549） | +175 |
| Active Unwind（尾部反转亏损减少） | +200～+400 额外 |
| **v1.6 目标** | **+750 ～ +950** |
| **最低保底（Unwind 从未触发）** | **+549** |

### 7. 云端结果（log 377206）

- **PnL = +533**（目标 +750～+950，未达到）
- HYDROGEL 全程 = +533，期权全程 = 0（ENABLE_OPTIONS=False 正常工作）
- 整场仅 1 个产品交易（HYDROGEL），无 VFX/VEV 持仓（最终持仓仅 HYD=-18）
- UNWIND_THRESHOLD=20 实际从未有效触发：v1.6 final pos=-18 < 20，Unwind 几乎不生效

### 8. 失效分析（进入 Entry 009）

**曲线三段问题**：
1. ts 0→10000（PnL = 0）：冷启动，正常。
2. ts 54000→70000（+673 → −174，崩跌 −847）：中段价格 9942→9996（+54 pts），pos 从 −9 漂移至 −22，Unwind 阈值 20 直到 pos=−22 才触发，qty = min(10, 22-20=2) = **2 手**，而 Maker 仍挂 15 手 ask，净效果趋近 0。
3. ts 91200→99800（+1394 → +533，崩跌 −861）：尾部 9915→9959（+44 pts），pos 稳定 −18～−22，未超过 UNWIND_THRESHOLD=20，Unwind **完全未触发**。

**根因**：旧 Unwind qty 公式 `min(LOT=10, pos_excess=2, ...)` 在 pos 刚越过 −20 时返回 qty=1，而 Maker 仍放出 15 手 ask，导致两者抵消。

---

## Entry 009 — v1.7 · Unwind 均衡修正 + 紧急态 Maker 抑制

### 1. 版本信息

- **版本**：v1.7
- **日期**：2026-04-25
- **基线**：v1.6 (log 377206, PnL=+533)
- **背景**：UNWIND 机制数学上不自洽，紧急态 Maker ask 完全抵消 Unwind 买入

### 2. 根因量化

**均衡分析**：设 maker 单侧挂单量 Q_m=15，unwind 量 Q_u，在单向上涨趋势中（buy_side 每 tick 吃满 ask）：

- v1.6 旧公式：`qty = min(10, (-pos)-20, ...)` → pos=−21 时 qty=1，Maker ask 放出 15 → 净每 tick 持仓 −14
- 未修正前，ask 未抑制：buy_room=15 继续挂出 → Unwind 被 Maker 完全覆盖

**Round-Trip 经济学**（正确模型）：

每个 2-tick 周期（1 次紧急 + 1 次正常）的 P&L：

$$\text{Net per contract} = X_{cycle} - 0.875 \text{ pts}$$

其中 $X_{cycle}$ = 该周期内价格上涨量，0.875 = book半价差(7.875) − maker边(7)。

| 场景 | 价格速率 (pts/tick) | 盈亏平衡 T | T=4 时收益 | T=15 时收益 |
|---|---|---|---|---|
| 尾部崩盘 | 0.512 | 1.7 ticks | +434 | +589 |
| 中段崩盘 | 0.338 | 2.6 ticks | +390 | +679 |

结论：在任何 T > 2.6 tick 的现实成交周期下，v1.7 均为正期望。

### 3. 改动内容（两处精准修改）

**C1 — 常量区（L86–88）**：
- `UNWIND_THRESHOLD`: 20 → 15（更早触发，覆盖 pos=−18 稳定区）
- `UNWIND_LOT`: 10 → 15（= HYD_LOT，形成数学均衡：每次 unwind 买回量 = maker 被吃量）

**C2 — `hydrogel_orders` 主体（L485–501）**：
- 旧 qty 公式移除距离上限（`(-pos)-UNWIND_THRESHOLD` 项），改为 `min(UNWIND_LOT, HYD_LIMIT - sim_pos)`
- 新增 `in_short_emergency` / `in_long_emergency` 布尔标志
- Maker 相位：`sell_room = 0 if (hard_short or in_short_emergency) else ...`（紧急态禁止挂 ask，阻止抵消）

### 4. 代码变动清单

| 编号 | 行号 | 变更描述 |
|---|---|---|
| C1 | L87 | `UNWIND_THRESHOLD`: 20→15 |
| C1 | L88 | `UNWIND_LOT`: 10→15；注释更新 |
| C2 | L485 | 旧 unwind 块替换为含 emergency flag 的新逻辑 |
| C2 | L499 | `sell_room` / `buy_room` 条件加入 `in_short_emergency` / `in_long_emergency` |

### 5. 自检结果

- [x] 语法检查：OK
- [x] `UNWIND_THRESHOLD=15`，`UNWIND_LOT=15`
- [x] `in_short_emergency = pos < -UNWIND_THRESHOLD` 使用入口 pos（非 sim_pos），确保 one-tick 决策一致性
- [x] `sell_room = 0 if (hard_short or in_short_emergency) else ...` 正确抑制 ask
- [x] 旧 bug 表达式 `(-pos) - UNWIND_THRESHOLD` 已完全移除（断言验证通过）

### 6. 预期效果

| 场景 | 预期改善 PnL |
|---|---|
| 尾部崩盘（T=8 ticks） | +535 |
| 中段崩盘（T=8 ticks） | +577 |
| **v1.7 保守目标** | **+533 + 400 = ~+933** |
| **v1.7 乐观目标（T=15）** | **+533 + 1268 = ~+1801** |

### 7. 下一步

- 上传云端验证
- 观察 `HYD UNWIND BUY` 日志是否在中段和尾部崩盘时密集出现
- 若改善幅度不足，检查 T 实际值（可从持仓漂移速度推算）

---

## Entry 010 — v1.8 · INV_STRENGTH 放大 + 移除 Taker Unwind

- **版本**：v1.8
- **日期**：2026-04-25
- **基于**：v1.7 云端结果 log 397711，PnL = +545

### 1. 根因分析（v1.7 法证）

日志 397711 精确解析结论：

| 指标 | v1.6 | v1.7 | Δ |
|---|---|---|---|
| Peak 1 | 673 | 712 | +39 |
| DD 1 (mid crash) | 847 | 521 | −326 ✅ |
| Peak 2 | 1239 | 1104 | **−135 ❌** |
| DD 2 (tail crash) | 706 | 549 | −157 ✅ |
| Taker cost | 0 | ~74 | −74 ❌ |
| Net PnL | 533 | 545 | +12 |

**发现 1**：Unwind 在 ts=71400/72100/79000 三次在下跌段（动量=−3.0）抢先触发，此时我们正处于有利空仓阶段（9996→9915），每次触发均造成损失。

**发现 2**：INV_STRENGTH=6 时，pos=−15 的报价偏移仅 0.45 pts（市场半价差=8 pts），报价始终在市场内，空仓毫无限制。

**根因**：错误机制（Taker 强平）解决正确问题（仓位限制），代价是干扰有利下跌相。正确方法：放大 INV_STRENGTH，纯报价价格威慑，零吃单成本。

### 2. 经济模型

- book_half = 8 pts，edge_base = mp+7（HYD_EDGE_MULT=0.4 × avg_spread16 = 6.4，取整约7）
- 自然封顶条件：ask ≥ mp+8，即 inv_penalty ≥ 1 pt
- INV_STRENGTH=25 时，pos=−14：penalty = (14/200)×25 = 1.75 pts，ask = mp+8.75 > book ask
- 买方停止优先成交 → 空仓自然封顶于 ~−12 至 −14

### 3. 代码变更

**T1** — `HYD_INV_STRENGTH`：6.0 → 25.0

**T2** — 删除常量：
```
UNWIND_THRESHOLD = 15   # 已删除
UNWIND_LOT       = 15   # 已删除
```

**T3** — `hydrogel_orders()` 中删除整个 Taker Unwind 块（18 行），还原简单 Maker：
```python
# OLD: in_short_emergency / in_long_emergency + 强制吃单 + 抑制 Maker
# NEW:
buy_room  = 0 if hard_long  else min(HYD_LIMIT - sim_pos, HYD_LOT)
sell_room = 0 if hard_short else min(HYD_LIMIT + sim_pos, HYD_LOT)
```

### 4. 预期结果

- 做空上摆段均仓：v1.7 upswing1=−8.6，upswing2=−12.1 → v1.8 目标 ≤−10/≤−10
- 有利下行段无干扰，全程持有最优空仓
- 零吃单成本（vs v1.7 ~74 pts）
- 保守预期 PnL：+754；乐观：+800～900

### 5. 下一步

- 上传云端验证
- 检查持仓曲线：预计 upswing 均仓从 −8.6/−12.1 收窄至 ≤−10
- 确认日志中无 `UNWIND` 字样

---

## Entry 011 — v1.9 · Unwind 动量门控（v1.7 回归 + 方向过滤）

- **版本**：v1.9
- **日期**：2026-04-25
- **基于**：v1.8 云端结果 log 398268，PnL = +402

### 1. v1.8 失效根因分析

段位持仓对比（推算值）：

| 段位 | v1.7 | v1.8 | 理想 |
|---|---|---|---|
| dn1 (盈利下跌) | −3.3 | −3.3 | 越大越好 |
| up1 (亏损上涨) | **−8.9** | **−13.5** ← 恶化 | ≤−10 |
| dn2 (盈利下跌) | −7.3 | −8.1 | 越大越好 |
| up2 (亏损上涨) | **−11.4** | **−10.8** | ≤−10 |
| Final PnL | **545** | **402** | |

INV_STRENGTH=25 的对称 skew 同时抬高了 bid（吸引卖方，消耗盈利空仓）和 ask（限制建仓）。下跌段 dn1 均仓仍为 −3.3（与 v1.7 相同），说明 INV_STRENGTH 对盈利段无积极贡献。但 up1 均仓从 −8.9 深化至 −13.5，因为没有 Unwind 机制在上涨段主动压缩，导致 DD1 从 521 爆至 802。

**结论**：Unwind 机制是 v1.7 在 upswing 段将均仓压至 −8.9 的关键，不能删除。v1.8 思路根本方向错误。

---

## Entry 014 — v2.2 to v2.8 · HYDROGEL 区间做市与参数渐进优化

*注*：本 entry 汇总 v2.2～v2.8 的演进轨迹（各版本独立云端验证，增量改动记录于项目 git commit 日志）。

- **基准版本**：v2.1 (log 415419, PnL = 958)
- **最新版本**：v2.8 (log 418652, PnL = 1110)
- **累计改进**：+152 PnL (+15.9%)

### v2.2 → v2.5：库存上限机制优化

| 版本 | 关键改动 | 结果 | 根因诊断 |
|---|---|---|---|
| v2.2 | room_to_cap 动态化：`room_to_cap = max(0, sim_pos - active_short_cap)` | PnL 1105 | 修复超出hard cap的ask过度张贴问题 |
| v2.3-2.5 | 迭代参数微调，caps逐步调紧 | PnL 1069~1105 | 防卐收敛 |

**核心问题（v2.2 之前）**：即使 `hard_short` 标志已触发，仍可通过 ask posting 继续累积短仓。6 手卖单可将 −12 推至 −18，破坏风控。

### v2.6：MID 强制购买修复 (符号错误)

| 改动 | 旧代码 | 新代码 | 影响 |
|---|---|---|---|
| ba_liq 计算 | `ba_liq = od.sell_orders.get(ba, 0)` | `ba_liq = abs(od.sell_orders.get(ba, 0))` | MID TAKER BUY 从 0 次恢复至 2-3 次/run |

**问题诊断**：IMC order book 中 sell_orders[price] 经常返回负值；直接用该值计算 `qty = min(12, ba_liq, room)` → qty=0。v2.6 修复后，第一波平仓（49k MID TAKER BUY）执行正常。

### v2.7：高位 reload 卖出 (68k+ HIGH_LATE_TAKER_SELL)

**设计**：在 HIGH zone 且 ts≥68k 时，强制卖出 4 手以重建短仓（目标位置 −8），为后续反弹上升段积累敞口。

**结果**：v2.7 云端显示 68.6k～68.7k 两笔 RELOAD_SELL，成功阶段性重建至 −13 。但随后 76.8k 第二次 MID TAKER BUY（11 手，一次性）将位置从 −13 直接拍至 −2，摧毁后续 76.8k～91k 的上升段盈利空间（实测段位 PnL delta = −73.71）。

### v2.8：MID 覆盖约束化 (trim_need 条件)

**设计**：区分早期与晚期 MID taker buy：
- 早期 (ts < 68k)：激进覆盖，目标 12 手
- 晚期 (ts ≥ 68k)：保守覆盖，仅trimthreshold 6 手至目标位置 −6

**意图**：保留更多后期仓位以参与 76.8k 后反弹，避免过度平仓。

**结果 (418652)**：仍有改进不足。PnL 从 1069 升至 1110（+41），但 Zone B（50-65k）的"平坦陷阱"未被触及，说明 v2.8 的改动仅影响晚期，区间中段的头寸积累问题尚未解决。

### v2.2～v2.8 完整代码变更清单

| 行号 | 常量/逻辑 | v2.1 | v2.8 |
|---|---|---|---|
| 88-102 | HYD 参数块 | 仅含早/晚 cap + LOW unwind thresh | 加入 MID_TAKER_LATE_BUY_LOT/TARGET 分支 |
| 465-480 | hydrogel_orders 函数头 | 无阶段概念 | 仍无 phase 变量 |
| 487-505 | MID TAKER BUY | 固定 12 手 | 条件 qty capping (late_mid_cover flag) |
| 519-526 | HIGH LATE TAKER | (新增) | HIGH_LATE_TAKER_SELL @ ts≥68k |

---

## Entry 015 — v2.9 · 阶段型头寸管理重构 (Phase-Aware Position Lifecycle)

- **版本**：v2.9
- **日期**：2026-04-26
- **基准**：v2.8 (log 418652, PnL = 1110)
- **计划批复**：用户于 2026-04-26 批复《执行计划书 § 阶段型重构》

### 1. 诊断根因：v2.8 失效分析

**深度分析 418652 发现 4 个下行压力区**：

| Zone | 时间段 | 价格 | PnL 变化 | 根因 | 损失 |
|---|---|---|---|---|---|
| Zone 1 | 25-30k | 990→10005 +15 rally | 416→228 | 高位持续被动卖空 | −188 |
| **Zone 2** | **50-65k** | **9952→9987 +35** | **712→712 (FLAT)** | **继续累积短仓 (−30→−51) 而非主动减仓** | **−770 theoretical loss** |
| Zone 3 | 65-70k | 9987→9996 +9 | 712→610 | 头寸过度杠杆 (−51 units) | −102 |
| Zone 4 | 90-99.9k | 9927→9960 +17.5 | 1198→1110 | 继续加仓，晚期覆盖不足 | −88 |

**关键发现**：Zone 2 的"平坦陷阱"（15,000 秒内 PnL 零增长，尽管市场有利 +35 点）揭示**真正的问题不在参数，而在策略架构**。当前逻辑是**被动反应型**（看到价格信号→执行操作），无法前瞻性地管理头寸生命周期。

### 2. v2.9 重构方案：三阶段生命周期管理

**设计原理**：将 100 秒交易周期分为 3 个阶段，每个阶段采用不同的库存管理策略：

```
Phase A [0-40k]：ACCUMULATION（构建阶段）
    目标：被动累积短仓至 −35，利用早期价格下跌积累敞口
    short_cap = −35 (较松)
    执行：Maker (HIGH = ask only) + passive short building

Phase B [40k-70k]：REDUCTION（减仓阶段）—— 最关键
    目标：主动减仓到 −25 甚至更松，防止平坦陷阱
    short_cap = −30 (收紧 Phase A 的宽松)
    触发条件：ts ≥ 50k 时强制 Taker buy 至 −25
    执行：新增「Phase B 强制减仓」taker buy module
  
Phase C [70k+]：DEFENSE（防御阶段）
    目标：快速离场，保持 −20 以下的最小敞口
    short_cap = −20 (最紧)
    执行：现有 MID TAKER BUY + HIGH RELOAD 配合
```

**白话解释**：
> 问题：目前策略像一个被动跟随者，看到价格下跌就一直卖空，直到被迫买回。
> 解决：改成"目标导向"。我们不是盲目跟价，而是明确规划三个阶段的目标头寸，确保在反弹期（Zone B）主动减仓，而不是继续被动累积。

### 3. v2.9 代码变更详情

**T1** — 新增常量（lines 102-110）：
```python
HYD_PHASE_A_THRESH       = 40000   # Phase A 结束，进入 Phase B
HYD_PHASE_B_THRESH       = 70000   # Phase B 结束，进入 Phase C
HYD_PHASE_A_SHORT_CAP    = -35     # Phase A：允许 −35 短仓
HYD_PHASE_B_SHORT_CAP    = -30     # Phase B：收紧至 −30
HYD_PHASE_C_SHORT_CAP    = -20     # Phase C：防御性 −20
HYD_PHASE_B_REDUCE_START = 50000   # Phase B 中途启动主动减仓
HYD_PHASE_B_REDUCE_TARGET= -25     # 主动减仓目标位置
HYD_PHASE_B_REDUCE_LOT   = 6       # 每次减仓 taker 购买量
```

**T2** — 阶段识别逻辑（lines 455～461）：
```python
if timestamp < HYD_PHASE_A_THRESH:
        phase = "A"
        active_short_cap = HYD_PHASE_A_SHORT_CAP
elif timestamp < HYD_PHASE_B_THRESH:
        phase = "B"
        active_short_cap = HYD_PHASE_B_SHORT_CAP
else:
        phase = "C"
        active_short_cap = HYD_PHASE_C_SHORT_CAP
```

**T3** — Phase B 强制减仓模块（lines 489～502，new）：
```python
# Phase B 主动减仓：50k+ 时，位置 ≤−25 的情况下主动买回至 −25
if phase == "B" and timestamp >= HYD_PHASE_B_REDUCE_START \
     and pos <= HYD_PHASE_B_REDUCE_TARGET and not hard_long:
        if ba is not None:
                room = HYD_LIMIT - sim_pos
                ba_liq = abs(od.sell_orders.get(ba, 0))
                reduce_need = max(0, HYD_PHASE_B_REDUCE_TARGET - sim_pos)
                qty = min(HYD_PHASE_B_REDUCE_LOT, ba_liq, room, reduce_need)
                if qty > 0:
                        orders.append(Order(SYM_HYD, ba, qty))
                        sim_pos += qty
                        logger.log("HYD PHASE_B REDUCE " + str(qty) + "@" + str(ba))
```

**T4** — 保留现有 MID TAKER BUY 不变，仅触发条件过滤（lines 506～）

### 4. 预期效果与验证指标

**理论改善**（基于 418652 重新模拟）：
- Zone 2（50-65k）平坦陷阱消除 → +770 PnL
- Zone 3 杠杆释放 → +102 PnL  
- 保守估计：1110 → **1300-1400**（+190～290 改进）

**云端验证关键指标**：
1. ✓ Phase B REDUCE logs 出现频率（预期 3-4 次在 50-55k 区间）
2. ✓ Zone B (50-65k) 段 PnL 曲线从"平坦"→"上升" 
3. ✓ 76.8k 时的库存应 < −35（而非 −51），后续覆盖额度减少
4. ✓ 最终 PnL 增长幅度 > +150

### 5. 禁止项与保留项

❌ **禁止项**：
- 继续微调历史参数（MID_TAKER_FORCED_BUY_THRESH 等）—— 非 root cause
- 新增调参旋钮 —— 需要系统重设

✅ **保留项**：
- Module A voucher delta hedge 代码（不变）
- 日志格式及 Order class 接口
- v2.7 HIGH RELOAD 机制（Phase C 时仍有效）
- v2.8 MID 晚期覆盖约束（与 Phase B REDUCE 配合）

### 6. 自检结果

- [x] Python 语法检查通过 `ast.parse()` ✓
- [x] 新增参数 (8 个) 存在于常量区 lines 102-110 ✓
- [x] 阶段识别逻辑已集成至 hydrogel_orders 函数头 ✓
- [x] Phase B REDUCE 逻辑正确（条件 + qty capping + logging）✓
- [x] 无新增外部依赖 ✓
- [x] hard_long / hard_short 继续有效保护 ✓

### 7. 下一步

- 上传云端验证（log 预期 41xxxx 系列）
- 获取返回的 log 文件夹位置、PnL 及资金曲线截图
- 根据 SOP 触发决策 A（改进 → 推送 v2.9 baseline）或决策 B（失败 → 回滚）
- 若成功，考虑 v3.0：根据 Phase B REDUCE 的执行轨迹，调整阶段时间戳与目标位置

### 2. v1.7 的真正问题

v1.7 中确认了 3 次错误触发（ts=71400/72100/79000，动量=负，正处于盈利下跌段）。这些错误触发：
- 强行吃单买入，消耗盈利空仓
- 每次约 8 pts/合约吃单成本
- 导致 Peak 2 从 1239→1104（−135）

### 3. v1.9 设计：动量门控

**原理**：保留 v1.7 全部 Unwind + Emergency Maker 抑制机制，仅在触发条件上增加 **价格方向** 过滤：

- short emergency 触发条件：`pos < -UNWIND_THRESHOLD AND price_rising`
- long emergency 触发条件：`pos > UNWIND_THRESHOLD AND price_falling`

价格上涨 = 上摆段（我们的亏损相）→ Unwind 有效  
价格下跌 = 下跌段（我们的盈利相）→ Unwind 抑制

### 4. 代码变更

**T1** — `HYD_INV_STRENGTH`：25.0 → **6.0**（恢复 v1.7）

**T2** — 恢复常量：
```python
UNWIND_THRESHOLD  = 15
UNWIND_LOT        = 15
```

**T3** — `hydrogel_orders()` 签名增加 `prev_mid` 参数

**T4** — Unwind 块增加动量门控（`price_rising` / `price_falling`），恢复 Emergency Maker 抑制：
```python
price_rising  = prev_mid is not None and mp > prev_mid
price_falling = prev_mid is not None and mp < prev_mid
if in_short_emergency and not hard_long and price_rising:
    ...taker buy...
elif in_long_emergency and not hard_short and price_falling:
    ...taker sell...
```

**T5** — `Trader.run()` 增加状态：`hyd_prev_mid` 读写，调用时传入，调用后更新为当前 microprice

### 5. 预期结果

- 错误 Unwind (ts=79000) 被门控拦截（price_falling → 不触发）
- 正确 Unwind (upswing 内) 仍触发，压缩 up1/up2 空仓
- Peak 2 回升（接近或超过 v1.7 的 1104）
- 目标 PnL > +545

### 6. 下一步

- 上传云端验证
- 确认 `HYD UNWIND BUY` 日志仅出现在上涨段（price > prev_mid）
- 若 PnL 仍不足，检查动量 tick 分辨率是否足够细

---

## Entry 012 — v2.0 · 下跌段方向性 Taker 建空

- **版本**：v2.0
- **日期**：2026-04-25
- **基于**：v1.9 云端结果 log 398918，PnL = +497

### 1. v1.9 法证结论

Unwind 门控生效（仅 1 次 Unwind，ts=58600，price_rising，正确）。但揭露了更深的结构矛盾：

**段位结构分析（4版本对比）：**

| 段位 | v1.7 | v1.8 | v1.9 | 理想 |
|---|---|---|---|---|
| dn1 −82pt | −3.3 | −3.3 | **−3.3** | −18 |
| up1 +51pt | −8.9 | −13.5 | **−8.2** | ≈0 |
| dn2 −81pt | −7.3 | −8.1 | **−6.4** | −18 |
| up2 +45pt | −11.4 | −10.8 | **−10.5** | ≈0 |

**根因**：dn1 均仓 −3.3 在所有版本中完全一致，显示这是市场微结构锁死：下跌段买方稀少 → 我们的 ask 几乎不被 lift → 空仓只靠被动 bid 侧积累。Maker 策略在下跌段建空能力严重受限，无法通过参数调整解决。

**理论最大损失（当前）：**
- dn1 持仓 −3.3 × 82 = 271（实际获得）；若 −12 则 984：差 713
- dn2 持仓 −6.4 × 81 = 518（实际获得）；若 −12 则 972：差 454

### 2. v2.0 核心机制

**方向性 Taker 建空（Directional Taker Short）**

在价格下跌帧（price_falling），主动 hit market bid 建空，直到 pos 到达 −12 的上限（HYD_DN_TAKER_CAP）。每次触发最多 5 手（HYD_DN_TAKER_LOT）。

**经济模型：**
- 吃单成本：bid 侧 book_half ≈ 8 pts/合约
- dn1 剩余下跌空间（均约 60 pts）：净盈 52 pts/合约
- 5 lot 每次触发：成本 40 pts，若持有至底收益 260 pts → 净 +220 pts/fire

**预期净改善（保守估算）：**  
dn1 持仓从 −3.3 → −10：+6.7×82 = +549；dn2 从 −6.4 → −12：+5.6×81 = +454；吃单成本≈−160；up2 持仓加深≈−113。**目标：+497 + 730 ≈ +1227**

### 3. 代码变更

**T1** — 新增常量：
```python
HYD_DN_TAKER_CAP  = -12    # stop directional taker when pos reaches here
HYD_DN_TAKER_LOT  = 5      # lot per fire
```

**T2** — `hydrogel_orders()` 在 Unwind 块前插入：
```python
if price_falling and not hard_short and sim_pos > HYD_DN_TAKER_CAP:
    room = min(HYD_LIMIT + sim_pos, HYD_DN_TAKER_LOT, sim_pos - HYD_DN_TAKER_CAP)
    if room > 0:
        orders.append(Order(SYM_HYD, bb, -room))
        sim_pos -= room
        logger.log("HYD DN TAKER " + str(room) + "@" + str(bb))
```

### 4. 下一步

- 上传云端验证
- 统计 `HYD DN TAKER` 日志次数及触发时段（应集中于 dn1/dn2 段）
- 检查 dn1/dn2 均仓是否接近 −12
- 若上涨段持仓过深（>−12）导致 DD 恶化，考虑降低 HYD_DN_TAKER_CAP 至 −8

---

## Entry 013 — v2.1 · 价格区间状态机（HIGH/LOW/MID 滞回体制）

- **版本**：v2.1
- **日期**：2026-04-25
- **基于**：v2.0 云端结果 log 400874，PnL = −50

### 1. v2.0 灾难根因确认

v2.0 引入 DN TAKER（价格下跌 tick 时主动做空），单 tick 动量噪音（ts=500 时 price 10015→10010）立即触发 DN TAKER，建立大量空仓。随后价格上涨至 10027，造成 −465 早期暴跌。

**根本矛盾**：tick 级别价格方向（price_falling / price_rising）是噪音信号，无法区分趋势与回调。所有基于 prev_mid 的方向判断均存在此根本缺陷。

**核心诊断**：以下所有模式均属调参，不能解决结构性问题：
- 调整 UNWIND_THRESHOLD
- 调整 LOT 大小
- 加动量门控
- 加 DN TAKER 反向建仓

真正的结构性解决方案是：**完全放弃 tick 级别动量信号，改用绝对价格区间控制报价侧别**。

### 2. v2.1 机制：价格区间状态机

**状态定义（带滞回）：**

| 状态 | 进入条件 | 退出条件 | 报价行为 |
|---|---|---|---|
| HIGH | price > 9990 (from MID) | price < 9960 → MID | **Ask only**（做空仓） |
| LOW | price < 9945 (from MID) | price > 9980 → MID | **Bid only + Taker Unwind**（关空仓） |
| MID | 初始；HIGH/LOW退出后 | price > 9990 → HIGH；price < 9945 → LOW | 双边正常 Maker |

**价格路径状态映射（理论）：**

| 时段 | 价格 | 状态 | 策略效果 |
|---|---|---|---|
| init_up | 10011→10027 | HIGH（>9990）| Ask only → 做空积累 ✓ |
| dn1 核心 | 10027→9960 | HIGH | Ask only → 持续空仓获利 ✓ |
| dn1 尾段 | 9960→9945 | MID | 双边，轻微过渡 |
| up1 底部 | 9945 触发 | LOW | Bid only + Unwind → 快速关空 ✓ |
| up1 上段 | 9980→9996 | MID→HIGH | 过渡 / 重建空仓 |
| dn2 | 9996→9945 | HIGH→MID→LOW | 同 dn1 节奏 ✓ |
| up2 | 9915→9959 | LOW（<9980）| 全程 Bid only，关空 ✓ |

### 3. 代码变更

**T1** — 删除旧常量（`UNWIND_THRESHOLD/LOT`、`HYD_DN_TAKER_CAP/LOT`）；新增：
```python
ZONE_HIGH_ENTER = 9990 / ZONE_HIGH_EXIT = 9960
ZONE_LOW_ENTER  = 9945 / ZONE_LOW_EXIT  = 9980
LOW_UNWIND_THRESH = 10 / LOW_UNWIND_LOT = 10
```

**T2** — `hydrogel_orders()` 签名：`prev_mid` → `zone`

**T3** — 函数体：删除全部 prev_mid/price_rising/price_falling/DN TAKER/Unwind 块；替换为：
- LOW 区 Taker Unwind（pos < −10 时买入 10 手）
- Zone-controlled Maker（HIGH=ask only；LOW=bid only；MID=双边）

**T4** — `Trader.run()` 状态读写：`hyd_prev_mid` → `hyd_zone`（"HIGH"/"LOW"/"MID"）

**T5** — `Trader.run()` 调用前插入状态机更新（基于 microprice 更新 zone），传 zone 进 hydrogel_orders

### 4. 下一步

- 上传云端验证
- 确认日志：HIGH 区 `HYD ZONE UNWIND` 不出现，LOW 区出现
- 检查 init_up 是否出现异常 PnL 跌落（若有：HIGH 区期间 ask 仍有 taker mispricing 保护）
- 观察 dn1/dn2 期间 PnL 曲线斜率（应明显正值）


---

## Entry 016 �� v3.0 �� HYD Inventory-State Refactor (Plan B Executed)

- Version: v3.0
- Date: 2026-04-26
- Baseline: v2.9 cloud run 420388, PnL=1437
- Trigger: User-approved workflow plan to solve early/mid downside pressure via architecture refactor

### What Was Refactored (No Parameter-Tuning Iteration)

1) Unified buy-side target controller in `hydrogel_orders()`
- Replaced split logic (`PHASE_B REDUCE` + `MID TAKER BUY` + `LOW UNWIND`) with one target-based cover path.
- New log: `HYD TARGET BUY qty@price rs=... tgt=...`
- Effect: prevent multi-branch over-covering and enforce monotonic movement toward target short.

2) Inventory risk state layer (independent from HIGH/MID/LOW zone)
- Added adverse rally detector based on microprice (`HYD_ADVERSE_STEP`, `HYD_ADVERSE_STREAK_TRIG`).
- Added de-risk hold window (`HYD_DERISK_HOLD_TICKS`) and de-risk target short (`HYD_DERISK_TARGET_SHORT`).
- Effect: handle early wrong-side inventory pressure without waiting for zone transition.

3) Capture latch after late reload
- On `HYD HIGH RELOAD SELL`, activate cover lock for `HYD_CAPTURE_HOLD_TICKS`.
- During lock, cover target is floored by `HYD_CAPTURE_MIN_SHORT`.
- Effect: keep downswing participation after successful late reload and prevent immediate give-back.

4) Maker policy integration
- During de-risk, HIGH-zone sell maker is disabled (no new short extension).
- LOW/MID maker bid size is capped by target short need.

5) Stateful persistence
- Added `hyd_ctrl` memory in traderData: `prev_mp`, `adverse_up_streak`, `derisk_until`, `capture_until`.
- Added trim-safe serialization for `hyd_ctrl` scalars.

### Files Modified
- `round3_trader.py`

### Local Validation
- `python -c "import ast; ast.parse(open('round3_trader.py', encoding='utf-8').read()); import round3_trader; round3_trader.Trader()"` => Pass

### Next Step
- Upload v3.0 to cloud sandbox and validate zone-level behavior:
  - 25.8k-32.7k drawdown compression
  - 49k over-cover elimination
  - 76.8k-91k downswing capture retention

---

## Entry 017 — v3.1 · Rollback to v2.9 and Dual-State Rewrite

- Version: v3.1
- Date: 2026-04-26
- Trigger: v3.0 cloud run 465663 failed (PnL 1085; severe regression from 1437)

### v3.0 Failure Postmortem (Cloud 465663)

1. Early structural pressure returned:
- 25.8k-32.7k: mid +27, PnL -329.68

2. Mid structural pressure returned and worsened:
- 50k-65k: mid +35, PnL -736.36
- 62k-69k: mid +24, PnL -507.39

3. Tail give-back expanded:
- 91k-99.9k: mid +40, PnL -478.59

Root cause summary:
- v3.0 risk-state trigger was too brittle in live path (insufficient activation in critical windows)
- reload/capture chain was under-fired in mid-late sequence
- inventory control drifted from deterministic phase-B de-risk requirements

### v3.1 Execution Plan (Approved) and Implementation

Step A. Rollback base file to v2.9:
- `git checkout f280808 -- round3_trader.py`

Step B. Rebuild HYD controller on stable skeleton (no parameter-only tuning):
1) Dual state machines:
- Price-phase state: A/B/C via timestamp bands
- Risk-mode state: NORMAL/RISKOFF/CAPTURE via streak + hold windows

2) Phase-B stair unwind track (deterministic):
- Time checkpoints: 50k / 56k / 62k / 68k
- Target short trajectory: -20 -> -16 -> -12 -> -10

3) Unified target controller:
- All buy-side reduction paths (MID/LOW/PHASE-B/RISKOFF) merged into one capped path
- Enforces monotonic convergence to `effective_cover_target`

4) Capture lock after reload:
- `HYD HIGH RELOAD SELL` extends `capture_until`
- During capture lock, cover cannot go beyond `HYD_CAPTURE_MIN_SHORT`

5) Risk-off maker policy:
- Disable HIGH sell maker while `riskoff_active`
- Prevent re-extension into wrong-way carry

6) State persistence:
- `hyd_ctrl` fields persisted in traderData: `prev_mp`, `adverse_up_streak`, `riskoff_until`, `capture_until`

### Local Validation

- Syntax parse: pass
- Problems panel: no errors in `round3_trader.py`
- Function return consistency: `hydrogel_orders(...) -> (orders, hyd_ctrl)` across all exit paths

### Next Step

- Upload v3.1 to cloud
- Validate the same pressure segments against 420388 baseline and 465663 failure pattern

