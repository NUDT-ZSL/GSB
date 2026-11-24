# 离线告警关联与去噪引擎

这是一个仅依赖 Python 标准库的离线实现，用于在同一批主机告警上比较 v1/v2
两代关联规则的统一视图，并产出确定性的告警绑定、事件簇、歧义与拒绝清单。
运行过程不访问网络、不读取账号，也不使用系统当前时间参与规则时间换算。

工程包含：

- `alert_engine/`：解析、规则仲裁、时间定版、谱系归并、调度、SQLite 账本、
  增量重算、原子检查点、CLI。
- `tools/generate_sample.py`：固定种子样本生成脚本。
- `tests/`：32 项自动化测试，覆盖解析、规则、归并、歧义、并发、补丁事务与恢复。
- `TECHNICAL.md`：实现口径、算法与持久化边界说明。
- `VALIDATION.md`：本次实际执行命令与结果。
- `samples/`：当前已生成的固定种子样本。

## 环境

目标是 Windows + Python 3.11，仅测试阶段安装 `pytest==8.2.0`。本次验证容器为
Linux/WSL2 + Python 3.10.12，代码未使用 3.11 专属能力，也没有第三方运行时依赖。

```bash
python --version
pip --version
pip install pytest==8.2.0
```

## 快速开始

```bash
# 1. 生成固定种子样本与真值同步标记
python tools/generate_sample.py --out samples

# 2. 先做测试收集，确认文件与依赖完整
python -m pytest --collect-only -q

# 3. 执行测试
python -m pytest -q

# 4. 端到端运行并校验 truth_sync.json 中的标记
python -m alert_engine verify \
  --sample samples \
  --workspace build/engine-work \
  --workers 4 \
  --queue-limit 64 \
  --host-timeout-ms 10000 \
  --max-alerts-per-host 100000

# 5. 从最后一个完整检查点恢复
python -m alert_engine run \
  --sample samples \
  --workspace build/engine-work \
  --workers 4 \
  --queue-limit 64 \
  --resume
```

也可以在代码中调用：

```python
from pathlib import Path
from alert_engine import Engine, EngineConfig
from alert_engine.engine import InputPaths

engine = Engine(
    Path("build/engine-work"),
    EngineConfig(max_workers=4, pending_host_queue_limit=64),
)
report = engine.run(InputPaths.from_directory(Path("samples")))
print(report["status"], report["counts"]["clusters"])
```

## 输入格式

### 告警流 `alerts.jsonl`

每行一个对象：

```json
{
  "alert_id": "A-1",
  "host_id": "H-1",
  "alert_ts_ms": 1700000001000,
  "rule_key": "R-POWERSHELL",
  "severity": 4,
  "process_guid": "ps-1",
  "parent_process_guid": "ps-0",
  "payload_digest": "64位十六进制SHA-256"
}
```

严格校验告警 ID 唯一性、时间非负整数、声明 severity 区间、规则键可解析、
进程标识以及 `payload_digest`。非法行进入拒绝清单，后续行继续处理。

### v1 快照 `rules_v1.json`

```json
{
  "schema_version": 1,
  "observation_clock_ms": 1700000000000,
  "severity_range": {"min": 1, "max": 5},
  "rules": [
    {
      "rule_key": "R-POWERSHELL",
      "effective_after_s": 0,
      "dedup_window_s": 60,
      "match_strings": ["powershell", "-enc"],
      "enabled": true
    }
  ]
}
```

v1 的相对秒只以 `observation_clock_ms` 为基准换算，绝不读取系统当前时间。
秒字段按“向下量化”建模：绝对生效时刻和去重窗口的未知亚秒部分均在
`[0, 999] ms` 内，分别记录 `effective_error_ms` 与 `window_error_ms`。

### v2 分片与元数据库

`v2_shards/*.json` 中每个分片：

```json
{
  "schema_version": 2,
  "shard_id": "v2-000",
  "rules": [
    {
      "rule_key": "R-POWERSHELL",
      "effective_ms": 1700000000000,
      "generation": 2,
      "dedup_window_ms": 60000,
      "match_strings": ["powershell", "-enc"],
      "enabled": true,
      "tombstone": false,
      "parent_rule_keys": []
    }
  ]
}
```

`v2_meta.sqlite` 的表 `v2_rule_meta` 至少包含：

```sql
rule_key TEXT,
effective_ms INTEGER,
generation INTEGER,
dedup_window_ms INTEGER,
tombstone INTEGER,
parent_rule_keys_json TEXT,
shard_id TEXT,
record_index INTEGER
```

解析时对分片与元数据逐项交叉校验。v2 的代数沿同一规则键的生效时刻严格递增；
不满足者逐条拒绝并用 `E_V2_GENERATION_ORDER` 标记。

### 补丁批次 `patches.jsonl`

每行一个完整批次，批内多规则原子生效：

```json
{
  "batch_id": "P100",
  "logical_ts_ms": 100,
  "rules": [
    {
      "rule_key": "R-PATCH",
      "effective_ms": 1700000025000,
      "generation": 4,
      "dedup_window_ms": 1000,
      "match_strings": ["patch"],
      "enabled": true,
      "tombstone": false,
      "parent_rule_keys": []
    }
  ]
}
```

批次按文件到达顺序逐批提交，但权威规则视图按 `effective_ms`、`generation`、
来源优先级与批次事实确定性裁决，因此最终结论不受文件乱序影响。

## 关键语义

- **权威裁决**：同一规范规则键先取最新绝对生效版本，同一生效时刻取更高代数；
  同代同时刻按墓碑、补丁、v2、v1 的稳定优先级裁决。较晚生效的墓碑覆盖较早的
  非墓碑记录。
- **规则冲突**：v1 与 v2/补丁对 `dedup_window_ms`、`match_strings`、`enabled`
  或墓碑状态给出不同结论时，规则视图仍按权威规则继续，同时输出
  `E_RULE_CONFLICT`，包含冲突字段与两侧代数。
- **版本定版**：每条告警绑定其时间点生效的版本，并给出到最近版本边界的
  `version_margin_ms`。当 v1 秒量化区间与 v2 绝对毫秒生效区间重叠时，不任意
  择一，告警进入 `E_VERSION_AMBIGUOUS`，其余告警继续定版。
- **事件簇**：同一 `host_id` 内，归并键由规则键与规范化 payload digest 派生；
  告警时间不得超出当前告警所绑定版本的窗口，且进程谱系必须连通。簇代表取最早
  告警，簇 ID 由主机、归并键和完整成员集合稳定派生。
- **进程环**：沿 `process_guid -> parent_process_guid` 检测有向环，输出
  `E_PROC_CYCLE` 与规范化环序列。谱系连通性按同一父子图的无向连通分量计算。
- **窗口精度双路径**：v1 窗口使用整数毫秒定点路径；float64 参考路径使用声明
  量化上界和真实浮点减法。两路径成员关系不同时，固定路径不合并该灰区告警，
  并输出 `E_WINDOW_AMBIGUOUS`、两条路径的簇 ID、残差与累计误差界。
- **补丁失效闭包**：比较补丁前后权威视图，得到变化规则键，再沿
  `parent_rule_keys` 扩展依赖闭包；只重绑该闭包内的告警并重建相关簇。
- **全量比对**：增量结束和每次恢复后都从最终规则视图做一次独立全量归并，
  逐字段比较规则视图、绑定、簇、歧义、进程环与拒绝清单。不一致时返回
  `E_RECOMPUTE_MISMATCH`。

## 调度与持久化

- 主机任务按 `host_id` 排序后进入“工作线程数 + 待处理队列容量”的确定性准入窗口；
  超出窗口返回 `E_QUEUE_FULL`。
- 每个主机任务用 `time.perf_counter()` 实测墙钟，超过
  `per_host_timeout_ms` 返回 `E_TIMEOUT` 并丢弃该主机本轮结果；超过
  `max_alerts_per_host` 返回 `E_BUDGET`。
- 每个补丁批次的规则写入、补丁审计和代数链复验位于同一个 SQLite 事务中。
- 运行工作区使用 `.engine.lock` 串行化账本、快照与报告写入；锁冲突返回 `E_LOCK`。
- 检查点在 SQLite 中记录已应用批次、逻辑水位与簇摘要；本地 JSON 快照包含完整
  可恢复状态和校验和。恢复时从最新完整快照继续，损坏/半写快照回退并列入残差。
- 检查点结构版本或归并口径版本变化时整体重建，不增量续接。
- 检查点和诊断报告均先写同文件系统临时文件，解析与校验通过后 `os.replace`。
- 进程被强制终止时不承诺生成退出报告；已有完整快照和报告不会被半写覆盖。

## 输出

`diagnostic_report.json` 的主要字段：

- `status`、`counts`、`business_codes`
- `rejections`：来源类别、记录序号、稳定业务码、字段与详情
- `rules`：原始归一化版本、权威时间轴、冲突与代数链问题
- `bindings`：每条合法告警的版本、代数、时间余量、簇归属或未归并原因
- `clusters`：稳定簇 ID、成员、代表、窗口、误差界与边界余量
- `diagnostics`：版本歧义、窗口歧义、进程环、增量重算审计
- `patch_processing`：到达顺序、逻辑顺序、乱序标记、逐批影响集合
- `scheduler`：配置、真实耗时、主机处理/拒绝计数与错误
- `checkpoint`：恢复来源、最新快照、损坏残差和孤立临时文件

稳定业务码的完整定义见 `alert_engine/codes.py` 与 `TECHNICAL.md`。

## 当前验证结果

在本工作区实际执行：

```text
python -m pytest --collect-only -q  -> 32 tests collected
python -m pytest -q                 -> 32 passed
python -m alert_engine verify ...   -> ok=true, status=OK
```

固定样本当前结果为：24 条合法告警、6 条拒绝、15 个事件簇；增量与全量重算一致；
报告包含 `E_RULE_CONFLICT`、`E_VERSION_AMBIGUOUS`、`E_WINDOW_AMBIGUOUS`、
`E_PROC_CYCLE` 及各项解析拒绝码。详细命令、输出和恢复结果见
`VALIDATION.md`。
