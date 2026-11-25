# TII 训练推理一体化数据编辑与诊断应用

这是一个可在 WSL Ubuntu 本地直接运行的最小版本，用固定种子数据诊断训练/推理一体化链路在数据规模、配置代数、任务 revision 与强制终止场景下的缓存复用、定向重算和恢复行为。

## 环境

- Python 3.10+
- NumPy 1.26+
- 标准库 `sqlite3`、`json`、`threading`、`hashlib`、`pathlib`
- 不使用 Docker，不访问外部网络或服务

当前工作区实测：

```text
Python 3.10.12
NumPy 1.26.4
Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.35
```

## 快速开始

```bash
python3 --version
python3 -m pip install -r requirements.txt

# 固定种子生成任务清单、两代配置和 SQLite 元数据库
python3 sample_generator.py --input-dir input --out-dir out --seed 42

# 正常执行训练 + 推理
python3 -m tii_app.cli run --input-dir input --out-dir out --mode both --seed 42

# 生成自检报告
python3 -m tii_app.cli selfcheck --input-dir input --out-dir out --seed 42

# 完整验收：正常完成 + 强制终止 + 恢复 + 查询 + 自检
python3 -m tii_app.cli demo --input-dir input --out-dir out --recovery-dir out_recovery --seed 42

# 按 case_id 查询完整产物，并导出训练梯度/推理预测等数组
python3 -m tii_app.cli query \
  --input-dir input --out-dir out --case-id case-007 --mode both \
  --export-npz out/reports/query-case-007.npz

# 双向配置迁移
python3 -m tii_app.cli migrate --direction v1-to-v2 \
  --config-path input/configs/config_v1.json \
  --out-dir out/migrations/v1-to-v2 --target-generation 2
python3 -m tii_app.cli migrate --direction v2-to-v1 \
  --config-path input/configs/v2 \
  --out-dir out/migrations/v2-to-v1 --base-epoch-ms 0

# 自动化测试
python3 -m unittest discover -s tests -v
```

## 随题样本

`sample_generator.py` 生成 10 个唯一 case，并额外写入 1 行完全相同的重复任务，用于验证“重复任务不重复计量”。任务按 `logical_clock_ms` 的时间窗跨越 v1/v2 两代配置：

- `input/tasks.jsonl`：逐行任务清单，字段为 `case_id`、`data_size`、`feature_dim`、`label_dim`、`dtype`、`quant_scale`、`quant_zero_point`、`block_plan`、`revision`、`logical_clock_ms`。
- `input/tasks.meta.json`：任务源版本、内容校验和、重复行计数。
- `input/configs/config_v1.json`：v1 单文件配置，相对生效秒 + 扁平超参数 + 校验和。
- `input/configs/v2/shards/*.json`：v2 两个分片，各自带生成代数、绝对毫秒时刻、分片索引和校验和。
- `input/configs/v2/meta.sqlite`：
  - `config_shards` 记录绝对时刻、代数、分片路径、超参数摘要、校验和、墓碑标记；
  - `config_tombstones` 记录失效墓碑。

样本参数由 `sha256(global_seed, case_id, revision, task_content_hash)` 派生的 PCG64 随机流生成。`data_size` 直接决定行数，`feature_dim`、`label_dim` 决定矩阵 shape 并做显式校验。

## 数据与算子契约

### 量化规则

- 缩放：`x / quant_scale`
- 舍入：`half_away_from_zero`
- 零点：`round(x / scale) + quant_zero_point`
- 饱和：裁剪到目标整数 dtype 的 `[min, max]`
- 反量化：`(q - quant_zero_point) * quant_scale`
- 累加与模型计算使用 `float64`

量化契约记录在样本缓存摘要中，代码位置为 `tii_app/sample_data.py`。

### 统一算子链

同一入口 `tii_app/ops.py::execute_operator_chain` 执行：

```text
fixed-point dequantize
  -> row block + feature col chunk linear
  -> tanh activation
  -> train: MSE gradient / infer: argmax + softmax confidence
```

- 行块边界由 `block_plan.row_block` 决定，列归约块由 `block_plan.col_block` 决定。
- 固定顺序为“先行块、后特征列块”，训练与推理共享同一 forward 缓存键和 forward digest。
- `block_plan.priority` 是逻辑时钟相同情况下的确定性调度次级键；逻辑时钟始终是第一排序键。
- 训练产物包含 `grad_weights`、`grad_bias`、loss 与激活留存；推理产物包含预测 id、概率与置信度。

## 内容寻址缓存

阶段键由输入内容独立恢复，不依赖旧缓存：

```text
config   = sha256(generation, config_hash)
sample   = sha256(task_content_hash, global_seed)
quantize = sha256(sample_key, dtype, scale, zero_point, rounding_contract)
model    = sha256(config_key, feature_dim, label_dim, model_hyperparameters)
forward  = sha256(quant_key, model_key, block_plan, operator_contract)
train    = sha256(forward_key, loss_contract, mode)
infer    = sha256(forward_key, prediction_contract, mode)
```

二进制产物写入 `out/stage_cache/stages/<key[:2]>/<key>.npz`，缓存索引、大小和 SHA-256 保存在版本化账本 `out/ledgers/stage_cache.json`。只有批次内所有任务阶段成功后才把索引整体提交；未提交的 `.pending` 中间产物会在终止/恢复时删除。

## 失效传播

`tii_app/invalidation.py` 与引擎运行时变更检测同时覆盖三类变化：

- 仅配置依赖：配置代数/哈希变化，复用 `sample`、`quantize`，重算 `model`、`forward`、训练/推理后缀。
- 仅数据依赖：`revision` 或 `data_size` 变化，复用 `model`，重算 `sample`、`quantize`、`forward` 和后缀。
- 两者同时变化：重算配置与数据汇合点及其后缀。

引擎会逐 case 比较已提交 entry 与当前 `task_content_hash + config_generation + config_hash + stage keys`。未变化 case 直接跳过；变化 case 只重建 key 已变化的阶段，未变化上游从已提交缓存复用。自检报告输出 `recompute_tasks`、`reuse_tasks`、交集与并集，并验证它们互斥且覆盖所有任务。

## 内存预算

固定三层预算：

```text
total_feature_buffer_size
single_pass_workspace_size
model_workspace_size
```

训练与推理有显式不同策略：

- 训练：完整 float64 特征 + 梯度输入驻留，模型工作区按权重/偏置/梯度扩大。
- 推理：量化特征常驻，仅当前行块反量化，提升内存密度和吞吐。
- `both`：任一层级按两种模式的最大需求准入。

任一需求超限会抛出 `E_MEMORY_BUDGET`，CLI 以 JSON 输出错误码并非零退出。可通过 `--total-feature-buffer-size`、`--single-pass-workspace-size`、`--model-workspace-size` 调小预算复现。

## 检查点与恢复

- 按逻辑时间窗和批次上限切分任务，批次边界提交检查点。
- 检查点包含单调递增版本、已完成 case 集合、内容校验和、逻辑时钟与关联缓存/样本账本版本。
- 提交顺序为：暂存产物 -> 样本账本 -> 缓存索引账本 -> 进度检查点。
- 只有本批次所有任务阶段成功才提交；中途强制终止不会提交该批次进度。
- 重启时从最近提交检查点继续；已完成且内容未变化的 case 不重算；未提交 pending 产物被清除；若崩溃窗口落在“缓存已提交、进度未提交”之间，启动时会将缓存/样本账本裁剪回检查点引用集。
- 配置或任务演进需要重算较早逻辑时钟的任务时，新一次执行使用重放时钟从当前最小任务开始推进；检查点保存所有已完成任务的逻辑时钟上界，避免恢复起点倒退。

使用 `--simulate-crash-after-tasks`、`--simulate-crash-after-stages` 或
`--simulate-crash-after-cache-commit` 可复现强制终止；最后一种专门落在
“缓存账本已提交、进度检查点未提交”的窗口，重启时执行裁剪与重算。

## 版本化账本与并发

以下账本均为完整 JSON 快照，不使用增量修补：

- `out/ledgers/tasks.json`
- `out/ledgers/configs.json`
- `out/ledgers/samples.json`
- `out/ledgers/stage_cache.json`
- `out/ledgers/progress.json`

每个快照包含 `format_version`、单调递增 `version`、全量 `data` 与 `sha256(canonical_json(payload))` 校验和。提交通过同目录 `O_EXCL` 事务锁完成：

- 版本或校验和不符合预期时整体拒绝，返回 `E_STAGE_BUSY`；
- 后到并发提交不阻塞等待；
- 新快照通过临时文件 `fsync + os.replace` 原子替换；
- 读取时自动验证校验和，篡改返回 `E_LEDGER_CORRUPT`。

## 查询与自检

`query` 返回 task、配置、样本摘要、阶段缓存键、数组 shape/dtype/摘要，并可导出完整 `.npz`：

```text
train_*: features, labels, quantized, dequantized, weights, bias,
         logits, activations, grad_weights, grad_bias
infer_*: features, labels, quantized, dequantized, weights, bias,
         logits, activations, predictions, probabilities, confidence
```

`selfcheck` 写入：

- `out/reports/selfcheck.json`
- `out/reports/selfcheck.md`

检查项包括：

1. 重复任务只计量一次；
2. 所有账本版本/校验和及检查点内容校验；
3. 阶段缓存键可由任务内容哈希与配置版本独立还原；
4. 所有已提交 artifact 文件存在且校验和一致；
5. 训练/推理共享 forward digest 与块边界/归约契约；
6. 任务执行顺序与逻辑时钟一致；
7. 三层预算不超限，极小预算返回 `E_MEMORY_BUDGET`；
8. 仅配置、仅数据、两者同时变化三类失效集合互斥且覆盖全部任务；
9. 无未提交 pending 产物。

报告不包含墙钟时间；相同输入、种子和提交状态重复生成时字节一致。

## 当前实测结果

执行：

```bash
python3 sample_generator.py --input-dir input --out-dir out --seed 42
python3 -m tii_app.cli demo \
  --input-dir input --out-dir out --recovery-dir out_recovery --seed 42
python3 -m unittest discover -s tests -v
```

结果：

- 样本：10 个唯一任务、1 个重复行、59 个内容寻址 artifact。
- 正常运行：`completed`，6 个批次检查点，进度版本 7，缓存版本 6。
- 强制终止：执行到第 5 个任务时模拟终止；前两个批次共 3 个 case 已提交，当前批次 2 个 case 的中间产物被丢弃。
- 恢复运行：从检查点继续，最终 10 个 case 全部完成；产物签名、缓存索引、完成集合、进度/缓存版本与正常运行一致。
- 自检：9/9 通过；恢复工作区同样 9/9 通过；自检报告重复生成一致。
- 自动化测试：17 项全部通过，覆盖确定性样本/量化、双模式算子、预算、账本并发、双向迁移、三类失效、运行时 revision/配置代数变化，以及任务级、阶段级、缓存/进度跨账本窗口的强制终止恢复。

具体机器可读结果见：

- `out/reports/demo_report.json`
- `out/reports/recovery_comparison.json`
- `out/reports/selfcheck.json`
- `out/reports/selfcheck.md`
- `out/reports/query-case-007.npz`
- `out/migrations/v1-to-v2/migration_report.json`
- `out/migrations/v2-to-v1/migration_report.json`

## 代码结构

```text
sample_generator.py          固定种子输入生成器
tii_app/models.py            任务 schema、校验、去重摄入
tii_app/sample_data.py       确定性样本、量化/反量化
tii_app/ops.py               统一训练/推理算子入口
tii_app/configs.py           v1/v2 读取、解析、双向迁移
tii_app/keys.py              内容寻址阶段键
tii_app/cache.py             NPZ 产物存储与校验
tii_app/budget.py            三层内存预算
tii_app/scheduler.py         逻辑时钟、优先级与批次切分
tii_app/ledger.py            原子版本账本、非阻塞提交锁
tii_app/engine.py            缓存感知执行、失效、检查点、恢复
tii_app/invalidation.py      配置/数据/两者变化的定向失效计划
tii_app/query.py             case_id 完整产物查询与导出
tii_app/diagnostics.py       跨模块自检与确定性报告
tii_app/demo.py              正常/终止/恢复验收编排
tii_app/cli.py               命令行入口
tests/                       标准库 unittest 测试
```
