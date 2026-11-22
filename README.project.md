# 低精度量化卷积本地数据处理管线与结果验证工具

面向低精度与量化卷积算子的本地确定性数据处理管线，核心解决**权重量化参数与校准统计在增量更新时的依赖失效**问题。不依赖 Docker、外部数据集或网络；所有样本由固定种子与配置确定性生成。

## 计算图模型

每个卷积任务（job）建模为四节点有向无环链：

```
quant_input_prep ──> weight_quant ──> blocked_mac ──> reduce_dequant
 量化输入准备        权重量化        分块定点乘加       累加器归约与
                                   (int8×int8→int32)  反量化输出
```

- **量化输入准备**：激活按 `quant_scale`/`zero_point` 仿射量化为 int8；padding 以 zero_point 填充（真实 0.0 的量化表示）。
- **权重量化**：卷积核按版本化的 `weight_params`（`weight_scale`/`weight_zero_point`@`weight_revision`）量化。
- **分块定点乘加**：im2col 后沿归约维按 `block_size` 分块，int8×int8→int32 乘加，逐块做零点偏差补偿（`Σ(qa−za)(qw−zw)` 展开为 int32 精确算术），归约为 int32 累加器。任意分块大小与 int64 真值**逐位一致**（测试锁定）。
- **归约反量化**：`acc × quant_scale × weight_scale` 得到浮点输出，再按版本化校准统计（`out_scale`/`out_zero_point`@`calib_revision`）重量化为 int8 输出。

## 内容寻址缓存

缓存键 = SHA256(节点名 + 规范化 payload)，payload 纳入：

- 输入张量内容哈希（`input_sha256`/`kernel_sha256`）、完整 job 清单；
- `quant_scale`、`zero_point`、`acc_dtype`、`block_size`；
- 版本化参数行（`weight_params`/`calib_stats`）与**上游节点产物键**；
- `weight_revision`/`calib_revision` 为版本簿记而非计算输入，被显式剔除，避免键漂移（版本化输入本身已在键内）。

键相同必复用、键不同必重算；每个条目落盘 `run/cache/<键前2位>/<键>.{bin,json}`，meta 记录完整 payload，支持 `replay --key` 独立重放并校验产物哈希。

## 依赖失效传播

| 更新类型 | 受影响节点 | 复用 |
|---|---|---|
| 权重量化参数（更高 `weight_revision`） | `weight_quant` → `blocked_mac` → `reduce_dequant` | `quant_input_prep` |
| 校准统计（更高 `calib_revision`） | `reduce_dequant` | 上游三节点 |

增量提交输出失效报告：受影响节点、受影响 job 集合、逐 `(node, job_id)` 的重算/复用清单。重算与复用按**缓存实际命中**判定。

## 偏差谱（float64 反量化路径为真值）

逐 job 输出四级偏差：`quant_input`（输入量化）、`fixed_point_mac`（定点乘加）、`accumulator_reduce`（分块归约，int32 精确故恒为 0）、`dequant_output`（反量化输出）。每级指标：`max_abs`、`mean_abs`、`max_rel`、分位数 `p50/p90/p99`、误差放大比 `amplification`（上一级精确时为 `null`）。主导误差源 = 相邻级间 max_abs 跃升最大的级别。任一级超容差返回 `E_TOLERANCE_EXCEEDED`，含首个越界元素位置与偏差量。

## 业务错误码

| 码 | 含义 |
|---|---|
| `E_DUPLICATE_JOB_ID` | 清单内重复 job_id |
| `E_SHAPE_MISMATCH` | input/kernel 维度不符（含 C 通道不一致、核超出 padded 输入） |
| `E_INVALID_QUANT_SCALE` | quant_scale 非正 |
| `E_INVALID_ZERO_POINT` | zero_point 越出 [-128,127] |
| `E_UNSUPPORTED_ACC_DTYPE` | acc_dtype 不支持（仅 int32） |
| `E_REVISION_ROLLBACK` | weight/calib 版本回退（≤ 已注册版本） |
| `E_TOLERANCE_EXCEEDED` | 偏差谱越容差（含位置与偏差量） |
| `E_STALE_RESULT` | 结果依赖版本已被取代（含版本差异） |
| `E_LINEAGE_BROKEN` | 追溯链缺失（无记录/上游产物丢失/重放哈希不符） |
| `E_STAGE_BUSY` | 并发增量提交冲突，立即返回不阻塞 |
| `E_RESOURCE_LIMIT` | 单次重算 job 数超限 |
| `E_INCONSISTENT` | 跨节点一致性自检失败（指明节点与 job_id） |

所有错误为结构化 JSON：`{code, message, job_id, field, node, details}`。单任务校验失败归因到具体节点，其余任务照常执行。

## 并发与资源控制

- **单提交生效**：`state/commit.lock` 非阻塞 flock，冲突方立即获得 `E_STAGE_BUSY`。
- **资源上限**（`config.json → limits`）：`max_recompute_jobs`（超限 `E_RESOURCE_LIMIT`）、`max_block_resident_bytes`（超限自动减半分块并标记 `BLOCK_SIZE_DEGRADED`）、`max_parallel_nodes`（超限降级并标记 `PARALLELISM_DEGRADED`）。

## 使用

```bash
python3 -m quant_conv_pipeline.cli gen-samples --out data          # 确定性样本
python3 -m quant_conv_pipeline.cli run --config config.json --data data
python3 -m quant_conv_pipeline.cli commit --config config.json \
    --updates data/updates/weight_params_v2.jsonl --kind weight    # 权重参数 v2
python3 -m quant_conv_pipeline.cli commit --config config.json \
    --updates data/updates/calib_stats_v2.jsonl --kind calib       # 校准统计 v2
python3 -m quant_conv_pipeline.cli verify --config config.json --snapshot snap-0002
python3 -m quant_conv_pipeline.cli check-result --config config.json \
    --snapshot snap-0001 --job job-002                             # 过期结果拒绝
python3 -m quant_conv_pipeline.cli replay --config config.json --key <缓存键>
python3 -m quant_conv_pipeline.cli report --config config.json --snapshot snap-0002
bash scripts/acceptance.sh                                         # 一键验收
python3 -m pytest tests/ -q                                        # 31 项单元测试
```

缓存/快照/状态目录由 `config.json` 指定（`cache_dir`/`snapshot_dir`/`state_dir`），均为本地可写路径。

## 一致性自检（verify）

1. 每个节点缓存键独立重放可还原产物（哈希一致）；
2. 四节点处理的 job_id 集合完全一致；
3. 重算集 ∩ 复用集 = ∅，且并集覆盖全部受影响 (node, job) 集合；
任一不满足返回 `E_INCONSISTENT` 并指出节点与 job_id；CLI 输出含失效报告与偏差谱的 JSON。

## 实际验证结果（本地确定性样本，12 jobs）

- 全量运行 `snap-0001`：48 条节点产物全部重算，0 容差失败，0 校验错误；跨进程重跑输出哈希**逐字节一致**。
- 权重参数 v2 提交 `snap-0002`：受影响节点 `[weight_quant, blocked_mac, reduce_dequant]`，job `{002,005,007}`；重算 9 条，`quant_input_prep` 3 条全部复用。
- 校准统计 v2 提交 `snap-0003`：仅 `reduce_dequant` 受影响，job `{003,007,009}`；重算 3 条，上游 9 条全部复用。
- 三个快照一致性自检全部 `consistent: True`（48/12/12 条重放全部哈希一致）。
- `check-result snap-0001 job-002` → `E_STALE_RESULT`（`weight_revision: 1→2`）；`snap-0003 job-007` → 重放一致、非过期。
- 偏差谱：L1 ≤0.021，L2 ≤0.111，L3 = 0（分块归约精确），L4 ≤0.142，主导误差源为 `dequant_output`（输出重量化截断）。
- 异常路径实测：`E_STAGE_BUSY`（持锁并发提交）、`E_RESOURCE_LIMIT`（重算 2 > 上限 1）、`BLOCK_SIZE_DEGRADED`（驻留上限 1KB 时 block 16→1）、`E_REVISION_ROLLBACK`、`E_LINEAGE_BROKEN`（删除上游产物）、`E_INCONSISTENT`（篡改报告）、六类清单校验错误各归因其节点。

## 目录结构

```
quant_conv_pipeline/     # 管线包
  errors.py              # 结构化业务码
  config.py              # 配置/容差/资源上限
  sample_gen.py          # 确定性样本与更新生成
  quant.py               # int8 量化/反量化原语
  conv_core.py           # im2col、分块定点 MAC、float64 参考
  cache.py               # 内容寻址缓存
  nodes.py               # 四节点 DAG 与重放
  validation.py          # 输入与配置校验链
  deviation.py           # 偏差谱与容差判定
  pipeline.py            # 全量运行/增量提交/并发与资源控制
  verify.py              # 结果验证与一致性自检
  cli.py                 # 命令行入口
tests/                   # 31 项 pytest 测试
scripts/acceptance.sh    # 一键验收脚本
config.json              # 本地路径/上限/容差配置
```
