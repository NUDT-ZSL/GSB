# 定点数与误差界技术说明

## 1. 定点网格

会话参数 `grid.exponent = k`（0..12 的整数）定义缩放因子 `S = 10^k`，
1 个整数格（1 ulp）代表 `1/S`。所有比例、速率、权重在核心判定路径上均以
`int` 表示，禁止浮点中间值。十进制展示通过整数拆位完成（`format_fixed`）。

## 2. 截断方向约定

| 运算 | 实现 | 截断方向 | 单次误差上界 |
| --- | --- | --- | --- |
| 有理数量化 `quantize(n,d)` | `divmod(n*S, d)` | floor（不高估） | 1 ulp，余数 `remainder` 可追溯 |
| 定点乘法 `mul_fixed(a,b)` | `divmod(a*b, S)` | floor | 1 ulp |
| 定点除法 `div_fixed(n,d)` | `divmod(n*S, d)` | floor | 1 ulp |
| 百分比阈值比较 `compare_percent` | `n*100 >= t*d` 交叉相乘 | 精确，无截断 | 0 |

所有除法余数均满足 `0 <= remainder < 分母`，因此量化真值落在
`[q/S, q/S + 1/S)`，误差严格小于 1 ulp，报告上界取 1 ulp。

## 3. 令牌桶补充的误差界

设速率 `r` 令牌/秒，经过 `e` 毫秒，网格 `S`。精确补充量（格）为

```
exact = r * S * e / 1000
```

实现维护整数余数结转 `carry`（`0 <= carry < 1000`）：

```
num = r * S * e + carry
refill, carry' = divmod(num, 1000)
```

* floor 截断方向保证**只欠发、不超发**，`over_issue_ulp = 0`。
* 余数跨事件结转而非丢弃，任意时刻相对精确轨迹的累计欠发严格小于 1 格，
  即 `cumulative_under_issue_ulp = 1`（上界）。
* 补充后若 `tokens + refill > capacity` 则封顶到容量，溢出格数显式记入
  `overflow_grid`，容量边界不会产生超发。
* 重复推进同序列得到逐位相同的 `tokens_grid / carry_grid` 轨迹（见
  `test_deterministic_bit_identical_trajectory`）。

## 4. 熔断器失败率的误差界

失败率以窗口内 `(failure + timeout) / total` 表示：

* 阈值触发使用整数交叉相乘 `failed*100 >= threshold*total`，比较本身零误差。
* 报告中的定点失败率 `quantize(failed, total, S)` 采用 floor，
  量化误差上界 1 ulp，余数 `failure_rate_quant_remainder` 一并输出。
* 超时与失败分别计数（`timeout` / `failure`），在统计口径上明确区分，
  仅在失败率分子中合并。
* 窗口按整分钟桶存储，滑出时整桶删除（`_evict`），不做逐条递减。

## 5. 降级链误差沿链传播

第 `m` 级成功概率定点值 `p_m` 与累计值 `c_{m-1}` 做一次定点乘法：

```
c_m, rem_m = divmod(c_{m-1} * p_m, S)
```

保守误差线性递推：

```
e_0 = 0
e_m = e_{m-1} + q_m + 1
```

其中 `q_m` 为该级输入量化误差上界：由窗口计数现场量化取 1，
策略直接给出的千分比定点值取 0；`1` 为乘法 floor 截断上界。
`ErrorTrace.levels` 逐级记录 `input_quantization_error_ulp`、
`truncation_error_ulp` 与 `cumulative_error_bound_ulp`，可追溯每级贡献。

误差主导标注：当累计误差上界不小于端到端概率与参考边界（默认 50%）的
格距离时，置 `error_dominated=True`，表示量化误差区间可能翻转结论。

## 6. 权重选择的确定性

选择器 `deterministic_selector` 仅由事件序号、毫秒时间戳、服务标识长度与
降级层级这些整数输入构成；随后严格按降级链**数组顺序**累计权重区间，
用 `slot = selector mod weight_sum` 命中，不依赖 dict/hash 遍历顺序，
相同输入逐位复现。权重和为零或链条耗尽统一以 `E_DEGRADE` 报错，不静默放行。
