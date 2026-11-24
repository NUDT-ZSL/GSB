# MemGuard：内存映射区属性守卫

离线、确定性的 Windows 主机加固组件。它读取固定种子生成的 JSONL 映射事件、区域声明、v1/v2 策略快照，合成统一裁决视图，识别只读位被追加写、可执行位被移除等越权改写，签发可回滚凭证，并通过 SQLite + 原子快照支持强杀恢复。

原始任务书保留在 `docs/TASK-SPEC.zh-CN.md`。

## 环境

- 组件运行仅使用 Python 标准库与 `sqlite3`，要求 Python 3.11+；代码使用 `from __future__ import annotations`，在当前 Python 3.10.12 环境也可运行。
- 测试使用 `pytest`，不属于运行时第三方依赖。
- 跨平台排他锁：Windows 使用 `msvcrt.locking`，POSIX/WSL 使用 `fcntl.flock`。
- 已实测：
  - `python --version`：当前 WSL 默认为 Python 3.10.12。
  - `python3.11 --version`：Python 3.11.18；`python3.11 -m compileall -q memguard` 与 `python3.11 -m memguard --help` 通过。
  - `python -m pytest -q`：`10 passed`。
  - 当前沙箱未暴露 `python.exe`/`py.exe` WSL 互操作，因此 Windows 原生锁分支未在此 Linux 内核中执行；实现已按 Win32 字节范围锁编写。

## 快速开始

```powershell
python --version
python -m pytest -q
python -m memguard generate-sample --workdir .
python -m memguard import --workdir .
python -m memguard process --workdir .
python -m memguard query --workdir . --region-id r-rdata
python -m memguard report --workdir .
python -m memguard verify-checkpoint --workdir . --full-replay
```

Windows 可直接运行完整验收脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\validate.ps1
```

默认路径：

- 样本：`samples/seed-400/`
- SQLite 账本、规范 bundle、检查点：`state/`
- JSON 报告：`reports/guard-report.json`

## CLI 子命令

| 类别 | 命令 | 作用 |
| --- | --- | --- |
| 导入 | `python -m memguard import` | 严格解析并规范化五类输入，写 `state/bundle.json` 与导入报告 |
| 处置 | `python -m memguard process` | 取排他锁、增量裁决、逐事件事务提交、签发凭证、更新检查点 |
| 查询 | `python -m memguard query` | 按 endpoint/process/region 查询 SQLite 当前保护状态 |
| 报告 | `python -m memguard report` | 导出可解析 JSON：区域结论、业务码、差异掩码、策略冲突、闭包 |
| 校验 | `python -m memguard verify-checkpoint --full-replay` | 校验半写残留、SQLite、检查点版本/水位，并用全量重放比对 |
| 回滚 | `python -m memguard rollback --credential-id CRED-E00001 --scope module` | 计算闭包；环依赖返回 `E_CRED_CYCLE` |
| 样本 | `python -m memguard generate-sample` | 生成固定种子样本与 `truth.json` |
| 锁验证 | `python -m memguard lock-hold --seconds 5` | 持有工作目录排他锁，供验证 `E_LOCK` |

退出码：

- `0`：全部成功且无输入拒绝。
- `1`：命令完成，但存在部分输入拒绝、半写隔离或其他部分失败。
- `2`：CLI 参数错误。
- `3`：`E_LOCK`，工作目录正被其他写凭证进程锁定。
- `4`：检查点、重放分歧或凭证环等完整性错误。

## 规范化口径

统一 RWX 保护位：

- `PROT_READ = 1 << 0 = 0x1`
- `PROT_WRITE = 1 << 1 = 0x2`
- `PROT_EXEC = 1 << 2 = 0x4`
- 另保留 `GUARD=0x8`、`NOCACHE=0x10` 修饰位。

三种书写口径都归一为同一整数掩码：

- 具名集合：`["read", "execute"]`、`"rx"`、`"PAGE_EXECUTE_READ"`。
- 八进制串：`"0o5"`、`"05"`。
- 整数掩码：`5`。

严格语法转换本身是精确的，因此三类解析误差上界均为 `0`；未知名称、非八进制数字、浮点掩码等直接拒绝，而不是猜测。若发送端额外给出 `precision_error_bound`，系统会沿“区域初始位 → 策略上限 → `prot_before` → `prot_after` → 观测量化”累加误差界，并计算允许集合边界余量。误差界使低端点和高端点结论不同时，输出 `E_ADJUDICATION_UNCERTAIN`，不签发凭证。

## 时间与策略合并

v1 相对启动秒只使用样本 `manifest.json` 声明的固定时钟：

```text
absolute_ms = observation_clock.boot_time_ms + relative_seconds * 1000
```

不调用系统当前时间。整秒量化区间保守表示为：

```text
[boot + s*1000, boot + (s+1)*1000)
```

因此端点宽度/误差上界为 `1000 ms`。v2 使用输入中的绝对毫秒，误差界为 `0`。

同一 `policy_key` 的候选按 `(generation, effective_at_ms, tombstone_precedence)` 选择当前时刻权威来源；墓碑在相同代次/时刻优先，并覆盖较早非墓碑记录。保护上限、`map_kinds` 或生效窗口不一致时，报告 `policy_conflicts`，同时保留 `side_a` 与 `side_b`。

## 裁决业务码

- `E_OWNER_POLICY_DENIED`：非外部来源的属主改写越过策略允许集合，例如只读区追加 `W`。
- `E_EXTERNAL_INJECTION`：`write_source` 命中 `external`、`inject`、`remote`、`WriteProcessMemory` 等外部注入口径。
- `E_ADJUDICATION_UNCERTAIN`：保护位误差界或秒/毫秒量化边界可能导致两种结论。
- `E_POLICY_TOMBSTONED` / `E_POLICY_DISABLED` / `E_POLICY_MISSING` / `E_POLICY_CONFLICT`：无法形成确定授权结论。
- `E_CRED_CYCLE`：凭证依赖图存在环。
- `E_LOCK`：排他锁冲突。
- `E_HALF_WRITE_RESIDUE`：发现未原子改名的 `.tmp` 残留。
- `E_REPLAY_DIVERGED`：恢复后的账本与确定性全量重放不一致。

每张越权凭证包含目标四元组、区域代次、应恢复掩码、策略键/代数、保护位差异掩码、来源、事件水位和凭证结构版本。

## 闭包

- 模块闭包：相同 `(endpoint_id, process_guid, module)` 的全部越权凭证。
- 进程闭包：相同 `(endpoint_id, process_guid)` 的全部凭证。
- 依赖边：区域声明中的 `rollback_depends_on_regions` 或凭证内显式 `depends_on_credentials`。
- 闭包 ID、凭证排序、SCC 环检测均排序后计算，结论与事件到达顺序无关。
- 对环内凭证请求回滚时返回 `E_CRED_CYCLE` 与环中凭证 ID；无关模块的闭包仍可回滚。

## 持久化与强杀恢复

SQLite `state/guard.sqlite` 包含：

- `meta`：格局版本、保护位口径、凭证版本、策略代数口径、输入指纹、全局水位。
- `region_state`：主键为 `(endpoint_id, process_guid, region_id, region_generation)`。
- `credentials`：完整凭证 JSON。
- `event_journal`：逐事件裁决 JSON。
- `rollback_journal`：闭包回滚记录。

每个事件在一个 `BEGIN IMMEDIATE` 事务中更新区域状态、凭证、事件日志和水位；前置校验在事务前完成，事务内异常回滚。检查点 `state/checkpoint.json` 是 SQLite 已提交状态的派生摘要，写入流程为：

1. 写 `checkpoint.json.tmp` 并 `fsync`。
2. 重新解析并校验 SHA-256 与版本。
3. `os.replace()` 原子替换。
4. fsync 目录。

强杀恢复边界：

- 已提交 SQLite 凭证是最后完整凭证点；检查点落后时由 SQLite 重建。
- `checkpoint.json.tmp` 会被 `verify-checkpoint` 判为 `E_HALF_WRITE_RESIDUE`；`process` 取锁后将其移动到 `state/quarantine/`，不会当作有效状态。
- 格局版本或输入中的区域/策略代次指纹变化时，旧库和旧检查点整体归档到 `state/archive/rebuild-*`，随后全量重建，避免新旧口径混用。
- 普通恢复只裁决 `event_seq > last_event_seq` 的事件，不重新处理已提交水位。
- `verify-checkpoint --full-replay` 在内存中做一次确定性全量裁决，并比较事件结论、凭证集合和模块闭包；分歧返回 `E_REPLAY_DIVERGED`。

## 确定性样本

`samples/seed-400/truth.json` 为每个有效事件记录：

- `sync_marker`
- 发送端原始 `sender_raw_prot_before` / `sender_raw_prot_after`
- 期望结论、业务码、是否签发凭证、恢复掩码

测试只通过 `sync_marker` 关联真值与组件输出，不使用 `event_seq` 做期望连接。样本覆盖：

- 只读追加写、可执行位移除、合法降级。
- 具名集合、八进制、整数混写。
- v1 秒到 v2 毫秒换算。
- v2 墓碑覆盖 v1 宽松策略。
- 两代策略冲突双依据保留。
- 精度误差导致的不确定记录。
- 模块/进程闭包与依赖环。
- 故意构造的 JSON、字段、区域重复、策略时间/代数非法记录。
- SIGKILL + 半写快照恢复。
- 排他锁冲突。

## 实测结果摘要

```text
$ python -m pytest -q
..........                                                               [100%]
10 passed in 0.87s

$ python -m memguard import --workdir .
events_valid=10 regions_valid=10 policy_candidates=27
policy_keys=9 policy_conflicts=3 rejections=17

$ python -m memguard process --workdir .
final_watermark=10 adjudicated_events=10 credential_count=7

$ python -m memguard verify-checkpoint --workdir . --full-replay
business_code=E_OK consistent=true credentials=7
```

强杀恢复测试中，故障钩子在第 2 张凭证提交后、快照原子改名前发送 `SIGKILL`：

```text
crash exit: -9/137
verify half-written tmp: E_HALF_WRITE_RESIDUE, exit 4
recovery: start_watermark=2 adjudicated=8 skipped=2 credential_count=7
full replay verification: E_OK consistent=true
```

锁冲突：

```text
holder: python -m memguard lock-hold --state-dir <state> --seconds 5
second writer: E_LOCK, exit 3
```

## 代码地图

- `memguard/parsers.py`：严格输入解析与拒绝记录。
- `memguard/precision.py`：保护位规范化、误差界和边界路径。
- `memguard/policy.py`：两代策略合并、冲突和时间选择。
- `memguard/engine.py`：统一裁决与凭证内容。
- `memguard/closure.py`：进程/模块/区域闭包与 SCC 环检测。
- `memguard/storage.py`：SQLite schema、事务、原子快照、恢复核对。
- `memguard/service.py`：用例编排。
- `memguard/cli.py` / `memguard/__main__.py`：命令行。
- `memguard/samplegen.py`：固定种子样本生成器。
- `tests/test_guard.py`：pytest 验收测试。
