# Transformer 算子增量计算与诊断工作台

面向 Transformer 前向计算中 `softmax` 与 `layernorm` 的本地确定性分块执行、内容寻址缓存、增量失效传播、快照防护与数值诊断工作台。

项目不依赖容器、GPU、外部服务或真实数据集。任务清单为本地 JSONL，输入张量由 `seed + NumPy PCG64` 确定性生成；所有业务输入、缓存、快照、输出、报告和业务码都落在当前工作区。

## 功能概览

- 统一分块入口执行 Softmax / LayerNorm，严格校验 `op_kind`、`axis`、`eps`、`dtype`、`block_size` 等字段。
- 四阶段 DAG：`validate_plan` → `block_stats` → `normalize` → `output`，每阶段产物内容寻址并带内容哈希与单调版本号。
- 缓存键包含输入张量内容哈希、算子、形状、`block_size`、`axis`、`eps`、`dtype`、策略签名和上游产物哈希。
- 增量批次合并；追加 job、修订 `block_size/eps`、提升 dtype、修改输入张量时自动判定失效集合，只重算受影响 job 的后缀。
- 增量结果与无缓存从零四阶段重算做输出 `.npy` 字节级裁决。
- 分块路径与一次性整段路径的 float64 偏差谱；Softmax 在线分母、LayerNorm Welford 统计均可由分块明细重新合并。
- `strict` / `band` 门限策略、eps 下限、分块上限、归一化分母保护和源 dtype 次正规数保护。
- 分块计划、容差配置、每 job 统计量快照使用版本号 + 双层校验和 + 原子整体替换；拒绝回退和损坏快照。
- 非阻塞提交锁；四阶段运行期异常注入；显式降级链路；未归因异常不会静默丢弃。
- 自包含 HTML 诊断工作台与终端交互工作台。
- 跨模块自检：阶段集合、失效集合覆盖、统计再合并、偏差谱重算、输出文件、快照和确定性。

## 目录结构

```text
.
├─ transformer_diag/
│  ├─ cli.py             # python -m transformer_diag 入口
│  ├─ engine.py          # 批次合并、失效传播、四阶段提交、独立全量裁决
│  ├─ compute.py         # Softmax/LayerNorm 分块计算、合并、数值策略
│  ├─ diagnostics.py     # 偏差谱、统计还原检查、明细落盘
│  ├─ cache.py           # 内容寻址缓存与缓存键索引
│  ├─ snapshots.py       # 版本化快照、整体替换、回退/损坏防护
│  ├─ input_store.py     # seed 驱动输入、输入哈希、输入变更
│  ├─ locking.py         # 跨平台非阻塞提交锁
│  ├─ selfcheck.py       # 跨模块一致性自检
│  ├─ report.py          # 自包含 HTML 工作台
│  └─ workbench.py       # 终端交互工作台
├─ tests/test_workbench.py
├─ pyproject.toml
└─ requirements.txt
```

运行后工作区会生成：

```text
config.json
.workbench/
  cache/objects/           # 内容寻址产物 .bin + 校验 sidecar
  cache/index.json         # cache_key -> product_hash / artifact_version
  inputs/                  # 确定性输入 .npy
  snapshots/               # plan / tolerance / stats 版本快照
  state/report.json        # 最新诊断报告
  state/nodes.json         # 当前 job/阶段节点及完整缓存键组成
  state/errors.jsonl       # 最近业务错误历史
outputs/<job_id>.npy
diagnostics/deviation/<job_id>.npz
reports/workbench.html
```

## 环境准备

目标环境为 Windows 10/11 + Python 3.11/3.12。开发验证环境使用 Python 3.10.12 + NumPy 1.26.4；代码不使用 3.11 专有语法，因此同样兼容当前验证环境。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install numpy==1.26.4
```

也可以安装项目本身：

```powershell
python -m pip install -e .
```

不安装也可以直接在项目根目录用 `python -m transformer_diag` 运行。

## 快速开始

建议给演示单独工作区，避免污染源码目录：

```powershell
python -m transformer_diag --workspace .\work init --reset
python -m transformer_diag --workspace .\work sample
python -m transformer_diag --workspace .\work run --jobs .\work\samples\batch_1.jsonl
python -m transformer_diag --workspace .\work run --jobs .\work\samples\batch_1.jsonl
python -m transformer_diag --workspace .\work run --jobs .\work\samples\jobs_full.jsonl
python -m transformer_diag --workspace .\work run --jobs .\work\samples\batch_2_revision.jsonl
python -m transformer_diag --workspace .\work selfcheck
python -m transformer_diag --workspace .\work workbench
```

浏览器打开：

```text
work\reports\workbench.html
```

一键跑完整演示并生成验收摘要：

```powershell
python -m transformer_diag --workspace .\work demo
```

结果写入 `work/demo_result.json`。

## 任务格式

一行一个 job：

```json
{
  "job_id": "softmax_b1",
  "op_kind": "softmax",
  "input_shape": [4, 16],
  "block_size": 4,
  "axis": -1,
  "eps": 1e-6,
  "dtype": "float32",
  "seed": 20260101,
  "batch_no": 1,
  "revision": 1
}
```

约束：

- `op_kind`：`softmax` 或 `layernorm`。
- `axis`：单个整数，可为负；按 rank 归一化。
- `eps`：严格大于 0 的有限数。
- `dtype`：`float16`、`float32`、`float64`。
- `seed`：非负整数；输入使用 `numpy.random.Generator(numpy.random.PCG64(seed)).standard_normal(...)` 生成后转目标 dtype。
- `revision`：正整数。相同 `job_id` 的新 revision 只有大于或等于已登记 revision 时才允许进入。
- 批次文件按增量合并：新文件只包含新 job 或修订 job 即可，已登记但未出现在本批次的旧 job 会保留。

修改输入张量使用显式命令，避免实现内部随机修改：

```powershell
python -m transformer_diag --workspace .\work mutate-input --job-id layernorm_b2 --flat-index 0 --value 0.125
python -m transformer_diag --workspace .\work mutate-input --job-id softmax_b1 --scale 1.5
```

## 四阶段计算模型

阶段内部名称与含义：

| 阶段 | 含义 | 主要产物 |
|---|---|---|
| `validate_plan` | 字段、形状、dtype、策略门限、次正规数检查与分块计划 | 块边界、有效 eps/block_size、保护位置、输入摘要 |
| `block_stats` | 沿 normalization axis 的分块统计 | Softmax `local_max/local_sumexp`；LayerNorm `count/mean/M2` |
| `normalize` | 跨块稳定合并并形成归一化参数 | Softmax 在线 max + denominator；LayerNorm Welford mean/variance/invstd |
| `output` | 写回目标 dtype，并保留 float64 分块/整段参考 | 输出数组、两条路径 SHA256、字节一致性标记 |

算子口径：

- Softmax 在 `axis` 上归一化，使用 max-shift 稳定计算；跨块执行在线 max/sum 合并。
- LayerNorm 在 `axis` 上使用总体方差（`ddof=0`），不包含未建模的 affine 参数；跨块使用 Welford 合并公式。
- `block_size` 表示沿归一化轴每个块的元素数；大于轴长时退化为单块。
- 分块路径输出、整段参考输出都在 float64 上计算，最后按 job 的 dtype 写回。

## 内容寻址缓存

每个节点的缓存键材料至少包含：

```json
{
  "schema": 1,
  "engine": "transformer-op-workbench/1.0",
  "stage": "normalize",
  "job_id": "softmax_b1",
  "input_hash": "<输入张量内容哈希>",
  "op_kind": "softmax",
  "input_shape": [4, 16],
  "block_size": 4,
  "axis": 1,
  "eps": "9.9999999999999995e-07",
  "dtype": "float32",
  "policy_signature": "<策略签名>",
  "upstream_hash": "<block_stats 产物内容哈希>"
}
```

```text
cache_key = SHA256(canonical_json(key_material))
product_hash = SHA256(canonical_json(stage_payload))
```

同样的 `cache_key` 必复用同一缓存对象；键变化即使内容碰巧一致也会重算。对象侧车记录内容哈希，读取时重新计算 SHA256，缺失或被篡改返回 `E_CACHE_CORRUPT`。

查询某个 job/stage 的真实键组成和产物哈希：

```powershell
python -m transformer_diag --workspace .\work inspect --job-id softmax_b1 --stage block_stats
```

查询原始缓存键：

```powershell
python -m transformer_diag --workspace .\work inspect --cache-key <sha256>
```

## 增量失效规则

引擎将当前批次与持久化 registry、上一次 run state 和输入内容哈希比较：

| 变更 | 失效判定 |
|---|---|
| 新 job | 该 job 四阶段全部失效 |
| `block_size/axis/eps/dtype/seed/op/shape` 修订 | 该 job 从 `validate_plan` 起整条后缀失效 |
| 输入张量内容变化 | 该 job 从 `validate_plan` 起整条后缀失效 |
| 仅 `revision` 或 `batch_no` 元数据变化 | 不失效，记录 `revision_metadata_only` / `batch_metadata_only` |
| 策略变化 | 所有 job 失效，因为分母/次正规数策略改变输出 |
| 仅容差变化 | 复用计算产物，只重新裁决偏差谱与统计一致性 |
| 其他 job 未变化 | 四阶段全部命中，产物哈希与版本保持不变 |

每次运行报告均包含：

- `affected_jobs` / `unaffected_jobs`
- `recomputed_nodes` / `reused_nodes`
- `recomputed_jobs` / `reused_jobs`
- `skip_reasons`
- 逐 job 独立无缓存四阶段重算的 SHA256 与 `byte_identical`

## 快照与版本防护

快照类型：

- `plan/global`：当前分块计划整体快照。
- `tolerance/global`：容差配置整体快照。
- `stats/<job_id>`：每个 job 的合并统计量、整段统计量、normalizer 和产物哈希快照。

每个快照文件包含：

- 单调版本号 `version`
- `previous_version`
- 内容校验和 `content_checksum`
- 整文件校验和 `envelope_checksum`
- `replacement = "atomic-whole"`

写入规则：

- 版本递增且内容变化：构造完整新文件，临时文件 `fsync` 后 `os.replace` 原子整体替换。
- 同版本同内容：幂等返回，不修补旧文件。
- 同版本不同内容：`E_SNAPSHOT_ROLLBACK`。
- 低版本覆盖高版本：`E_SNAPSHOT_ROLLBACK`。
- 字段、内容或校验和被破坏：`E_CACHE_CORRUPT`。

查看和验证：

```powershell
python -m transformer_diag --workspace .\work snapshot list
python -m transformer_diag --workspace .\work snapshot get --kind tolerance --key global
python -m transformer_diag --workspace .\work snapshot verify
```

手工测试整体替换或回退：

```powershell
python -m transformer_diag --workspace .\work snapshot put --kind tolerance --key global --version 999 --content-file .\new_tolerance.json
python -m transformer_diag --workspace .\work snapshot put --kind tolerance --key global --version 1 --content-file .\old_tolerance.json
```

第二条命令会立即返回 `E_SNAPSHOT_ROLLBACK`。

## 数值一致性与偏差谱

每个 job 计算：

- 分块路径 float64 输出与一次性整段 float64 输出的最大绝对偏差。
- 最大相对偏差。
- 绝对/相对偏差最大位置。
- 超出 `atol + rtol * |reference|` 的元素位置。
- 按 `|Δ|` 排序的前 64 项偏差谱。
- 完整偏差明细：`diagnostics/deviation/<job_id>.npz`，包含 `block`、`whole`、`abs_dev`、`rel_dev`。

以下情况返回：

- `E_TOLERANCE_EXCEEDED`：元素偏差超容差，并输出位置、偏差谱和明细路径。
- `E_STAT_MISMATCH`：分块明细重新合并的结果与整段统计口径超容差。

默认容差位于 `config.json`：

```json
{
  "tolerance": {
    "atol": 1e-9,
    "rtol": 1e-8,
    "stat_atol": 1e-10,
    "stat_rtol": 1e-8
  }
}
```

可用 `--config override.json` 覆盖。把四个容差设为 `0` 可稳定触发 `E_STAT_MISMATCH` 和 `E_TOLERANCE_EXCEEDED`，用于验收错误链路。

## 数值稳定性策略

`config.json` 的 `policy` 支持：

```json
{
  "policy": {
    "strategy": "band",
    "band": 1e-15,
    "eps_min": 1e-12,
    "block_size_max": 1048576,
    "denom_min": 1e-300,
    "subnormal_policy": "flush"
  }
}
```

- `strict`：eps 小于下限或 block 超过上限立即返回 `E_POLICY_GUARD`。
- `band`：相对容差带比例。允许 eps/block_size 落在门限相对带内，并把有效 eps 提升到下限或把有效 block 收敛到上限，同时记录保护介入。
- 归一化分母低于 `denom_min` 或处于容差带时，替换为安全下限，记录 group 坐标、原值、保护值。
- 源 dtype 次正规数按该 dtype 的 `tiny` 识别；`flush` 清零并记录元素位置，`preserve` 保留但记录。
- 非法策略、负/非有限容差、非法分母下限、非法 dtype 等返回 `E_POLICY_INVALID`。

保护介入次数与位置写入 `validate_plan` 和 `normalize` 产物，可在 `inspect`、报告和工作台中核对。

## 异常注入、降级与并发

注入只通过运行参数或配置触发，不需要修改源码：

```powershell
python -m transformer_diag --workspace .\work run --jobs .\work\samples\batch_1.jsonl `
  --inject block_stats:E_INJECT_BLOCK:softmax_b1 --degrade-policy skip
```

格式：`STAGE[:CODE[:JOB_ID]]`，阶段名为：

- `validate_plan`
- `block_stats`
- `normalize`
- `output`

注入记录包含 `type/stage/code/job_id/source`。下游节点记录 `status=skipped`，降级链包含：

```json
{
  "stage": "normalize",
  "upstream_stage": "block_stats",
  "upstream_code": "E_INJECT_BLOCK",
  "category": "skip",
  "policy": "skip"
}
```

`--degrade-policy fail` 使用 `category=hard_fail`，任务状态为 failed；`skip` 使用 `category=skip`，任务状态为 skipped，整体报告为 degraded。

并发提交使用 `O_EXCL` 目录锁，不阻塞等待。后到者立即返回：

```json
{
  "code": "E_STAGE_BUSY",
  "field": "commit.lock",
  "stage": "commit"
}
```

可用 `--hold-lock-seconds` 人为扩大提交窗口做并发验证。

## 自检

```powershell
python -m transformer_diag --workspace .\work selfcheck
```

自检逐项验证：

- 四个阶段的成功 job 集合一致，报告中的 completed job 四阶段齐全。
- 重算集合与复用集合无交集，并覆盖受影响集合。
- 缓存对象内容哈希、sidecar 和节点记录一致。
- 分块统计重新合并可还原 `normalize` 阶段统计。
- 偏差谱可从 `npz` 明细重新计算且与报告一致。
- 输出文件、输出产物 SHA256、独立全量重算 SHA256 一致。
- 同一输入连续两次无缓存执行的所有阶段产物哈希一致。
- 快照版本、内容校验和、整文件校验和有效。
- 未归因异常记录必须带阶段、消息和细节。

任一不一致返回 `E_INCONSISTENT`，并列出失败检查；自检结果写入 `.workbench/state/selfcheck.json`。

## 业务错误码

| 业务码 | 含义 |
|---|---|
| `E_OP_UNSUPPORTED` | 未知 `op_kind` |
| `E_AXIS_INVALID` | axis 越界或非整数 |
| `E_EPS_INVALID` | eps 非正、非有限或非法类型 |
| `E_SHAPE_INVALID` | 输入形状非法 |
| `E_BLOCK_INVALID` | block_size 非正整数 |
| `E_DTYPE_UNSUPPORTED` | dtype 不在 float16/32/64 |
| `E_SEED_INVALID` | seed 非法 |
| `E_JOB_INVALID` | 必填字段、job_id、batch/revision 等非法 |
| `E_REVISION_STALE` | 旧 revision 覆盖新 revision，或同 revision 配置冲突 |
| `E_POLICY_INVALID` | 策略、容差、注入参数非法 |
| `E_POLICY_GUARD` | strict 策略下 eps/block_size 越过硬门限 |
| `E_TOLERANCE_EXCEEDED` | 分块/整段 float64 偏差超容差 |
| `E_STAT_MISMATCH` | 跨块统计合并无法还原整段口径 |
| `E_NUMERIC_UNSTABLE` | 计算出现非有限值 |
| `E_CACHE_CORRUPT` | 缓存对象、索引、快照损坏或校验和不符 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退或同版本内容冲突 |
| `E_STAGE_BUSY` | 已有提交在执行，本次不排队 |
| `E_INCONSISTENT` | 自检发现跨模块不一致 |
| `E_UNATTRIBUTED` | 未被阶段业务码覆盖的异常，已显式记录 |

所有业务错误均包含 `code/message/job_id/field/stage/details`（适用字段可能为 null），并通过标准输出 JSON 呈现。

## 命令行索引

```text
python -m transformer_diag [--workspace DIR] init [--reset]
python -m transformer_diag [--workspace DIR] sample [--out-dir DIR]
python -m transformer_diag [--workspace DIR] run --jobs FILE [--mode incremental|full]
    [--config FILE] [--inject STAGE[:CODE[:JOB_ID]]]... [--degrade-policy fail|skip]
    [--no-verify] [--hold-lock-seconds N]
python -m transformer_diag [--workspace DIR] mutate-input --job-id ID ...
python -m transformer_diag [--workspace DIR] inspect --job-id ID --stage STAGE
python -m transformer_diag [--workspace DIR] snapshot list|get|put|verify
python -m transformer_diag [--workspace DIR] selfcheck
python -m transformer_diag [--workspace DIR] verify
python -m transformer_diag [--workspace DIR] errors [--limit N]
python -m transformer_diag [--workspace DIR] workbench [--interactive] [--no-render]
python -m transformer_diag [--workspace DIR] demo
python -m transformer_diag [--workspace DIR] reset
```

`--workspace` 也可以放在子命令后，或通过环境变量 `TD_WORKSPACE` 设置。

## 实际验证结果

本机验证环境：Linux/WSL 上的 Python 3.10.12、NumPy 1.26.4；使用与 Windows 相同的 `python -m`、NumPy 1.26.4、文件缓存和原子替换路径。

自动化测试：

```text
$ python -m pytest -q tests/test_workbench.py
...........                                                              [100%]
11 passed

$ python -m unittest discover -s tests -v
...
Ran 11 tests

OK
```

覆盖：

- 初次全量、重复提交全命中。
- 追加批次只重算新增 job。
- `block_size/eps/dtype` 修订只重算目标 job。
- 手动修改输入张量只触发目标 job。
- 直接覆盖输入 `.npy` 文件也能被内容哈希检测并触发目标失效。
- 干净工作区全量重算与增量输出逐字节比较。
- 非法 op/axis/eps/dtype 的业务码与 job/field。
- 零容差下 `E_STAT_MISMATCH`、`E_TOLERANCE_EXCEEDED`、位置与偏差谱。
- band 门限保护、源 float32 次正规数清理、strict 门限拒绝。
- 快照整体替换、回退拒绝、损坏检测。
- 缓存对象损坏归因。
- 四阶段异常注入、下游降级、非阻塞 `E_STAGE_BUSY`。
- 每个阶段分别注入均产生记录，并验证对应下游降级链。
- 人为制造未归因异常时记录 `E_UNATTRIBUTED`，不静默吞掉。
- 同 revision 配置冲突返回 `E_REVISION_STALE`。
- 自检发现被篡改的偏差摘要并返回 `E_INCONSISTENT`。

一键演示验收结果：

```text
append_only_new_jobs_affected=True
incremental_full_byte_identical=True
initial_all_recomputed=True
input_mutation_affected_only_target=True
repeat_all_reused=True
revision_recomputed_only_revised_job=True
selfcheck_ok=True
```

最终增量状态下四个 job 均 `status=ok`，分块/整段偏差均远低于默认容差，独立全量重算逐字节一致：

| job | output SHA256 | max_abs | 越界元素 |
|---|---|---:|---:|
| `layernorm_b1` | `df0bd78ab4d92f3fcaa63a699ce7a4ff8b55c62c85d5ed16ca4bcab2c6154b3b` | `6.66e-16` | 0 |
| `layernorm_b2` | `3ab590b12e705976902e0a93928e2832cbe2ccaeaf321d6737514826e2f0b4e1` | `2.22e-16` | 0 |
| `softmax_b1` | `99ed10e8f4028ee5396303491573cbb6dda912a85465aaaeba0768c25fc166f8` | `8.33e-17` | 0 |
| `softmax_b2` | `334269afd0f5f44eb4b163834b90392d74c515d6042f07858685d4252a796035` | `1.11e-16` | 0 |

对应若使用同一工作区重新执行 `selfcheck`，所有检查通过，`code=OK`。

