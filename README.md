当前是空白工作区，需要从零实现一个面向文档数据库的本地查询执行与片段索引存储引擎。样本以本地 JSON 给出两代索引快照：v1 为单文件 JSON，记录文档 ID、字段值、片段偏移与该文档字段对应的扁平倒排条目；v2 为分片 JSON 加 SQLite 元数据，记录字段级倒排、文档版本号、片段校验和、索引代数与删除墓碑。文档片段本身存放在本地内容寻址的片段目录中，由样本清单给出每段的字节内容摘要。查询模板为受限的文档查询子集（字段等值、范围、多字段合取、按命中回取片段）。引擎需要把两代快照合并成统一内存索引后执行查询并回取片段，在查询执行过程中维护可持久化的状态演进（索引代数、查询批次水位、片段提交状态），并在进程被强制终止后依据 SQLite 中的持久化元数据与片段目录恢复。全部时间相关判定使用可注入的逻辑时钟毫秒值。输出以稳定排序 JSON 呈现，供调用方验证合并裁决与查询结果的可复现性。

任务环境与准备：
安装 Python 3.11 并在工作区创建虚拟环境，安装 SQLite 支持（Python 标准库 sqlite3）与 NumPy，确认能导入 numpy 且 sqlite3 版本支持 WAL 模式。
在项目内准备固定本地样本：v1 单文件 JSON、v2 分片 JSON 与 SQLite 元数据库、片段目录与片段清单，样本中需包含冲突文档、墓碑文档、片段校验失败项与合法查询模板。
确认工作目录可写入临时文件与 SQLite 数据库文件，所有路径通过配置注入，不使用系统墙钟，逻辑时钟由命令参数或样本清单显式提供。
在 Windows 本地执行命令时统一以 UTF-8 编码运行，确保 JSON 输出按稳定排序与固定缩进产生可逐字节比较的结果。

业务要求：
1. 实现 v1 与 v2 两代索引快照的解析校验：v1 校验文档 ID 唯一、字段值类型合法、片段偏移区间不越界、扁平倒排条目每项均能追溯到已声明的文档与字段；v2 读取分片 JSON 与 SQLite 元数据库，校验分片内容哈希、文档版本号单调、片段校验和与片段目录内容一致、墓碑标记引用的文档必须存在；任何一项失败须返回各自不同的业务错误码且不写入任何持久化状态。
2. 实现跨版本快照合并与统一内存索引构建：同一文档同一字段同时出现在 v1 与 v2 时，按快照声明的逻辑时钟与文档版本号裁决最新值，两者相同则按来源格式优先级裁决；合并结果须记录每个字段值的来源版本、原始片段偏移与裁决依据；同一批样本任意顺序加载必须收敛到同一索引结果，字段级倒排须可按键与值区间双向访问。
3. 实现受限查询执行与片段回取：支持字段等值、范围、多字段合取与按命中回取片段，回取时校验片段内容，校验和不符必须与查询未命中区分并映射到不同业务错误码；结果以稳定排序 JSON 输出文档 ID、命中字段、片段内容摘要、来源快照版本与索引代数；同一查询在索引与逻辑时钟不变时必须逐字节一致。
4. 实现查询批次执行状态与索引代数的原子演进：每次查询批次须记录批次号、绑定索引代数、扫描的倒排区间摘要、回取片段数与失败计数，索引代数或片段目录版本变化时进行中的批次必须按绑定代数完成，不得读到新旧混合索引；状态只依据已提交批次结果推进，推进前须校验快照内容摘要，摘要不匹配须拒绝推进并保持状态不变。
5. 实现强制终止后的恢复与半写入清理：把索引代数指针、已提交批次水位、批次结果摘要、片段提交状态与逻辑时钟水位以事务原子写入 SQLite，提交采用临时表加版本号整体替换索引指针；进程在合并、查询或片段回取中途被强制终止后，恢复路径须依据持久化状态识别已提交与半提交步骤，只重放未完成步骤，且不得出现索引指针已推进而片段目录缺失对应内容。
6. 实现多进程并发一致性与幂等提交：允许多个查询进程与快照替换进程并行运行，通过 SQLite 事务与文件锁保证同一快照版本只被合并提交一次、同一批次号同一索引代数只被提交一次，重复提交相同批量查询结果必须幂等；读取方只能看到某个完整已提交索引代数，不得观察到合并中间态。
7. 实现异常链路隔离与业务错误码映射：分片哈希不匹配、片段校验和不符、文档版本号回退、字段类型与查询谓词不兼容、查询模板语法非法、逻辑时钟回拨超过容差分别映射到不同业务错误码，并在输出中保留可诊断字段（快照版本、文档 ID、字段名、片段偏移）；单个查询模板异常不得中断其他模板或污染已提交的索引状态。
8. 提供可重复的命令行接口与验证工具：支持 load、merge、query、resume、status、verify 子命令；query 以稳定排序 JSON 输出查询结果与绑定索引代数，status 输出版本指针、已提交批次水位与片段统计；使用固定本地样本连续运行两次，全部子命令输出须逐字节一致，含冲突文档、墓碑文档、片段校验失败与逻辑时钟回拨的样本必须给出确定裁决与可读诊断。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
用固定本地样本验证两代快照合并裁决、受限查询与强制终止后恢复，连续两次运行输出逐字节一致。

---

# 实现说明

## 环境

- Python 3.11.14（`.runtime/python`，python-build-standalone 本地运行时），虚拟环境 `.venv`
- NumPy 2.4.6（字段倒排排序键投影与区间二分）；SQLite 3.50.4（`PRAGMA journal_mode=WAL` 已验证）
- 全部路径经 `samples/config.json` 注入；不使用系统墙钟，逻辑时钟由 `--clock-ms` 或样本清单 `manifest.json` 的 `clocks` 提供

## 结构

- `docengine/snapshot_v1.py` — v1 单文件快照解析校验（文档 ID 唯一、字段类型、片段偏移区间、扁平倒排可追溯）
- `docengine/snapshot_v2.py` — v2 分片 + SQLite 元数据校验（分片哈希、版本单调、片段校验和、墓碑引用存在）
- `docengine/merge.py` — 跨版本合并裁决（版本 → 逻辑时钟 → 来源优先级 v2>v1）与统一内存索引（NumPy 排序键，等值/区间双向访问）
- `docengine/query.py` — 受限查询子集（eq / range / and / 片段回取），单模板异常隔离
- `docengine/state.py` — SQLite WAL 持久化（索引代数指针、批次水位、片段提交状态、逻辑时钟水位），事务原子提交 + 文件锁
- `docengine/engine.py` — load / merge / query / resume / status 编排
- `docengine/cli.py` — CLI 与 `verify` 端到端套件
- `tools/make_samples.py` — 确定性样本生成；`tools/check_determinism.py` — 双跑字节一致验收
- `tests/test_engine.py` — 15 个单元测试

## 样本（samples/）

- 冲突文档：`doc-001`（版本裁决）、`doc-006`/`doc-007`（时钟裁决，v2/v1 各胜一次）、`doc-008`（同版本同时钟，来源优先级裁决）
- 墓碑文档：`doc-005`（v2 墓碑，合并后不可见）
- 片段校验失败：`frag4` 磁盘字节被污染，查询回取时映射 `E_FRAGMENT_CHECKSUM`，与未命中（`no_hit`）区分
- 合法模板 `templates/legal.json`（等值、范围、合取、回取、墓碑、空命中），非法模板用于错误码演示

## 业务错误码

`E_V1_DUPLICATE_DOC` / `E_V1_FIELD_TYPE` / `E_V1_SPAN_RANGE` / `E_V1_INVERTED_ORPHAN` /
`E_V2_SHARD_HASH` / `E_V2_VERSION_REGRESS` / `E_V2_FRAGMENT_CHECKSUM` / `E_V2_TOMBSTONE_ORPHAN` /
`E_FRAGMENT_CHECKSUM` / `E_PREDICATE_TYPE` / `E_QUERY_SYNTAX` /
`E_CLOCK_REGRESSION` / `E_STATE_DIGEST_MISMATCH` / `E_BATCH_CONFLICT` / `E_NO_INDEX`

错误输出保留可诊断字段（快照版本、文档 ID、字段名、片段偏移、校验和等）。

## 用法

```bash
.venv/bin/python -m docengine.cli --config samples/config.json load   --clock-ms 3000
.venv/bin/python -m docengine.cli --config samples/config.json merge  --clock-ms 3100
.venv/bin/python -m docengine.cli --config samples/config.json query  --clock-ms 3200 --templates legal.json
.venv/bin/python -m docengine.cli --config samples/config.json status
.venv/bin/python -m docengine.cli --config samples/config.json resume --clock-ms 3300
.venv/bin/python -m docengine.cli --config samples/config.json verify
```

崩溃注入测试钩子：`merge --crash-after staging`（提交中途强杀，退出码 90）、
`query --crash-after running`（批次运行中途强杀，退出码 91），随后 `resume` 清理半提交合并并重放未完成批次。

## 验证结果（实跑）

- `python -m unittest discover -s tests`：15 个测试全部通过（校验错误码、裁决规则、加载顺序收敛、幂等提交、时钟回拨、崩溃恢复）
- `tools/check_determinism.py`：load/merge/query×4/status/resume/verify 全部子命令连续两次运行输出逐字节一致（`DETERMINISM: OK`）
- `verify` 双跑：`cmp` 逐字节一致（34,641 字节），内含合并裁决、片段校验失败、时钟回拨、强杀恢复全链路
- 并发：5 进程并行 `merge` 仅提交 1 个索引代数（4 个幂等空转）；5 进程并行 `query` 获得互不相同的批次号 1–5
