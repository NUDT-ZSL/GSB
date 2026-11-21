# 本地推理服务动态批处理精度诊断工作台

本工作台在本地逻辑时钟下推进动态批处理队列，逐批构建“精度谱”，并诊断两类问题：

- 队列水位参与乘性累计后，批级估算误差是否超出预算并持续放大；
- 两个候选批次或副本的估算差落入浮点容差内时，批次裁决是否精度敏感、换用定点通路是否会翻转结论。

实现不依赖网络、账号、容器或外部数据集。状态保存在 SQLite，正常提交以单事务完成；恢复路径可从已持久化的批次载荷补齐半提交精度谱，并从下一个未完成时步继续。

## 环境

- Python 3.11 或以上
- NumPy
- pytest
- Python 标准库 `sqlite3`

当前工作区实测环境为 Python 3.12.13、NumPy 2.5.5、pytest 9.2.0、SQLite 3.37.2。

```bat
cd /d <工作区根目录>
py -3.11 -m pip install -r requirements.txt
```

Linux/macOS 对应使用 `python3.11 -m pip install -r requirements.txt`。所有命令都在工作区根目录执行，样本路径相对当前工作目录解析。

## 目录

```text
batchdiag/              核心包：校验、双通路估算、调度、SQLite、诊断、CLI
samples/                确定性 JSON 样本
tests/                  pytest 回归测试
artifacts/demo-run/     demo.py 生成的数据库、报告与验证摘要
demo.py                 一键可重复演示
demo.bat / demo.sh      Windows / POSIX 启动脚本
run_tests.bat / .sh     测试启动脚本
workbench.py            Windows 友好的等价入口
```

## 样本

| 文件 | 用途 |
|---|---|
| `samples/model.json` | 模型 ID、4 层参数量、默认 token 契约 |
| `samples/replicas.json` | `r0`/`r1` 算力、显存、最大批、最大序列、调度配额 |
| `samples/requests.json` | 常规 12 请求轨迹 |
| `samples/requests_long_backlog.json` | 40 请求长积压轨迹，触发水位放大 |
| `samples/requests_near_tolerance.json` | 近似同成本请求与近似同算力副本，触发精度敏感 |
| `samples/precision.json` | 公式参数、定点标度、容差、误差预算、水位阈值、采样比例 |

服务等级是闭合枚举：`gold`、`silver`、`bronze`。当前调度按 `(arrival_ms, request_id)` FIFO 排序，服务等级用于结局诊断，不用于插队；题目重点是水位演进和误差传播，而不是优先级插队。

## 快速开始

以下命令均在 Windows `cmd` 中可直接执行：

```bat
py -3.11 -m batchdiag load ^
  --model samples\model.json ^
  --replicas samples\replicas.json ^
  --requests samples\requests.json ^
  --config samples\precision.json ^
  --db artifacts\work\state.sqlite ^
  --force

py -3.11 -m batchdiag advance --db artifacts\work\state.sqlite --steps 15

py -3.11 -m batchdiag spectrum ^
  --db artifacts\work\state.sqlite ^
  --output artifacts\work\report.json

py -3.11 -m batchdiag status --db artifacts\work\state.sqlite

py -3.11 -m batchdiag verify ^
  --db artifacts\work\state.sqlite ^
  --report artifacts\work\report.json
```

POSIX shell 只需把续行符换成 `\`，路径换成 `/`。

`--to 100` 可替代 `--steps`，表示处理逻辑时钟不超过 100 的所有时步。`--steps` 与 `--to` 互斥。时钟从 `start_clock_ms`（样本为 0）开始：先处理时刻 0，再递增 `clock_step_ms`；因此到达时刻不大于当前时步时钟的请求会进入持久化队列。

## 子命令

### `load`

校验模型、副本、请求和诊断配置，全部通过后才创建数据库。非法输入返回明确 `E_` 业务码，不创建或替换任何数据库。`--force` 仅在校验成功后替换已有数据库。

### `advance`

每个时步执行：

1. 将已到达请求加入队列，将 `deadline_ms < clock_ms` 的排队请求转为 `expired`；
2. 按 FIFO 顺序取队首至多 `dispatch_quota` 个候选；
3. 在副本算力、显存、最大批大小和最大序列长度内组装批次；
4. 用定点与 float64 双通路估算 `compute`、`memory`、`queue_wait`；
5. 在单个 SQLite 事务中写入时步、队列、请求结局、批次、三条精度谱和累计误差状态。

每步提交一个批次。若队首单请求即超过所有副本容量，该请求转为 `failed`，业务结局码为 `E_EXECUTION_FAILED`；超期请求结局为 `expired`，业务结局码为 `E_REQUEST_EXPIRED`。二者不会混用。

### `spectrum`

输出稳定排序 JSON，包含：

- `batch_spectrum`：逐批逐估算量的定点串、float64 串、标度、舍入方向、换算前原始值、逐操作累计误差上界、跨批来源和批号链；
- `amplification`：放大标记、首个超预算时步、水位、超预算倍数和参与累计批号链；
- `precision_sensitivity`：批次边界、副本选择、容量阈值三类敏感标记及其精度谱引用；
- `request_outcomes`：请求结局和持久化状态；
- `summary` 与 `report_digest`。

`--output` 同时把同一报告原子写入本地文件。

### `resume`

先执行恢复检查：

- 已提交批次缺少一条或多条精度谱：直接用批次中持久化的完整载荷补齐；
- 时步已推进但批次状态未完成：补齐状态并保留既有累计误差上界；
- 正常进程在事务提交前被终止：SQLite 回滚该事务，恢复后从同一时步确定性地重新执行。

随后从当前逻辑时钟继续推进。可用 `--steps` 或 `--to` 指定续算范围；都不指定时自动推进到所有请求形成最终结局。

### `status`

输出当前逻辑时钟、版本、队列、高水位、请求状态计数、累计误差状态和未完成持久化工件。

### `verify`

检查 SQLite 完整性、模式版本、队列计数、时步/版本一致性、批次序号、请求唯一入批、每条批次恰好三条精度谱、跨批传播连续性、累计误差最终状态，以及可选的报告文件逐字段一致性。报告不一致返回 `E_STATE_MISMATCH`。

## 估算公式

每个量都实现同一公式结构的定点通路和 NumPy `float64` 通路：

```text
compute = base
        + input_token  * total_input_tokens
        + output_token * total_output_tokens
        + parameter    * total_parameters
        + batch_size   * batch_request_count
        + queue_depth  * queue_watermark
        + carry        * previous_compute
        + queue_carry  * queue_watermark * previous_compute

memory = 同结构，使用 memory 公式参数
queue_wait = base + queue_depth*queue_watermark
           + batch_size*batch_request_count
           + carry*previous_queue_wait
           + queue_carry*queue_watermark*previous_queue_wait
```

`queue_watermark` 是出队前、剔除已超期请求后的显式持久化队列水位。`queue_carry` 是水位对上一批误差的乘性传播项；没有上一批时该项为零。

`float_tolerance` 可取非负十进制值；`diagnostic_sample_ratio` 取 `[0,1]`，为 0 时保留全部逐批精度谱但关闭敏感标记审计。定点标度必须是正十进制幂且不大于 `1e12`。

定点通路把 JSON 十进制参数按配置标度转为整数，加减为整数精确运算，乘法按 `nearest_even` 量化到标度并逐步累计：

- 参数量化误差；
- 乘法传播误差；
- 乘法量化误差。

float64 通路用 NumPy 标量执行同一操作顺序，并用 `Decimal.from_float` 精确记录每次 IEEE 754 舍入误差。每个估算量的权威界取 `max(fixed_bound, float64_bound)`，同时分别保留两通路界和“界是否覆盖实际误差”字段。

## 放大判定

仅当同时满足以下条件时生成放大标记：

1. 批次已提交，且 `queue_watermark > amplification.queue_threshold`；
2. 该量水位乘性项已参与累计；
3. 已持久化的累计误差上界 `> error_budget`。

标记中的 `over_budget_multiple = error_bound / error_budget`，批号链来自该精度谱条目逐步保存的 `accumulation_chain`。诊断不重新拟合、不读取内存临时值，只读已提交批次与精度谱。

## 精度敏感判定

- `replica_choice`：多个可容纳批次的副本算力利用率差小于 `float_tolerance`，且副本选择依赖该差；
- `batch_boundary`：配额或容量边界上的两个候选批次数值差小于容差；
- `capacity_threshold`：float64 与定点对“是否越过容量上限”的判断不同。

每个标记给出参与请求号、候选值、差值、容差、误差上界、换算操作引用、精度谱引用，以及 `flip_if_fixed`。放大标记和精度敏感标记位于报告的不同顶层数组，互不覆盖。

## 强制终止与恢复演示

故障注入仅用于测试进程强制终止路径：

```bat
set BATCHDIAG_FAULT=torn_spectrum
set BATCHDIAG_FAULT_STEP=3
py -3.11 -m batchdiag advance --db artifacts\work\crash.sqlite --steps 12
```

`torn_spectrum` 模拟批次头已提交但只写入一条精度谱的撕裂状态，进程以 138 退出。随后执行：

```bat
py -3.11 -m batchdiag status --db artifacts\work\crash.sqlite
py -3.11 -m batchdiag resume --db artifacts\work\crash.sqlite --steps 9
py -3.11 -m batchdiag verify --db artifacts\work\crash.sqlite --report artifacts\work\long-backlog-report.json
```

`kill_before_commit` 与 `kill_after_commit` 也可用于验证事务回滚与已提交时步幂等。只有精确匹配 `BATCHDIAG_FAULT_STEP` 的时步会触发注入。

## 可重复演示与测试

Windows：

```bat
demo.bat
run_tests.bat
```

POSIX：

```sh
./demo.sh
./run_tests.sh
```

直接调用：

```sh
python3.11 demo.py
python3.11 -m pytest -q
```

`demo.py` 生成数据库、常规报告、长积压报告、近容差报告、恢复报告和并发报告，并将摘要写入 `artifacts/demo-run/verification.json`。也支持 `--workdir <目录>`。

## 本次实际验证结果

- pytest：`29 passed`
- 常规轨迹：10 批，报告摘要与重跑逐字节一致
- 长积压轨迹：3 个放大标记，覆盖 `compute`、`memory`、`queue_wait`
  - `compute`：首次超预算为 `b000003`，逻辑时钟 20 ms，水位 32，倍数 `1.012924416`
  - `memory`：首次超预算为 `b000003`，逻辑时钟 20 ms，水位 32，倍数 `1.045779886222222222222222222`
  - `queue_wait`：首次超预算为 `b000008`，逻辑时钟 70 ms，水位 12，倍数 `1.0304`
- 近容差轨迹：4 个敏感标记，覆盖 `batch_boundary` 与 `replica_choice`
- 半提交恢复：补齐 `b000003/memory`、`b000003/queue_wait`，提交已写入但有撕裂状态的 `b000003/compute`，续算报告与不中断报告逐字节一致
- 4 进程各推进 5 步：最终 20 步、10 批，报告与串行 20 步逐字节一致，`verify` 通过

完整数据见 `artifacts/demo-run/verification.json`。

## 业务码

非法输入主要业务码包括：

`E_JSON_PARSE`、`E_MODEL_SCHEMA`、`E_MODEL_ID`、`E_LAYER_NAME`、`E_LAYER_DUPLICATE`、`E_LAYER_PARAMETER`、`E_DUPLICATE_REPLICA`、`E_REPLICA_COMPUTE`、`E_REPLICA_MEMORY`、`E_REPLICA_MAX_BATCH`、`E_REPLICA_MAX_SEQUENCE`、`E_REPLICA_QUOTA`、`E_DUPLICATE_REQUEST`、`E_REQUEST_ID`、`E_REQUEST_CLOCK`、`E_REQUEST_DEADLINE`、`E_SERVICE_CLASS`、`E_REQUEST_TOKENS`、`E_SEQUENCE_LIMIT`、`E_SCALE`、`E_TOLERANCE`、`E_ERROR_BUDGET`、`E_SAMPLE_RATIO`、`E_CLOCK_STEP`、`E_FORMULA_PARAM`、`E_AMPLIFICATION_THRESHOLD`。

运行期业务码包括：

`E_CLOCK_ROLLBACK`、`E_STATE_EXISTS`、`E_DB_NOT_FOUND`、`E_DB_SCHEMA`、`E_STATE_CONFLICT`、`E_DUPLICATE_REQUEST_ASSIGNMENT`、`E_STATE_MISMATCH`。请求结局码另用 `E_REQUEST_EXPIRED` 与 `E_EXECUTION_FAILED` 区分超期和执行失败。

JSON 错误输出形状固定为：

```json
{
  "ok": false,
  "code": "E_...",
  "message": "...",
  "details": {}
}
```

成功输出使用 `sort_keys=True`、固定缩进和稳定列表排序。CLI 业务失败退出码为 2，`E_STATE_MISMATCH` 为 3。
