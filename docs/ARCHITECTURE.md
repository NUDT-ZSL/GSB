# 架构与一致性说明

## 1. 确定性边界

任务输入的规范化结果是一个 `JobSpec`。矩阵值只由 `(seed, matrix_shapes, dtype)`
决定：`numpy.random.default_rng(seed)` 依次生成 A、B，再转为目标 dtype，最后统一
转成 `float64` 参与计算。`layout=col` 只改变内存序，不改变逻辑元素值。

策略决策只依赖 `JobSpec` 与 `PipelineConfig`，不读取时间、随机数或进程状态。
策略记录使用 `json.dumps(sort_keys=True, separators=(",", ":"))` 规范化，校验和
基于该字节序列，因此“同 job_id、seed、配置”的记录逐字节一致。

## 2. 四阶段状态机

阶段索引固定为：

```text
1 validation -> 2 planning -> 3 accumulation -> 4 aggregation
```

每个阶段完成全部任务后，`SnapshotStore.commit_stage` 才允许写快照。提交时：

1. 获取 `commit.lock`；已存在则立即抛 `E_STAGE_BUSY`
2. 扫描并验证全部已提交快照及前序校验和链
3. 要求目标版本严格等于最新版本 + 1，且阶段顺序正确
4. 要求 `completed_jobs` 与期望任务集合完全相等
5. 计算完整文档的内容校验和
6. 原子写 `snapshots/NNNN-stage.json`
7. 原子写 `CURRENT.json`

`CURRENT.json` 不是恢复的唯一依据。启动时扫描快照目录并按版本链重建已提交阶段，
这覆盖了“快照已原子写入、但 `CURRENT.json` 尚未更新时进程终止”的窗口。

## 3. 恢复模型

恢复开始后：

1. 验证配置指纹、源 JSONL 校验和、任务指纹
2. 读取最新已提交快照
3. 检查 `progress.json`；若其阶段索引高于已提交阶段，则其中 `blocks_done` 计入
   `discarded_blocks`
4. 删除 `progress.json`
5. 从最新已提交阶段之后开始执行

`progress.json` 只记录阶段名、任务计数和已尝试完成的分块数，不包含可用于恢复的
矩阵累加值。即使它被篡改，也只影响恢复报告中的丢弃计数，不会影响计算输入。
实际中间累加数组只存在于进程内存中，硬退出后自然丢失。

已提交阶段的 payload 是后续阶段的唯一状态输入。例如 `aggregation` 从
`accumulation` 快照中的 base64 float64 数组解码，不会再次执行矩阵乘。

## 4. 数值路径

GEMM 被视为 batch=1 的 3D 计算，因此两条路径共用同一个核：

```text
accumulator[b,m,n] += A[b,m,k] @ B[b,k,n]
```

每个 K 分块产生一个 `PartialTile`。`plain` 直接相加；`kahan` 和 `neumaier`
维护与输出同形状的补偿数组。最终再加补偿量、执行可选次正规数清理与定向舍入。

所有输入块、块贡献、累加器、补偿器、参考结果和输出均为 little-endian
`float64`。快照中的数组以原始 float64 字节 base64 编码，并单独记录数组校验和，
避免 JSON 十进制往返造成元素级差异。

参考结果由 `numpy.matmul` 对完整 float64 输入独立计算。分块结果与参考结果做
`atol + rtol * |reference|` 检查；首个越界元素及最多 10 个位置写入
`E_TOLERANCE_EXCEEDED.details.violations`。

## 5. 资源上限

`resident_bytes` 估算当前分块路径同时驻留的 A 块、B 块、C 块和补偿块：

```text
batch_tiles * (bm*bk + bk*bn + bm*bn + compensation*bm*bn) * 8
```

当整批输出内存超过 `max_accum_bytes` 时先从 `batched` 降为 `per_batch`；单块仍
超限时按 2 的幂缩小块尺寸。降级类别写入策略和逐任务结果摘要。如果分块数超过
`max_block_count`，不继续盲目缩小，而是返回 `E_RESOURCE_LIMIT`。

## 6. 完整性与并发

- 快照内容变化会破坏 SHA-256 校验和
- 前序校验和链阻止中间阶段被替换
- 文件名中的版本/阶段必须与 JSON 内容一致
- 版本只能递增，旧版本覆盖返回 `E_SNAPSHOT_ROLLBACK`
- 提交锁只在提交窗口持有；后到提交者立即失败，不排队
- 锁文件超过 60 秒视为进程崩溃遗留可回收，正常提交窗口远小于该阈值

## 7. 验证分层

`verify` 不信任输出目录中的派生文件，而是从已提交快照恢复业务对象和计划，然后：

- 对最终 `.npy` 做文件校验和与逐元素比较
- 独立重算分块路径
- 用保存的 `PartialTile` 列表重新合并
- 对参考 matmul 做容差检查
- 切换 `layout` 并做同容差复核
- 检查转置恒等式
- 构造 batch=1 BMM / GEMM 等价任务并比较

任何失败都转换为 `E_INCONSISTENT`，并保留类别与 `job_id`，便于定位是策略、分块、
输出文件还是跨算子语义问题。
