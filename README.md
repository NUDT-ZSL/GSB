# 潜空间分块去噪诊断引擎

这是一个无真实模型、无外部服务依赖的本地诊断引擎。它把文生图链路压缩为四个确定性阶段：

1. 受控词表文本条件嵌入
2. `prompt_seed` 驱动的初始噪声构造
3. 按 `block_grid` 分块逐步去噪与全局统计量边界归一化
4. 潜帧到 RGB 的确定性解码

引擎同时执行 float64 整块参考路径，并把边界元素偏差、拼接后最大绝对/相对偏差、误差放大比和主导误差来源写入 SQLite。所有中间结果和账本均使用内容寻址文件，提交状态使用版本化快照。

## 快速开始

环境：Linux/WSL Ubuntu、Python 3.10+、NumPy。

```bash
python3 -m pip install -r requirements.txt

# 固定种子样本；scripts/generate_samples.py 的默认种子也是 20261126
python3 scripts/generate_samples.py --out samples --seed 20261126

# 输入校验
python3 -m latent_diag validate \
  --tasks samples/tasks.jsonl \
  --config samples/config.json

# 基线运行
python3 -m latent_diag run \
  --tasks samples/tasks.jsonl \
  --config samples/config.json \
  --db artifacts/demo/ledger.sqlite \
  --state artifacts/demo \
  --workers 2 \
  --batch-size 2

# 独立重算账本并检查四阶段任务集合、缓存文件、快照与事件分区
python3 -m latent_diag selfcheck \
  --config samples/config.json \
  --db artifacts/demo/ledger.sqlite \
  --state artifacts/demo \
  --json-out artifacts/demo/selfcheck.json
```

一次完整验收（测试套件 + 预算 + 失效传播 + 容差 + 强制恢复）：

```bash
python3 scripts/run_acceptance.py
```

结果写入 `artifacts/validation_report.json`。

## 输入契约

任务清单为 JSONL。核心字段：

```json
{"task_id":"t001_forest_f64","prompt":"Quiet forest with red fox","prompt_seed":1001,"latent_shape":[4,24,32],"block_grid":[2,2],"sampler_steps":5,"noise_scale":0.55,"quant_scale":1024,"rounding_mode":"nearest_even","dtype_profile":"float64"}
```

扩展字段：

- `revision`：正整数，默认 1；用于单调修订检查。
- `depends_on`：依赖任务列表，用于下游失效传播。
- `latent_file`：可选。样本生成器会写 `.npy` 文件；执行器同时按种子重算并校验文件内容哈希。
- `noise_shape`：可选。若存在必须等于 `latent_shape`。

`latent_shape` 为 `[C, H, W]`，`block_grid` 为 `[grid_h, grid_w]`，必须分别整除 H 和 W。`dtype_profile` 支持 `float64`、`float32`、`fixed`；定点档使用显式 `quant_scale` 和 `rounding_mode`。

## 确定性算子

### 条件嵌入

prompt 先转小写并压缩空白，再按受控词表映射。OOV token 映射到 `<unk>`。每个词元向量由 `SHA256` 计数器模式生成，不依赖 Python 进程随机哈希。嵌入加入 `<bos>`、`<eos>` 和位置权重后做 L2 归一化。因此仅大小写不同的 prompt 得到相同条件向量和相同哈希。

### 初始噪声

```python
rng = np.random.Generator(np.random.PCG64(prompt_seed))
latent = rng.standard_normal(latent_shape, dtype=np.float64) * noise_scale
```

不使用系统时钟、全局随机状态或未声明随机源。

### 分块去噪与边界归一化

每个采样步使用同一算子序列：

```text
raw   = decay * latent + bias + phase(step, condition)
mean  = mean(raw over the assembled full frame)
std   = std(raw over the assembled full frame)
next  = (1 - take) * raw + take * (raw - mean) / std
```

`take` 在块边界元素上为 `boundary_mix`，其他位置为 0。每个块独立计算 `raw` 和归一化，但 `mean/std` 来自当前步所有块拼接后的整帧；参考路径对同一步整帧使用相同统计量。这样：

- float64 分块路径与 float64 整块参考路径逐元素一致。
- 分块边界没有独立统计口径。
- float32/定点路径与整块同精度路径的调度差为 0；相对 float64 真值的差异只来自精度实现与边界归一化放大。

### 精度档

- `float64`：NumPy float64 算子。
- `float32`：每一步中间结果保持 float32。
- `fixed`：内部为 int64 定点值，`value_int = round(value * quant_scale)`，支持 `nearest_even`、`nearest_away`、`floor`、`ceil`、`trunc`。

三种精度档共用同一个表达式和同一个块遍历顺序。RGB 解码只使用“当前精度档最终潜变量去量化后的数值”，不存在额外的精度档专用解码分支。

## 调度与预算

调度按 JSONL 顺序切批，每批最多 `tasks_per_batch` 个任务，由 `ProcessPoolExecutor` 使用 `worker_count` 个本地进程执行。结果按输入顺序归并和提交，不按完成时刻归并。

预算口径：

- 总去噪迭代次数：每个任务 `block_count * sampler_steps`，对已准入任务累加。
- 驻留潜变量内存：每个任务估算候选潜帧、float64 参考、两份工作副本、RGB 和固定开销；每批内存为该批准入任务估算之和。
- 超限任务按 `budget.priority` 决定优先返回 `E_ITER_BUDGET` 或 `E_MEMORY_BUDGET`。
- 被拒绝任务写入 `rejected_tasks` 和 `event_log`，不进入四阶段结果集合。

## 误差账本

float64 整块参考路径是唯一真值。每个任务记录：

- 每个块的边界最大绝对偏差与首个偏差位置。
- 整帧拼接后的 `max_abs`、`max_rel`。
- 误差放大比：`max_abs / max(quantization_floor_error, 1e-12)`。
- 量化下限误差。
- 归一化统计量偏差。
- 同精度“分块 vs 整块”的调度误差。
- 主导误差来源：`quantization`、`normalization`、`block_boundary` 或 `none`。

超过容差时任务状态为 `tolerance_failed`，账本仍完整写入；结构化错误 `E_TOLERANCE_EXCEEDED` 携带 `task_id` 和 `[channel, row, col]` 首个偏差位置。

## 内容缓存与失效

以下对象均写入 `state/cache/blobs/<content-hash>.{npy,json}`，并在 `cache_index` 中记录种类、缓存键、内容哈希和路径：

- 条件向量
- 初始潜帧
- 每个采样步的候选/参考潜帧
- 每步归一化统计量
- 最终候选、参考、RGB float/u8
- 账本条目

任务 `dtype_profile`、`quant_scale`、`block_grid` 或 `prompt_seed` 在高修订号下变化时，该任务失效，并沿 `depends_on` 图传播到所有下游任务。未受影响任务复用检查点和账本。运行摘要显式给出：

- `invalidation.affected`
- `invalidation.reuse_candidates`
- `results.reused`
- `results.recomputed`
- `results.reuse_recompute_disjoint`

## 快照、并发提交与恢复

`state/snapshots/generations/bundle-XXXXXXXX/` 保存整包快照：

- `manifest.json`
- `budget.json`
- `ledger.json`
- `cache.json`

每个域快照都有单调递增版本号和覆盖 `domain + version + body` 的 SHA256 校验和。`CURRENT.json` 带整包校验和并原子替换，不修改旧快照文件。

提交使用非阻塞 `flock`。拿不到锁立即返回 `E_STAGE_BUSY`。任务边界提交发生在快照提交之前，因此快照冲突不会撤销已完成的任务结果。

每个成功任务提交一个 `checkpoint_entries` 记录。进程被 `SIGKILL` 后：

```bash
python3 -m latent_diag run ...   # 默认复用最近检查点
python3 -m latent_diag run ... --recompute-committed
# 强制重算已提交任务；账本哈希必须与旧值一致
```

## 自检

`selfcheck` 执行以下检查：

- 四阶段 `condition/noise/denoise/rgb` 的 task_id 集合一致。
- 每个账本条目由任务明细独立重算后输入哈希与账本哈希一致。
- 所有内容寻址 blob 的内容哈希与索引一致。
- 最新运行中 `reuse`、`recompute`、`rejected`、`recompute_failed` 集合两两不交。
- 复用检查点账本哈希一致；强制重算记录的旧账本哈希一致。
- 快照版本/校验和、快照账本哈希、快照缓存哈希与 SQLite 当前状态一致。

## 结构化业务码

| 业务码 | 含义 |
|---|---|
| `E_DUPLICATE_TASK_ID` | 重复 task_id |
| `E_LATENT_SHAPE_MISMATCH` | latent/noise 维度不一致 |
| `E_BLOCK_GRID_NOT_DIVISIBLE` | 分块网格不整除潜空间 |
| `E_QUANT_SCALE_INVALID` | quant_scale 非正 |
| `E_BUDGET_INVALID` | 预算、worker 或批大小非正/非法 |
| `E_ITER_BUDGET` | 总去噪迭代预算超限 |
| `E_MEMORY_BUDGET` | 批次驻留内存预算超限 |
| `E_TOLERANCE_EXCEEDED` | 相对 float64 真值超过容差 |
| `E_REVISION_REGRESSION` | task revision 回退 |
| `E_DEPENDENCY_CYCLE` | 依赖图存在环 |
| `E_STAGE_BUSY` | 另一快照提交正在进行 |
| `E_SNAPSHOT_CHECKSUM` | 快照校验和/元数据不匹配 |
| `E_CACHE_CORRUPT` | 内容寻址缓存缺失或哈希不符 |

## 实际验证

`python3 scripts/run_acceptance.py` 在本地 WSL Ubuntu、Python 3.10.12、NumPy 1.26.4 上通过。固定种子为 `20261126`，6 个任务覆盖三种精度档。

基线账本：

| task | max_abs | max_rel | 放大比 | 主导来源 | 分块调度误差 |
|---|---:|---:|---:|---|---:|
| t001_forest_f64 | 0.0 | 0.0 | 0.0 | none | 0.0 |
| t002_city_f32 | 7.52e-7 | 3.40e-7 | 11.41 | quantization | 0.0 |
| t003_ocean_fixed | 3.56e-3 | 1.81e-3 | 14.57 | normalization | 0.0 |
| t004_robot_f64 | 0.0 | 0.0 | 0.0 | none | 0.0 |
| t005_desert_f32 | 7.56e-7 | 4.41e-7 | 12.71 | quantization | 0.0 |
| t006_snow_fixed | 2.83e-3 | 1.44e-3 | 11.58 | quantization | 0.0 |

其余验收结果：

- 原清单重复运行：6/6 复用，0 重算。
- `t002` 以 revision 2 切到 float64：仅 `t002` 与其下游 `t004` 重算，其余 4 个复用；交并集正确。
- 迭代预算 50：接受 `t001,t004`，其余返回 `E_ITER_BUDGET`。
- 内存预算 700000 bytes：接受 `t001,t004,t005`，其余返回 `E_MEMORY_BUDGET`。
- 收紧容差：4 个非 float64 任务返回 `E_TOLERANCE_EXCEEDED`，均带 task_id 和首个偏差位置。
- 强制 `SIGKILL`：2 个检查点保留；重启复用这两个任务，重算其余 4 个；再执行 `--recompute-committed` 后 6 个账本哈希不变。
- 自动化测试：15 项全部通过。

完整证据见 `artifacts/validation_report.json`。

