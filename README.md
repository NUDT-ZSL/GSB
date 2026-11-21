# DeployLab：LLM 低精度部署、原子进度与崩溃恢复

DeployLab 是一个完全本地、确定性的模型包量化与推理部署实验平台。它不依赖 GPU、真实模型、账号或网络，只使用 Python 3.10+、NumPy 与标准库。

系统围绕四个阶段建模：

1. **模型包校验**：读取 JSONL 清单、权重与校准数组，检查清单结构、形状链和文件完整性。
2. **逐层量化**：支持 `int8` / `int4`、`per_tensor` / `per_channel`、可选 `:group=N` 分组缩放与零点。
3. **校准统计**：对每层生成离线权重误差、输入统计、输出统计与校准输出校验和。
4. **逐层推理**：按 `layer_index` 串联确定性矩阵乘与 `tanh` 激活，产出逐层激活校验和和最终预测向量。

每个阶段只有在全部层完成后才在 SQLite 中原子提交。进度版本全局单调递增，快照包含已完成层集合、内容校验和、前序快照链接和产物摘要。进程被 `SIGKILL` 后，只读取最后一个已提交快照恢复；未提交层结果不会被持久化或复用。

## 快速开始

```bash
python3 -m pip install numpy

# 生成确定性样本与损坏清单
python3 scripts/generate_samples.py --out data/sample --force

# 完整运行
python3 -m deploylab.cli run \
  --bundle data/sample \
  --db artifacts/deploylab.sqlite \
  --force-new

# 可重复演示：完整运行、量化阶段 SIGKILL、恢复对照、
# 权重版本失效复用、校准更新、预算降级
python3 -m deploylab.cli demo --work-dir artifacts/demo

# 21 项测试 + 完整演示，并写出 artifacts/verification_report.json
python3 scripts/run_verification.py

# 本地交互式 Web 应用
python3 -m deploylab.cli serve --host 127.0.0.1 --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

Web 控制台展示当前阶段、代际、进度版本、逐层量化口径、恢复报告、影响集/复用集、预算账本、最近业务错误与快照轨迹，并可一键触发完整运行、崩溃恢复演示、版本升级、校准更新、异常注入和并发提交探测。

## 样本包

`python3 scripts/generate_samples.py` 会生成：

```text
data/sample/
├── manifest.jsonl                 # 5 层 JSONL 清单
├── bundle_meta.json               # 校准样本数、校准 revision
├── weights/L0*.npy                # 固定种子 NumPy 权重
├── calibration/L0*.npy            # 固定 calib_seed 校准输入
├── initial_input.npy              # 确定性推理初始输入
└── corrupt/
    ├── manifest_truncated.jsonl
    ├── manifest_unknown_dtype.jsonl
    ├── manifest_shape_mismatch.jsonl
    └── README.txt
```

清单字段严格包含：

```json
{
  "model_tag": "llm-local-demo-v1",
  "layer_id": "L02",
  "layer_index": 2,
  "layer_shape": [48, 64],
  "weight_revision": 1,
  "quant_dtype": "int4",
  "scale_hint": "per_channel:group=8",
  "zero_point": 0,
  "calib_seed": 1103
}
```

权重与偏置的 RNG 种子由 `model_tag + layer_id + layer_index + layer_shape + weight_revision` 的 SHA-256 派生；校准输入由 `model_tag + layer_id + calib_seed` 派生。所有数组以 little-endian float64、C 顺序和规范化字节摘要处理。

## 量化口径

`scale_hint` 支持：

| 口径 | 示例 | 说明 |
|---|---|---|
| per-tensor | `per_tensor` | 整个权重张量一个缩放因子 |
| per-tensor 分组 | `per_tensor:group=64` | 展平后每 64 个元素一组 |
| per-channel | `per_channel` | 每个输出通道一个缩放因子 |
| per-channel 分组 | `per_channel:group=16` | 每行通道内再按 16 个元素分组 |

`int8` 范围 `[-128, 127]`，`int4` 范围 `[-8, 7]`。量化采用可复现的仿射映射、`np.rint` 和显式裁剪；反量化使用同一份缩放元数据。非法 `quant_dtype`、`scale_hint` 或 `zero_point` 返回 `E_QUANT_SCHEME`，并携带 `layer_id` 与 `field`。

## 快照与恢复语义

阶段序列固定为：

```text
model_validation → quantization → calibration → inference
```

- 快照只在阶段全部层完成后提交；部分阶段的产物不进入 `snapshots`/`artifact links`。
- 一次提交事务同时写入快照、产物引用、产物摘要与轨迹；读取方只能看到提交前或提交后的完整状态。
- `version` 全局单调递增；`content_checksum` 覆盖代际、阶段、版本、前序链接、已完成层、产物摘要和元数据。
- 崩溃恢复从最新已提交阶段继续；当前未提交阶段从层起点重建。崩溃诊断 marker 仅供报告“丢弃了哪些内存中间层”，不参与恢复决策。
- 权重/校准更新生成新代际，版本继续递增。已提交代际不可被旧版本覆盖。

### 依赖失效与缓存复用

若层 `k` 的 `weight_revision` 升高或校准统计更新：

```text
affected = [k, k+1, ..., N-1]
reused   = [0, 1, ..., k-1]
```

四个阶段均记录 `reused` / `recomputed` 层集合；两组无交集，并集覆盖全部层。缓存是内容寻址的，键包含层规格、权重文件摘要、量化产物摘要、校准输入摘要、上游激活摘要和推理策略。受影响层使用代际 nonce 强制绕过旧缓存，未受影响上游层直接复用已提交产物。

## 预算与降级

配置位于 `config/default.json`：

- `max_layers_per_stage`：单阶段层数硬上限，超限返回 `E_RESOURCE_LIMIT`。
- `max_resident_bytes`：驻留反量化权重与激活的预算。
- `max_quant_macs` / `max_inference_macs`：量化与推理乘加上限。
- `allow_degrade`、`degraded_group_size`、`inference_tile_min_rows`：降级策略。

可降级类别：

- `DEGRADED_GROUP_SIZE`：缩小分组缩放尺寸，降低单组临时工作集。
- `DEGRADED_STREAMING`：逐层流式推理，不常驻全部反量化权重。
- `DEGRADED_TILED`：按输出行分块推理。

无法满足的最低资源仍返回 `E_RESOURCE_LIMIT`。

## 异常注入

注入由 CLI/HTTP 参数触发，不需要修改源码：

```bash
python3 -m deploylab.cli inject \
  --stage quantization \
  --kind unknown_quant_dtype \
  --layer L02 \
  --db artifacts/inject.sqlite
```

| 阶段 | kind | 预期业务码 |
|---|---|---|
| `model_validation` | `truncate_weight` | `E_PACKAGE_WEIGHT_TRUNCATED` |
| `model_validation` | `truncate_manifest` | `E_PACKAGE_MANIFEST_TRUNCATED` |
| `quantization` | `unknown_quant_dtype` | `E_QUANT_SCHEME` |
| `quantization` | `invalid_scale_hint` | `E_QUANT_SCHEME` |
| `quantization` | `invalid_zero_point` | `E_QUANT_SCHEME` |
| `calibration` | `calib_sample_mismatch` | `E_CALIB_SAMPLE_MISMATCH` |
| `calibration` | `calib_truncated` | `E_CALIBRATION_TRUNCATED` |
| `inference` | `input_dim_mismatch` | `E_INFER_DIM_MISMATCH` |

每次异常写入 `errors` 表，包含阶段、业务码、层、字段、injected 与 attributed 标志。未被业务阶段归因的异常记录为 `E_UNATTRIBUTED`，并递增持久化 `unattributed_count`。

## 一致性自检

`python3 -m deploylab.cli verify --db artifacts/deploylab.sqlite` 检查：

- 四个阶段已完成层集合完全一致；
- 快照链、前序校验和与内容校验和；
- 每层原始权重、缩放参数与反量化权重可重放核对；
- 推理产物引用当前代际量化快照，无旧 revision 混入；
- 独立重算逐层激活与最终预测，逐字节比较；
- 影响集、复用集、重算集无冲突且覆盖完整；
- 校准样本数与 revision 元数据。

失败返回 `E_INCONSISTENT`，`details.categories` 指出失败类别。

## 主要文件

| 路径 | 作用 |
|---|---|
| `deploylab/quant.py` | 量化方案解析、分组缩放、反量化与重放核对 |
| `deploylab/pipeline.py` | 四阶段流水线、确定性推理、失效/恢复编排 |
| `deploylab/store.py` | SQLite 快照链、事务提交、并发锁与错误审计 |
| `deploylab/budgets.py` | 双预算与降级策略 |
| `deploylab/faults.py` | 参数化异常注入计划 |
| `deploylab/consistency.py` | 跨模块一致性自检 |
| `deploylab/demo.py` | SIGKILL、恢复、版本与预算的端到端演示 |
| `deploylab/webapp.py` + `deploylab/static/index.html` | 本地交互式应用 |
| `tests/` | 21 项 unittest |
| `docs/design.md` | 设计不变量与事务模型 |
| `docs/verification.md` | 实际验证记录 |

## 已验证结果

在 Python 3.10.12、NumPy 1.26.4 上：

- `python3 -m unittest discover -v`：21 项全部通过。
- 量化阶段 `SIGKILL`：子进程退出码 `-9`；崩溃后仅 `model_validation@v1` 可见，`quantization` 未提交。
- 恢复从 `model_validation@v1` 继续，重算量化/校准/推理；最终预测 JSON 字节与逐层激活校验和同不间断完整运行一致。
- 将 `L02` 从 revision 1 升至 2：影响集 `[L02, L03, L04]`，复用集 `[L00, L01]`；升级结果与同版本干净重跑逐字节一致。
- 校准更新 `L01`：影响集 `[L01, L02, L03, L04]`，复用集 `[L00]`。
- 资源降级演示同时产生 `DEGRADED_GROUP_SIZE` 与 `DEGRADED_STREAMING`；硬层数上限返回 `E_RESOURCE_LIMIT`。
- 并发提交探测返回 `E_STAGE_BUSY`；旧 revision 尝试返回 `E_SNAPSHOT_ROLLBACK`。

完整机器可读结果见 `artifacts/verification_report.json`。

## 设计说明与边界

- 进度按阶段整体提交。这是为了满足“阶段全部层完成才提交”和“未提交中间结果必须丢弃”的强约束；因此崩溃发生在量化中途时，该阶段从第一层重建，而不是信任未提交的逐层临时结果。
- 跨代际复用来自**此前已提交阶段**的内容寻址产物，而不是崩溃进程的内存状态。
- SQLite 用于单机原子持久化；写锁冲突通过 `BEGIN IMMEDIATE` 与零 busy timeout 立即转化为 `E_STAGE_BUSY`。
- 字节可复现性依赖相同的 Python/NumPy/CPU 浮点环境。系统统一使用 float64、确定性 RNG、显式行级求和与规范化字节编码。
