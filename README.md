存储团队这周刚确定了新对象存储原型的验收边界，当前工作目录还没有任何代码或样本文件，需要在 Linux（WSL Ubuntu）上从零实现一个元数据先写与后写日志的缓存崩溃一致性诊断引擎。全部输入由随工程交付的固定种子脚本确定性生成，不依赖真实对象存储、外部服务或网络。业务对象包括：对象清单记录对象键、原始字节大小、内容版本号、预期数据签名；写入事件流按逻辑时钟给出 WRITE、UPDATE、DELETE 三类操作及载荷大小；元数据快照存在两代格式，v1 为单文件 JSON 记录相对生效秒数与扁平键值，v2 为分片 JSON 加 SQLite 元数据库记录绝对毫秒、缓存代数和失效墓碑。组件需在可注入逻辑时钟下比较“元数据先写”与“后写日志”两种顺序策略，判定每个写入事件在进程强制终止后是否产生脏缓存、孤儿对象或元数据失效，并输出精确复现的裁决证据链。

任务环境与准备：
安装 Python 3.10+，NumPy 与 SQLite 使用 Python 标准库自带模块。
运行随题交付的固定种子脚本生成对象清单、写入事件流、两代元数据快照与异常注入清单到本地目录。
确认工作区为 Linux（WSL Ubuntu），无需网络或外部对象存储服务。

业务要求：
1. 实现对象清单、写入事件流与两代元数据快照的解析校验，对象键唯一且格式合法，内容版本号为正整数，写入事件序号严格递增且逻辑时钟非负，v1 相对秒数非负且扁平键值可追溯，v2 分片哈希与缓存代数单调；任何非法输入返回明确业务错误码且不触发后续裁决。
2. 基于注入逻辑时钟驱动写入事件，对每个事件在元数据先写路径执行：先落对象数据到临时目录并原子改名，再提交元数据记录，顺序颠倒或中途终止会产生元数据指空或对象孤块，必须分别归类为脏缓存、孤儿对象与双写成功。
3. 基于注入逻辑时钟驱动写入事件，对每个事件在后写日志路径执行：先提交元数据增补日志并原子持久化，再执行实际数据写入，强制终止后必须用日志回放重建元数据，并识别日志完整、日志半写与日志丢失三类结局。
4. 构建两代元数据快照的统一视图，同一对象键在 v1 与 v2 同时存在时依据声明的逻辑时钟与缓存代数裁决最新版本；v1 相对秒数在注入逻辑时钟下换算为绝对毫秒，记录换算标度、舍入方向与余量，同一逻辑时钟序列重复判定必须逐位一致。
5. 实现写路径一致性对照工具，对同一个写入事件流分别执行元数据先写与后写日志两条完整路径，并做到逐对象键、逐逻辑时钟比对最终缓存状态与孤儿/脏块集合，不一致时输出首个分歧对象、两条路径的中间状态和触发证据。
6. 实现可注入异常注入与单点失效注入，在元数据先写的数据落盘、元数据提交阶段，以及后写日志的日志追加、日志回放阶段分别注入强制终止、写入中断与缓存代数回退，每种异常映射到独立错误码，并保留阶段名、对象键、逻辑时钟与预期签名。
7. 把缓存版本指针、日志回放水位、孤儿对象集合、脏缓存标记、两代快照合并裁决和逻辑时钟水位以事务原子写入 SQLite，每次提交整体替换当前账本指针；进程被强制终止后，恢复路径只处理未提交的半写状态，不重复计入已确认的正常对象。
8. 实现多进程并发一致性与可重复命令行验证，多个诊断实例并行处理不同对象键时通过 SQLite 事务与文件锁保证同一对象同一版本只被一个路径标记一次、日志回放不被并发覆盖；提供 load、run、inject、resume、status、verify 子命令，全部结果以稳定排序 JSON 输出。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
用本地固定种子样本驱动元数据先写与后写日志两条路径，注入强制终止后恢复，验证缓存与日志账本裁决一致。

---

## 交付实现

本目录已包含可直接运行的确定性实现，不依赖真实对象存储或外部服务。

### 快速开始

```bash
python3 generate_fixtures.py --output fixtures/seed
python3 -m diagnostic_engine.cli load --fixtures fixtures/seed
python3 -m diagnostic_engine.cli run \
  --fixtures fixtures/seed \
  --state-dir diagnostic_workspace/demo \
  --policy both
python3 -m diagnostic_engine.cli verify \
  --fixtures fixtures/seed \
  --state-dir diagnostic_workspace/demo \
  --expect-consistent
```

所有 CLI 结果均为键名稳定排序的 JSON。常用子命令：

- `load`：解析并校验清单、事件流、v1/v2 快照、SQLite 和注入清单。
- `run`：执行 `metadata_first`、`write_log`、`direct_reversed` 或 `both`。
- `inject`：按固定注入用例或临时参数注入故障；默认安全模拟，`--hard-kill` 才真实 `SIGKILL`。
- `resume`：只恢复 SQLite 中的 `pending_operations` 半写状态。
- `status`：输出缓存指针、水位、孤儿、脏缓存、计数和账本不变量。
- `verify`：校验输入、SQLite 指针、文件签名、唯一标记和双路径一致性。

### 强制终止与恢复

```bash
STATE=$(mktemp -d /tmp/diag-demo-XXXXXX)

# Shell 中会看到 137 / Killed，这是预期结果。
python3 -m diagnostic_engine.cli inject \
  --fixtures fixtures/seed \
  --state-dir "$STATE" \
  --case INJ-MF-RENAME-KILL \
  --hard-kill || test $? -eq 137

python3 -m diagnostic_engine.cli resume \
  --fixtures fixtures/seed \
  --state-dir "$STATE" \
  --case INJ-MF-RENAME-KILL

python3 -m diagnostic_engine.cli verify \
  --fixtures fixtures/seed \
  --state-dir "$STATE"
```

日志路径可用 `INJ-LOG-APPEND-HALF`、`INJ-LOG-APPEND-KILL`、
`INJ-LOG-REPLAY-KILL`、`INJ-LOG-REPLAY-LOST` 等用例验证完整、半写和丢失结局。
全部 13 个注入用例均可安全注入并恢复：

```bash
bash scripts/acceptance.sh
python3 -m pytest tests/test_diagnostic_engine.py
```

### 多进程并发

内置分片：

```bash
python3 -m diagnostic_engine.cli run \
  --fixtures fixtures/seed \
  --state-dir diagnostic_workspace/parallel \
  --policy metadata_first \
  --workers 4
```

也可手动启动多个 `--shard-index N --shard-count 4` 实例。`fcntl.flock`
串行化账本事务，SQLite `PRIMARY KEY (object_key, content_version, claim_scope)`
保证同一对象同一版本只标记一次。

### 主要错误码

- 输入错误：`DIAG-1002` 键非法、`DIAG-1003` 键重复、`DIAG-1004` 版本非法、
  `DIAG-1007` 事件序号非严格递增、`DIAG-1008` 逻辑时钟为负。
- 快照错误：`DIAG-1102` v1 秒数非法、`DIAG-1103` v1 扁平追溯值非法、
  `DIAG-1202` v2 哈希不匹配、`DIAG-1203` v2 代数非单调、`DIAG-1204` SQLite 非法。
- 注入故障：`DIAG-2001` 强制终止、`DIAG-2002` 写入中断、
  `DIAG-2003` 缓存代数回退、`DIAG-2004` 日志半写、`DIAG-2005` 日志丢失。

阶段名、对象键、逻辑时钟、预期签名和注入用例 ID 均写入错误证据、SQLite findings
以及 `state/evidence/*.json`。

### 代码结构

- `generate_fixtures.py` / `diagnostic_engine/seed.py`：固定种子样本生成器。
- `diagnostic_engine/validation.py`：输入解析与业务校验。
- `diagnostic_engine/clock.py` / `diagnostic_engine/snapshots.py`：时钟换算和两代快照合并。
- `diagnostic_engine/engine.py`：两条写路径、故障注入、日志回放和恢复。
- `diagnostic_engine/ledger.py`：SQLite 全量指针账本、唯一标记和文件锁。
- `diagnostic_engine/compare.py`：逐键逐时钟一致性对照。
- `diagnostic_engine/cli.py`：六个核心子命令及稳定 JSON 输出。
- `tests/test_diagnostic_engine.py`：13 个单元/集成/并发/硬终止测试。
- `docs/DESIGN.md`：状态机、错误分类和原子提交设计。
- `docs/VALIDATION.md`：实际验证记录。
