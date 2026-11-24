# 互动叙事推演组件技术说明

## 1. 交付范围

本仓库包含一个可集成的 Python 推演组件、固定种子样本生成器、命令行示例和 `unittest` 回归测试。运行时只依赖 `numpy>=1.26`，不需要数据库、容器、网络服务或外部账号。

主要模块：

- `narrative_engine/schema.py`：JSON 剧本、状态声明、选择序列解析与 `E_SCHEMA` 校验。
- `narrative_engine/fixedpoint.py`：`Decimal` 量化、定点整数和 NumPy `float64` 转换。
- `narrative_engine/world.py`：世界状态、定点条件判定、副作用定义域检查和 float64 参考轨迹。
- `narrative_engine/engine.py`：确定性事件选择、副作用提交、因果链和中断控制器。
- `narrative_engine/branches.py`：全分支枚举、选择序列批量推演、死锁归并。
- `narrative_engine/checkpoint.py`：原子快照、SHA-256 摘要、版本归档和最近有效快照恢复。
- `narrative_engine/concurrency.py`：有界线程池、在途内存预算和分块错误取消。
- `narrative_engine/generator.py`：固定种子 `351` 的本地样本生成器。
- `narrative_engine/cli.py`：`generate-sample`、`validate`、`run`、`explore`、`verify-snapshot` 命令。
- `tests/test_narrative_engine.py`：19 个验收与韧性测试。

## 2. Windows 快速开始

要求 Windows 10/11 和 Python 3.11 或 3.12。当前实现也兼容 Python 3.10，实际验证环境见 `docs/VALIDATION.md`。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\scripts\run_demo.ps1
```

也可以手动执行：

```powershell
python -m narrative_engine.cli generate-sample --out samples --seed 351
python -m narrative_engine.cli validate --script samples\story.json --choices samples\choice_sequence.json
python -m narrative_engine.cli run --script samples\story.json --choices samples\choice_sequence.json --state-dir runtime\state --report runtime\run_report.json
python -m unittest discover -s tests
```

`samples/story.json` 通过 `state_schema_ref` 引用独立状态声明；`samples/story_bundle.json` 是内嵌状态声明的单文件版本，二者均由生成器产出。

## 作为 Python 组件集成

```python
from narrative_engine import load_script, run_choice_sequence
from narrative_engine.reporting import build_run_report, verify_report_correspondence

script = load_script("samples/story_bundle.json")
engine = run_choice_sequence(
    script,
    ["choice_code_vault", "choice_vault_accept_light"],
)
report = build_run_report(script, engine)
assert verify_report_correspondence(report, script) == []
print(report["world_state_exact"], report["first_residual_exceedance"])
```

分支预计算使用 `narrative_engine.branches.explore_branches()`；需要持久化时把 `CheckpointStore.save_snapshot(engine.snapshot_body(run_fingerprint))` 作为 `on_event` 回调传入 `run_choice_sequence()`。

## 3. 剧本格式

顶层字段：

```json
{
  "format": "interactive-narrative/1",
  "version": "sample-1.0.0",
  "fixed_point": {"scale": 1000000, "residual_tolerance": 1e-16},
  "state_schema": {},
  "initial_state": {},
  "start_node": "node_arrival",
  "nodes": []
}
```

状态类型：

- `relation` / `number` / `fixed`：定点数值，必须声明有限的 `min`、`max`、`initial`。
- `integer` / `int` / `count`：精确整数。
- `boolean` / `bool` / `flag` / `clue` / `item`：布尔状态；`item` 表示是否持有，`clue` 表示是否揭示。

条件示例：

```json
{"key": "trust", "op": ">=", "value": 0.8}
```

支持 `==`、`!=`、`>`、`>=`、`<`、`<=` 及 `eq`、`gte` 等别名。

副作用示例：

```json
{"key": "trust", "op": "add", "value": 0.1}
```

数值操作支持 `add`、`subtract`、`set`；布尔操作支持 `set`、`toggle`。所有运行时累加后的值仍必须处于声明定义域内。

校验覆盖：

- 场景节点 ID 唯一且起始节点存在。
- 事件和选择引用的状态键均已声明。
- 选择目标节点存在。
- 关系值、阈值、初始值和副作用量均为有限数。
- 数值阈值和 `set` 值位于声明定义域。
- 选择序列中的 ID 存在且按当前节点路由合法。

非法输入由 CLI 输出：

```json
{"ok": false, "error": {"code": "E_SCHEMA", "message": "...", "details": {}}}
```

退出码为 `2`。解析和完整校验发生在创建状态目录或报告文件之前，因此非法输入不会产生输出文件。

## 4. 定点数学与 float64 对照

剧本中的十进制字面量通过 `json.load(..., parse_float=Decimal)` 保留为 `Decimal`，再按 `fixed_point.scale` 以 `ROUND_HALF_UP` 量化为整数定点值。以 `scale=1_000_000` 为例：

- `0.1 -> 100000`
- `0.8 -> 800000`

所有条件比较和副作用累加均使用定点整数。float64 参考路径从同一个量化值转换得到，并按事件、按副作用声明顺序用 `numpy.float64` 累加；它只用于诊断，不参与最终事件选择或终局状态。

样本包含 8 个各增加 `trust +0.1` 的事件。定点结果精确等于 `800000`，而连续 float64 累加为 `0.7999999999999999`。因此：

- 定点条件 `trust >= 0.8`：通过。
- 报告中的 float64 反事实判定：不通过。
- 首次超过 `1e-16` 残差容差的位置：第 `8` 个事件 `evt_echo_trust_08`、状态键 `trust`、节点 `node_arrival`，绝对差为 `1.1102230246251565e-16`。

每份事件记录的 `fixed_float_residuals` 列出当时所有定点数值键的残差；顶层报告给出 `first_residual_exceedance` 和 `residual_exceedances`。

## 5. 确定性推演语义

进入节点后，内核重复执行以下过程直到没有可触发事件：

1. 将节点事件按 `(-priority, declaration_index)` 稳定排序。
2. 选择排序后第一个未消费的 `once` 事件中前置条件全部满足的事件。
3. 在候选状态上按声明顺序应用全部副作用并检查定义域。
4. 全部副作用成功后提交状态、float64 参考轨迹、真实事件记录和因果链。
5. 每个事件成功后触发快照回调；若收到终止请求，则在下一事件边界退出。

默认事件 `once=true`，避免叙事事件重复触发；显式配置 `once=false` 时受 `--max-events` 资源上限保护。选择本身也可带条件和副作用，但示例中主要通过分支后的真实事件改变状态，从而保证关键变化均有事件因果指针。

同一剧本和同一选择序列的报告不含时间戳；对最终报告做规范 JSON SHA-256 摘要可逐字段比较世界状态、事件集合和执行顺序。

## 6. 因果链

每个实际变化的状态键生成一条因果记录，包含：

- `change_seq`：状态变化序号。
- `event_seq` 与 `event_ref: "event:<seq>"`：指向本次运行中真实追加的事件记录。
- `event_id`、`node_id`、状态键和前后定点值。
- `satisfied_conditions` / `fixed_judgements`：被满足的前置条件、实际定点值和期望定点值。
- `upstream_choices`：决定该事件可达的上游选择 ID。
- `node_sequence`：到达该事件的节点序列。

`verify_report_correspondence(report, script)` 会从初始声明和事件效果日志重建状态，检查因果指针、效果前后值、事件状态摘要和终局摘要一致。CLI 成功运行时会自动执行该校验。

## 7. 分支、终局统计和死锁

`explore_branches()` 从起始状态克隆分支，穷举所有启用选择，并受 `max_depth`、`max_paths` 限制。每条路径报告：

- `choice_ids`、`node_sequence`、`node_count`、`unique_node_count`。
- `event_count`、`execution_order`。
- `world_state_exact`、十进制字符串和状态摘要。
- `deadlocked`、`terminal`、`truncated`。
- 定点状态快照和可行性列表。

节点事件稳定到无新事件后，如果节点不是显式结局且没有任何启用选择，则该路径标记为死锁。节点级汇总按状态摘要归并；只有该节点的所有可达状态均无可行继续方式时，才放入 `deadlock_nodes`。最终路径按 `(choice_ids, node_sequence)` 规范排序，因此反向遍历选择不会改变死锁节点集合或状态摘要。

## 8. 快照、版本和恢复

每次事件成功后，CLI 调用 `CheckpointStore.save_snapshot()`：

1. 在同一状态目录用 `tempfile.mkstemp()` 创建隐藏临时文件。
2. 写入并 `flush + fsync`。
3. `os.replace()` 原子替换 `snapshot.json`。
4. 同时写不可变序号文件 `snapshots/snapshot-00000001.json` 等，用于损坏回退。

快照体包含世界状态、`event_seq`、定点标度、`version_id`、运行指纹、事件记录、因果链和残差日志。`digest` 是除 `digest` 字段外规范化 JSON 的 SHA-256。

`version_id` 由规范化后的剧本节点、条件、效果、状态声明、初始状态和定点配置计算：

```text
ver-<sha256 prefix 16>
```

恢复规则：

- 摘要、JSON、`event_seq == len(event_records)` 或记录序号不匹配：该快照判定为 `E_DIGEST`。
- 当前 `snapshot.json` 失效：按事件序号从新到旧扫描 `snapshots/snapshot-*.json`，回退到最近的同版本、同选择序列有效快照。
- 没有任何有效快照且曾出现摘要错误：返回 `E_DIGEST`，退出码 `3`。
- `version_id` 不一致：归档旧状态后从头推演。
- 额外的 `run_fingerprint` 绑定选择序列，避免同剧本但不同选择序列误恢复。

可手动验证摘要：

```powershell
python -m narrative_engine.cli verify-snapshot --path runtime\state\snapshot.json
```

## 9. 中断和强制终止

CLI 安装 `SIGINT`、`SIGTERM`，Windows 上若可用也安装 `SIGBREAK`。信号处理函数只设置 `StopController` 标志，不直接写状态。内核在事件边界检查标志；当前事件的全部副作用成功并完成原子快照后，才写入 `interrupt.json` 并以约定退出码 `130` 退出。

确定性测试钩子：

```powershell
python -m narrative_engine.cli run ... --stop-after-events 3
```

外部信号示例（WSL/POSIX）：

```bash
python -m narrative_engine.cli run ... --event-delay-ms 250 &
kill -TERM $!
```

Windows 可在控制台使用 `Ctrl+C` 或 `Ctrl+Break`；自动化场景建议使用 `--stop-after-events`，或用 `subprocess.CREATE_NEW_PROCESS_GROUP` 后发送 `CTRL_BREAK_EVENT`。

若进程被 `SIGKILL`/任务管理器强制结束，来不及写中断标记或退出报告也没关系：临时文件不会成为正式快照，`os.replace()` 保证当前快照要么是旧有效版本，要么是新完整版本；重启时按摘要和序号恢复。

## 10. 并发预计算和资源上限

`run_bounded_chunks()` 使用 `ThreadPoolExecutor(max_workers=N)`，同时：

- 最多容纳 `N` 个执行中 future。
- 按每个 `WorkChunk.estimated_bytes` 预留内存，超过 `memory_budget_bytes` 时等待或拒绝超大分块。
- 任一分块异常后设置共享取消事件、取消排队 future，并抛出第一个分块错误。

CLI 示例：

```powershell
python -m narrative_engine.cli explore `
  --script samples\story.json `
  --choice-sequences samples\choice_sequences.json `
  --workers 4 --chunk-size 2 --memory-budget-mb 256 `
  --report runtime\branch_report.json
```

分支规模由 `--max-depth`、`--max-paths` 限制，单路径事件数由 `--max-events` 限制。
