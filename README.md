# convedit：确定性卷积/插值算子图编辑器

`convedit` 面向本地固定种子样本，支持二维卷积、双线性插值、最近邻插值的算子图执行、交互式编辑、节点级缓存失效、并发执行和断点恢复。实现只依赖 Python 标准库与 NumPy，不使用容器、网络服务或外部存储。

默认张量布局为 `NCHW`，计算类型固定为 `float64`。所有持久化 JSON、节点产物和报告均通过同目录临时文件写入、`fsync`、`os.replace` 和目录 `fsync` 原子提交。

## 快速开始

```bash
# 环境（首次）
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 校验样本
.venv/bin/python -m convedit validate \
  --graph samples/graph.json --config samples/config.json

# 初始化并完整执行
.venv/bin/python -m convedit init \
  --graph samples/graph.json --input samples/input.json \
  --config samples/config.json --state artifacts/demo/manual/state \
  --report artifacts/demo/manual/init.json
.venv/bin/python -m convedit run \
  --state artifacts/demo/manual/state \
  --report artifacts/demo/manual/run.json

# 应用编辑序列（每次编辑提升图版本并增量执行，逐版写报告）
.venv/bin/python -m convedit edit \
  --state artifacts/demo/manual/state \
  --edit-file samples/edits.json \
  --report-dir artifacts/demo/manual/reports

# 显式恢复（run 也会从已有快照继续）
.venv/bin/python -m convedit resume \
  --state artifacts/demo/manual/state \
  --report artifacts/demo/manual/resume.json

# 交互式编辑
.venv/bin/python -m convedit shell --state artifacts/demo/manual/state

# 等价的固定入口（自动使用项目 .venv）
bin/convedit validate --graph samples/graph.json --config samples/config.json
```

本机若无法创建带 `pip` 的虚环境，可先执行 `python3.10 -m venv --without-pip .venv`，再用 Python 官方 `get-pip.py` 安装 `requirements.txt`。应用运行期不访问网络。

## 一键验证

```bash
# 18 个测试 + 完整验收演示
./scripts/run_demo.sh

# 外部 kill -9：在 conv2 延迟执行期间强杀，随后从最近完整快照恢复
./scripts/force_kill_repro.sh
```

本次实际执行结果：

- `python -m unittest discover -s tests -v`：18/18 通过。
- `python -m convedit demo --output artifacts/demo`：整体 PASS。
- 5 次编辑中的每一次，增量产物都与“按最终图从空白状态全量重算”的产物逐节点逐元素一致。
- 4 线程并发与单线程的节点产物、失败顺序、重算/复用清单一致。
- SIGTERM 优雅中断退出码为 `0`，已完成 4 个节点；恢复时复用这 4 个节点，只重算 `conv2`。
- 真实 SIGKILL 返回码 `-9`；恢复后复用 4 个节点，只重算 `conv2`，最终结果与基线一致。
- 四类故障注入均命中并隔离：形状不一致、越界填充、非法缩放因子、非有限输入。
- 配置哈希或图指纹不一致返回 `E_CONFIG_MISMATCH`。

机器可读结果见 `artifacts/demo/verification.json`，摘要见 `artifacts/demo/SUMMARY.md`。逐步报告位于 `artifacts/demo/incremental/reports/`。

## 目录

```text
bin/convedit              固定本地命令行入口
convedit/                 实现
  graph.py                解析、结构/参数校验、拓扑排序、图指纹
  ops.py                  确定性 conv2d / bilinear / nearest
  engine.py               缓存键、失效传播、波次并发、快照游标、恢复
  state.py                原子状态、阶段快照、节点产物、恢复校验
  edits.py                update/set_enabled/add/delete/replace 等编辑
  injections.py           独立故障与中断注入
  inputs.py               固定种子输入生成
  cli.py / demo.py        命令行、交互 shell、端到端验收
samples/                  图、输入、配置、编辑序列、注入配置
scripts/                  演示与强杀复现
tests/test_convedit.py    单元与集成测试
artifacts/demo/           本次真实验收证据
artifacts/force_kill/     外部 SIGKILL 复现证据
```

## 样本与 JSON 格式

### 算子图

`samples/graph.json` 是双分支图：

```text
input0 -> conv1 -> interp_bi -> conv2
       -> conv3 -> interp_nn
```

结构示例：

```json
{
  "schema_version": 1,
  "graph_id": "local_feature_pipeline",
  "input_tensors": {
    "input0": {"shape": [1, 2, 9, 10], "dtype": "float64"}
  },
  "nodes": [
    {
      "id": "conv1", "op": "conv2d", "tensor": "conv1", "enabled": true,
      "params": {
        "kernel_size": [3, 3], "stride": [1, 1], "padding": [1, 1],
        "in_channels": 2, "out_channels": 2,
        "weights": [[[[0.1]]]],
        "bias": [0.0, 0.0]
      }
    },
    {
      "id": "interp_bi", "op": "bilinear", "tensor": "interp_bi",
      "params": {"output_size": [13, 15], "align_mode": "half_pixel"}
    }
  ],
  "edges": [
    {"id": "e0", "from": "input0", "to": "conv1", "tensor": "input0"}
  ],
  "outputs": ["conv2", "interp_nn"]
}
```

约束：

- 节点 ID 唯一；边两端存在；每个张量只有一个生产者。
- 三类算子均为一元算子，因此每个节点必须恰有一条输入边。
- 图必须无环；拓扑排序使用节点 ID 最小堆，保证稳定顺序。
- `conv2d` 支持 `kernel_size`、`stride`、`padding`、`in_channels`、`out_channels`、`weights`、`bias`。
- 插值必须且只能提供 `output_size` 或 `scale_factor` 之一；对齐模式为 `half_pixel`、`asymmetric`、`align_corners`。
- `enabled=false` 会关闭节点，节点及其下游都被标记失效；下游以 `E_DEPENDENCY_FAILED` 跳过。

### 输入张量描述

```json
{
  "schema_version": 1,
  "tensor_id": "input0",
  "shape": [1, 2, 9, 10],
  "dtype": "float64",
  "layout": "NCHW",
  "generator": {
    "kind": "numpy_pcg64",
    "seed": 20261117,
    "distribution": {"kind": "uniform", "low": -1.0, "high": 1.0}
  },
  "special_values": [
    {"index": [0, 0, 2, 3], "value": 0.0},
    {"index": [0, 1, 5, 6], "value": 2.5}
  ]
}
```

输入指纹由张量形状、`float64` 小端 C 顺序内容共同计算。`special_values` 支持有限数以及字符串 `nan`、`inf`、`-inf`。

### 运行配置

```json
{
  "max_workers": 4,
  "conv_padding_value": 0.0,
  "tensor_layout": "NCHW",
  "compute_dtype": "float64",
  "node_delay_ms": {}
}
```

`conv_padding_value` 是卷积边界填充值；`node_delay_ms` 仅用于外部强杀复现或调试。

## 确定性算子语义

### 二维互相关

卷积输出尺寸：

```text
Hout = floor((H + 2*pad_h - kh) / stride_h) + 1
Wout = floor((W + 2*pad_w - kw) / stride_w) + 1
```

计算顺序固定为：

```text
batch -> output_channel -> output_row -> output_col
      -> input_channel -> kernel_row -> kernel_col
```

每个累加器从 `0.0` 开始，按上述顺序累加 `padded_value * weight`，最后加 bias。不去零、不做相同值合并、不做容差处理。节点元数据保存循环顺序和填充值。

### 插值

输出尺寸：

- `output_size`：直接使用。
- `scale_factor`：`max(1, floor(input_size * scale))`。

采样坐标：

```text
half_pixel:    (out + 0.5) * (in / out) - 0.5
asymmetric:    out * (in / out)
align_corners: out * (in - 1) / (out - 1)  (out == 1 时为 0)
```

双线性使用 floor/ceil 四点采样、边界裁剪，权重计算顺序固定；最近邻在裁剪后使用 `floor` 取源像素。每个插值产物元数据显式包含：

```json
{
  "alignment": "asymmetric",
  "rounding_rule": "floor_ceil",
  "boundary": "clip",
  "boundary_clipped": true,
  "source_index_rule": "...",
  "operator_version": "convedit-ops-1.0.0:bilinear"
}
```

最近邻的 `rounding_rule` 固定为 `floor`。对齐模式属于参数指纹的一部分，修改后节点缓存键必然变化。

## 缓存、编辑与失效传播

每个节点缓存键：

```text
sha256(
  op + normalized params + operator implementation version
  + input tensor content fingerprint
)
```

每个产物记录包含 `cache_key`、`product_version`、图版本、输入指纹、输出形状、张量 SHA-256、统计摘要和算子元数据。

编辑时比较新旧图中的节点参数、算子、张量名、启用状态和输入边。失效根包括：

- 新增、删除、参数/对齐模式/启用状态变化的节点；
- 输入边发生变化的节点；
- 当前快照中缺少有效产物的节点。

失效集合是根节点在最终图中的全部下游。未失效的有效产物直接复用，不进入线程池，也不会出现在 `execution_trace` 中。执行器会在边内容变化时重新计算缓存键；修改对齐模式不会误用旧缓存。

支持的编辑操作：

```json
{"op": "update_params", "node_id": "interp_bi", "params": {"align_mode": "asymmetric"}}
{"op": "set_enabled", "node_id": "interp_bi", "enabled": false}
{"op": "add_node", "node": {}, "edges": [], "outputs": []}
{"op": "delete_node", "node_id": "conv2"}
{"op": "replace_subgraph", "remove_nodes": [], "add_nodes": [], "add_edges": [], "outputs": []}
{"op": "add_edge", "edge": {}}
{"op": "remove_edge", "edge_id": "e0"}
{"op": "set_outputs", "outputs": []}
{"op": "replace_graph", "graph": {}}
```

`--index` 使用 1 起始序号只应用一条操作；不指定时按文件顺序逐条编辑。每次成功编辑都提升 `graph_version`，并在执行前原子保存调度快照。

交互命令：

```text
update-params NODE JSON [replace]
set-enabled NODE true|false
delete-node NODE
replace-subgraph JSON
add-node JSON
edit JSON
run
status
reuse
show
quit
```

## 并发与稳定顺序

- 使用 `ThreadPoolExecutor`，最大线程数来自配置 `max_workers`。
- 只并发提交当前输入都已就绪的节点；依赖失败后，下游不再执行。
- 每个任务携带 `stable_sort_key = 8位拓扑位次 + ':' + node_id`。
- 最终节点、失败清单、重算清单、复用清单和失效链均按拓扑序输出，不受线程完成顺序影响。
- 每个产物先写不可变缓存文件，再由主线程原子更新游标快照。

## 快照与恢复

状态目录：

```text
state/
  current.json                  当前完整快照
  snapshots/v000005/            topology_scheduled / node_execution / product_summary / complete 阶段快照
  products/<cache-key>.npy      不可变节点产物
  input.npy                     输入张量
  input_descriptor.json
  config.json
  .lock                         进程级 flock
```

`current.json` 包含图版本、图内容指纹、配置哈希、输入指纹、拓扑、稳定排序键、已完成节点、部分产物摘要、节点产物版本、失败清单、注入命中、失效链和执行游标。

阶段：

1. `topology_scheduled`
2. `node_execution`（每完成一个节点更新一次）
3. `product_summary`
4. `complete` / `interrupted` / `completed_with_failures`

恢复时校验配置哈希、图指纹和图版本；不一致返回 `E_CONFIG_MISMATCH`。已完成且指纹匹配的产物直接复用，其余从游标继续。强杀只可能遗留临时文件或未被快照引用的孤立产物，不会形成半写 `current.json`。

## 注入配置

`samples/injections.json` 包含：

- `shape_mismatch`：指定节点期望输入形状，运行时触发 `E_SHAPE_MISMATCH`。
- `padding_out_of_range`：覆盖卷积填充，触发 `E_PARAM_OUT_OF_RANGE`。
- `invalid_scale`：覆盖插值缩放因子，触发 `E_PARAM_OUT_OF_RANGE`。
- `nonfinite_input`：在输入张量指定位置写入 NaN/Inf，算子计算阶段触发 `E_NONFINITE_INPUT`。
- `interrupt`：在节点产物与快照提交后发送 SIGTERM/SIGINT；CLI 退出码为 `0`。
- `hard_kill`：在节点计算期间或快照提交后发送 SIGKILL。

默认项均为 `enabled=false`，可通过命令行启用：

```bash
.venv/bin/python -m convedit run \
  --state artifacts/demo/manual/state \
  --report /tmp/fault.json \
  --injections samples/injections.json \
  --enable-injection INJ-SHAPE-001
```

单节点失败只阻断其后继；同一拓扑波次或独立分支中可执行的节点继续运行。失败记录包含 `node_id`、`stage`、业务码、异常类型和 `injection_id`。报告中的 `injections_hit` 同时保留命中证据。

## 业务码与退出码

| 业务码 | 含义 |
|---|---|
| `E_GRAPH_INVALID` | 结构、端点、重复生产者、环等图错误 |
| `E_PARAM_OUT_OF_RANGE` | 卷积/插值/配置参数越界 |
| `E_SHAPE_MISMATCH` | 输入、通道或注入边形状不一致 |
| `E_NONFINITE_INPUT` | 算子输入含 NaN/Inf |
| `E_DEPENDENCY_FAILED` | 上游失败/禁用导致跳过 |
| `E_CONFIG_MISMATCH` | 恢复时配置哈希或图指纹不一致 |
| `E_INPUT_INVALID` | 输入描述或张量文件错误 |
| `E_IO` | 状态/文件/锁错误 |
| `E_OPERATOR_FAILED` | 未归类计算异常 |
| `E_INTERNAL` | 内部错误保留码 |

CLI 退出码：

- `0`：完成、仅复用、或收到可处理退出信号并完成快照提交。
- `2`：图/参数/运行失败，或注入故障导致部分节点失败。

## 验收证据

- `artifacts/demo/verification.json`：完整机器可读结果，包括每个节点的逐元素比较、缓存键与摘要比较。
- `artifacts/demo/incremental/reports/graph_v0001.json` … `graph_v0005.json`：5 次编辑的重算、复用、失效链和业务码。
- `artifacts/demo/recovery_graceful/interrupted.json` 与 `resumed.json`：SIGTERM 中断与恢复。
- `artifacts/demo/recovery_hard/resumed.json`：真实 SIGKILL 后恢复。
- `artifacts/demo/faults/INJ-*/report.json`：四类故障的隔离结果和注入 ID。
- `artifacts/demo/concurrency/{serial,parallel}/report.json`：串行/并发稳定顺序对照。
- `artifacts/force_kill/resumed.json`：外部 `kill -9` 脚本复现结果。
