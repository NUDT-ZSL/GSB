# PDRE 终端进程行为策略判定引擎交付说明

本交付是一个纯本地 Python 3.11 工程，不依赖 Docker、外部服务、真实账号或网络。固定种子脚本会生成事件 JSONL、谱系声明、v1/v2 策略声明以及带同步标记的发送端真值文件。

## 1. 环境与样本

Linux/macOS/WSL：

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pdre.cli generate-sample --out samples/demo
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pdre.cli generate-sample --out samples\demo
```

固定依赖为 `numpy==1.26.4` 与 `pytest==8.2.2`。仓库已在 Python 3.11.18 虚拟环境中完成验证。

生成内容：

- `samples/demo/events.jsonl`：有效事件以及字段非法、JSON 半行损坏记录。
- `samples/demo/lineage.jsonl`：根树、会话间分叉、任意长度环、父子自引用、缺失父声明和非法声明。
- `samples/demo/policies_v1.jsonl`：相对观测时钟秒、扁平摘要匹配串、动作码。
- `samples/demo/policies_v2.jsonl`：绝对毫秒时刻、规范键、规则代数、墓碑。
- `samples/demo/sample_config.json`：样本内唯一观测时钟 `observed_clock_ms` 和摘要算法。
- `samples/demo/truth/sender_truth.jsonl`：发送端原始字段、原始行摘要和稳定同步标记。

## 2. 运行与测试

```bash
python -m pdre.cli run \
  --input-dir samples/demo \
  --report reports/report.json \
  --checkpoint-dir state/checkpoints \
  --snapshot-dir state/snapshots \
  --ledger state/ledger.jsonl \
  --batch-size 8
```

固定样本故意包含非法记录、谱系异常和策略冲突，因此命令按约定返回退出码 `3`；可解析 JSON 报告中包含全部业务码。干净输入返回 `0`，命令行参数错误返回 `2`。

顺序无关验证：

```bash
python -m pdre.cli verify --input-dir samples/demo --iterations 50
pytest -q
```

一键验收脚本：

```bash
bash scripts/acceptance.sh
```

Windows 初始化脚本：`scripts/setup-windows.ps1`。

## 3. 代码结构

- `pdre/generator.py`：固定种子、numpy 确定性风险序列、UUID5 GUID 和 SHA-256 摘要样本。
- `pdre/parsing.py`：严格 JSONL/字段解析、来源类别与行号拒绝、v1 秒到毫秒换算。
- `pdre/canonical.py`：规范策略键、动作码、摘要位宽和稳定 JSON 摘要。
- `pdre/lineage.py`：父边图、根/缺失/自引用/环/分叉分类、候选根和反向子树。
- `pdre/policies.py`：两代策略统一视图、权威来源、墓碑覆盖和跨代冲突。
- `pdre/judgment.py`：窗口/摘要命中、稳定裁决、真实墙钟超时和事件预算。
- `pdre/runtime.py`：可配并发与有界单元队列。
- `pdre/engine.py`：全量分析、规范化报告和语义差异比较。
- `pdre/incremental.py`：策略键到事件到单元、谱系反向子树的增量闭包。
- `pdre/storage.py`：跨平台文件锁、原子 JSON、哈希链账本、SQLite 与快照恢复。
- `pdre/pipeline.py`：输入装载、稳定批次、检查点续跑和 SIGKILL 故障注入。
- `tests/`：解析、谱系、策略、增量、顺序无关、预算、锁、检查点和子进程强杀恢复测试。

## 4. 关键口径

### 解析和时钟

- v1 的绝对开始/结束毫秒唯一换算公式为 `sample.observed_clock_ms + effective_*_sec * 1000`，不读取系统时间。
- `observed_ms`、`event_seq`、规则代数等严格拒绝布尔值冒充整数。
- 摘要支持 `md5`、`sha1`、`sha256`、`sha384`、`sha512` 前缀；无前缀时使用样本声明算法。十六进制字符数必须等于算法位宽 / 4。
- 每条字段错误均输出 `source_category`、`source_name`、`record_seq`、业务码和字段信息；坏行不会中断后续行或其他文件。

### 谱系单元

- 权威边来自谱系声明：`process_guid -> parent_process_guid`。
- 显式 `"parent_process_guid": null` 表示根声明。
- 被引用但没有谱系声明的 GUID 进入 `E_LINEAGE_MISSING_PARENT`，并作为临时终止根保留事件，避免丢数。
- 自引用返回 `E_LINEAGE_SELF_REFERENCE`；长度大于 1 的环返回 `E_LINEAGE_CYCLE`，`cycle_guids` 以最小 GUID 为规范起点并闭合物化。
- 同一进程存在多个不同父选项时返回 `E_LINEAGE_FORK`，列出会话、来源和全部可达候选根；分叉自身及其下游子树进入 `unit:fork:<guid>`，不会被静默判给任一候选根。

### 策略合并和裁决

- 规范键采用 NFKC、去首尾空白、`casefold` 和空白折叠。
- 权威来源按 `(rule_generation desc, effective_start_ms desc, effective_end_ms desc, content_hash)` 稳定选择；同内容重复声明不影响结果。
- 权威记录为墓碑时策略不进入活动视图，并在 `tombstone_overrides` 保留被覆盖的较早非墓碑依据。
- 两代在 `action_code`、`match_string` 或 `effective_window` 上任一不同即进入 `policy_conflicts`，两侧均保留规则代数和 `source_ref`。
- 命中条件只有两个：`start_ms <= observed_ms < end_ms`（无结束时间则右端开放），且规范化匹配摘要等于事件 `payload_digest`。
- 唯一结论按 `rule_generation desc`、`action_code desc`、`canonical_policy_key asc`、`process_guid asc`、`observed_ms asc`、`event_seq asc` 稳定产生；其余命中均为 `suppressed`。

### 增量闭包

- 策略变更先取规范策略键，再通过事件显式 `policy_key` 索引和新旧匹配摘要索引找到事件，最后映射到判定单元。摘要索引可避免错误信任事件自报键。
- 谱系变更对变更 GUID 在旧图和新图分别求反向子树，覆盖所有可能从根单元变为分叉单元或反向合并的进程。
- 只把受影响且仍存在的单元提交给判定器；未受影响单元从缓存复用。报告中的 `incremental.recomputed_unit_ids` 与 `reused_unit_ids` 可审计。
- 每次增量结果都可与从零全量结果做语义比较；不一致时写 `E_REJUDGE_MISMATCH`。

### 顺序无关

所有输出列表均按规范键、GUID、来源或内容摘要排序；不依赖字典插入顺序。`pdre verify` 会用固定随机种子交错打乱全部事件、谱系和策略记录，并随机切分批次，要求所有轮次得到同一个 `semantic_digest`。

### 检查点和恢复

- SQLite：`checkpoints.sqlite3` 保存版本、水位、批次号、账本条目数/字节偏移/摘要/尾哈希、快照摘要和完整状态 JSON。
- 快照：先写同目录 `.<name>.*.tmp`，执行 `json.load` 校验后 `os.replace` 原子替换。
- 账本：`ledger.jsonl` 为哈希链 JSONL；检查点保存已提交前缀的 SHA-256 和字节偏移。
- 恢复时读取最后一条完整 SQLite 检查点，校验快照摘要、重放哈希链和账本前缀摘要；检查点之后的半写行/未提交条目按偏移截断，并在 `checkpoint_recovery.residue` 报告。
- 检查点结构版本或合并口径版本不一致时返回 `E_VERSION_MISMATCH`，管线归档旧账本并整体重建。
- `--fault residue-then-kill-after-first-checkpoint` 会在首个完整检查点后追加半写账本和 `.tmp` 快照残留，再发送 `SIGKILL`（Windows 上使用可用的强制终止信号路径），用于验证真实强杀恢复。

### 资源预算和锁

参数：

- `--concurrency`
- `--unit-timeout-ms`
- `--max-queue-size`
- `--max-events-per-unit`
- `--lock-timeout-ms`

超时使用 `time.perf_counter()` 真实墙钟，在事件间和策略间检查截止时间；超时单元返回 `E_TIMEOUT` 并停止处理剩余事件。事件数超限返回 `E_BUDGET`。有界队列满时立即返回 `E_QUEUE_FULL` 并计入报告。检查点锁使用 Linux `fcntl.flock(LOCK_EX | LOCK_NB)` 或 Windows `msvcrt.locking`；冲突返回 `E_LOCK`。

## 5. 主要业务码

| 类别 | 业务码 |
| --- | --- |
| 解析 | `E_JSON_DECODE`, `E_RECORD_NOT_OBJECT`, `E_FIELD_*`, `E_CLOCK_REFERENCE_MISSING`, `E_INPUT_READ` |
| 谱系 | `E_LINEAGE_MISSING_PARENT`, `E_LINEAGE_SELF_REFERENCE`, `E_LINEAGE_CYCLE`, `E_LINEAGE_FORK` |
| 策略 | `E_POLICY_CONFLICT`, `W_POLICY_TOMBSTONE_OVERRIDE` |
| 增量/恢复 | `E_REJUDGE_MISMATCH`, `E_LEDGER_REPLAY`, `E_VERSION_MISMATCH`, `E_LOCK` |
| 资源 | `E_TIMEOUT`, `E_BUDGET`, `E_QUEUE_FULL` |

## 6. 实际验证

最新验证命令与结果记录在 `docs/VERIFICATION.md`。
