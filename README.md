# QConv：低精度量化卷积调参工作台

这是一个无容器、无外部数据依赖的本地应用，用于在固定种子合成张量上编辑量化卷积图、反复调参、并行调度、增量重算并验证断点恢复。实际计算路径是整数定点卷积，浮点只用于“整数累加结果 × 权重缩放 × 激活缩放”的确定性反量化归约及报告中的舍入误差统计。

## 快速启动

Windows PowerShell（Python 3.10+）：

```powershell
py -3 --version
py -3 -m pip install -r requirements.txt
py -3 -m qconv generate-samples --out samples
py -3 -m qconv run --samples samples --session session --report report.json
py -3 -m qconv repl --samples samples --session session
```

Linux/WSL（本仓库实际验证环境为 Python 3.11.0rc1 + NumPy 2.4.6）：

```bash
python3 --version
python3 -m pip install --user -r requirements.txt
python3 -m qconv generate-samples --out samples
python3 -m qconv run --samples samples --session session --report report.json
python3 -m qconv repl --samples samples --session session
```

运行产物：

- `session/snapshots/latest.json`：最新完整阶段快照（原子替换）
- `session/cache/*.qcache.npz`：内容寻址的节点产物缓存
- `session/working_graph.json`：交互编辑后的工作图与量化模式
- `report.json`：每次完整运行的诊断报告（原子替换）

同一个 session 默认恢复 `working_graph.json`，因此可继续编辑和断点恢复；需要从原始图完全干净地全量运行时，使用新的 `--session` 目录或 `--no-resume-working-graph`。

## 样本与文件格式

`generate-samples` 使用固定种子 `20261117` 生成四节点 DAG：

- `branch_l`、`branch_r`、`stem` 从外部输入并行分支
- `tail` 依赖 `stem`
- 三个根节点同层，可由线程池并发执行；下游节点只在前层完成后调度
- 宽松预算：全局 100000 MAC、单节点 20000 MAC
- 紧凑预算：全局 6000 MAC、单节点 3000 MAC，可稳定触发 `E_BUDGET_EXHAUSTED`

### 图 JSON

```json
{
  "schema_version": 1,
  "graph_version": 1,
  "input": {
    "name": "input", "shape": [1, 4, 6, 6], "pack": "input",
    "bit_width": 8, "scale": 0.03125, "dtype": "int8"
  },
  "nodes": [{
    "id": "stem", "input": "input", "output": "stem",
    "in_channels": 4, "out_channels": 4,
    "kernel": [3, 3], "stride": [1, 1], "padding": [1, 1],
    "weight_pack": "stem", "weight_shape": [4, 4, 3, 3],
    "weight_bit_width": 8, "weight_scale": 0.02,
    "activation_bit_width": 8, "activation_scale": 0.03125,
    "accumulator_bits": 32, "overflow_policy": "error"
  }],
  "edges": [{"from": "input", "to": "stem"}],
  "outputs": ["stem"]
}
```

张量采用 signed NCHW；每个节点恰有一个数据输入。结构校验覆盖唯一节点 ID、边端点、单生产者、单入边、DAG 无环、通道链和输出形状。节点级量化参数非法会产生 `E_QUANT_PARAM` 并只阻断该节点及其下游；结构错误产生 `E_GRAPH_INVALID`。

### QPAK 量化包

`packs.json` 保存形状、dtype、来源位宽和来源缩放因子；实际整数在 `.qpak` 文件中。容器头为小端：

| 字段 | 类型 |
| --- | --- |
| magic `QPK1` | 4 bytes |
| format version | uint16 LE |
| endian marker `0x1234` | uint16 LE |
| dtype code（int8/int16/int32） | uint16 LE |
| ndim | uint16 LE |
| element count | uint64 LE |
| shape | ndim × uint32 LE |
| payload | signed little-endian integers |

文件必须与头部元素数和描述形状完全一致。截断、尾随补字节、计数不符、形状不符或端序标记不符均产生 `E_PACK_SHAPE_MISMATCH`，不会截断或补零。

## 计算与编辑语义

整数卷积循环顺序固定为 batch、输出通道、输出行、输出列、核行、核列、输入通道升序。每次加法前用配置的 `accumulator_bits` 做有符号范围检查：

- `overflow_policy=error`：立即产生 `E_ACC_OVERFLOW`，记录节点 ID、累加位宽和完整位置（输出坐标、核坐标、输入通道、MAC 下标）
- `overflow_policy=saturate`：饱和并在节点溢出摘要中记录

反量化使用 Python `Decimal` 对 JSON 中的十进制缩放因子做确定性乘法和舍入。支持 `half_even`、`half_up`、`half_down`、`floor`、`ceil`、`truncate`、`away_from_zero`；实际模式和 `dequant_decimals` 写入缓存元数据及节点报告。结果不是通过浮点卷积得到的。

REPL 支持：

```text
run [REPORT_PATH]
set-bitwidth NODE weight|activation BITS
set-scale NODE weight|activation VALUE
set-quantization on|off
replace-subgraph NODE PATCH_JSON
save-graph PATH
export-outputs DIRECTORY
show
exit
```

位宽编辑会将整数激活/权重夹取到新的有符号范围；缩放因子编辑进入反量化因子。对于上游节点，下游会按自己的激活位宽/缩放因子重新量化上游浮点产物。关闭量化后仍执行整数卷积，但不再乘缩放因子，并使用上游整数累加结果作为下游整数输入。

每次编辑会标记目标节点和全部下游为失效。缓存键包含算子实现版本、节点量化参数、权重内容指纹、输入内容指纹、量化模式与舍入配置。未失效且键匹配的节点直接加载缓存产物，不重复执行。

## 并行与预算

调度顺序使用“拓扑层级 + 节点 ID”定义稳定键。每一层按稳定顺序依次做缓存判定和预算准入，然后交给 `ThreadPoolExecutor` 并发执行。预算按完整真实 MAC 数（输出元素 × 输入通道 × 核面积）预留；执行异常或溢出的未用部分按确定性规则退回，并在账本中同时记录预留、执行和退回。

每个节点账本行包含：`required_macs`、`requested_macs`、`reserved_macs`、`executed_macs`、`remaining_before`、`remaining_after_reservation`、`remaining_after_reconcile`、`refund_macs`、状态和原因。预算失败不会伪装成线程超时，也不会启动该节点。

## 快照、终止与恢复

快照阶段包括 `graph_load`、`integer_conv`、`dequant`、`aggregate`。每次提交写入同目录临时文件、执行 `fsync`，再用 `os.replace` 原子替换 `latest.json`。快照包含快照版本、图版本与指纹、配置哈希、已完成节点、部分张量摘要、缓存键/产物版本和中断标记。

`SIGINT`/`SIGTERM`（Windows 还有可用的 `SIGBREAK`）会把停止标志写入调度器。应用完成当前已启动层的节点产物和缓存写入后提交快照，以退出码 `0` 结束。`SIGKILL` 等强制终止后，只有已完整替换的 `latest.json` 会被读取；缓存产物必须通过快照中的键、摘要和当前缓存内容三重校验后才会被复用。

快照配置哈希覆盖调度、舍入和注入配置；`debug_node_delay_ms` 不参与哈希。量化模式保存在 `working_graph.json` 和缓存键中，可交互切换而不触发配置哈希不匹配。

## 注入与报告

`injection*.json` 是独立配置。样本覆盖：

- `INJ-NAN-01`：非有限缩放因子 → `E_QUANT_PARAM`
- `INJ-BW-01`：位宽越界 → `E_QUANT_PARAM`
- `INJ-OVF-01`：极小累加位宽 → `E_ACC_OVERFLOW`
- `INJ-CACHE-01`：缓存驻留预算为零
- `node_exception`：可指定 `dequant` 等阶段，报告 `E_EXECUTION_FAILED`

单节点异常写入阶段、业务码和异常类型，其他可调度节点继续。每次运行的 JSON 报告包含：拓扑稳定键、失效/复用/重算清单、逐节点缓存命中、整数/浮点摘要、重算耗时、误差摘要、预算账本、注入命中、首帧响应、快照版本及失败清单。`--export-outputs DIR` 会原子写出每个节点的 `.npy` 张量和 `outputs.json`。

## 验证

执行：

```bash
python3 -m unittest discover -s tests -v
```

当前环境的实际结果：13 项测试全部通过（约 5 秒），覆盖：

- 整数卷积、逐位置溢出和确定性舍入模式
- QPAK 计数/端序/长度严格校验
- 重复 ID、缺边、环、非法量化参数的图诊断
- 并发度 1 与 4 × 全局预算宽松/紧凑的结果逐字段一致
- 位宽/缩放编辑只重算目标及下游，并与保存后的干净全量运行逐元素一致
- 子图替换后未受影响分支复用，并与干净全量运行逐元素一致
- 全缓存复用、零缓存注入、NaN/位宽/溢出/节点异常隔离
- 配置哈希不符的 `E_CONFIG_MISMATCH`
- 真实 `SIGTERM` 退出码 0、快照恢复，以及真实 `SIGKILL` 后恢复

## 实现边界

- 仅实现无 bias 的二维 signed 量化卷积，支持 batch ≥ 1。
- 缓存驻留预算按缓存文件实际字节数计算；淘汰按缓存键字典序进行，保证跨并发度确定性。
- 报告为避免巨大 JSON，节点输出使用摘要和前 8 个值预览；完整张量通过 API 或 `export-outputs` 获取。
- 未使用 Docker、容器、网络服务或外部数据。

