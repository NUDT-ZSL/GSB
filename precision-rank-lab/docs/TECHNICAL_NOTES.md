# 技术说明与验证记录

本文记录关键设计决策、误差传播口径以及在本机得到的实际验证结果。
所有结果均可通过 `scripts/run_demo.sh` 或文档中的命令复现。

## 1. 确定性保证

- 样本生成只使用 `np.random.default_rng(seed)`，query / item 均按 id 排序后写 JSONL，
  输出使用 `sort_keys=True` 与固定 `\n` 行尾，同种子逐字节一致。
- 排序统一使用 `np.lexsort((item_id, -score))`，平局只由 `item_id` 升序决定，
  代码中没有任何依赖 `dict` 遍历顺序的分支。
- 报告哈希对除 `report_hash` 外的全部内容做规范化 JSON 的 SHA-256，
  不含时间戳或主机名，因此固定种子重跑哈希相同且报告内容逐字节相等。

## 2. 三档共用语义

流水线代码只调用 `PrecisionContext` 的算子，不感知当前档位：

| 算子 | float64 / float32 | fixed |
| --- | --- | --- |
| 加减 | 同 dtype 数组运算 | 同尺度整数直接加减 |
| 乘 | dtype 运算 | 整数乘积后按 `S^2 -> S` 显式重缩放 |
| 除 | dtype 运算 | 被除数先乘 `S` 再按配置舍入 |
| exp | `2^k·exp(r)` 规约 + 24 项级数 | 同一公式的整数实现 |
| sigmoid | 按符号分支的稳定公式 | 同一分支的整数公式 |

定点档使用 Python 任意精度整数（object 数组），避免 int64 在
`exp` 中间步骤静默溢出；`scale`、舍入模式、级数项数均由配置显式给出。

## 3. 误差口径

- 参考真值：同快照内的 float64 档结果。
- 逐级对象：特征矩阵（4 列展平）、线性打分、sigmoid 分数、排序 / 重排结构。
- 指标：绝对偏差 max / mean / RMSE、相对偏差（分母取 `max(|ref|,1e-12)`）、
  分位数误差区间、SciPy 偏度 / 峰度与均值 95% t 置信区间。
- 放大比：`amplification_vs_feature`（相对特征级）与
  `amplification_vs_prev`（相对上一级）。
- 归因：连续误差的主导来源在 feature_construction / score_scaling /
  activation_function 中取最大 max 偏差；排序逆序对是离散事件，
  通过 `ranking_comparison_triggered` 单独标记，不与连续量纲混比。

## 4. 实际验证结果（seed=20261120，40 query，3806 候选）

全流程单机耗时约 2.5 秒（三档 + 全部统计），29 个 unittest 全部通过。

### 4.1 逐级偏差（相对 float64）

| 档位 | 阶段 | mean abs | max abs | max rel |
| --- | --- | --- | --- | --- |
| float32 | features | 4.7e-09 | 3.0e-08 | 5.6e-08 |
| float32 | linear | 4.9e-08 | 2.5e-07 | 1.4e-03 |
| float32 | sigmoid | 2.1e-08 | 9.5e-08 | 1.8e-07 |
| fixed(Q16) | features | 2.5e-06 | 8.4e-06 | 8.1e-05 |
| fixed(Q16) | linear | 8.4e-06 | 3.8e-05 | 2.2e-01 |
| fixed(Q16) | sigmoid | 4.4e-06 | 1.7e-05 | 4.4e-05 |

观察：线性打分级是主要的数值放大环节（权重相乘累加），sigmoid 因压缩到
(0,1) 区间反而抑制了绝对偏差；定点档误差整体比 float32 高约两个数量级，
与 Q16 的量化步长 `1/65536 ≈ 1.5e-5` 同量级。

### 4.2 定点可重算性

`fixed_recompute_check` 输出：
`recompute_exact_match=true`、`all_within_quantization_bound=true`、
`max_recompute_delta=0.0`，即仅用 `scale + rounding` 即可重算定点编码，
且单步偏差不超过半 ULP 界。

### 4.3 门限稳定性

390 个门限附近候选上：

- strict 策略跨档准入翻转 **2 次**；
- tolerance_band 策略跨档准入翻转 **0 次**。

这直接验证了容差带对门限附近舍入翻转的保护作用，且同一档位重复运行结果恒定。

### 4.4 排序与重排

- 三档处理的 query_id 集合一致、重排前后候选集合一致（自检全通过）。
- float32 出现 1 个逆序对 / 2 个位置翻转，定点 Q16 该数据下无翻转；
  所有差异均能在报告 `stages.ranking` 与 `detail` 中追溯到具体 item。
- 重排 trace 满足同类目位置衰减单调，类目惩罚仅对同类目第二次出现生效。

### 4.5 异常注入与降级

- 数据集级：重复 query 返回 `E_DUP_QUERY`（退出码 21），写入
  `--error-report`，终止运行。
- query 级多点注入：`E_ITEM_EMPTY`、`E_VALUE_RANGE(price/quality)`
  触发 `fallback_float64` 降级，降级标记含 `trigger_code` 与
  `upstream_chain`，健康 query 仍保留三档结果。
- 参数级：`float16` 触发 `E_PRECISION_UNKNOWN`（24），
  scale=0 / 负数触发 `E_SCALE_INVALID`（25），
  非法门限触发 `E_CONFIG_INVALID`（26），
  快照篡改触发 `E_SNAPSHOT_CORRUPT`（28）。

### 4.6 可复现性

两次基线运行报告哈希均为
`sha256:b4cf4c83189214f556f9b513754be496b8a63e2b86599af9fd2ab10c3ffb65ba`，
`hash-check` 返回 `identical=true`，规范化 JSON 文本逐字节相等。

## 5. 退出码映射

业务码统一映射到 `20 + 序号`，落在 Windows 可移植的 0..255 区间；
业务码同时出现在 stderr 的 JSON 与报告文件中，详见 `prank/errors.py`。

## 6. 复现实验命令

```bash
./scripts/run_demo.sh
.venv/bin/python -m unittest discover -s tests
```
