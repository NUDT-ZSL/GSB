# LLM 采样互操作与迁移诊断组件

这是一个可在 Linux / WSL Ubuntu 上独立运行的 Python 组件与完整示例。它从固定种子生成本地任务样本，将 v1 JSON/JSONL、v2 分片 JSON + SQLite、v3 嵌套 JSON 归一化为统一任务表示，再执行确定性的 TopK/TopP、候选掩码联合过滤、温度缩放、种子采样与误差谱诊断。

组件不依赖真实模型、外部服务或网络。唯一的第三方运行时依赖是 NumPy，SQLite 使用 Python 标准库。

## 快速开始

```bash
cd /home/zsl/feedback/uid2d1b8ac8-default-414
python3 -m llm_sampling_diag.cli demo --suite samples --out artifacts/demo
python3 -m unittest discover -s tests -v
python3 examples/complete_example.py
```

也可以直接运行：

```bash
./run_demo.sh
```

主要输出：

- `samples/manifest.json`：固定种子、任务集合与文件/逻辑校验和。
- `samples/v1/tasks.json`、`samples/v1/tasks.jsonl`：v1 JSON 与 JSONL。
- `samples/v2/meta.db`、`samples/v2/shards/*.json`：v2 SQLite 元数据与分片。
- `samples/v3/tasks.json`、`samples/v3/incremental_rev*.json`：v3 嵌套格式与增量修订。
- `samples/invalid/`：未知版本、重复 `case_id`、缺失种子、形状不符、v2 时间倒退、SQLite/JSON 不一致样本。
- `artifacts/demo/demo_report.json`：迁移、自检、误差谱、快照与错误码的汇总。
- `artifacts/demo/selfcheck_float64.json`、`artifacts/demo/selfcheck_float32.json`：逐任务重算报告。

## 目录

```text
llm_sampling_diag/
  model.py          统一不可变任务模型、配置与迁移策略
  formats/          v1/v2/v3 读取器与版本/字段集分派
  migrate.py        三格式双向迁移、默认补全与显式有损记录
  operators.py      TopK/TopP、掩码、温度、采样
  diagnostics.py    float64 参考误差谱与容差诊断
  pipeline.py       单任务缓存键、结果校验和
  selfcheck.py      跨格式归一化与逐任务重算
  snapshots.py      任务/参数/结果快照与并发阶段锁
  samplegen.py      固定种子样本生成器
  cli.py            generate / selfcheck / demo 命令
tests/              42 个 unittest 验收用例
examples/           完整示例入口
samples/            已生成的确定性样本
artifacts/demo/     实际运行报告
```

## 统一表示

每个任务归一化后至少包含：

- `case_id`、绝对时间 `effective_at_ms`、一维 `logits` 与 `logits_shape`
- `k_mode`（`top_k` 或 `top_p`）、`k_value`、`temperature`
- 显式 `tie_policy` 与 `seed`
- 掩码排除位置集合 `candidate_mask.excluded_indices`，v3 保留原始嵌套 selectors
- `error_budget`、单调修订号 `revision`、`revision_id`
- 分块方案 `chunk_plan`、`num_samples`、可选 `top_p`
- 来源格式、来源位置与迁移补全/丢失记录

读取器会保留 v3 的嵌套 selectors，同时计算其排除索引并集供算子使用。v1/v2 的扁平掩码在升级时写入同一 `excluded_indices`，因此三种格式可以逐字段比较。

## 三种格式

### v1

单文件扁平表，`effective_after_s` 相对 `base_time_unix_ms` 生效。基础字段包括 `case_id`、`logits`、`k_mode`、`k_value`、`temperature`、`tie_policy`、`seed`。兼容的扁平扩展字段包括 `mask_excluded_indices`、`error_budget`、`num_samples`、`top_p`、`chunk_size`。

JSONL 第一行是带 `base_time_unix_ms` 的版本头，之后每行一个任务。

### v2

每个分片是 `{"format":"task-v2-shard","version":2,"shard_id":...,"tasks":[...]}`。`meta.db` 保存：

- `shards.ordinal`：全局时间校验顺序。
- `tasks.effective_at_ms`：绝对毫秒时间。
- `tasks.chunk_size` / `tasks.chunk_count`：分块方案。
- `tasks.extended_json`：显式 tie policy、seed、采样数、top_p。
- `tasks.mask_json`：扁平候选掩码。

读取时逐字段比对 SQLite 与分片 JSON，并检查分片路径、行号、`case_id`、绝对时间、分块方案、误差预算、掩码和扩展采样参数。时间戳必须随分片顺序单调不减。

### v3

结构化嵌套格式，包含 `case.effective.at_unix_ms`、`logits.shape/values`、`sampling`、`candidate_mask`、`error_budget`、`revision` 与 `chunk_plan`。`candidate_mask.selectors` 支持递归 `groups`，每层都是显式 `exclude`。

## 迁移规则

| 源/目标 | 规则 |
|---|---|
| v1 -> unified | 从相对秒换算绝对毫秒；补默认空扁平掩码、误差预算、分块方案、修订号；逐项记录默认来源 |
| v2 -> unified | 验证 SQLite 与分片后直接保留扁平掩码、分块和扩展采样参数；补修订号默认值 |
| v3 -> unified | 递归展平嵌套掩码为排除索引并集，保留原 selectors；保留误差预算和修订 |
| unified -> v3 | 保留嵌套 selectors；扁平输入合成为显式 selector；全字段保留 |
| unified -> v2 | 嵌套 selectors 按显式 `flatten` 规则写成扁平 `mask_json`；v2 不表达修订，非默认修订标记为有损 |
| unified -> v1 | 默认 `mask_policy=flatten`，把嵌套排除写为 `mask_excluded_indices`；可选 `drop` 显式丢弃掩码；v1 不表达修订，非默认修订标记为有损 |
| strict | 若存在任何 loss，迁移立即返回 `E_SEMANTIC_INCOMPLETE`，并指出字段与策略 |

迁移记录包含 `field`、`action`、`source`、`value`、`lossy`。因此降级不会静默丢失嵌套结构或修订信息。

## 算子语义

固定执行顺序：

1. 校验任务、配置、logits 形状、掩码范围、tie policy 和 seed。
2. 按 `temperature` 缩放分数。
3. `mask_first`：先移除全部掩码位置，再做 TopK/TopP。`k_first`：先 TopK，再从选中集合移除掩码。
4. TopK 候选按温度缩放分数降序排列；同分按原始索引升序。
5. 对保留候选重新 softmax；TopP 按排序后的累积概率截断，至少保留一个候选。
6. 候选掩码位置不会进入候选集合，也不会被采样。
7. TopP 后重新归一化；用显式 `PCG64(seed)` 和 `Generator.random()` 逐样本抽取。
8. 输出原始索引采样序列和候选排名序列。

任何未声明或非法的 tie policy 返回 `E_TIE_POLICY_UNSPECIFIED`。存在掩码且未声明或非法 `mask_priority` 返回 `E_MASK_PRIORITY_INVALID`。seed 缺失返回 `E_SEED_UNSPECIFIED`。随机数算法必须在配置中显式声明。

移除记录给出每个未保留候选的 `index`、`reason` 与 `stage`，原因包括 `mask_excluded`、`top_k_cutoff`、`top_p_cutoff`。

## 误差谱

每次诊断都执行两条路径：

- 参考路径：`float64`、无额外舍入、无次正规保护。
- 实际路径：配置的 `float32`/`float64`、舍入模式与次正规保护。

误差谱输出：

- 每个候选在参考/实际路径中的分数与绝对偏差。
- 每个候选概率与绝对偏差。
- 候选顺序是否一致、采样序列是否一致、首个采样偏差位置。
- 最大分数偏差、最大概率偏差与最大绝对偏差。
- `E_TOLERANCE_EXCEEDED` 时的 `case_id`、候选索引与阶段。
- 舍入模式、量子与变更计数。
- 次正规数事件：阶段、位置、原值、替换值、保护策略与候选索引。

有效容差取配置 `tolerance` 与任务 `error_budget` 的较小值。示例使用 `float32` 时误差均低于有效容差，极端样本触发 `clamp_tiny` 保护事件。

## 快照

`SnapshotManager` 按 `task`、`parameter`、`result` 三个独立阶段维护：

- 单调递增 `version` 与规范化 JSON SHA-256。
- 新版本/新校验和时深拷贝并整体替换。
- 低版本提交返回 `E_SNAPSHOT_ROLLBACK`。
- 同版本不同内容返回 `E_SNAPSHOT_CONFLICT`。
- 同阶段并发提交使用非阻塞锁，后到者立即得到 `E_STAGE_BUSY`。
- 返回对象是深拷贝，外部修改不会污染当前快照。

## 错误码

| 错误码 | 触发条件 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 显式未知版本或无法按版本头/字段集分派 |
| `E_FORMAT_CORRUPT` | 字段缺失/不可解析、时间倒退、SQLite 与分片不一致 |
| `E_TIE_POLICY_UNSPECIFIED` | tie policy 缺失或不支持 |
| `E_MASK_PRIORITY_INVALID` | 掩码与 k_value 同时存在但优先级缺失/非法 |
| `E_SEED_UNSPECIFIED` | 采样种子未声明 |
| `E_TOLERANCE_EXCEEDED` | 候选顺序、分数/概率偏差或采样序列超过容差 |
| `E_SNAPSHOT_ROLLBACK` | 尝试写入较低快照版本 |
| `E_STAGE_BUSY` | 并发提交同一快照阶段 |
| `E_DUP_CASE` | 同一输入内重复 `case_id` |
| `E_SHAPE_MISMATCH` | logits 声明形状、实际长度、掩码位置或分块不匹配 |
| `E_SEMANTIC_INCOMPLETE` | strict 迁移会丢弃语义 |
| `E_INCONSISTENT` | 三格式归一化或重算结果不一致，并输出首个偏差路径 |

格式错误统一携带 `field`、`line`、`case_id` 与细节；v2 还携带 SQLite `db_rowid`。

## 实际验证结果

验证日期：2026-11-26。环境：Python 3.10.12、NumPy 1.26.4、SQLite 3.37.2。

```text
python3 -m unittest discover -s tests -v
Ran 42 tests in 0.527s
OK
```

固定种子 `20261126` 的样本逻辑校验和：

```text
934020a02f775a177f1091552bc01eec111c898b9d58a6a13eff4c4e574fcef7
```

`float64` 与 `float32` 自检均为 `4` 个 case、`12` 次逐任务重算、`all_consistent=true`。三格式采样序列完全一致：

| case_id | 采样序列（v1 = v2 = v3） |
|---|---|
| `case_tie_topk_mask` | `[8, 6, 8, 9]` |
| `case_topp_mask` | `[2, 1, 7, 1]` |
| `case_topk_topp_mask` | `[2, 6, 6, 4]` |
| `case_precision_subnormal` | `[0, 0, 0, 0]` |

`float32` 实际路径的最大误差谱偏差均低于 `1e-5` 有效容差，极端样本最大绝对偏差约 `4.36e-6`，并记录两次次正规数保护事件。`float64` 路径最大绝对偏差为 `0.0`。

示例同时验证：

- `E_SEMANTIC_INCOMPLETE`：修订号 2 严格降级到 v1。
- 有损降级记录：`revision` 为 `lossy=true`，快照/缓存键可据此拒绝过期结果。
- `E_SNAPSHOT_ROLLBACK` 与快照历史 `[1, 2]`。
- `E_STAGE_BUSY`：同阶段并发写入立即拒绝。
- 未知版本、重复 case、缺种子、形状不符、v2 时间倒退、SQLite/分片不一致均命中预期错误码。

完整机器可读结果见 `artifacts/demo/demo_report.json`，其报告校验和随样本与配置确定性生成。
本次报告校验和为 `5186ca077b075d4f76fb2c9037858844e9a4e57fec2f1198f0785b81443f171b`。

## 作为库集成

```python
from llm_sampling_diag.formats import read_suite
from llm_sampling_diag.model import OperatorConfig
from llm_sampling_diag.pipeline import run_task
from llm_sampling_diag.migrate import migrate_task, MigrationPolicy

suite = read_suite("samples")
task = suite.per_format["v3"].tasks[0]
config = OperatorConfig(
    mask_priority="mask_first",
    dtype="float32",
    rounding_mode="none",
    subnormal_guard="clamp_tiny",
    tolerance=1e-5,
)
result = run_task(task, config)
down = migrate_task(task, "v1", policy=MigrationPolicy(mask_policy="flatten"))
```

所有结果均可通过 `canonical_json` 和 SHA-256 做逐字节比较，适合接入缓存键、快照和回归测试。
