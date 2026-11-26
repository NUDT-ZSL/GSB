# 表格二分类线性模型正则化诊断引擎

这是一个面向本地固定种子样本的确定性诊断引擎，覆盖特征标准化、类别编码、正则化路径求解、分块归约、三档精度执行、异常注入降级、配置快照迁移、误差传播归因、结果校验和检查点恢复。实现仅依赖 Python 标准库与 NumPy，不使用 Docker 或外部服务。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python generate_samples.py --out data
.venv/bin/python -m diagnostic_engine train \
  --data data/samples.jsonl \
  --snapshot data/snapshots/v2 \
  --out artifacts/run \
  --precision all
```

当前工作区已经创建 `.venv`，安装的是 Python 3.10.12、NumPy 2.2.6 和 SQLite 3.37.2。若系统缺少 `python3-venv`，可使用已安装的 `virtualenv`：

```bash
python3 -m virtualenv .venv
.venv/bin/python -m pip install -r requirements.txt
```

测试与完整验收：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_acceptance.py
```

验收结果写入 `artifacts/acceptance/summary.json` 和 `artifacts/acceptance/summary.md`。

## 数据与快照

固定种子脚本位于 `generate_samples.py`，核心实现在 `diagnostic_engine/generator.py`。默认种子 `20261127`，生成 384 行 JSONL：

```text
sample_id, age, income, tenure, score, region, channel, label
```

生成物：

- `data/samples.jsonl`：确定性样本清单，标签由固定线性 logit 加固定噪声生成。
- `data/snapshots/v1/config.json`：v1 单文件扁平参数，含相对秒与完整训练参数。
- `data/snapshots/v2/manifest.json`：v2 分片清单、绝对毫秒、配置代数和内容摘要。
- `data/snapshots/v2/shards/*.json`：`features`、`solver`、`execution` 和扩展参数分片。
- `data/snapshots/v2/meta.db`：SQLite 元数据库，记录快照与各分片校验和。
- `data/manifest.json`：样本、v1/v2 快照的校验和与生成参数清单。

v1 与 v2 都解析为统一的 `EngineConfig`。升级时补全 `generation`、`created_at_ms`、`content_sha256` 和分片来源；降级时显式记录丢弃的 `generation`、绝对毫秒、内容摘要和分片元数据。

快照安装采用不可变目录加原子指针切换：

```bash
.venv/bin/python -m diagnostic_engine snapshots upgrade \
  --v1 data/snapshots/v1/config.json --out /tmp/v2
.venv/bin/python -m diagnostic_engine snapshots downgrade \
  --v2 data/snapshots/v2 --out /tmp/v1.json
.venv/bin/python -m diagnostic_engine snapshots install \
  --source data/snapshots/v2 --store /tmp/snapshot-store
```

版本下降返回 `E_SNAPSHOT_ROLLBACK`；已有提交持锁时，后到者立即返回 `E_STAGE_BUSY`，不阻塞等待。

## 执行链路

1. 读取 JSONL，校验 `sample_id`、二元标签、数值列和类别列。
2. 按配置对数值列做 z-score 或 min-max；类别列按配置词表固定顺序 one-hot。
3. 在精度切换前一次性计算分块方案，所有精度共用同一 `decision_sha256`。
4. 以 float64 整块路径作为参考真值，并生成每档迭代次数与收敛标记时间表。
5. float64、float32、定点三档使用相同特征顺序、相同分块、相同路径档位和相同迭代时间表求解。
6. 逐档输出系数、截距、目标值、收敛标记、分块/整块目标差。
7. 计算相对 float64 整块参考的误差传播谱和主导误差级别。
8. 做独立标准化重算、目标函数重算、分块覆盖、跨精度系数/目标/收敛自检。
9. 用配置精度路径生成推理分数与预测，写入 `predictions.jsonl`。

定点档将标准化特征量化为 `int64 * fixed_scale`，系数与中间算子按同一缩放因子和显式舍入模式量化。默认 `fixed_scale=8192`、`rounding_mode=nearest_even`。

## 分块规则

分块决策同时考虑样本数、特征数和 `block_budget_bytes`：

```text
bytes_per_sample = 8 * (feature_count + 3) + 16
budget_capacity = floor(block_budget_bytes / bytes_per_sample)
selected = min(configured_or_64, budget_capacity, sample_count)
```

预算不足时标记 `small_block_degrade=true`。分块按升序块号、块内特征顺序归约，覆盖 `[0, sample_count)` 全部行。报告包含代价估计、决策依据、覆盖摘要和决策校验和。

## 异常注入

注入由输入/参数触发，不通过修改源码制造。四阶段示例：

```bash
.venv/bin/python -m diagnostic_engine train \
  --data data/samples.jsonl \
  --snapshot data/snapshots/v2 \
  --out artifacts/run-injected \
  --inject 'feature_standardization:row_index=3,column=region,value=__OUT_OF_VOCAB__' \
  --inject 'path_solve:index=2,value=0,fallback=0.01' \
  --inject 'block_reduce:value=0' \
  --inject 'result_validation:value=0'
```

对应降级：

| 阶段 | 业务码 | 降级类别 |
|---|---|---|
| 特征标准化 | `E_CATEGORY_OUT_OF_RANGE` | `unknown_category_zero_fill` |
| 逐档求解 | `E_REG_STRENGTH_NONPOSITIVE` | `regularization_strength_clamped` |
| 分块归约 | `E_BLOCK_SIZE_ZERO` | `auto_block_plan` |
| 结果校验 | `E_TOLERANCE_NONPOSITIVE` | `default_validation_tolerance` |

直接配置 `block_size<=0`、`tolerance<=0` 或非正强度时也会走同一结构化降级链。每条记录包含阶段、业务码、动作、降级类别和细节。

## 校验与错误码

`validate_results` 会重算标准化统计量、目标函数、分块覆盖、块/整块目标差、跨精度系数与目标、收敛标记和迭代时间表。首个不一致返回 `E_INCONSISTENT`，并在 `first_divergence` 中给出检查名、精度、路径档位、字段、期望值、实际值、容差，系数偏差还给出首个系数下标。

主要错误码：

- `E_SNAPSHOT_ROLLBACK`：尝试安装更低版本快照。
- `E_STAGE_BUSY`：另一个快照提交持锁。
- `E_INCONSISTENT`：结果自检失败。
- `E_FORCED_TERMINATION`：测试用强制终止信号，CLI 退出码 75。
- `E_SNAPSHOT_CHECKSUM` / `E_SNAPSHOT_METADATA`：快照内容或 SQLite 元数据不一致。
- `E_CHECKPOINT_CORRUPT` / `E_CHECKPOINT_IDENTITY_MISMATCH`：检查点损坏或配置/数据/特征/分块变化。

## 检查点与恢复

每次路径档位提交后写入 `cp-XXXXXXXX.json`，并在阶段边界写入 `features_ready` 或 `path_complete`。检查点包含：

- 单调递增版本号；
- 内容 SHA-256；
- 快照校验和与逻辑配置指纹；
- 数据摘要、特征矩阵摘要、分块决策摘要；
- 已完成档位和下一档位；
- 精度、正则路径及收敛时间表。

模拟强制终止：

```bash
.venv/bin/python -m diagnostic_engine train \
  --data data/samples.jsonl \
  --snapshot data/snapshots/v2 \
  --out artifacts/run-recovery \
  --precision all \
  --abort-precision float64 \
  --abort-after-steps 2

# 退出码 75；随后直接重启同一命令即可恢复
.venv/bin/python -m diagnostic_engine train \
  --data data/samples.jsonl \
  --snapshot data/snapshots/v2 \
  --out artifacts/run-recovery \
  --precision all
```

恢复时从最近有效检查点继续；已提交档位直接复用，报告中的 `reused_steps` 与 `recomputed_steps` 可核验。

## 报告结构

`artifacts/run/run_report.json` 主要字段：

- `snapshot`：快照版本、代数、绝对毫秒、校验和与元数据补全来源。
- `data`：样本校验和、类别分布、特征名与标准化统计量。
- `blocking`：实际分块方案、整块对照方案、代价和决策依据。
- `precisions`：各精度逐档结果、检查点与恢复信息。
- `reference_whole`：float64 整块参考真值。
- `diagnostics`：特征缩放、分块累加、定点量化、梯度更新的绝对/相对偏差、误差区间、放大比和主导级别。
- `validation`：逐项自检结果；失败时抛出 `E_INCONSISTENT`。
- `injections`：结构化注入与降级记录。
- `inference`：预测文件位置、校验和、准确率和正类数。
- `recovery`：检查点目录与版本元数据。

## 本次实测结果

环境：Python 3.10.12、NumPy 2.2.6、SQLite 3.37.2，固定种子 `20261127`，384 行样本。

- `unittest` 与 `pytest`：均为 16 项全部通过。
- 验收脚本：20 项检查全部通过。
- 正常运行 `run_id`：`ae4d80cf35c9a3364f9bf9294398e09c40b101a5609a115bba3d89d4308760cc`。
- 三精度收敛标记均为 `[true, true, true, true, true]`，迭代时间表均为 `[41, 28, 49, 105, 189]`。
- 最终目标值：float64 `0.3571631728110526`，float32 `0.3571631610393524`，fixed `0.3570556640625`。
- 相对 float64 整块参考最大系数偏差：float32 `1.98e-6`，fixed `9.26e-4`。
- 最大目标偏差：float32 `3.74e-8`，fixed `1.08e-4`。
- 分块/整块最大目标差：float64 `1.11e-16`，float32 `5.96e-8`，fixed `0`。
- 推理准确率：`0.9036458333`。
- 主导误差级别：float32 为 `feature_scaling`，fixed 为 `gradient_update`。
- 四阶段注入后状态为 `degraded`，四类降级全部记录，`E_INCONSISTENT` 未触发。
- 强度注入场景下三精度仍共享收敛时间表 `[41, 28, 113, 195, 260]`；其中最后一档在迭代上限内未收敛，三精度标记一致为 `false`。
- 强制终止在第 2 档后发生；重启复用 2 档、重算 3 档；再次启动所有精度复用 5 档、重算 0 档。
- 快照回退返回 `E_SNAPSHOT_ROLLBACK`；持锁并发提交返回 `E_STAGE_BUSY`；v1/v2 统一参数完全一致。

完整机器可读结果见 `artifacts/acceptance/summary.json`，可读摘要见 `artifacts/acceptance/summary.md`。

