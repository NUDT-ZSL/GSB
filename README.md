# Feature Diagnostic

本项目是一个完全本地、固定种子的在线特征数值误差诊断应用，入口为 `benchmark.py`。它把一次特征计算拆成四级流水：

1. `normalize`：确定性样本生成与显式 z-score 归一化
2. `quantize`：仿射定点量化与反量化
3. `aggregate`：按 `block_plan` 的两两特征交叉聚合
4. `spectrum`：以 float64 整块路径为参考的误差谱、容差诊断和账本统计

所有输入均由 `python benchmark.py init` 在本地生成，不访问模型、外部服务或网络资源。NumPy 固定为 `2.0.0`，SQLite 使用 Python 标准库。

## 环境

- Python 3.10+
- Linux / WSL Ubuntu
- `python -m pip install numpy==2.0.0`

## 快速开始

```bash
python benchmark.py
```

无子命令时执行完整验收演示，等价于：

```bash
python benchmark.py demo
```

演示会生成固定输入、运行完整诊断、做无缓存复现、注入崩溃并恢复、执行 v1/v2 双向迁移、验证定向失效传播、验证快照整替换/回滚/非阻塞冲突，并将结果写入：

- `artifacts/demo/verification_report.json`
- `artifacts/demo/invalidation_plan.json`
- `artifacts/runs/demo-baseline/error_ledger.jsonl`
- `artifacts/runs/demo-recovery/error_ledger.jsonl`

常用命令：

```bash
python benchmark.py init --force
python benchmark.py validate
python benchmark.py run --run-id diagnostic --fresh
python benchmark.py run --run-id diagnostic          # 从已提交检查点恢复
python benchmark.py verify --run-id diagnostic
python benchmark.py migrate --from data/spec_v1.json --to artifacts/v2-migrated
python benchmark.py migrate --from data/spec_v2 --to artifacts/v1-downgraded.json
python benchmark.py commit-snapshot data/spec_v2
python benchmark.py invalidate --baseline-run diagnostic
```

测试：

```bash
python -m unittest discover -s tests -v
```

## 输入与输出

生成器产出：

- `data/tasks.jsonl`：任务清单，含显式量化/反量化公式、舍入模式、分块计划、归约规则、修订号和逻辑时钟
- `data/batches.jsonl`：样本批次流
- `data/config.json`：固定主种子、默认容差和归约规则
- `data/spec_v1.json`：v1 单文件扁平表
- `data/spec_v2/`：v2 分片 JSON + `metadata.sqlite`
- `data/tasks_next.jsonl`、`data/spec_v2_next/`：定向失效演示输入

运行期产物位于 `artifacts/`：

- `runs/<run_id>/checkpoints/stage_0.json` 至 `stage_3.json`
- `runs/<run_id>/stage_00.v1/` 等不可变阶段目录
- `runs/<run_id>/error_ledger.jsonl`
- `runs/<run_id>/state.json`、`run_manifest.json`、`recovery_report.json`
- `cache/index.json`：内容寻址缓存索引
- `snapshots/active_snapshot.json`：单文件整快照，原子替换

详细数据契约、算法、检查点顺序、缓存键和迁移规则见 `TECHNICAL_DESIGN.md`。实际验收数据见 `VERIFICATION.md`。

## 数值口径

归一化：

```text
mean_j = fsum_i(x_ij) / n
var_j  = fsum_i((x_ij - mean_j)^2) / (n - ddof)
std_j  = sqrt(max(var_j, 0))
z_ij   = (x_ij - mean_j) / max(std_j, epsilon)
```

定点路径：

```text
q_ij        = R(z_ij / quant_scale) + quant_zero_point
z_hat_ij    = (q_ij - quant_zero_point) * quant_scale
R = half_away_from_zero 或 half_to_even（由任务清单显式指定）
```

float64 参考路径和定点路径读取同一份归一化数组与统计量，不重新估计统计量。

分块交叉：

```text
pair_sum(b, i, j) = fsum_samples(z_si * z_sj),  i <= j 且 i,j 位于同一 block
```

每个特征对只归属一个 block；各 block 的输出按 `reduction_rule` 的规范块序拼接。`math.fsum` 给出正确舍入的精确累加结果，因此分块路径与整块路径逐位一致。若不一致，抛出 `E_BLOCK_MISMATCH`，上下文包含首个偏差的 `feature_pair`、`block_index`、`pair_local_index` 和输出位置。

## 错误码与退出码

| 错误码 | 退出码 | 含义 |
|---|---:|---|
| `OK` | 0 | 成功 |
| `E_INTERNAL` | 1 | 未预期内部错误 |
| `E_BAD_BATCH` | 2 | 缺失批次字段、重复批次或未知任务 |
| `E_DUP_TASK` | 2 | 重复 `task_id` |
| `E_PARAM_INVALID` | 2 | 量化参数/公式/舍入/分块等非法 |
| `E_REVISION_ROLLBACK` | 2 | 任务修订号回退 |
| `E_SNAPSHOT_ROLLBACK` | 2 | 快照版本回退 |
| `E_CLOCK_ROLLBACK` | 2 | 逻辑时钟回退 |
| `E_VERSION_UNSUPPORTED` | 2 | 未知快照格式版本 |
| `E_FORMAT_CORRUPT` | 2 | 字段缺失、校验和失败或时间戳倒退 |
| `E_QUANT_INPUT` | 3 | 量化输入含 NaN/Inf |
| `E_QUANT_LAYER` | 3 | 量化/反量化维度不符 |
| `E_BLOCK_MISMATCH` | 3 | 分块结果与整块结果不一致 |
| `E_TOLERANCE_EXCEEDED` | 3 | 偏差超过配置容差 |
| `E_STAGE_BUSY` | 4 | 快照提交锁被占用，立即返回 |
| `E_SIMULATED_CRASH` | 6 | 仅用于恢复测试的故障注入 |

命令行输入/配置异常以单行 JSON 写入 stderr；运行诊断结果写入 stdout JSON、台账 JSONL 和检查点。
