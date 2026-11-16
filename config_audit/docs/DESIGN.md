# 技术设计：定点精度、容差判定与误差传播

本文档固定本组件的数值口径。所有规则只依赖整数与精确十进制运算，不依赖浮点，
因此结论在不同机器、不同运行次序下逐位一致。

## 1. 统一精度格度（Grid）

一次审计会话由一个不可变 `Grid` 定义：

- `places`：小数位数（会话参数，范围 0..12）。
- `factor = S = 10^places`：整数缩放因子（会话参数）。
- 任意实数 `x` 的定点表示为整数格值 `X = round_half_even(x * S)`。

舍入规则固定为**银行家舍入（HALF_EVEN）**。`float` 输入先经最短往返 `str`
转为 `Decimal`，避免二进制尾数引入不确定性。

### 1.1 量化误差界（整数格）

对每次量化记录非负整数的向下/向上误差界：

- 若精确缩放值 `x*S >= X`，则 `up_error = ceil(x*S - X)`，`down_error = 0`；
- 否则 `down_error = ceil(X - x*S)`，`up_error = 0`。

半偶取整下二者之一恒为 0，取值仅可能为 0 或 1（单个输入的残差不超过一个格）。
值、派生系数、派生常量分别量化，每条记录都带 `raw / quantized / down_error /
up_error / rounding`，在最终报告 `trace` 中可逐项追溯。

## 2. 派生性与整数误差传播

派生项为线性式 `y = sum(c_i * x_i) + k`。为保证全程整数且量纲正确，约定：

- 上游值格值 `X_i = round(x_i * S)`（含一个因子 `S`）。
- 系数**不**带值缩放，量化为整数系数 `C_i = round_half_even(c_i)`，残差以
  整数格记录（`quantize_coefficient`）。
- 常量格值 `K = round_half_even(k * S)`。

于是派生整数表达式为

```
Y = sum(C_i * X_i) + K
```

每个乘积恰好携带一个 `S`（来自系数侧），量纲与上游值一致。

### 2.1 区间合成

每个节点用闭整数区间 `[low, high]`（格单位）表示其可能取值：

- 人工项：`low = high = X`，再并入其量化误差界。
- 派生项：对每个系数做符号感知的区间乘法
  - `C_i >= 0`：`[C_i*low_i, C_i*high_i]`
  - `C_i < 0`：`[C_i*high_i, C_i*low_i]`
  累加所有项与 `K`。这同时涵盖上游值误差、系数量化误差与常量量化误差。

按系数绝对值加权的上游贡献逐项记录为
`weight_abs_coef = |C_i|`，`weighted_down = |C_i| * down_i`，
`weighted_up = |C_i| * up_i`，多级派生按拓扑序（DFS，键排序）逐级合成，
报告 `propagation.contributions` 给出每一级、每个上游的贡献与传播路径。

代表值 `Q_y = round_half_even(exact(y) * S)`，相对区间端点的误差界为

```
down_error = max(0, Q_y - low)
up_error   = max(0, high - Q_y)
```

若某上游键缺失或误差界不可确定，立即以 `E_PROPAGATE` 终止并给出断点键。
派生图成环由 DFS 三色标记检测，报 `E_CYCLE`。

## 3. 一致性与偏差判定

同一键在两节点的格值 `A、B`，偏差量 `d = |A - B|`（整数格）。

- 绝对容差 `T_abs = round(abs_tolerance * S)`。
- 相对容差 `T_rel = ceil(ppm * max(|A|,|B|) / 1_000_000)`，以整数参与。
- 有效容差 `T = max(T_abs, T_rel)`。
- **边界包含规则（显式）**：`d <= T` 判一致（含等号），`d > T` 判偏差。
- 容差归属：`T_rel > T_abs` 时记 `relative`，否则记 `absolute`
  （平局由绝对容差胜出，保证确定性）。

### 3.1 误差界主导的取舍

配对不确定性 `U = max(down_a,up_a) + max(down_b,up_b)`。

- 当 `U > 0` 且 `d <= U`：数学差异无法与累积量化/传播误差区分，判
  `indistinguishable`，并置 `error_bound_dominated = true`。
- 否则按容差判 `consistent`（`d <= T`）或 `deviating`（`d > T`）。

精确零误差的相等（`U = 0, d = 0`）是真正的 `consistent`，不会被误标为
误差界主导。

## 4. 格度翻转与陈旧标记

提高 `places` 会缩小单格宽度、改变量化误差界与相对容差整数，可能使某键结论
翻转（样本中 `sampling.rate` 在 `places=0` 为 `indistinguishable`，在
`places=3` 为 `consistent`）。每次报告带 `grid.signature`；当传入
`previous_grid_sig` 且与当前不同，报告置 `grid_changed=true` 并把旧签名记为
`stale_previous_signature`。不同格度的结论集哈希不同，从结构上杜绝混用。

## 5. 快照、整体替换与重放

快照为单份规范 JSON，持久化档案版本、格度参数、容差、量化值与全部结论。

- 恢复先比对档案版本映射：不一致时**整体替换**当前审计状态（绝不增量合并），
  以当前输入按快照格度重建，`restore.action = whole_state_replaced_version_mismatch`。
- 版本一致时执行逐位重放：重算报告与快照逐键比较，并核对规范哈希；任何差异以
  `E_REPLAY` 报告并列出差异键。
- 快照缺失格度块时在加载阶段报 `E_GRID`。

## 6. 确定性保证

- 所有档案、键、节点在输出前排序；JSON 统一 `sort_keys=True`。
- 判定仅用整数/`Decimal`；`numpy` 只用于实验脚本的整数汇总，不参与判定。
- 报告 `canonical_hash` 为去除哈希字段后规范序列化的 SHA-256，可用于跨运行
  与跨机器的逐位比对。
