# 设计说明

## 1. 核心不变量

DeployLab 把业务约束表达为以下不变量：

1. 阶段顺序不可跳越：`model_validation → quantization → calibration → inference`。
2. 一个阶段提交时，`completed_layers` 必须等于清单全部层。
3. 快照版本全局单调递增，快照内容校验和链接前一快照。
4. 快照、产物引用、产物摘要与轨迹在同一 SQLite 写事务中可见。
5. 推理只能引用当前代际已提交的量化/校准快照产物。
6. 权重或校准更新只能从旧 revision 上升；任何降级返回 `E_SNAPSHOT_ROLLBACK`。
7. 变更层及其所有下游层必须重算；上游层复用已提交缓存。
8. 受影响层集合与复用层集合无交集，并集覆盖全部层。

## 2. 持久化模型

`SnapshotStore` 使用 SQLite WAL + `synchronous=FULL`。主要表：

- `generations`：代际、规范化清单、校准配置、失效计划、是否完成。
- `snapshots`：阶段、版本、已完成层、产物摘要映射、元数据、内容校验和、前一版本链接。
- `artifacts`：`(stage, cache_key)` 内容寻址产物，payload 为规范化 JSON，摘要为 SHA-256。
- `snapshot_artifacts`：快照到产物的层级引用及 `reused/recomputed` 处置。
- `errors`：结构化业务错误审计。
- `meta`：全局最新版本、未归因计数。

`commit_stage` 的执行步骤：

1. 事务外验证层集合完整性，拒绝部分阶段。
2. `BEGIN IMMEDIATE` 获取唯一写锁；锁冲突立即转为 `E_STAGE_BUSY`。
3. 校验代际、阶段顺序、manifest 和 revision 单调性。
4. 计算全局版本号与前一快照链接。
5. 写入/复用内容寻址产物，写入快照与层引用。
6. 更新最新版本，推理阶段完成时标记代际完成。
7. 单事务提交。

因此读取方不会看到“快照已存在但产物引用缺失”的中间状态。

## 3. 内容校验和

快照校验和覆盖：

```text
generation
stage
version
prev_version
prev_checksum
completed_layers
artifacts[layer_id] = {digest, cache_key, disposition}
metadata
```

时间戳不进入校验和，便于跨次运行比较。快照链验证同时重新计算每一版校验和及前序链接。

## 4. 量化

`QuantScheme` 显式记录：

- dtype、位宽、`qmin/qmax`；
- granularity、请求分组大小、有效分组大小；
- 零点；
- 缩放组数量；
- 反量化摘要、量化误差、MAC 估计。

per-tensor 将张量展平后分组；per-channel 在每一输出通道行内分组。每个缩放组独立确定 scale，使用 `np.rint` 得到整数码并裁剪到 dtype 范围，再立即反量化。量化参数以 `float.hex()` 保存为文本，避免 JSON 浮点格式差异。

## 5. 推理

推理输入是 bundle 的确定性 `initial_input.npy`。每层：

```text
z = sum(weight_dequant * activation, axis=input) + bias
a = tanh(z)
```

矩阵乘按输出行计算 float64 内积，保证 resident/streaming/tiled 策略在同一环境下共享相同行级求和顺序。每层产物保存输入摘要、输出摘要、激活校验和、量化产物摘要和校准产物摘要。最终预测即最后一层激活向量。

## 6. 恢复

崩溃时：

- 已提交快照中的层产物仍有效；
- 当前阶段正在计算但尚未提交的产物仅存在于内存，进程被杀后自动丢失；
- 可选 crash marker 只记录“诊断上丢弃了哪些内存层”，恢复逻辑不读取它作为进度。

重启时：

- 读取最新持久化代际；
- 若代际未完成且 manifest/校准配置一致，从下一未提交阶段开始；
- 已完成阶段跳过；
- 当前阶段从第一层重建；
- 报告 `resumed_from_version/stage`、已提交层、重建层和诊断性丢弃层。

阶段级提交而不是层级提交，是对“部分完成阶段不得提交”的直接实现。跨代际上游层复用仍然有效，因为这些层来自此前**已提交**的代际快照。

## 7. 依赖失效

代际规划比较规范化层字段、`weight_revision`、校准 revision 与 seed：

- 所有变化层中的最小 `layer_index` 为失效起点；
- `affected = index >= start`；
- `reused = index < start`；
- 受影响层在本代际的所有阶段使用带代际 nonce 的缓存键，强制重算；
- 上游层使用内容寻址基础键，命中此前已提交产物。

这避免两件事：

1. 下游层错误复用旧激活链；
2. 受影响层因“内容恰好相同”而跳过应发生的重算记录。

## 8. 预算

预算账本随阶段快照持久化。量化阶段在内存预算压力下缩小有效分组；推理阶段优先 resident，之后降级 streaming，最后 tiled。层数与 MAC 硬上限直接返回 `E_RESOURCE_LIMIT`。所有降级类别写入 `budget.degradations`。

## 9. 错误归因

业务错误使用 `DeployError(code, stage, layer_id, field, details, injected)`。每个阶段顶层将未预期异常包装为 `E_UNATTRIBUTED`，以 `attributed=0` 写入审计表并递增全局计数。异常 injected 标志随记录持久化，便于区分人为注入与系统故障。

## 10. 一致性

`ConsistencyChecker` 不信任运行期摘要，重新从持久化快照和原始 bundle 重放：

- 阶段层集合；
- 快照链和内容校验和；
- 原始权重 + 缩放元数据 → 反量化重放；
- 量化产物 → 逐层激活 → 最终预测；
- 影响集/复用集；
- 校准样本与 revision。

失败抛出 `E_INCONSISTENT`，并在 `details.categories` 中分门别类列出问题。

