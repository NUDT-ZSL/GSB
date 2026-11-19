# LatentFlow 技术说明

## 1. 确定性边界

业务输出的确定性边界由以下输入完全决定：

1. 提示词文本；
2. 整数随机种子；
3. 画幅、帧数、去噪步数、潜帧通道数；
4. 时序混合、条件强度、噪声强度；
5. 锚定路径与锚定强度（仅 I2V）。

实现中不读取系统时间、随机源、环境变量或网络。`time.perf_counter` 只用于观测性能，不参与任何控制流或像素计算。

### 哈希与 PRNG

- 提示词条件向量：`SHA-256(prompt)` 作为 SHA-256 计数器 PRNG 的密钥，生成 64 个 `float32` 条件值。
- 初始潜帧：密钥包含种子、潜帧 shape 和通道数，生成完全相同的 `float32` 张量。
- 每一步去噪：密钥包含种子、步号和 shape；每步输出都进入 `stage_hashes`。
- 数组哈希：对 C-order、little-endian 的连续字节流计算 SHA-256。
- 配置序列化：`sort_keys=True`、固定分隔符、拒绝 NaN/Infinity。

因此同一输入可获取完全相同的条件向量、初始潜帧、每步去噪潜帧、最终潜帧和 RGB 帧序列。

## 2. 分块一致性

### 共享上下文

解码器在第一次处理前构造只读上下文 `D`：

```text
global_mean[c] = mean(latents[:, :, :, c] over all frames and spatial positions)
global_std[c]  = std(latents[:, :, :, c]  over all frames and spatial positions)
N[t]           = (latents[t] - global_mean) / global_std
```

`D` 永远基于完整潜帧序列。块 `[a,b)` 不会重新计算自己的均值/方差。

### 逐帧函数

对任意 `t`：

```text
M[t] = (1 - temporal_mix) * N[t] + temporal_mix * N[t-1]   (t > 0)
M[0] = N[0]
RGB[t] = Project(NearestNeighborUpsample(M[t]))
RGB[t] = AnchorStatistics(RGB[t], t)                      (I2V, t > 0)
Frame[t] = Quantize(RGB[t])
Frame[0] = anchor_pixels                                  (I2V)
```

所有依赖都来自完整潜帧上下文和帧索引，不读取块起点、块终点或已解码 RGB。

### 等价关系

设 `Decode(t)` 为上述逐帧函数，则：

```text
DecodeChunk(a, b) = [Decode(a), Decode(a+1), ..., Decode(b-1)]
DecodeWhole       = [Decode(0), Decode(1), ..., Decode(T-1)]
```

所以对任意块边界：

```text
Concat(DecodeChunk(0,k), DecodeChunk(k,2k), ...) ≡ DecodeWhole
```

不只统计量一致，块边界处的时序混合也使用前一块最后一帧的潜帧，而非解码后的像素反馈。量化采用 `floor(clip(x,0,255)+0.5)`，不依赖批次 shape 或 BLAS 分块路径。

`compare_frame_sequences` 做全帧全像素比较；差异时返回：

- 首个不一致的 `block_index`；
- 全局 `frame_index`；
- `pixel_position = {x, y, channel}`；
- 期望值、实际值、两侧 SHA-256。

## 3. 延迟观测与裁决

### 计划探针

`DecodePlanner` 对候选块实际执行一次 `decode_range(0, block_size)`，以 `perf_counter_ns` 记录：

- 总耗时；
- `normalize`、`temporal_mix`、`project_rgb`、`quantize_anchor` 四个阶段耗时；
- 每帧每阶段平均耗时；
- 选中首块的估算延迟。

这不是 sleep 或固定常数。计划记录 `local_decode_loop_probe`、探测帧数、阶段数、阶段耗时和探测总耗时。

### 显式优先级

1. **一致性硬不变量**：不允许块局部统计量、减少解码阶段或改变量化口径。
2. **资源硬限制**：单块 RGB 内存超限时缩小块；连一帧都容不下则拒绝。
3. **延迟预算**：从请求块大小向下寻找能通过真实探针的最大块。
4. **校验强度**：块越小、边界越多，计划提高校验等级；任何等级都保留一次整段全像素扫描。

若所有候选都超过预算，返回 `E_LATENCY_BUDGET`，其中包含：

```json
{
  "stage": "decode_planning",
  "completed_stages": ["prompt_hash", "seed_initial_latent", "denoise"],
  "decoded_blocks": 0,
  "rejected_candidates": [{"block_size": 8, "measured_first_block_ms": 16.1}]
}
```

首块真正执行后还会以实际耗时再次硬校验预算。若计划探针与执行时负载不同导致首块超限，返回 `stage=first_chunk_decode`、`decoded_blocks=1` 和实际阶段耗时。

## 4. 图生视频锚定

- 支持项目内本地 `.ppm/.pnm` 与定长 `.rgb/.raw`。
- 输入尺寸、格式或文件长度不符返回 `E_ANCHOR_MISMATCH`。
- `anchor_strength` 超出 `[0,1]` 返回 `E_CONFIG_INVALID`。
- 第 0 帧在量化后直接替换为输入图的原字节副本。
- 后续帧将自身通道均值/方差匹配到参考图通道均值/方差，再按强度与原帧混合。
- 解码完成后再次比较首帧与锚点；不一致返回 `E_ANCHOR_MISMATCH`。
- 锚定只依赖参考图和帧索引，因此分块与整段一致。

UI 的“注入锚点异常”通过翻转首帧一个像素模拟下游损坏，再由正式校验器捕获，用于证明错误路径可达。

## 5. 快照

三个类别：

- `decode_params`：配置、生成阶段哈希、锚定信息、完成报告；
- `chunk_plan`：完整解码计划、裁决依据、选中块大小、本地计时；
- `ready_blocks`：完整已就绪块清单，不是增量 patch。

提交过程：

1. 计算 payload 的规范 JSON SHA-256；
2. 版本 = 该类别历史最大版本 + 1；
3. 写入不可变历史文件 `category-00000001.json`；
4. `fsync` 后通过 `os.replace` 原子替换 `active-category.json`；
5. 原子写版本提示文件，便于损坏时仍能报告版本。

加载时验证类别、payload 类型和 checksum。损坏返回：

```json
{
  "code": "E_SNAPSHOT_CORRUPT",
  "details": {
    "category": "decode_params",
    "version": 1,
    "cause": "checksum does not match payload"
  }
}
```

重启后 `observe_replacement` 记录旧缓存版本、新加载版本、是否整体替换以及 payload checksum 是否变化。

## 6. 并发与资源

- HTTP 使用 `ThreadingHTTPServer`，本地确定支持多进程环境（验收环境执行了 `spawn` 多进程探针）。
- 分块计划租约由非阻塞 `Lock.acquire(blocking=False)` + 活动任务 ID 实现。
- 第二个计划提交立即得到 `E_STAGE_BUSY`，不会等待第一个完成。
- 同一计划下，不同块可由 `ThreadPoolExecutor` 并发解码。
- `in_flight` 集合限制单任务并发解码数；超过上限立即 `E_RESOURCE_LIMIT`。
- `max_frames` 超限拒绝；`max_chunk_bytes` 超限优先降块并记录 `block_size_reduced_for_memory`。

## 7. 已知边界

- 这是确定性的本地业务流水线，不是训练好的神经扩散模型；目的是验证生成任务、分块解码、延迟预算和一致性协议。
- 像素结果保证在同一平台、同一 NumPy/CPU 浮点路径下逐字节复现。跨不同 CPU 指令集/数学库的位级可移植性不在本实现承诺内。
- 首块探针是单次本地实测，机器负载会改变毫秒数；哈希和一致性不受影响。
- UI 中帧切换的 70 ms 间隔仅用于已计算帧的可视回放，不参与计算完成判定。
