# 技术说明

## 1. 数据流

```text
devices.json + events.json + config.json
        |
        v
规范化与校验 -> version_id / 两个输入摘要
        |
        v
每设备采样点整数级包络
        |
        v
物理帧游程合并 -> 帧上限续块 -> 帧块 sha256
        |
        v
有界线程池 + 全局在途预算 + 原子块持久化
        |
        v
全块摘要审计 -> 锚点诊断 -> 原子导出
```

## 2. 输入规范化

设备按 `id` 排序；事件按 `(start_ms, target_device, id)` 排序；锚点的目标设备
列表排序。规范化后的设备数组计算 `manifest_digest`，规范化事件和锚点计算
`events_digest`。版本标识为：

```text
sha256({
  engine_schema,
  manifest_digest,
  events_digest
})
```

并发度、预算、容差和路径不进入版本标识，因为它们不改变规范指令内容。

## 3. 采样点域包络

设设备采样率为 `r`（Hz），事件起始为 `s` ms，持续为 `d` ms：

```text
start = ceil(s * r / 1000)
end   = ceil((s + d) * r / 1000)
```

第 `k` 个采样点（`0 <= k < end-start`）的绝对采样序号为 `start+k`，局部时间为：

```text
local_ms = ((start + k) * 1000 / r) - s
```

对包络控制点 `(t_i, v_i)` 做线性插值。首个控制点之前取首值，末个控制点之后取
末值。量化公式：

```text
scaled = (v - strength_min) / (strength_max - strength_min) * (levels - 1)
level  = clamp(floor(scaled + 0.5), 0, levels - 1)
```

不同事件在同一设备上重叠时，对每个采样点的整数级取最大值。静音采样点为 0。
最终时间轴覆盖 `[0, 本设备最大事件结束采样点)`，因此驱动流中的时间空档也有
明确的零强度采样点，不会因帧装箱而移位。

## 4. 帧块装箱

物理帧 `f` 的采样范围由同一 `ceil` 规则得到：

```text
frame_start = ceil(f * frame_duration_ms * r / 1000)
frame_end   = ceil((f + 1) * frame_duration_ms * r / 1000)
```

对 `levels[frame_start:frame_end]` 做游程编码，得到有序元组
`(sample_start, sample_count, level)`。每 `max_commands_per_frame` 个游程切成
一个块：

- 第一块保留原始 `source_frame_index`；
- 续帧块也保留原始来源帧和采样区间；
- 第一块记录额外块数 `split_count` 和原因 `FRAME_COMMAND_LIMIT`；
- 每块 `command_count <= max_commands_per_frame`；
- 所有块的 `completed_sample_*` 首尾相接，完整覆盖设备时间轴。

该规则的实质是“按完整游程切提交帧”，不是修改采样时间轴。因此超限处理不会
拆分一个同强度游程，也不会丢弃采样点。

## 5. 并发、预算与确定性

每个设备只有一个顺序工作单元。多个设备工作单元由有界线程池并行执行。设备内
指令编号、样本区间和帧块边界均在进入预算等待前确定，所以线程调度不影响结果。

`InflightBudget` 维护：

- 可用指令额度；
- 以 `(device_id, block_index, ticket)` 排序的等待堆；
- 实际使用峰值、等待次数和累计等待时间。

块在原子写入期间持有额度，写入完成立即释放。优先级队列保证预算不足时，低设备
标识先获得额度；高标识即使请求更小也不会越过正在等待的低标识请求。由于配置
要求单块指令数不超过总额度，等待不会死锁。

预算是提交阶段的在途容量控制，不是量化或采样策略；报告中的 `peak` 可用来验证
从未超过 `limit`。

## 6. 状态一致性

块文件摘要覆盖除 `digest` 外的完整块 JSON，包括 `digest_algorithm` 和声明
指令条数。文件写入顺序：

1. 同目录 `mkstemp` 临时文件；
2. 写入完整 JSON、换行、`flush` 和 `fsync`；
3. `os.replace` 原子替换正式块；
4. 尽力 `fsync` 父目录。

启动时先检查版本。版本不一致时把旧状态和旧导出目录整体改名归档，避免旧块或
旧导出被误用。版本一致时逐块检查：

- 文件存在；
- JSON 可解析且为对象；
- 摘要重算一致；
- `command_count == len(commands)`；
- 完整内容等于当前输入决定的期望块。

缺失块直接补算；无效块记录 `E_DIGEST` 后补算。所有 worker 完成后再执行一次
全量审计，随后从磁盘重新读取指令流。若外部在两次校验之间修改文件，最终读取仍
会再次校验并拒绝不一致内容。

## 7. 锚点诊断

每个锚点标记在每台设备上取最早事件。参考设备提供参考采样点时间。其他设备：

```text
expected_sample = round(reference_time_ms * target_rate / 1000)
sample_delta    = actual_sample - expected_sample
delta_ms        = actual_sample_time_ms - reference_time_ms
```

同时检查实际锚点采样点确实被某条指令覆盖，并记录 `command_index` 和
`command_sample_offset`。容差比较使用 `abs(delta_ms)`，只要一个设备越限就立即
返回 `E_SYNC`，不进行平均。

## 8. 信号与强制终止

Python 信号处理器只设置事件和记录信号编号，不做文件 I/O。工作线程只在以下位置
观察停止事件：

- 准备处理一个块之前；
- 一个块原子持久化并调用进度回调之后；
- 预算等待的短超时轮询中。

因此可处理信号不会截断正在替换的块。优雅中断写 `interrupted.json`，不写最终
导出。强制终止可能留下临时文件，但不会留下摘要有效的半块；重启清理临时文件并
按块摘要恢复。

## 9. 复杂度

设设备 `d` 的总采样点数为 `N_d`，指令游程数为 `R_d`，帧块数为 `B_d`。

- 包络与量化：`O(sum N_d)`；
- 帧内游程与装箱：`O(sum N_d)`；
- 块摘要和原子写入：`O(sum R_d)` 数据量；
- 启动恢复与最终审计：`O(sum B_d)` 文件读取和摘要；
- 锚点诊断：每锚点每设备线性扫描一次该设备指令列表。

样例规模很小，所有中间数据只驻留本地，不访问网络。
