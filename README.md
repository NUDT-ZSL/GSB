# 会话固定离线验证组件

这是一个不依赖真实账号、外部 IdP 或网络的确定性验证工具。它读取固定种子样本，严格解析会话声明、登录事件和两代轮换策略，执行会话固定/轮换/撤销裁决，并用 SQLite 账本、检查点和本地快照保证强制终止后的恢复边界。

运行期只使用样本 `manifest.json` 中声明的观测时钟，不读取系统当前时间，也不发起网络请求。

## 环境

- Python 3.11。本项目已在 `Python 3.11.18` 下验证。
- SQLite 使用 Python 标准库 `sqlite3`。
- 测试使用 pytest。

Linux/WSL：

```bash
python3.11 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install pytest
.venv/bin/python -m pip install -e .
.venv/bin/python -m pytest
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest
```

安装后也可直接使用 `sessionfix` 控制台命令；未安装时可用 `PYTHONPATH=src python -m sessionfix`。

## 固定样本

```bash
python tools/generate_fixed_sample.py --output samples/fixed-seed --seed 20261125
```

默认样本的观测时钟为固定值（当前种子下为 `1893456000000`），生成器内部随机源只使用给定种子。同一 Python/SQLite 版本下，JSON 样本内容与 SQLite 元数据内容可重复。

样本结构：

```text
fixed-seed/
├── manifest.json
├── sessions.json
├── events.json
└── policies/
    ├── v1.json
    └── v2/
        ├── index.json
        ├── shard-000.json
        ├── shard-001.json
        └── policy_meta.sqlite3
```

### 会话声明

`sessions.json` 可包含 `{"sessions": [...]}` 或会话数组。关键字段：

```json
{
  "session_id": "sf-vuln-0000",
  "user_id": "user-vuln",
  "issued_at_ms": 1893455099000,
  "expires_at_ms": 1893459600000,
  "device_id": "device-vuln",
  "generation": 0,
  "parent_session_id": null,
  "window_start_ms": 1893454800000,
  "window_end_ms": 1893459600000,
  "policy_key": "default"
}
```

解析器校验会话标识唯一、时间类型与大小关系、设备与用户标识、代数和前代链。合法前代必须满足相同用户、`parent.generation == generation - 1`、子会话不早于父会话。非法记录写入拒绝清单并继续处理其他记录。

### 登录事件

`events.json` 可包含 `{"events": [...]}` 或事件数组。关键字段：

```json
{
  "event_seq": 1,
  "session_id": "sf-vuln-0000",
  "event_time_ms": 1893455400000,
  "attempt_result": "success",
  "privilege_escalated": true,
  "event_type": "login",
  "user_id": "user-vuln",
  "device_id": "device-vuln"
}
```

事件先按内容稳定排序再编号，因此仅改变 JSON 数组顺序不会改变拒绝清单。最终裁决按 `(event_time_ms, event_seq, ...)` 的稳定键计算，不依赖到达顺序。

若事件未提供 `event_time_ms`，解析器使用样本观测时钟加事件序号作为逻辑毫秒时刻（`observed_clock_ms + event_seq`），仍不读取系统时间。

### v1 策略

`policies/v1.json`：

```json
{
  "records": [
    {
      "policy_key": "default",
      "relative_start_seconds": 0,
      "rotation_mode": "enforce",
      "min_rotation_interval_seconds": 120,
      "bind_device_required": true,
      "policy_generation": 1
    }
  ]
}
```

v1 的 `relative_start_seconds` 只按样本 `manifest.observed_clock_ms` 换算为绝对毫秒，不使用系统时间。

### v2 策略

`policies/v2/index.json` 引用分片和 SQLite 元数据库。工具会校验分片 SHA-256 与记录数：

```json
{
  "metadata_db": "policy_meta.sqlite3",
  "shards": [
    {"path": "shard-000.json", "sha256": "...", "record_count": 2}
  ]
}
```

分片记录：

```json
{
  "policy_key": "default",
  "effective_at_ms": 1893455999000,
  "policy_generation": 2,
  "rotation_mode": "enforce",
  "min_rotation_interval_seconds": 120,
  "bind_device_required": true,
  "tombstone": false
}
```

SQLite 中的 `policy_metadata` 与 `policy_shards` 是 v2 元数据权威索引，工具以只读 URI 打开。

## 合并规则

- 策略键无大小写差异并按去空白、小写规范化。
- 同一键依次按 `(policy_generation, effective_at_ms, tombstone, source, record_index)` 裁决权威记录。
- 较新墓碑覆盖较早非墓碑记录。
- v1 与 v2 最新活动记录对 `rotation_mode` 或 `binding_required` 不一致时，字段变为不可判定，输出 `E_POLICY_CONFLICT` 和双方原记录，不静默选择一方。
- v2 缺失最小轮换间隔时，从同键较早策略取保守的最大值。

## 业务码

- `E_SESSION_FIXATION`：既有会话登录成功且权限提升后没有后继轮换，`reused_session_id` 给出原会话。
- `E_ORPHAN_SESSION`：后继会话已建立后，旧会话仍被事件引用。
- `E_ROTATE_TOO_FAST`：同一父会话两次轮换间隔小于合并策略最小值。
- `E_SESSION_CYCLE`：前代图存在环，列出环中会话标识。
- `E_BINDING_MISMATCH`：策略要求设备绑定但事件设备不一致。
- `E_REVOCATION_INVALIDATED`：会话位于撤销传播闭包中。
- `E_POLICY_CONFLICT` / `E_POLICY_TOMBSTONED` / `E_POLICY_UNKNOWN`：策略不可直接执行。
- 解析拒绝码使用 `E_SESSION_*`、`E_EVENT_*`、`E_POLICY_V1_*`、`E_POLICY_V2_*` 等前缀。

`repair_state` 取值包括 `fixed`、`vulnerable`、`orphaned`、`rate_limited`、`revoked`、`inactive`、`indeterminate`、`clean`。`fixed` 表示权限提升登录之后出现了合法后继轮换；`vulnerable` 表示固定缺陷或绑定失败。

## CLI

五类核心子命令：

```bash
# 1. 导入并严格解析
python -m sessionfix import --sample samples/fixed-seed --workdir run

# 2. 固定缺陷裁决（每批写检查点）
python -m sessionfix adjudicate --workdir run --batch-size 2

# 3. 撤销会话或登录事件，计算传递失效闭包
python -m sessionfix revoke --workdir run --session revoke-a
python -m sessionfix revoke --workdir run --login-seq 3

# 4. 导出 JSON 诊断报告
python -m sessionfix report --workdir run --output run/diagnostic.json

# 5. 校验最后一个完整检查点与半写残留
python -m sessionfix verify-checkpoint --workdir run
```

另有固定种子生成命令：

```bash
python -m sessionfix generate --output samples/custom --seed 20261125
```

`adjudicate --sample PATH` 可在策略格局变化时重新导入并整体重建账本；`--restart` 从零重新裁决。多样本目录可批量 `import`，每个样本使用 `workdir/samples/<name>/` 独立账本。

退出码：

- `0`：全部成功。
- `1`：部分样本失败、解析拒绝、策略冲突/环、锁冲突、检查点校验失败等可诊断失败。
- `2`：命令行参数错误。

所有命令输出可解析 JSON。报告、快照和检查点快照均先写同目录临时文件，完成 `fsync`、重新解析校验后再用 `os.replace` 原子替换。

## 账本与恢复

主键为 `(session_id, generation)`，记录：

- 修复状态；
- 依据策略代数；
- 设备绑定结论；
- 判定水位；
- 后继边、业务码、拒绝项、策略冲突与撤销结果。

每轮裁决在一个 `BEGIN IMMEDIATE` 事务中整体替换派生账本。策略布局摘要（轮换模式、绑定口径、最小间隔、墓碑/代数）变化或样本摘要变化时，删除旧账本与旧检查点后整体重建，不混合旧策略结论。

检查点包含已处理事件水位、批次号和完整账本快照。恢复时：

1. 只选择最后一个快照可解析、文件摘要一致、账本摘要一致且可通过样本重放比对的检查点。
2. 若数据库领先于检查点（例如检查点写入前中断），回滚到该检查点后继续。
3. `E_REPLAY_MISMATCH` 识别“文件/数据库哈希都自洽但语义被篡改”的半写或损坏。
4. 临时文件残留在 `verify-checkpoint` 中报告为 `E_HALF_WRITE`，不破坏既有账本。
5. 强制终止时不写退出报告；只有完整恢复并完成全部批次后才原子生成报告。

同一工作目录的写操作使用跨平台文件锁。POSIX 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`；冲突返回 `E_LOCK`，且不会进入 SQLite 写事务。

## 实测结果

运行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tools/run_acceptance.py --output artifacts/acceptance-run.json
```

本次结果：

- pytest：`16 passed`
- Python：`3.11.18`
- 固定样本：`sessionfix-fixed-20261125`，种子 `20261125`，观测时钟 `1893456000000`
- `E_SESSION_FIXATION`：`sf-vuln-0000`
- 修复成功会话：`sf-fixed-0000`（权限提升后存在后继轮换，状态 `fixed`）
- `E_ORPHAN_SESSION`：`orphan-0000`
- `E_ROTATE_TOO_FAST`：`fast-base`
- 撤销 `revoke-a` 的传递闭包：`revoke-a, revoke-b, revoke-c`
- 强制终止：退出码 `137`，未生成报告
- 恢复后：会话结论、后继关系、账本摘要与未中断运行完全一致
- 最终 `verify-checkpoint`：`ok=true`，`half_write_detected=false`

完整机器可读验收输出见 `artifacts/acceptance-run.json`。
