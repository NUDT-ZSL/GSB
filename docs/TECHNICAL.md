# 技术说明

## 1. 数据模型

每个任务是一个不可变 `TaskCase`。组件先做语法/注册表检查，再进入可行性检查：

1. `parse`：字段结构、已知算子名、已知 `activation_kind`；
2. `feasibility`：轴序合法排列、块大小、eps、dtype、相邻融合对与归约依赖；
3. `policy/resource`：内存、局部性、遍历次数和资源上限；
4. `fusion_kernel`：非有限输入检查和数值执行；
5. `adjudication`：容差配置与逐元素裁决；
6. `cache/snapshot`：内容寻址提交与版本校验。

未知算子返回 `E_OP_UNKNOWN`，非法轴序返回 `E_AXIS_INVALID`，两者均带 `case_id`。

## 2. 激活语义

逐元素激活包括 `relu`、`gelu`、`silu`、`sigmoid`、`tanh`、`exp`、`leaky_relu`。
`softmax` 被登记为非逐元素归约激活，因此 `softmax -> transpose` 不可融合。

- `gelu` 使用 `gelu_tanh_v1`：
  `0.5*x*(1+tanh(sqrt(2/pi)*(x+0.044715*x^3)))`
- `sigmoid` / `silu` 将 manifest 的 `eps` 作为分母显式稳定项；
- 其余下游算子 `layernorm`、`softmax` 等也使用 float64 执行。

所有公式由代码版本和参数共同决定。缓存 key 包含 eps 与算子链摘要，因此激活口径变化不会命中旧产物。

## 3. 块映射

设输入形状 `S=(s0,...,s_{n-1})`，轴序 `A=(a0,...,a_{n-1})`，则：

```text
output_shape[k] = S[A[k]]
for flat input index i:
    c = unravel_index(i, S)
    out_coord = (c[A[0]], ..., c[A[n-1]])
    target = ravel_multi_index(out_coord, output_shape)
    out_flat[target] = activation(x.reshape(-1)[i])
```

由于 `A` 是排列，每个输入元素对应唯一输出元素，且无写冲突。块边界严格由
`[start, min(start+block_size, numel))` 确定并写入执行报告。

融合路径只保留当前块的激活结果。逐算子路径先写满完整激活张量，再调用 `np.transpose`。
前者 `materialized_intermediate_bytes=0`，后者记录完整中间字节数。

## 4. 等价性证据

同一输入下记录并比较：

- `fused_intermediate_digest` 与 `separate_intermediate_digest`；
- 融合/逐算子最终 dtype 输出的 bytes；
- 与逐算子 float64 参考的逐元素偏差；
- 融合输出经“逐算子激活 + 轴映射独立复算”的结果。

普通 fp64 case 最大绝对偏差通常为 0；fp32 case 与 fp64 参考存在精度 cast 误差，
但仍小于声明容差。紧容差注入会触发 `E_TOLERANCE_EXCEEDED`，并输出偏差谱和位置。

## 5. 缓存键

`fused_cache_key` 的输入摘要示例：

```json
{
  "schema": "tf-fused-cache-key-v1",
  "input_sha256": "...",
  "op_chain_digest": "...",
  "axis_order": [0, 2, 1],
  "requested_block_size": 5,
  "effective_block_size": 5,
  "eps_hex": "0x1.5798ee2308c3ap-20",
  "dtype": "float64",
  "precision_tier": "fp64",
  "activation_kind": "gelu",
  "input_shape": [2, 3, 4]
}
```

key 是该 canonical JSON 的 SHA-256。缓存条目保存 artifact SHA-256、entry checksum、
数组 shape/dtype/digest 和执行元数据；缓存索引保存全局版本与 checksum。

## 6. 部分失效依赖

| 节点 | 依赖 |
|---|---|
| `input` | 输入内容/形状 |
| `activation` | input、op_chain、activation_kind、eps、block、dtype |
| `axis_mapping` | input_shape、op_chain、axis_order |
| `transpose` | activation + axis_mapping |
| `fused` | activation + axis_mapping + 完整 cache key |
| `downstream:*` | fused、后续 chain 配置 |
| `final` | 最后下游节点 |

变化分类：

- `axis_order`：复用 `activation`，失效 `axis_mapping`、`transpose`、`fused`、下游；
- `eps`：复用 `axis_mapping`，失效 `activation`、`transpose`、`fused`、下游；
- `input`：失效 `input` 及其后全部节点，但轴映射可复用（形状/轴序不变时）；
- `policy/tolerance`：只更新计划和快照，不失效张量内容节点。

`cache_diagnose` 还会实际重跑未变化 case 验证缓存命中，只重算受影响 case，并输出
以 case 为作用域的失效/复用节点集合。两个集合必须无交集，其并集必须覆盖该 case 的全部节点。

## 7. 融合取舍

估计值包括：

- 融合遍历次数 1，逐算子遍历次数 2；
- 融合中间写 0，逐算子中间写 1；
- 融合块工作集：输入、激活、目标索引、坐标临时量；
- 逐算子块工作集：输入、激活；
- 轴映射局部性：相邻输出地址比例与平均地址跳数的加权分数。

固定优先级：

1. 硬资源上限；
2. case policy；
3. 内存；
4. 局部性；
5. 遍历次数收益。

`fusion_first` 在资源允许时优先融合；`memory_first` 在融合块内存更高时退回；
`locality_first` 在局部性分数低于 0.5 时退回；`balanced` 同时要求局部性下限与
融合/逐算子块内存比不超过 2。

## 8. 资源退回

资源限制包括 `max_resident_bytes`、`max_blocks`、`max_parallel_cases`、
`max_parallel_recompute_cases`。处理顺序：

1. 块数超限时尝试扩展块；
2. 内存超限时尝试缩小块；
3. 可配置降低 fp64→fp32；
4. 可配置退回逐算子；
5. `strict=true` 或仍无法满足时返回 `E_RESOURCE_LIMIT`。

退回生成 `fallback_category` 和 `upstream_code`，已完成结果不被修改，且仍逐 case 输出
与 float64 参考一致的裁决。

## 9. 快照

每个 case 的 `plan.json`、`snapshot.json`、`output.npy` 都是完整文件。提交时：

1. 校验旧 snapshot/plan/output/index 的版本与 checksum；
2. 生成新的全局单调版本号；
3. 依次原子替换 output、plan、snapshot；
4. 最后原子替换 index。

任何 checksum/版本不一致返回 `E_SNAPSHOT_CORRUPT`。默认不会修补损坏快照；
如显式启用修复选项，会丢弃旧内容并整体写入新快照。

## 10. 自检

`equivalence_report` 调用 `run_selfcheck`，检查：

- manifest/parse/feasibility/kernel/adjudication/cache/snapshot 的 case 集合；
- 每个 fused case 的 key 是否可由当前任务重算；
- cache artifact 哈希、输入哈希、中间摘要、输出 bytes 是否互相对账；
- snapshot 版本/checksum 与 store 是否一致；
- 融合输出能否由独立“激活 + 轴映射”路径复算；
- 失效/复用节点是否无交集且覆盖全部节点。

不一致返回 `E_INCONSISTENT`，并携带 `case_id` 与阶段。

## 11. 隔离与确定性

- 不使用系统时间、随机全局状态或外部服务；
- 注入只修改目标 case 的副本或输入 override；
- 每次注入使用独立缓存/快照命名空间；
- 未注入 case 比较 status、输入哈希、中间/最终摘要、cache key 和执行计划路径；
- 快照/缓存写入使用临时文件 + `os.replace` 原子替换；
- 目录写前检查和跨进程锁兼容 POSIX 与 Windows。
