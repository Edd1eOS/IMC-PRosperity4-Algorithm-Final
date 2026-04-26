# Algorithm_improve_strategy.md — Round 3 宏观策略演进档

> 本档记录 R3 **宏观层面**的策略架构与版本演进逻辑。
> 与 `coding_process.md` 的区别：本档关心 **"为什么这样设计"** 与 **"未来往哪走"**，
> 不关心具体代码行号与编辑细节。

---

## 1. 产品图谱与 R3 核心挑战

### 1.1 P4 Wiki 原文（不可篡改的事实锚）

> "There are 2 'asset classes' in the three products you trade. The HYDROGEL_PACK and VELVETFRUIT_EXTRACT are 'delta 1' products, similar to the products in the tutorial and rounds 1 and 2. The 10 VELVETFRUIT_EXTRACT_VOUCHER products (each with a different strike price) are options."

> "All products are traded independently, even though the price of VELVETFRUIT_EXTRACT_VOUCHER might be related to that of VELVETFRUIT_EXTRACT due to the nature of options."

### 1.2 R3 核心挑战三元组

1. **Voucher 的 IV 错定价捕捉**：Wiki 明言 "traded independently"，说明市场会持续偏离理论价。
2. **Delta 预算严重不足**：10 只 voucher × 300 = 3000 虚拟股可能 delta 敞口，但标的对冲预算仅 200。**对冲能力 < 裸敞口上限，是本轮硬约束**。
3. **HYDROGEL_PACK 未知结构**：Wiki 仅说 "delta 1"，未说均值回归/趋势/波动分布。首版必须不做假设。

---

## 2. v1.0 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                       Trader.run()                          │
│                                                             │
│   ┌─────────────────┐    ┌──────────────────┐               │
│   │  State Decode   │    │  State Encode    │               │
│   │  (iv_dev hist,  │    │  (auto-trim if   │               │
│   │   hyd_spread)   │    │   > 3800 B)      │               │
│   └────────┬────────┘    └────────▲─────────┘               │
│            │                      │                         │
│   ┌────────▼──────────────────────┴─────────┐               │
│   │         Module A (Vouchers+Hedge)       │               │
│   │  ┌──────────┐  ┌──────────┐  ┌────────┐ │               │
│   │  │ Filter & │→ │ Parabolic│→ │ Z-Score│ │               │
│   │  │  IV Rev. │  │ Smile Fit│  │ Trade  │ │               │
│   │  └──────────┘  └──────────┘  └────┬───┘ │               │
│   │                                   │     │               │
│   │         ┌─────────────────────────▼──┐  │               │
│   │         │  Projected Net Delta Guard │  │               │
│   │         │  (|delta| <= 180 pre-check)│  │               │
│   │         └─────────────┬──────────────┘  │               │
│   │                       │                 │               │
│   │         ┌─────────────▼──────────────┐  │               │
│   │         │  Underlying Delta Hedge    │  │               │
│   │         │  (deadband=3, step<=20)    │  │               │
│   │         └────────────────────────────┘  │               │
│   └─────────────────────────────────────────┘               │
│                                                             │
│   ┌─────────────────────────────────────────┐               │
│   │      Module B — HYDROGEL_PACK MM        │               │
│   │  microprice ± edge(rolling) - inv_skew  │               │
│   │  + opportunistic taker                  │               │
│   └─────────────────────────────────────────┘               │
│                                                             │
│   ┌─────────────────────────────────────────┐               │
│   │   Module C — Global Position Clamping   │               │
│   │   (safety net per-symbol at limit)      │               │
│   └─────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 设计哲学

### 3.1 "安全为先，收益为次"（首版基调）


---

## 4. v2.x 演进路线：从"反应型"到"前瞻型"

### 4.1 v2.0～v2.8 回顾：参数微调的局限性

**演进方向**：v2.0 ~ v2.5 → 库存上限机制 → v2.6 符号修复 → v2.7 high reload → v2.8 覆盖约束

**根本问题**：所有这些改动都是**参数内的迭代**，无法解决**架构级缺陷**。

| 版本 | 改动类型 | 最终 PnL | 诊断 | 改进存在的天花板 |
|---|---|---|---|---|
| v2.1 | 状态机 (HIGH/LOW/MID) | 958 | **反应型决策**：基于实时价格区域做反应 | 只能对已发生的现象响应 |
| v2.2-2.5 | Cap 上限微调 | ~1100 | 参数优化 | HYD_SHORT_HARD_CAP 再降又会漏掉下跌 |
| v2.6 | 符号修复 | ~1000 | BUG fix | 修复 MID taker 的发动机，但战术未变 |
| v2.7 | Reload 卖出 | 1069 | 段位优化 | 68k reload 有效，但 76.8k 追平了 |
| v2.8 | 覆盖约束 | 1110 | 晚期修复 | 76k 后段改善 +41，但中段"平坦"问题未触 |

**关键洞察**：v2.8 即使达到完美表现，其上限应该不超过 1400 左右。因为：
- Phase A (0-40k)：被动卖空收益 ±200
- Phase B (40-70k)：**反应型不能主动识别反向的来临**，平坦陷阱持续消耗 700+ PnL
- Phase C (70k+)：reload + 覆盖集中 +200～300
- 合计理论上限：± (200+0+300) = ±200 ~ 500（极保守）

**为何 Phase B 是瓶颈**：
- v2.1～v2.8 仅在"价格已经跌破 HYD_ZONE_LOW"或"短仓 > MID_TAKER_FORCED_BUY_THRESH"时才触发买回
- 这些条件都是**事后**（价格信号已经变化）而非**事前**（头寸已经过量）
- 18 秒的反应延迟在高频市场中等同于 0 收益

### 4.2 v2.9 突破点：从"被动跟价"到"目标驾驭"

**根本改变**：引入**阶段生命周期**，让策略具有"前瞻性"。

**三阶段设计**：

```
Timeline: ├─ Phase A (0-40k) ─┼─ Phase B (40k-70k) ─┼─ Phase C (70k+) ┤
Strategy: │   ACCUMULATE     │      REDUCE         │    DEFEND      │
			 │ (build shorts)   │ (unwind position)   │ (exit prep)    │
			 │ cap = -35        │ cap = -30, trim@50k │ cap = -20      │
				 (松)               (中)强制 reduce@-25      (紧)    
```

**设计原理**：
- **Phase A**：早期下跌周期，价格有利建仓，故允许积累到 -35。此时市场还未反转，继续卖空是理性的。
- **Phase B**：中期反弹起点（typically 40k-70k），价格从 9952 反弹至 9996 (+44 点），这是心理反转区。在此区间，头寸应从"构建"切换为"减仓"。**新增强制减仓逻辑在 50k 启动**，主动买回至 -25，防止后续大反弹时头寸过量导致爆损。
- **Phase C**：后期快速上升或剧烈波动，此时已无多余缓冲，应保守至极（-20），为最后的离场预留充分空间。

**关键差异**（vs v2.8）：
- v2.8 的 MID TAKER BUY 仍是被动触发（price ∈ MID zone && pos ≤ -8）
- v2.9 的新模块 "Phase B REDUCE" 是**主动的**：只要进入 Phase B 且超过目标位置，直接执行批量减仓，**无关当前 zone 和价格**

**预期改善量化**（基于 418652 重模拟）：
- Zone B 平坦陷阱消除：~+770 PnL（35 点 × 22 手）
- Zone C 杠杆释放：~+102 PnL  
- 理论上限：1110 → **1400+**（+290 改进）

### 4.3 v2.9 后续迭代方向（v3.0+）

若 v2.9 云端验证成功（PnL > 1300），下一步可考虑：

1. **自适应阶段边界**：当前 40k/70k 是固定值。根据 Phase B REDUCE 的实际执行轨迹，可动态调整为 "价格反转 > 10 点" 或 "PnL 平坦 > 5000 ticks"。

2. **目标位置动态化**：当前目标位置 (A=-35, B=-25, C=-20) 是固定的。可改为根据市场波动率动态调整，e.g., `target = -25 * (1 + vol_percentile)`。

3. **多周期叠加**：若发现更小粒度的价格周期（e.g., 5-10 秒反弹频率），可加入 sub-phase 逻辑，在极细粒度上捕捉反弹尖峰。

4. **整体 PnL 约束**：如果 Phase B REDUCE 执行过度（e.g., 非常频繁的小额减仓导致交易成本累积），可加全局 PnL 增长率门槛（e.g., 每 1000 ticks 的 PnL 增长 > 10 才执行减仓）。

### 4.4 设计约束与 risk fence

**保留的风控守卫**：
- hard_long / hard_short（绝对位置墙）：Phase 改变后这些门仍然有效
- Delta hedge 预检（Module A）：对 voucher 的任何增持都受 180 限制
- per-symbol clamping（全局网）：任何产品总敞口不超 limit

**新增的防护层**：
- Phase B REDUCE 本身有三重条件：(1) 必须在 Phase B，(2) 必须 ts ≥ 50k，(3) 必须 pos ≤ target -25 才触发
- 减仓 lot 固定为 6（vs v2.8 全 12），单次影响有限
- 日志记录 "HYD PHASE_B REDUCE"，云端可审计执行频率
- 全局 clamping 作为最后安全网，即使前层逻辑出错也保证 |pos| ≤ limit

### 3.2 "一切推导都可白盒追溯"

- IV 反解失败的 voucher → 明确写 `reason = "IV_INVALID"` 字段
- Smile 拟合失败 → 不开新仓，只维持
- Z-score < 阈值 → 不开新仓，让已有仓位随市场回归自然平仓（对称的 z 信号触发反向单）

### 3.3 "失败不应引发雪崩"

- Module A 异常 → try/except 捕获，不影响 Module B
- Module B 异常 → try/except 捕获，不影响对冲与 clamping
- 即使所有决策逻辑失效，Trader 最坏情况下返回空订单，不亏钱

---

## 4. 未来迭代路线图（仅作方向规划，不作承诺）

### v1.1 候选改进（以云端 PnL 数据为触发）
- **若 voucher 模块贡献正但 hedge 频繁**：引入 delta EMA 平滑，降低对冲频率
- **若 HYDROGEL 持续被撕**：加入 microprice EMA + 库存时间惩罚
- **若特定 strike 偏离持久**：改用「smile + strike-specific residual bias」双层模型

### v1.2 候选改进
- **Vega 风险聚合**：若发现某方向 vega 集中（如 IV 全面偏高），用卖 ATM + 买 Wings 构造 vol-spread 对冲
- **跨日 IV 基准**：从历史 day 1-3 数据中提取 IV "公允均值"，作为 rolling_std 之外的第二锚

### v2.0 候选改进
- **Garch/OU IV 模型**：对 iv_dev 序列建模，给 z 信号加"可信度"权重
- **自适应阈值**：根据近期 PnL 表现动态调整 Z_THRESHOLD

**严格原则**：任何改进都必须先走 SOP 流程（蓝图→授权→编码→自检→云端验证→决策）。

---

## 5. R4/R5 前瞻

- R4: TTE = 4d，R5: TTE = 3d。Theta 加速衰减。
- 需监控：是否有新产品在 R4/R5 引入（Wiki 暂未披露后续）。
- Voucher 模块可能需要提升 z 阈值（因 theta 惩罚变大，需要更确定的信号）。

---

## 版本历史

| 版本 | 日期 | 状态 | 要点 |
|---|---|---|---|
| v1.0 | 2026-04-24 | 失败（PnL=−5007） | 初始架构：BS + Live Smile + Z-Score + Delta Hedge + HYDROGEL MM |
| v1.1 | 2026-04-24 | 已部署（PnL=−1205） | z-score 公式修复；HEDGE_DEADBAND 3→8；HYD_INV_STRENGTH 3→6 |
| v1.2 | 2026-04-24 | 失败（PnL=−7116，已回滚） | 截面 ratio 目标仓位重构；HYDROGEL 关闭（HYD_LOT=0）；抛物线微笑结构偏差导致持续建仓亏损 |
| v1.3 | 2026-04-25 | 已部署（PnL=+549） | (1) Phase 1 过滤换代：delta 分类替代价格比较（删 DEEP_ITM_BUFFER 等）；(2) Phase 4 vega 经济性门槛；(3) HYDROGEL 库存硬截止 HYD_INV_HARD_CAP=100 |
| v1.4 | 2026-04-25 | 已部署（PnL=+479） | z-score 解封调参（MIN_ROLLING_STD=0.001），期权开始成交，净亏 −70；HYDROGEL 同 v1.3 +549 |
| v1.5 | 2026-04-25 | 失败（PnL=−559，log 376046） | 框架重写：VRP 信号 + 趋势抑制。致命 bug：RV_ann=0.422 >> ATM_IV=0.276 恒为负 → 永远 BUY vega → 期权净亏 −1821；趋势检测封锁 HYDROGEL 做市超过 8900 ts，HYD 降至 +374 |
| v1.6 | 2026-04-25 | 已部署（PnL=+533，log 377206） | 期权全局禁用（ENABLE_OPTIONS=False）；移除趋势检测；新增主动平仓层（UNWIND_THRESHOLD=20，UNWIND_LOT=10）；Unwind 数学缺陷：qty 公式含距离上限且未抑制 Maker ask，导致实际零效果 |
| v1.7 | 2026-04-25 | 待云端验证 | UNWIND_THRESHOLD=15，UNWIND_LOT=15（均衡修正）；新增 in_short/long_emergency flag；紧急态禁止同侧 Maker 报价，阻止 Unwind 被 Maker 抵消；预期 PnL +900～+1300 |

---

## 6. v3.0 Architecture Shift (Approved Plan B)

v2.9 solved the 50k-65k flat-trap and raised PnL to 1437, but two structural pressures remained:
- Early wrong-side carry (25.8k-32.7k drawdown)
- Mid/late over-cover leakage (49k and 76.8k-91k)

v3.0 introduces a dual-state control model:
- Price-zone state: HIGH/MID/LOW (market microstructure view)
- Inventory-risk state: NORMAL / DERISK / CAPTURE_LOCK (inventory lifecycle view)

Core idea:
- Separate "when market is in a zone" from "whether current inventory is healthy".
- All buy-side reduction goes through a single target controller to avoid conflicting local heuristics.

Expected benefit direction:
- Reduce early adverse carry duration
- Remove branch-overlap over-cover events
- Preserve late reload downswing participation

---

## 7. v3.1 Redesign After v3.0 Regression

### Why v3.0 Failed in Cloud (465663)

Observed regressions:
- Early pressure returned (25.8k-32.7k drawdown)
- Mid pressure sharply worsened (50k-69k two-step collapse)
- Tail rebound give-back expanded

Strategic conclusion:
- The architecture direction (stateful inventory control) is correct.
- The v3.0 activation path was too sparse and non-deterministic in key windows.
- The controller must include a deterministic mid-cycle de-risk trajectory, not only signal-triggered behavior.

### v3.1 Architecture

v3.1 keeps the dual-state concept but adds deterministic structure:

1. Price-phase state machine (A/B/C)
- Still defines base inventory intent by market lifecycle.

2. Risk-mode state machine (NORMAL/RISKOFF/CAPTURE)
- RISKOFF: entered by adverse short-carry streak and held by timer.
- CAPTURE: entered after late reload sells; temporarily preserves downside participation.

3. Phase-B stair unwind track (deterministic)
- 50k -> target -20
- 56k -> target -16
- 62k -> target -12
- 68k -> target -10

4. Unified effective-cover target
- `effective_cover_target = max(base_phase_target, stair_target, riskoff_target)`
- During capture lock: floor by `HYD_CAPTURE_MIN_SHORT`

5. Maker policy gating
- In HIGH zone, no sell-maker extension during RISKOFF
- LOW/MID maker buy sizes bounded by target need

### Strategic Intent of v3.1

The key shift is from mostly reactive branch firing to mixed reactive + deterministic control:
- Deterministic in the 50k-70k danger window (stair unwind)
- Reactive outside that window (risk streak and capture lock)

This is designed to prevent recurrence of both historical structural failures:
- wrong-way carry persistence
- over-cover and under-capture oscillation
