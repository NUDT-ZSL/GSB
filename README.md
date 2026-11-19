# QConv Budget Lab

面向低精度与量化卷积算子的本地领域数据编辑、调度与诊断应用。项目不使用容器、GPU、外部账号或在线服务；任务清单、数组、进程池执行、误差谱和版本化快照都在本地完成。

核心链路：

```text
JSONL 任务清单
  -> 输入/配置校验
  -> MAC / 内存 / worker 三预算准入
  -> 分块、循环顺序、worker 与归约路径决策
  -> 本地进程池执行
  -> float64 反量化参考与四级误差谱
  -> 跨模块一致性自检
  -> 计划 / 预算账本 / 结果摘要整体快照
```

## 快速开始

要求 Python 3.11+、NumPy、pytest。

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m qconv generate-samples
python -m pytest
```

Linux/macOS：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m qconv generate-samples
python -m pytest
```

启动本地交互应用：

```bash
python -m qconv serve
# 浏览器打开 http://127.0.0.1:8765
```

也可以直接运行 `python run_app.py` 或 `python run_demo.py`。

## 常用命令

```bash
# 固定种子生成 samples/jobs.jsonl、conflict_jobs.jsonl 与数组
python -m qconv generate-samples

# 校验保存的 npz 数组可由 seed 精确还原
python -m qconv verify-samples

# 执行正常批次
python -m qconv run

# 同时运行预算冲突和异常归因演示
python -m qconv demo --which both

# 校验最新快照的校验和与跨模块一致性
python -m qconv self-check

# 清理快照与缓存
python -m qconv clean

# 单分块 worker 入口；用于在独立进程中验证一个分块
python -m qconv.worker_main --task-json chunk_task.json --output-prefix out/chunk
```

常用覆盖参数：

```bash
python -m qconv run \
  --jobs samples/jobs.jsonl \
  --mac-budget 20000000 \
  --memory-budget-bytes 8000000 \
  --worker-slots 6 \
  --max-workers-per-task 2 \
  --atol 0.05 --rtol 0.01
```

故障注入格式为 `job_id:chunk_id:stage[:code]`，阶段可选
`prepare`、`quantize_input`、`fixed_mac`、`accumulate_reduce`、
`dequantize_output`。

## 目录结构

```text
qconv/
  models.py          任务、预算、容差与配置模型
  validation.py      JSONL 与配置校验
  quant_conv.py      确定性数组、8 位量化、分块 int8/int32 卷积
  scheduler.py       三预算准入、分块/worker/归约路径决策
  executor.py        确定性进程池执行与结构化 worker 错误
  diagnostics.py     四级误差谱、分位点、放大比与越界定位
  snapshots.py       整文件替换、校验和、非阻塞 stage 锁
  consistency.py     计划/账本/执行摘要/误差谱一致性自检
  service.py         端到端服务编排
  web.py             无外部前端依赖的本地交互应用
  cli.py             CLI 与本地入口
  worker_main.py     单分块最小进程入口
config/default.json  固定种子、默认预算和策略
samples/             本地生成的 JSONL、manifest 与 npz 数组
snapshots/           可配置、可清理的版本化快照
tests/               量化、调度、预算、执行、误差、快照和端到端测试
```

## 关键业务语义

### 量化与卷积

每个任务使用同一组 `quant_scale`、`zero_point` 对激活和卷积核做 per-tensor 量化：

```text
q = clip(rint(x / scale) + zero_point, -128, 127)
x_dequant = (q - zero_point) * scale
```

分块计算把中心化 int8 激活与中心化 int8 权重相乘，在 int32 分段累加器中累加；分段上限由 int32 最坏乘积累加上界保护，再合并到 int64。最终输出为 `acc * scale^2`。float64 反量化输入/权重上的卷积是该快照的参考真值。

### 准入顺序与优先级

任务按以下稳定键排序：

1. `priority` 降序；
2. 量化精度档：`fine`、`balanced`、`coarse`；
3. `job_id` 字典序。

每个任务的最小准入检查顺序固定为：

1. `E_MAC_BUDGET`：卷积 MAC 超出剩余乘加预算；
2. `E_WORKER_SLOTS`：没有至少一个 worker 槽位；
3. `E_MEMORY_LIMIT`：不存在满足剩余驻留内存的分块。

被拒任务的所有预算扣减均为 0，账本中显式记录零残留。只有全部任务获得一个 mandatory worker 后，才会把剩余槽位按优先级分配给可缩短预计轮数的任务，避免高优先级任务在准入阶段饿死后续任务。

### 冲突取舍

调度记录为每个准入任务写入决策依据与 tradeoff 码，包括：

- 更大 tile 降低 chunk/归约开销，但放大每 worker 驻留内存；
- 更多 worker 缩短预计轮数，但增加并发驻留内存并分摊单 worker MAC；
- `coarse` 量化档在精确归约路径放不进内存时，可走
  `float32_chunk_reduce`，节省每输出 4 字节，同时显式接受一次 float32 舍入；
- 溢出保护需要分段时记录额外归约开销。

计划、决策、chunk 边界和 chunk 顺序都使用规范 JSON；相同输入与配置下计划字节一致。

### 误差谱

每个成功任务输出四级指标：最大绝对偏差、最大/平均相对偏差、P50/P90/P99 分位点、相对上一级和相对量化级的放大比、Top 位置明细。最终输出超过 `atol + rtol * |reference|` 时返回 `E_TOLERANCE_EXCEEDED`，并给出四维位置、参考值、实际值、允许值和偏差量。

### 快照与并发

快照是单个 JSON 文件，计划、预算账本、结果摘要各自包含：

```json
{"version": 1, "checksum": "sha256(payload)", "payload": {}}
```

提交时使用跨平台非阻塞目录锁；锁已存在立即返回 `E_STAGE_BUSY`。新版本通过临时文件、`fsync` 和 `os.replace` 整体替换，不做增量修补。读取时验证组件校验和与快照整体校验和，失败返回 `E_SNAPSHOT_CORRUPT`。

## 交互应用

页面支持：

- 直接编辑 JSONL 任务清单和三项预算；
- 执行编辑后的任务；
- 一键触发预算冲突演示；
- 一键触发异常归因演示；
- 查看任务清单、准入/拒绝结论、调度计划、预算账本、四级误差谱、最近错误码和快照版本；
- 清理快照与缓存。

HTTP 接口：

```text
GET  /api/state
POST /api/run       {"mode":"normal|budget-conflict|fault-attribution", ...}
POST /api/clean
GET  /api/snapshot
GET  /healthz
```

## 实际验证结果

本机 Python 3.12.13、NumPy 2.5.5、pytest 9.2.0：

```text
$ python -m pytest -q
......................                                           [100%]
22 passed

$ python -m qconv verify-samples
{"ok": true, "problems": [], "checked_jobs": 10}
```

正常批次：

```text
status=ok code=OK
admission: admitted=4 rejected=0 workers=5 chunks=5
ledger: mac=694728/20000000 memory=233566/8000000B workers=5/6 reconciled=True
self-check: PASS
```

预算冲突演示：

```text
admitted 3 / rejected 3
conflict_mac_heavy:    E_MAC_BUDGET
conflict_fine_memory:  E_MEMORY_LIMIT
conflict_slot_denied:  E_WORKER_SLOTS
conflict_coarse_memory: route=float32_chunk_reduce
ledger: mac=529058/2000000 memory=2592635/2600000B workers=3/3 reconciled=True
self-check: PASS
```

异常归因演示：

```text
status=partial_failure code=E_INJECTED_FAULT
failed: job_balanced_conv, chunk 0, stage=fixed_mac
completed: job_fine_conv, job_coarse_conv, job_large_conv
self-check: PASS
```

更多实现细节见 [`docs/TECHNICAL.md`](docs/TECHNICAL.md)。
