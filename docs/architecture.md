# KLP 审计引擎设计说明

## 数据流

```text
bytes
  -> indexed parser (header / directory / CRC)
  -> record-level reject list
  -> per-session stable normalization
  -> lifecycle state machine
  -> derivation forest
  -> deterministic adaptive policy
  -> per-session budgeted validation
  -> stable JSON report + checkpoint
```

解析和会话划分在并发执行前完成。各会话校验只读取不可变事件和全局采样策略，因此 `concurrency` 不参与任何业务判定。

## 状态机不变量

设阶段秩为 `negotiate=0, derive=1, rotate=2, seal=3, destroy=4`。

1. 第一个合法事件必须是 `negotiate`。
2. 允许秩前进一格。
3. 仅 `derive` 和 `rotate` 允许保持当前秩，用于多层派生与多次轮换。
4. 其他重复、任何秩回退或跳级均为 `E_TRANSITION`。
5. 秩最终小于 4 时，缺失阶段产生 `E_INCOMPLETE`。

输入事件先按阶段序号排序，所以乱序到达、重复发送和并发读取不会改变结论。完全相同事件在规范化阶段幂等消失；同序号但语义内容不同则保留确定性的首条，同时产生 `E_SEQ_CONFLICT`。

## 派生森林

每个 `level=1` 事件创建一棵新链。`level=N` 事件的父节点是：

1. 同一会话；
2. `level=N-1`；
3. 阶段序号小于当前节点；
4. 满足以上条件的最大阶段序号（记录索引仅用于稳定打破并列）。

找不到父节点时产生 `E_PARAM`。每条链独立检查盐复用，因而不同链允许相同盐。长度和迭代规则检查直接父子边：

- `child.output_length <= parent.output_length`
- `child.iterations >= parent.iterations`

违规对象同时包含：

- `node` / `parent_node`
- `path`（直接父子路径）
- `chain_id`
- `details.first_node_path` / `details.duplicate_node_path`（盐复用时的链内路径）

## 规模自适应

观测值固定为：

- `input_bytes`：完整文件字节数；
- `derived_objects`：规范化后全批次 `derive` 事件数。

严格超过任一阈值才降级。降级后：

1. 以 `(session_id, stage_seq, record_index)` 排序所有派生节点；
2. 按配置块大小切块；
3. 对每块稳定键计算 SHA-256 排名；
4. 选择 `ceil(块数 * sampling_ratio)` 个完整块。

报告写出实际覆盖节点数、总节点数、实际比例、覆盖块数/总块数，而非只写请求比例。未覆盖任何节点的会话派生检查为 `not_sampled`，会话最多为 `degraded_pass`。

## 真实预算

- 每会话开始时计算 `deadline = perf_counter() + timeout`。
- 状态机、森林构建、父子检查、盐检查中按批检查 `perf_counter() > deadline`。
- 超时产生 `E_TIMEOUT`，丢弃该会话未完成结论。
- `derive` 数量在派生计算前检查；超限产生 `E_BUDGET`，派生检查为 `not_run`。
- 所有会话 Future 的异常被隔离，批内其他会话继续完成。

墙钟判定天然受操作系统调度影响；因此部署时应给正常样本留出足够余量，避免把并发压测造成的调度抖动当作协议失败。业务结论的确定性由状态机和派生校验保证，资源预算按真实执行情况独立判定。

## 检查点与一致性

检查点按稳定会话顺序保存连续前缀，水位为最后一条会话标识。写入前计算除完整性字段外全文档的 SHA-256。读取顺序：

1. 检查点结构版本；
2. 报告结构版本；
3. 算法策略版本；
4. 业务配置指纹；
5. 源文件 SHA-256/大小；
6. 完整性摘要；
7. 会话连续前缀、水位和聚合计数。

任一失败均从头重建。检查点和报告共享工作目录锁，写入采用同目录临时文件与 `os.replace`，因此中断只会留下完整旧版本或完整新版本。

## 稳定排序

- 会话：按 `session_id`；
- 会话事件：按 `(stage_seq, record_index)`；
- 错误：按阶段秩、序号、偏移、错误码、消息；
- 报告数组和错误码：显式排序；
- JSON 键：序列化时 `sort_keys=True`。

除真实资源预算产生的耗时字段外，相同输入与策略得到相同业务结论、降级标记、覆盖比例和稳定排序。

