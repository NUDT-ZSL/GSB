# 低精度推理诊断工作台

面向 LLM 部署前处理链路的本地确定性诊断系统。项目从固定种子生成本地权重，不依赖真实模型、外部账号、数据库或网络资源。

核心能力：

- 四阶段流水线：模型包校验、逐层量化、校准统计、逐层推理。
- `int8` / `uint8` / `int4` / `uint4` / `int16` 分组量化与反量化。
- 逐层激活、最终预测向量、量化参数与反量化权重交叉核对。
- 量化舍入、分组缩放、激活累加、逐层串联四级误差谱与主导来源归因。
- 阶段级原子快照、强制终止恢复、未提交中间结果丢弃。
- 权重修订与校准更新后的下游后缀失效，未受影响前缀复用。
- 单阶段并发提交控制、层数/驻留权重/校准比较次数预算。
- 四个输入或参数注入点，以及容差越界注入。
- 本地 Web 可视化诊断台。

## 环境

- Windows 10/11 或 Linux
- Python 3.10+
- NumPy 是唯一第三方依赖
- 持久化仅使用 Python 标准库文件读写与 SHA-256

当前验证环境：Python 3.10.12，NumPy 1.26.4，本地 CPU。

## 快速启动

Windows PowerShell：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 工作区已附带确定性样本；如需重新生成：
python -m quant_diag generate --workspace . --force

python -m quant_diag run --runtime-dir runtime\state
python -m quant_diag status --runtime-dir runtime\state
python -m quant_diag verify --runtime-dir runtime\state
python -m quant_diag web --runtime-dir runtime\workbench --open
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m quant_diag run --runtime-dir runtime/state
python -m quant_diag web --runtime-dir runtime/workbench --open
```

浏览器打开 `http://127.0.0.1:8765`。工作台提供：

- 完整运行
- 异常注入
- 强杀并恢复，自动与完整运行比较
- 权重修订后缀重算
- 误差图谱、预测向量、阶段提交、恢复报告、复用集合、自检与事件时间线

## 一键验收演示

```powershell
python demo.py
```

等价的 CLI 入口：

```powershell
python -m quant_diag demo --workspace . `
  --runtime-root runtime\acceptance `
  --output artifacts\verification_report.json
```

演示会依次执行：

1. 完整量化推理。
2. 在独立目录重复完整运行并比较结果摘要。
3. 在逐层量化阶段处理到 `layer_1` 后以退出码 `137` 强制终止。
4. 从最近提交的模型包校验快照恢复，丢弃未提交的中间层结果。
5. 将 `layer_1` 的 `weight_revision` 提升到 `2`，仅重算 `layer_1..layer_3`。
6. 注入四类输入/参数异常和一次零容差异常。
7. 篡改快照校验和，验证 `E_SNAPSHOT_CORRUPT`。
8. 尝试用旧版本号提交，验证 `E_SNAPSHOT_ROLLBACK`。
9. 写出 `artifacts/verification_report.json`。

## 样本与模型

根目录样本：

- `model_manifest.jsonl`
- `calibration_samples.jsonl`

清单字段：

```json
{
  "model_tag": "tiny-llm-lab",
  "layer_id": "layer_1",
  "layer_index": 1,
  "layer_shape": [40, 48],
  "weight_revision": 1,
  "quant_dtype": "int4",
  "scale_hint": 1.02,
  "zero_point": 0,
  "calib_seed": 2207
}
```

共 4 层，形状按层序保持串联：

```text
32 -> [48x32] -> 48 -> [40x48] -> 40 -> [24x40] -> 24 -> [16x24] -> 16
```

权重生成：

- 种子由 `model_tag + layer_id + weight_revision + calib_seed + "weights-v1"` 的 SHA-256 派生。
- 使用 `numpy.random.default_rng` 和 `standard_normal`。
- 行尺度与确定性离群值仅由该种子决定。
- 不读取真实模型、不访问网络。

校准样本每层 6 条，首层第一条校准向量作为推理输入。校准样本向量宽度必须等于层输入维度。

## 量化与推理

每个展平权重按 `group_size` 分组：

```text
base_scale = max(abs(group)) / max(qmax - zero_point, zero_point - qmin)
scale      = max(base_scale, 1e-12) * scale_hint
code       = clip(round(weight / scale) + zero_point, qmin, qmax)
dequant    = (code - zero_point) * stored_scale
```

`stored_scale` 默认量化为 `float16` 后转回 `float64`，用于暴露“分组缩放”误差。精确 `scale` 同时保存在快照中，供往返校验与误差分解使用。

推理：

```text
h_0 = calibration_vector
z_l = W_dequant_l @ h_(l-1)
h_l = tanh(z_l)
prediction = h_last
```

默认激活累加使用 `float32`，参考链路使用 `float64`。

## 四阶段快照

阶段顺序固定：

1. `model_package_validation`
2. `layer_quantization`
3. `calibration_statistics`
4. `layer_inference`

只有某阶段全部层完成，才能提交该阶段。提交协议：

1. 获取非阻塞进程锁；冲突立即返回 `E_STAGE_BUSY`。
2. 读取当前版本并校验内容校验和。
3. 仅允许写入 `current_version + 1`。
4. 先原子写入 `runtime/snapshots/vNNNNNNNNNNNN.json`。
5. 再原子替换 `runtime/current.json` 指针。
6. 读者只会看到旧的完整视图或新的完整视图。

阶段数据包含：

- 单调递增 `version`
- `completed_layers`
- `affected_layers`
- `reused_layers`
- `rebuilt_layers`
- 内容 `checksum`
- 阶段完整产物

未提交层只写入 `runtime/inflight.json`，恢复时读取后丢弃。

## 恢复与失效

强制终止恢复：

- 读取最近提交快照。
- 丢弃 `inflight.json` 中的未提交层。
- 从已提交点之后的第一阶段、第一层继续。
- 阶段级提交保证已提交层不会重算；不完整阶段整体重建。
- 恢复报告列出最近提交点、丢弃记录和重建起点。

权重或校准变化：

- 权重修订必须单调递增；同一修订下的权重字段变化返回 `E_REVISION_ROLLBACK`。
- 受影响集合是最早变化层及其全部下游层。
- 未受影响前缀复用已提交量化参数、校准统计和激活。
- 仅重建受影响后缀。
- 新快照完整写入新版本文件，旧快照不被增量修补。

## 误差谱

四条确定性链路：

1. 原始权重 + `float64`：参考真值。
2. 量化整数 + 精确 scale + `float64`：仅含量化舍入。
3. 量化整数 + `float16` 存储 scale + `float64`：加入分组缩放误差，同时作为快照内 float64 参考。
4. 实际反量化权重 + 配置累加精度：加入激活累加误差。

因此输出同时包含：

- 原始 float64 权重参考（量化误差的外部真值）。
- 已提交反量化权重的 float64 推理参考（快照内参考）。
- 实际低精度累加预测。

每层、每个来源输出：

- 绝对偏差均值、最大值
- 相对偏差
- `P05/P50/P95` 区间与完整范围
- 相对上一层的误差放大比
- 主导来源

整体串联误差为实际低精度链路与原始 `float64` 链路的逐层差。若某层 `abs_mean` 或相对偏差超过配置容差，返回 `E_TOLERANCE_EXCEEDED`，并携带全部越界层及偏差。

## 资源预算

命令行或配置文件可设置：

```json
{
  "group_size": 16,
  "degrade_group_size": 8,
  "scale_storage_dtype": "float16",
  "accumulation_dtype": "float32",
  "tolerance_abs": 0.30,
  "tolerance_rel": 0.50,
  "min_calibration_samples": 2,
  "max_layers_per_stage": 64,
  "max_resident_weight_mb": 8.0,
  "max_calibration_comparisons": 2000000,
  "injections": [],
  "input_override": null
}
```

行为：

- 单阶段层数超限：`E_RESOURCE_LIMIT`。
- 单层驻留峰值超硬预算：`E_RESOURCE_LIMIT`。
- 全模型驻留峰值超预算但单层可容纳：降级为 `group_size=8` 的分组量化，写入 `grouped_quantization` 降级记录。
- 校准比较次数超限：`E_RESOURCE_LIMIT`。

## 异常注入

异常只能通过输入或参数注入，不修改源码：

```bash
python -m quant_diag inject --runtime-dir runtime/inject \
  --inject weight_truncate@layer_1

python -m quant_diag inject --runtime-dir runtime/inject \
  --inject unknown_dtype@layer_1

python -m quant_diag inject --runtime-dir runtime/inject \
  --inject calibration_count_mismatch@layer_1

python -m quant_diag inject --runtime-dir runtime/inject \
  --inject inference_input_dim@layer_0
```

零容差注入：

```bash
python -m quant_diag run --runtime-dir runtime/tolerance \
  --tolerance-abs 0 --tolerance-rel 0
```

## 自检

`python -m quant_diag verify` 与 Web 工作台执行：

- 四阶段已完成层集合完全一致。
- 每层量化参数可重建反量化权重。
- 量化码在合法范围，分组边界连续且覆盖完整。
- 重算集合与复用集合无交集，并集覆盖全部层。
- 受影响集合等于重算后缀。
- 最终预测等于最后一层激活。
- 仅使用已提交反量化权重与初始输入可独立复算最终预测。

任一失败返回 `E_INCONSISTENT` 并携带类别。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

测试覆盖：

- 量化/反量化与完整运行字节确定性
- 缺层 `E_LAYER_INCOMPLETE`
- 强杀后阶段重建与完整运行一致
- 权重修订与校准更新的下游失效
- 并发提交 `E_STAGE_BUSY`
- 层数、内存、校准比较预算
- 分组量化降级
- 四类异常注入与容差越界
- 快照损坏与版本回退保护
- 跨模块自检篡改检测
- Web 服务完整运行与注入

实际结果见 `artifacts/VERIFICATION.md` 和 `artifacts/verification_report.json`。详细设计见 `TECHNICAL.md`。
