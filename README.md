# RoPE 变体裁决与内容寻址缓存组件

这是一个只依赖 Python 3.10+ 与 NumPy 的本地领域组件。它对同一批注意力任务同时计算两种位置编码变体，裁决角度映射、分块/整段注意力一致性、跨变体打分偏差，并把四类 DAG 节点写入内容寻址缓存。

核心目标不是迁移配置，而是保证：

- 同一 case、变体和固定种子下，旋转角与旋转后 Q/K 可逐字节复现。
- 快照版本号变化不会制造新的缓存键；只有语义或上游内容变化才改变键。
- 快照切换后只重算真正失效的节点，结果与全量重算逐节点哈希一致。
- 快照提交非阻塞互斥，进程被强制终止后仍能恢复到最后一个完整提交。

## 快速开始

```bash
python3 --version
python3 -m pip install -r requirements.txt

python3 -m rope_arbiter.cli --generate-sample --cases data/sample_cases.jsonl
python3 -m rope_arbiter.cli \
  --cases data/sample_cases.jsonl \
  --cache-dir .cache/rope-arbiter \
  --reports-dir reports \
  --parallelism 8
```

默认报告写入 `reports/variant_arbitration_report.json`。缓存目录和报告目录都会自动创建。

## 目录

```text
rope_arbiter/
  variants.py       # 四种角度变体、旋转、映射声明与残差裁决
  attention.py      # 分块打分、在线 softmax、整段对照、排序一致性
  cache.py          # 内容寻址对象库、清单、诊断与资源限制
  models.py         # case、不可变快照、原子持久化、非阻塞提交阶段
  engine.py         # 四类节点 DAG、并发执行、业务错误聚合
  diagnostics.py    # 跨模块自检
  workflow.py       # 快照序列、定向失效验证、全量重算对照
  cli.py            # 命令行完整示例
data/
  sample_cases.jsonl
tests/
reports/
  variant_arbitration_report.json
```

## 变体定义

所有变体共享半维配对口径：

```text
第 i 维与第 i + head_dim/2 维配对，i = 0 .. head_dim/2 - 1
Q 和 K 使用同一组旋转角

x'[i]     = x[i] * cos(theta_i) - x[i+h] * sin(theta_i)
x'[i+h]   = x[i+h] * cos(theta_i) + x[i] * sin(theta_i)
```

基础频率为：

```text
omega_i = base_theta ** (-2i / head_dim)
```

四种变体的角度定义为：

| 变体名 | 定义 |
| --- | --- |
| `rope` | `theta(pos, i) = pos * omega_i` |
| `position_interpolation` | `theta = (pos / scale_ratio) * omega_i` |
| `ntk_aware` | `base_eff = base_theta * scale_ratio ** (head_dim/(head_dim-2))`，再以 `base_eff` 生成频率；等价于 `theta = pos * omega_i * scale_ratio ** (-2i/(head_dim-2))` |
| `block_frequency` | 将 `head_dim/2` 个频率对连续分成 `len(freq_block)` 块，第 `b` 块乘 `freq_block[b]`：`theta = pos * omega_i * freq_block[block(i)]` |

`head_dim=2` 时 NTK 指数分母取 `max(head_dim-2, 1)`。任务中的 `freq_block` 可写成正数列表、正整数块数或 `{"count": n, "decay": r}`；快照中统一规范化为列表。

Q/K/V 由 `numpy.random.Generator(numpy.random.PCG64(seed))` 按 Q、K、V 固定顺序生成，形状为 `(num_heads, seq_len, head_dim)`，不读取系统时间或系统随机源。

## 角度映射裁决

每个有序 `variant_pair` 必须在内置或自定义 `MappingRegistry` 中声明。内置声明覆盖四种变体的全部有序组合，映射形式为：

```text
predicted_target[pos, i] = source_angle[pos, i]
                         * target_factor[i] / source_factor[i]
```

其中 PI 的位置因子为 `1/scale_ratio`，NTK 和分块频率使用各自逐维因子。裁决使用圆周残差：

```text
residual = wrap_to_pi(observed_target - predicted_target)
```

报告包含完整 `residual_spectrum`、逐维最大值、最大残差位置和首个超容差位置。未声明映射返回 `E_MAPPING_UNDECLARED`；残差超容差返回 `E_MAPPING_DRIFT`，上下文含 `case_id`、维度索引和偏差量。

## 注意力路径

注意力分数为 `Q_rot K_rot^T / sqrt(head_dim)`。分块路径按 `block_size` 划分 Q/K 块，对每个 query 块逐 key 块执行在线 softmax：

- 维护行最大值、指数和、加权 V 累加量与修正因子。
- `causal=True` 时只访问 `key_position <= query_position` 的块和元素。
- 分块矩阵组装结果与独立整段 `QK^T` 逐元素比较。
- 在线 softmax 输出与整段 softmax 输出、行最大值、指数和、logsumexp 比较。

任一偏差超过 `block_tolerance` 时返回 `E_BLOCK_MISMATCH`，报告首个偏差元素索引、实际值、预测值和偏差。跨变体报告给出有效单元上的最大绝对打分偏差和逐 query 行 pairwise 排序一致率。

注意：`equivalence.conclusion` 是整体结论，包含角度映射、块/整段路径、跨变体打分和注意力输出四部分。不同位置编码变体通常具有精确的角度映射关系，但跨变体注意力打分并不等价，因此整体结论可以是 `NOT_EQUIVALENT`，同时 `angle_mapping_equivalent=true` 且 `block_path_equivalent=true`。

## 缓存 DAG 与键

每个变体生成四类节点：

```text
angles
  -> rotated_qk
       -> block_scores
            -> normalization_stats
```

`normalization_stats` 同时依赖 `block_scores` 与 `rotated_qk`。每个节点键都包含下列字段，但不包含快照版本号：

```text
schema, node, case_id, variant, base_theta,
scale_ratio, freq_block, block_size, causal,
seq_len, head_dim, num_heads,
content_hashes, block_content_hash,
upstream_hashes, upstream
```

不适用于该变体或节点的参数写为 `null`，例如 `rope` 的 `scale_ratio`、`angles` 的 `block_size`。因此：

- `scale_ratio` 变化只改变 PI/NTK 的角度键及其下游。
- `freq_block` 变化只改变分块频率变体的角度键及其下游。
- `block_size` 变化只改变 `block_scores` 与 `normalization_stats` 键。
- `base_theta` 变化改变所有变体角度键及其下游。
- 仅快照版本号变化时，全部键与产物哈希保持不变并复用。

产物使用确定性二进制 artifact 编码后原子写入 `cache/objects/`。清单 `cache/manifest.json` 同样原子替换。`ContentAddressedCache.diagnose(case_id, variant, node)` 返回键组成、上游引用、产物哈希和完整命中/重算历史。

## 快照与并发

`VariantSnapshot` 是不可变整体，携带：

- 单调递增 `version`
- 规范化内容的 SHA-256 `content_checksum`
- 每个 case 的参数摘要

`SnapshotStage.try_commit()` 使用非阻塞锁。另一个提交正在进行时，后到者立即收到 `E_STAGE_BUSY`，不排队等待。提交前验证回退、同版本内容冲突和校验和；持久化成功后一次性替换活动快照引用。计算侧在调用开始时读取一次快照对象，不会混读新旧参数。

快照文件、缓存对象和缓存清单都采用临时文件 + `fsync` + `os.replace` 的原子写入。测试会在子进程完成提交后发送 `SIGKILL`，再从磁盘重新加载并验证快照与缓存对象完整。

## 资源限制与降级

`EngineLimits` 和 `ContentAddressedCache` 提供：

- 单次调用注意力内存峰值上限，超限 `E_RESOURCE_LIMIT`。
- 缓存唯一条目数上限，并发下使用预留机制，超限 `E_RESOURCE_LIMIT`。
- 并行重算节点上限。请求并行度高于上限时降低到上限，并报告 `PARALLEL_RECOMPUTE_REDUCED` 降级类别。

降级只影响调度，不改变节点语义、键或 artifact 哈希。

## 可集成接口

```python
from rope_arbiter import (
    ArbiterEngine,
    ContentAddressedCache,
    EngineLimits,
    SnapshotStage,
    VariantSnapshot,
)

snapshot = VariantSnapshot.from_cases(cases, version=1)
stage = SnapshotStage(initial=snapshot)
cache = ContentAddressedCache(".cache/rope", max_entries=4096)
engine = ArbiterEngine(cache, limits=EngineLimits())
report = engine.run(cases, stage.snapshot(), parallelism=4)
```

主要入口：

- `compute_angles(case, variant)`、`generate_qkv(case)`、`rotate_qk(q, k, angles)`
- `adjudicate_angles(...)`、`build_builtin_mapping_registry()`
- `compute_block_scores(...)`、`compute_online_normalization(...)`
- `SnapshotStage.try_commit(...)`、`SnapshotStore.load/save`
- `ContentAddressedCache.diagnose(...)`
- `ArbiterEngine.run(...)`、`run_self_check(report)`

## 报告内容

`variant_arbitration_report.json` 包括：

- 每个快照的提交回执、版本和内容校验和
- 每次切换的期望失效集、实际重算集、复用集和重算 case 集
- `affected_but_reused_from_cache`：键已失效但历史缓存中已有同语义 artifact 的节点
- 与全新空缓存全量重算的逐节点 artifact 哈希对照
- 每个 case 的角度残差谱、逐维残差、最大残差、跨变体打分偏差、排序一致率
- 分块长度、块数、块内容哈希、块/整段最大误差和首个偏差
- 四类节点的键、上游引用和产物哈希
- 缓存诊断样本、业务码、降级标记和跨模块自检结果

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 裁决通过 |
| `E_MAPPING_UNDECLARED` | 有序变体对没有映射声明 |
| `E_MAPPING_DRIFT` | 实测角度与映射预测角度超出容差 |
| `E_BLOCK_MISMATCH` | 分块路径与整段路径超出容差 |
| `E_RESOURCE_LIMIT` | 注意力内存、缓存条目或资源上限超限 |
| `E_STAGE_BUSY` | 已有快照提交正在进行 |
| `E_SNAPSHOT_ROLLBACK` | 请求版本小于当前版本 |
| `E_SNAPSHOT_CORRUPT` | 快照/缓存清单损坏或校验和不符 |
| `E_SNAPSHOT_MISSING_CASE` | 快照缺少任务 case |
| `E_CACHE_CORRUPT` | 缓存清单不可读取或结构非法 |
| `E_INVALID_CASE` | 任务字段或变体参数非法 |

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 四种变体角度公式、半维配对、Q/K/V 和旋转逐字节确定性
- 映射未声明、映射漂移
- causal/noncausal 分块与整段一致性、人为块偏差定位
- 内容寻址复用、缓存诊断、条目上限、并发预留
- 快照原子持久化、回退、损坏、同版本冲突、非阻塞 `E_STAGE_BUSY`
- `SIGKILL` 后快照与缓存恢复
- 快照定向失效、版本号无关复用、全量重算哈希一致
- 并行降级、内存上限、缓存上限
- CLI 报告和跨模块自检

本次实际验证结果（Python 3.10.12、NumPy 1.26.4）：

```text
单元测试                 23/23 通过
样本 case                6
每个快照处理节点         48（6 case × 2 变体 × 4 节点）
跨模块自检               ok=true，7/7 个快照转换通过
最大角度映射残差         4.440892098500626e-16
最大分块/整段打分误差    0.0
最大注意力输出误差       8.881784197001252e-16
最大归一化统计误差       1.7763568394002505e-15
映射/分块/归一化业务码   全部 OK
降级类别                 PARALLEL_RECOMPUTE_REDUCED
最终缓存条目             72
报告 SHA-256             071c8ad640fc654f5e37314b96b477e5d459fff5aba8e35792ddf4426676e86e
```

定向失效实测：

| 快照转换 | 失效节点 | 实际重算 | 复用 |
| --- | ---: | ---: | ---: |
| 初始版本 1 | 48 | 48 | 0 |
| `scale_ratio`（PI） | 4 | 4 | 44 |
| `scale_ratio`（NTK） | 4 | 4 | 44 |
| `freq_block` | 4 | 4 | 44 |
| `block_size` | 4 | 4 | 44 |
| `base_theta` | 8 | 8 | 40 |
| 仅版本号从 6 到 7 | 0 | 0 | 48 |

每次转换均与使用全新空缓存的全量重算逐节点 artifact 哈希一致。样本中的整体等价结论为 `NOT_EQUIVALENT`，因为跨变体注意力打分不同；但所有 case 的角度映射、分块/整段路径分别都通过了声明容差。

完整报告：`reports/variant_arbitration_report.json`。
