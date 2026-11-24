安全团队只拿到一份目标描述，没有现成系统与样本，需要在 Windows 本地工作区从零实现一个进程隔离与能力限制组件，并以可集成库加完整示例交付。业务对象是由本地确定性脚本按固定种子生成的沙箱策略清单：进程画像声明可执行路径、启动参数占位、所需能力集合（文件读、文件写、网络出站、子进程派生、符号链接跟随等具名能力及对应的整数位掩码），能力授予声明以两种口径并存书写——v1 为相对过期秒、扁平授权列表与无依赖声明，v2 为分片记录，携带绝对毫秒生效时刻、授权代数、依赖的其他授权键与墓碑标记。组件需把两代授予合并为统一能力视图，在策略以补丁批次增量更新时计算撤销闭包与受影响在途进程，并让账本与快照在中断恢复后仍可复现。核心难点在于：撤销必须沿授权依赖边与进程派生边同时传播，而两代口径的时间基准与代数语义不同，任何一处裁决若依赖到达顺序，同一批补丁就会得出不同的进程存活结论。

任务环境与准备：
Windows 10 或以上，安装 Python 3.11（python.org 安装包，安装时勾选 Add python.exe to PATH）。
执行 python -m pip install pytest==8.2.0 安装测试依赖；SQLite 使用 Python 标准库 sqlite3，无需额外安装。
在空工作区创建项目目录结构；固定种子样本生成器与生成后的样本目录由题目实现方提供，运行前先执行一次生成命令产出本地样本。
运行测试使用 python -m pytest -q；命令行入口以 python -m 模块方式在项目根目录执行。

业务要求：
1. 实现进程画像、两代能力授予、补丁批次三类的解析规范化层：校验进程画像路径语法与标识唯一性、能力位掩码与具名能力集合的对应关系、v1 相对过期秒为非负整数、v2 绝对毫秒时间戳与授权代数为非负整数、依赖的授权键可解析，对非法记录输出含来源类别与记录序号及稳定业务码的拒绝清单并继续处理其余记录。
2. 把两代授予合并为统一能力视图：v1 的相对过期秒须以样本声明的观测时钟为唯一基准换算为绝对毫秒，不得使用系统当前时间；同一授予键按授权代数与生效时刻裁决权威来源，墓碑标记必须覆盖较早的非墓碑记录；两代对同一授予的能力集合或过期时刻结论不一致时必须显式报告冲突字段与两个来源版本，不得静默取其一。
3. 实现补丁批次的事务式增量应用：每个补丁批次在单个 SQLite 事务内完成，前置校验失败不得改动能力账本任何状态；批次内操作需支持新增授予、缩减能力、设置依赖与墓碑化，并记录每条操作的应用水位与规范键，供后续失效判定与重放使用。
4. 实现撤销闭包计算：当某授予被墓碑化或能力被缩减时，沿授权依赖边计算直接与传递依赖它的授予集合，再沿进程画像到派生进程的派生边映射到受影响在途进程；遇到授权依赖环或进程派生环须返回 E_DEP_CYCLE 并列出环中规范键，环存在时不得给出不完整的撤销集合。
5. 保证撤销与回活结论与补丁到达顺序无关：对同一组补丁批次，任意合法应用顺序下最终有效能力位、撤销闭包集合、受影响进程清单与拒绝清单必须完全一致；时刻或代数相同时须以代数与规范键稳定排序确定次序，不得依赖字典遍历或到达顺序，报告中各项须按稳定键归一化输出。
6. 实现检查点、版本识别与强制终止后的恢复边界：以已应用补丁水位与能力账本摘要持久化检查点到 SQLite 与本地快照文件，恢复时只从最后一个完整检查点继续并重放校验账本一致性以识别半写残留；检查点结构版本、能力白名单版本或合并口径版本变化时须整体重建而非增量续接；进程被强制终止后不承诺生成退出报告，只允许恢复到最后一个完整检查点。
7. 实现原子快照整体替换与并发写隔离：检查点与裁决报告均先写临时文件，校验内容可解析后再整体重命名替换；同一工作目录并发写检查点须以文件锁串行化，锁冲突返回 E_LOCK 且不得破坏既有账本；快照整体替换须识别能力账本版本变化，旧版本快照不得与新补丁混合生效。
8. 交付可交互命令行诊断工作台与确定性本地验证：提供导入补丁、查询进程有效能力、导出撤销闭包报告、校验检查点四类子命令，并展示统一能力视图、撤销闭包链与冲突明细；单份输入解析失败不得中断同批其他输入，退出码区分全部成功、部分失败与参数错误，业务码统一写入可解析 JSON 报告；附固定种子样本生成器与 pytest 测试，样本内须显式写入观测时钟、授权代数与方法同步标记，验证只依据这些标记与规范化结果比较。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
使用本地固定样本验证统一能力视图、撤销闭包顺序无关性与检查点恢复，并核对业务码与报告。

---

# 交付实现：`sandbox-guard`

本目录已交付一个可集成 Python 库、`python -m sandbox_guard` 命令行诊断工作台、固定种子样本生成器和 pytest 测试。组件只处理本地确定性样本，不访问网络或未授权目标。

## 快速验证

Windows PowerShell / Python 3.11：

```powershell
python -m pip install pytest==8.2.0
python -m sandbox_guard generate-sample --seed 361 --out samples/generated
python -m pytest -q

$work = ".sandbox-guard-demo"
python -m sandbox_guard import-patches `
  --work-dir $work `
  --policy samples/generated/policy.json `
  samples/generated/patches/patch-100-revoke-root.json `
  samples/generated/patches/patch-200-reduce-and-dependency.json `
  samples/generated/patches/patch-300-revive-tombstone.json `
  samples/generated/patches/patch-400-revive-restore.json `
  --report artifacts/reports/01-import-valid.json

python -m sandbox_guard query-process 4103 --work-dir $work `
  --report artifacts/reports/04-query-denied-4103.json
python -m sandbox_guard export-closure --work-dir $work `
  --report artifacts/reports/05-closure-active.json
python -m sandbox_guard verify-checkpoint --work-dir $work `
  --report artifacts/reports/07-verify.json
```

也可运行库集成示例：

```powershell
python examples/integrate_workspace.py
```

交互式工作台：

```powershell
python -m sandbox_guard shell --work-dir .sandbox-guard-demo
```

## CLI 子命令

- `import-patches`：首次用 `--policy` 初始化账本，随后按文件导入补丁批次；每个批次一个 SQLite 事务。
- `query-process <pid|profile|process-key>`：按样本声明的裁决时钟查询统一能力视图和进程存活结论。
- `export-closure [--key grant:...] [--all-events]`：导出授权依赖闭包、闭包链、画像/进程派生路径、受影响在途进程和冲突明细。
- `verify-checkpoint [--repair]`：校验 SQLite 检查点、摘要、本地原子快照和半写临时残留；`--repair` 从最后完整检查点恢复。
- `generate-sample`：额外提供的固定种子样本生成命令。
- `shell`：额外提供的交互式 REPL。

退出码：

- `0`：全部成功且无拒绝项。
- `1`：部分失败、业务拒绝、`E_DEP_CYCLE`、`E_LOCK`、检查点异常或未找到对象。
- `2`：命令行参数错误，报告码为 `E_ARG`。

所有 `--report` JSON 和 stdout JSON 均可直接解析；拒绝项统一包含 `code`、`source`、`index`、`message`，并按稳定键排序。

## 目录结构

- `sandbox_guard/capabilities.py`：能力白名单版本和稳定位掩码。
- `sandbox_guard/parsing.py`：画像、v1/v2 授予、进程、补丁批次解析与业务码拒绝清单。
- `sandbox_guard/merge.py`：两代授予、补丁事实、权威版本、冲突和进程有效能力归并。
- `sandbox_guard/graphs.py`：稳定环检测、反向依赖传播和派生图遍历。
- `sandbox_guard/closure.py`：撤销闭包、闭包链、受影响进程和 `E_DEP_CYCLE` 报告。
- `sandbox_guard/workspace.py`：SQLite 事务账本、水位、摘要、检查点和恢复。
- `sandbox_guard/atomicio.py`：临时文件、回读 JSON 校验、`os.replace` 原子整体替换。
- `sandbox_guard/locking.py`：POSIX `flock` / Windows `msvcrt` 文件锁。
- `sandbox_guard/samplegen.py`：固定种子样本生成器。
- `sandbox_guard/cli.py`、`sandbox_guard/__main__.py`：CLI 与 `python -m sandbox_guard` 入口。
- `tests/`：解析、顺序无关、闭包环、检查点恢复、锁和 CLI 测试。
- `examples/integrate_workspace.py`、`examples/demo.ps1`：库集成与 Windows 演示。
- `samples/generated/`：已生成的固定样本和预期摘要。

## 能力位掩码

当前白名单版本为 `cap-whitelist-v1`：

| 能力名 | 位值 |
| --- | ---: |
| `file_read` | `1` |
| `file_write` | `2` |
| `network_outbound` | `4` |
| `process_spawn` | `8` |
| `symlink_follow` | `16` |
| `registry_read` | `32` |
| `registry_write` | `64` |

同时给出 `capabilities`/`required_capabilities` 与 `capability_mask` 时，两者必须完全对应；只给其一时自动派生另一个表示，未知名称或未知位返回 `E_CAP_MASK`。

## 规范化与权威裁决

- 规范键形如 `profile:<id>`、`grant:<key>`、`process:<pid>`。
- v1 的绝对过期时刻唯一换算为 `observation_clock_ms + ttl_seconds * 1000`，不读取系统墙钟。
- v1 是旧口径，隐式 `generation=0`、`effective_at_ms=observation_clock_ms`，且不得声明非空 `dependencies`。
- v2 使用记录中的非负 `generation` 与 `effective_at_ms`。
- 权威次序为 `(generation, effective_at_ms, canonical_key, fact_id)`；最后一个 `fact_id` 仅用于完全同坐标时的确定性兜底，不使用字典遍历或 SQLite 自增到达顺序裁决。
- 墓碑是普通权威事实；更高代数/更晚生效的新增授予可以确定性“回活”。
- v1 与 v2 对同一键的能力集合或过期时刻不一致时，报告 `field=capabilities|expires_at_ms` 的冲突，并列出两个 `source`/`fact_id`/值和最终权威事实。

## 顺序无关性

补丁在 SQLite 中是不可变事实；`seq`/`apply_watermark` 只用于物理重放和检查点边界，不参与能力权威排序。账本摘要对逻辑事实按 `fact_id` 排序后计算，因此同一组批次无论以何种合法顺序应用，下列规范化结果相同：

- 每个授予的权威来源、有效位和依赖集合；
- 每个进程的有效能力位、`alive`、`decision` 和缺失能力；
- 撤销闭包键、闭包链和受影响 PID；
- 拒绝清单和逻辑账本摘要。

`tests/test_policy_flow.py` 会对 4 个有效批次的全部 `4! = 24` 种排列逐一建库并比较规范化裁决，同时验证检查点摘要一致；非法批次插入到不同位置时拒绝清单也一致。

## 闭包与环

依赖边方向为“授予 → 它依赖的授予”。撤销 `G` 时沿反向边求所有直接/传递依赖 `G` 的授予。随后：

1. 通过授予的 `profile_key` 映射进程画像；
2. 沿画像 `spawns` 派生边扩展子画像；
3. 选择这些画像中的在途进程（`pending`、`running`、`suspended`）；
4. 沿进程 `parent_pid → pid` 派生边扩展后代进程。

若授权依赖图、画像 `spawns` 图或进程父子图存在环，报告：

```json
{
  "ok": false,
  "code": "E_DEP_CYCLE",
  "cycles": [{"kind": "grant_dependency|profile_spawn|process_parent", "keys": ["..."]}],
  "closure_grant_keys": null,
  "affected_processes": null
}
```

环存在时不输出可能不完整的闭包集合。

## SQLite、检查点与恢复

工作目录包含：

- `guard.db`：`meta`、`batches`、`operations`、`checkpoints` 表；
- `checkpoint.json`：原子替换的本地快照；
- `.guard.lock`：跨进程文件锁。

每个补丁批次的批次头和全部操作在同一个 SQLite 事务中写入；任一前置引用、字段或业务校验失败，不插入该批次任何记录。每条操作记录稳定 `fact_id`、`canonical_key`、逻辑 `watermark`、物理 `apply_watermark/seq`、代数和生效时刻。

检查点包含 `checkpoint_format_version`、`capability_whitelist_version`、`merge_policy_version`、已应用批次/水位、`apply_watermark` 和 `ledger_digest`。恢复时：

1. 获取文件锁；
2. 清理并记录 `.checkpoint.json.tmp-*` 半写残留；
3. 从新到旧寻找摘要与操作前缀一致的最后完整 SQLite 检查点；
4. 重放其后已提交操作并重建统一视图；
5. 若快照缺失、JSON 损坏或与 SQLite 检查点摘要不符，用 SQLite 中的完整检查点原子重写快照；
6. 若检查点结构、能力白名单或合并口径版本变化，返回 `E_VERSION_REBUILD`，要求从源策略和补丁整体重建，不允许旧快照与新补丁增量混合。

强制终止不会生成承诺的退出报告；已提交批次由 SQLite 事务保证，未重命名的临时快照不会替换上一完整检查点，恢复边界只到最后完整检查点及其后可重放的已提交事务。

## 原子写与并发

检查点和 JSON 报告都写入同目录临时文件，flush/fsync 后回读并执行 `json.loads`，通过后才 `os.replace` 整体替换。`FileLock(timeout_ms=0)` 在锁被占用时立即返回 `E_LOCK`；正超时会等待并重试，使并发检查点串行完成。锁冲突不会写入或破坏既有 `guard.db`/`checkpoint.json`。

## 主要业务码

- 解析/参数：`E_ARG`、`E_JSON`、`E_SCHEMA`、`E_FIELD_TYPE`。
- 画像/能力：`E_PROFILE_ID`、`E_DUP_ID`、`E_PROFILE_PATH`、`E_PROFILE_ARGS`、`E_CAP_MASK`。
- 授予：`E_GRANT_KEY`、`E_GRANT_PROFILE`、`E_V1_TTL`、`E_V1_DEPENDENCIES`、`E_V2_TIMESTAMP`、`E_V2_GENERATION`、`E_V2_EXPIRY`、`E_DEP_KEY`。
- 补丁/账本：`E_WATERMARK`、`E_PATCH_OP`、`E_TARGET_KEY`、`E_BATCH_CONFLICT`、`E_POLICY_MISMATCH`。
- 闭包/并发/恢复：`E_DEP_CYCLE`、`E_LOCK`、`E_NOT_FOUND`、`E_VERSION_REBUILD`、`E_CHECKPOINT_MISSING`、`E_CHECKPOINT_CORRUPT`、`E_SNAPSHOT_CORRUPT`、`E_LEDGER_DIGEST`。

## 固定样本结论

种子 `361` 的样本显式写入：

- `observation_clock_ms = 1760000000000`；
- `default_evaluation_clock_ms = 1760000010000`；
- 每条 v2/补丁事实的 `generation`；
- `markers.method_sync_marker = SBXGUARD-METHOD-SYNC-V1`。

应用 4 个有效补丁后：

- 权威冲突：`(grant:g-clock, capabilities)`、`(grant:g-clock, expires_at_ms)`。
- 默认当前撤销触发：`grant:g-root`、`grant:g-writer`。
- 闭包授予：`grant:g-edge`、`grant:g-middle`、`grant:g-root`、`grant:g-standalone`、`grant:g-writer`。
- 受影响在途 PID：`4101`、`4102`、`4103`、`4104`、`4105`。
- PID `4106` 被 gen50 墓碑化后又由 gen60 新增授予回活；`--all-events` 会显示该历史触发和 `revived_trigger_keys`。
- 最终检查点：`cp-5`；逻辑账本摘要：`b7b5230138428e1734544bc0994f0c1d77e95c999af4c099a154ecce6caac485`。

## 实际验证记录

本次交付环境实际为 Python `3.10.12`、pytest `8.2.0`、sqlite3 `3.37.2`（代码只使用 3.10+ 兼容语法，目标 Windows/Python 3.11 按上文命令运行）。

```text
python3 -m pytest -q
....................                                                     [100%]
```

共 21 个测试通过，覆盖：

- 固定样本无拒绝解析和非法记录继续处理；
- 能力位掩码不匹配；
- 统一视图冲突、撤销闭包和回活；
- 4 个补丁批次全部 24 种排列的顺序无关；
- 授权、画像派生、进程派生环；
- 半写快照/临时残留恢复、最后完整检查点选择、检查点后事务重放；
- 版本变化拒绝增量续接；
- `E_LOCK` 锁冲突和并发检查点串行；
- CLI 成功/部分失败/参数错误退出码和 JSON 报告。
- CLI 同组补丁中跨批次前向引用在不同参数顺序下的摘要一致性。

端到端报告已再生到 `artifacts/reports/`，库集成输出为 `artifacts/reports/library-integration.json`。
