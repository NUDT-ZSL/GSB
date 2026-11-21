# 本地 BMM / GEMM 四阶段流水与恢复验证工具

该项目在空工作区中实现了一个纯本地、确定性的矩阵乘执行管线，覆盖 GEMM 与批量
矩阵乘 BMM。矩阵数据由 `job_id` 对应的固定 `seed` 通过 NumPy 生成，不依赖网络、
外部模型或数据附件。一次执行被拆成四个持久化阶段：

1. `validation`：输入校验与批次语义解析
2. `planning`：按规模选择分块、循环顺序与批量路径
3. `accumulation`：分块累加、补偿求和与容差检查
4. `aggregation`：结果聚合、偏差裁决与输出固化

每个提交的阶段快照都有单调递增版本号、完整任务集合和 SHA-256 内容校验和。进程被
硬终止后，重启只从最后已提交阶段之后继续；未提交阶段会整体重建，未提交的分块贡献
不会进入最终结果。

## 环境

- Python 3.10+
- NumPy 1.24+
- 不使用 Docker、数据库、网络服务或其他第三方依赖
- Windows 命令行使用 `python -m ...` 与标准库 `argparse` / `pathlib` / `subprocess`

Windows 示例：

```bat
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install "numpy>=1.24"
python -m bmm_pipeline.cli generate --out samples\jobs.jsonl
python -m bmm_pipeline.cli run --jobs samples\jobs.jsonl --run-dir .runs\main
python -m bmm_pipeline.cli status --run-dir .runs\main
python -m bmm_pipeline.cli verify --run-dir .runs\main
```

也可以执行项目内脚本：

```bat
python scripts\generate_samples.py --out samples\jobs.jsonl
python scripts\kill_resume_demo.py --work-dir .demo --fault-after-blocks 20
scripts\kill_resume_demo.bat
```

## JSONL 任务格式

一行一个 JSON 对象：

```json
{"job_id":"gemm-row-plain","op_kind":"gemm","matrix_shapes":[[24,18],[18,20]],"dtype":"float64","layout":"row","seed":20261223,"block_size":5,"accum_mode":"plain"}
{"job_id":"bmm-batched","op_kind":"bmm","matrix_shapes":[[2,9,7],[2,7,8]],"dtype":"float32","layout":"col","seed":20261224,"block_size":4,"accum_mode":"kahan"}
```

字段约束：

- `op_kind`：`gemm` 或 `bmm`
- GEMM 形状：`[[m,k],[k,n]]`
- BMM 形状：`[[batch,m,k],[batch,k,n]]`
- `dtype`：`float16`、`float32`、`float64`；所有计算中间量统一转为 `float64`
- `layout`：`row` 或 `col`
- `seed`：非负整数
- `block_size`：正整数；实际策略会按维度截断并保证完整覆盖
- `accum_mode`：`plain`、`kahan`、`neumaier`

`layout` 不影响逻辑矩阵值，只影响输入物理内存序；列主序使用 NumPy Fortran-order
数组。验证器会同时检查行/列序结果容差一致性和 `(A@B)^T = B^T@A^T` 转置语义。

## 快照与恢复

运行目录结构：

```text
run/
  snapshots/
    0001-validation.json
    0002-planning.json
    0003-accumulation.json
    0004-aggregation.json
  CURRENT.json
  progress.json
  errors.jsonl
  results/
    <job-id-sha256>.npy
  final_results.json
```

关键不变量：

- 一个阶段只有在全部任务完成后才能提交；部分任务集合返回 `E_STAGE_INCOMPLETE`
- 快照文件和 `CURRENT.json` 都通过同目录临时文件、`fsync`、`os.replace` 原子发布
- 每个快照包含阶段名、版本、任务集合、前序快照校验和、完整 payload 与整体校验和
- 提交使用 `O_CREAT|O_EXCL` 锁；并发后到者立即得到 `E_STAGE_BUSY`，不等待
- 版本只能按 `1,2,3,4` 递增；旧版本或重复阶段返回 `E_SNAPSHOT_ROLLBACK`
- 任何解析失败、内容校验和不符或前序校验和链断裂都返回 `E_SNAPSHOT_CORRUPT`，
  并携带阶段名与版本号，不使用部分可解析内容
- 已提交阶段直接加载结果，不重算；`progress.json` 仅做未提交进度遥测，恢复时读取
  丢弃分块数后立即删除，绝不用于累加

恢复报告包含 `committed_stages`、`resumed_from_stage`、`rebuilt_stages`、
`executed_stages` 和 `discarded_blocks`。配置指纹或源 JSONL 文件内容发生变化时
返回 `E_RESUME_MISMATCH`，防止把不同输入拼接到旧快照上。

## 分块、资源与数值策略

策略记录包含：

- `dimensions`、`block_size`、`loop_order`、`batch_path`
- `block_count`、`estimated_flops`、`estimated_cost`、`resident_bytes`
- `decision_basis` 与维度/代价估算公式
- `degraded` 与 `degradation_categories`

相同任务、seed 和配置下，策略记录用规范化 JSON 序列化，逐字节一致。分块起点由
`range(0, size, block)` 生成，末块截断到维度上界，因此不会遗漏或越界。

资源超限策略：

- 单阶段任务数超过 `max_stage_jobs`：立即返回 `E_RESOURCE_LIMIT`
- 批次聚合驻留内存超限：降级为 `per_batch`，标记
  `BATCH_AGGREGATION_DISABLED`
- 单分块缓冲仍超限：逐次减半分块尺寸，标记 `SMALLER_BLOCKS`
- 降级后分块数超过 `max_block_count` 或已无法继续缩小：返回 `E_RESOURCE_LIMIT`

`kahan` / `neumaier` 在 K 方向分块累加时维护补偿量，并统计补偿介入次数和受影响
元素数。`subnormal_mode` 支持 `preserve` / `flush`；`rounding_mode` 支持
`nearest` / `up` / `down` / `toward_zero`。定向舍入在最终 float64 落盘前执行，
用于验证可配置舍入路径；默认 `nearest` 不额外改变数值。

## 业务错误码

| 错误码 | 场景 |
|---|---|
| `E_SHAPE_INVALID` | 形状非法、秩错误、内维不匹配、JSONL 记录非法 |
| `E_BATCH_MISMATCH` | BMM 批次维度不一致 |
| `E_DTYPE_UNSUPPORTED` | dtype 不受支持 |
| `E_BLOCK_SIZE_INVALID` | block_size 非正整数 |
| `E_ACCUM_MODE_UNKNOWN` | accum_mode 未知 |
| `E_TOLERANCE_EXCEEDED` | 分块结果超出 float64 参考容差，包含首个越界索引和偏差 |
| `E_STAGE_BUSY` | 另一提交持有锁 |
| `E_STAGE_INCOMPLETE` | 阶段任务未全部完成 |
| `E_SNAPSHOT_ROLLBACK` | 尝试回退、重复或覆盖旧版本 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏、校验和/版本/前序链不一致 |
| `E_RESOURCE_LIMIT` | 任务数、分块数或驻留缓冲超限 |
| `E_INCONSISTENT` | 一致性自检失败 |
| `E_RESUME_MISMATCH` | 恢复时配置或源任务清单变化 |

所有业务错误 JSON 均包含 `code`、`message`；与任务相关的错误包含 `job_id` 和
`field`，必要时附带 `stage`、`version`、`details`。

## 状态与一致性验证

`status` 展示：

- 当前阶段、运行生命周期、快照版本与最新校验和
- 每阶段已完成任务数、逐任务阶段完成矩阵
- 补偿求和介入次数、次正规数清理次数
- 每任务最大/平均/P95/P99 偏差与越界计数
- 最近业务错误码与错误日志

`verify` 独立执行以下检查，失败时返回 `E_INCONSISTENT`，并给出
`category`、`job_id` 和相关偏差：

- 四阶段任务集合完整
- 策略记录逐字节复现，分块完整覆盖
- 最终 `.npy` 文件校验和与累加快照逐元素一致
- 独立重算结果与已提交结果一致
- 所有分块部分和可合并还原整体结果
- 分块结果在 float64 参考容差内
- 行主序/列主序容差一致
- `(A@B)^T = B^T@A^T` 转置语义
- GEMM 与 batch=1 BMM 独立结果一致
- `final_results.json` 与聚合快照一致

交互式面板：

```bat
python -m bmm_pipeline.cli interactive --run-dir .runs\main
```

可用命令：`status`、`snapshots`、`verify`、`errors [N]`、`help`、`quit`。

## 强制终止演示

```bat
python -m bmm_pipeline.cli demo --work-dir .demo --fault-after-blocks 20
```

`demo` 会启动三个真实子进程：

1. 基线：完整执行 8 个样本任务
2. 故障：完成 20 个累加分块后调用 `os._exit(97)`，不执行清理
3. 恢复：从已提交的 `planning` 继续，重建 `accumulation` 和 `aggregation`

随后逐任务比较恢复输出与基线输出，并执行完整 `verify`。演示报告写入
`.demo/demo_report.json`。`--work-dir` 应由演示独占；首次使用空目录，后续目录会
带 `.bmm_demo` 标记。命令拒绝清理项目根、其父目录或没有标记的非演示目录。

## 测试

```bat
python -m unittest discover -s tests -t . -v
```

测试覆盖必选错误码、策略确定性、分块覆盖、降级标记、容差越界位置、四阶段提交、
回滚、损坏快照、并发提交、资源上限、真实硬退出恢复、逐元素一致性、输出篡改检测
和演示脚本。实际运行结果见 `VALIDATION.md`。

## 代码结构

```text
bmm_pipeline/
  jobs.py        JSONL 解析、任务校验、确定性样本
  matrices.py    固定 seed 矩阵生成与 float64 参考实现
  strategy.py    分块策略、代价估算、资源降级
  kernels.py     分块 GEMM/BMM、补偿求和、容差检查
  snapshots.py   原子快照、校验和链、提交锁
  engine.py      四阶段状态机、恢复、结果聚合
  status.py      状态面板数据
  verify.py      跨模块一致性自检
  demo.py        硬退出与恢复对比演示
  cli.py         argparse 统一入口
scripts/         样本生成与进程控制脚本
tests/           标准库 unittest 验收测试
```

更详细的不变量与故障模型见 `docs/ARCHITECTURE.md`。
