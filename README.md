# 主从复制延迟观测与多版本格式互操作编辑台

这是一个完全本地、无第三方运行时依赖的数据库复制实验台。它使用 SQLite
保存主库操作索引与原始记录载荷、从库回放进度、延迟样本和快照登记，并用
本地段文件保存带版本的主库操作日志。目标运行环境是 Windows 桌面上的 Python 3.11+ 与
官方安装包自带的 Tkinter；核心逻辑和演示脚本也可在无 GUI 环境运行。

项目不访问网络，不使用 Docker，不需要真实数据库或账号。所有时间均为
确定性整数逻辑毫秒，不读取系统墙上时钟。

## 环境与启动

```powershell
py -3.11 -c "import tkinter, sqlite3; print(tkinter.TkVersion, sqlite3.sqlite_version)"
py -3.11 -m replication_lab demo --output artifacts/demo
py -3.11 -m replication_lab gui --root lab-data
```

无需安装依赖；如需安装命令行入口，可执行 `py -3.11 -m pip install -e .`，
之后使用 `replab` 命令。`requirements.txt` 明确声明没有第三方包。

当前开发验证环境是 Linux/Python 3.11，未安装 Tkinter，因此自动化验证覆盖
核心、CLI、恢复和演示全部路径；GUI 模块在无 Tk 环境下可导入并给出
`E_GUI` 诊断，实际窗口需在带 Tkinter 的 Windows Python 上启动。

## 项目结构

```text
replication_lab/
  segment.py       v1/v2/v3 段与记录编解码、CRC、严格校验、升级编码
  estimator.py     确定性回放参数、整数定点统计、误差界与参数敏感性
  replay.py        批次回放计划、SQLite 批次持久化、强制终止恢复
  upgrade.py       逐段原子升级、只读备份、升级检查点与断点续跑
  storage.py       SQLite schema、操作索引、批次/样本/快照登记
  snapshot.py      快照保存、段摘要比较、整视图替换、重放校验
  service.py       会话服务与公开 API
  gui.py           Tkinter 交互界面
  demo.py          三个确定性验收场景
  crash_worker.py  在批次中途 os._exit 的子进程
samples/deterministic_operations.json
tests/             26 项标准库 unittest
artifacts/demo/    本次实际运行的演示报告与工作目录
```

运行时默认根目录为 `./lab-data`，其中包含：

```text
logs/                         seg-XXXXXXXX.rlog 段文件
backups/                      升级前段的只读 .bak 副本
checkpoints/upgrade_checkpoint.json
snapshots/snap-NNNNNN/        快照 manifest 与段字节副本
archives/                     整视图恢复前被替换磁盘视图的归档
lab.sqlite3                   操作索引与载荷、回放批次、样本、快照登记
```

## 段文件格式

所有整数为大端。段头 24 字节，记录采用长度前缀帧；段头、记录载荷和 v3
偏移均自校验。

```text
segment header
  magic[8] = "RLOGSEG\x01"
  container_version:u16 = 1
  format_version:u16      段内最高记录版本
  segment_id:u32
  record_count:u32
  header_crc32:u32        前 20 字节的 CRC32

record frame
  payload_length:u32
  payload[payload_length]
  payload_crc32:u32

payload v1
  format_version:u16, commit_seq:u64, txn_id:u32

payload v2 = v1 +
  commit_ts_ms:u64, txn_size_bytes:u64

payload v3 = v2 +
  segment_id:u32, segment_offset:u32, source_format_version:u16
```

`source_format_version` 是来源版本标记：v1/v2 记录升级后，存储格式与段头
均为当前 v3，但界面/API 仍能辨识其原始版本。v1 缺少的时间戳和大小在升级
时编码为保留哨兵 `UINT64_MAX`，解码为 `None`，绝不伪造为 0。

解码会校验：魔数、容器版本、头 CRC、记录长度、载荷 CRC、字段精确长度、
支持版本集合、正数提交序号/事务标识、段内和跨段提交序号严格递增、v3
`segment_id` 与段头一致、v3 `segment_offset` 与实际帧偏移一致。错误统一为
`E_FORMAT`，并带 `segment` 和记录起始 `offset`。

## 逻辑时钟与回放模型

时钟来源：会话整数逻辑时钟，单位为毫秒。主库时间由样本中的
`commit_offset_ms` 加会话原点生成；从库完成时间由确定性批次调度生成。
观测方式固定为：

```text
delay(seq) = replica_completion_ts_ms(seq) - master_commit_ts_ms(seq)
```

回放参数为整数 `R`（事务/秒）与 `C`（并发度）。记录按提交序号每 `C` 条
组成一批：

```text
D = ceil(1000 * C / R)                 每批逻辑毫秒数
B = ceil(n / C)                        批次数
batch i 完成时刻 = origin + (i + 1) * D
```

默认 `origin` 为全部已知主库时间戳的最大值；首次回放后原点写入 SQLite，
恢复时不得改变。未完成批次不产生已确认样本；缺少提交时间戳的 v1 记录
产生 `delay_ms=None` 且 `status=DELAY_UNKNOWN` 的样本。未知项不填补、不删除。

## 定点统计与误差传播

默认估算参数版本为 `fixed-point-v1`，`Q=1000`（每毫秒 1000 个定点单位）。
均值用整数四舍五入：

```text
mean_fixed = round_half_up(sum(delay_ms) * Q / known_count)
```

因此均值量化误差不超过 `1/(2Q)=1/2000 ms`。P95 使用 nearest-rank 从整数
毫秒样本直接选择，最大延迟也直接取整数样本，二者无插值误差。误差报告还
给出：

- 每批 `D=ceil(1000*C/R)` 的向上取整误差小于 1 ms；
- 末批累计调度量化保守上界为 `B` ms；
- 总保守上界为 `B + 1/(2Q)` ms；
- `rate-1` 与 `concurrency+1` 对固定记录集总调度跨度的影响；
- 未知样本的完整推导步骤与统计状态说明。

状态规则是显式的：

- 无已知样本或少于 `min_samples=5`：`INSUFFICIENT` / `NONE`；
- 已知样本足够但存在未知延迟样本或未知批次：`DEGRADED_PARTIAL` / `LOW`；
- 全部已知且样本足够：`COMPLETE` / `HIGH`。

统计量只描述已确认子集；存在未知项时不会声称全体样本无偏。完整推导步骤
同时出现在 JSON 的 `error_bound_steps` 与文本观测报告中。

## 升级、检查点与强制终止

打开日志目录时，升级器读取每个段声明的版本和实际记录版本。低于当前 v3
的段逐记录升级，并通过同目录临时文件、`fsync`、原子替换写入目标文件。
旧段复制到 `backups/` 后设为只读，文件名含源版本与摘要前缀。

检查点按段记录 `PENDING/IN_PROGRESS/COMPLETED/FAILED`、源/目标摘要和备份
路径。完成条件不是“检查点写了”而是“目标文件存在、可完整解码且摘要匹配”：

- 替换后、检查点前崩溃：重启验证目标摘要后可标记完成；
- 替换前崩溃：只能留下临时文件，原段仍是权威版本，重启继续升级；
- 半写/损坏段、非法长度/CRC/版本：`E_FORMAT`，原段保持不变并进入报告；
- 一个段的升级完成并写检查点后，才处理下一段。

回放批次在开始执行前先以 `IN_PROGRESS` 写入 SQLite；样本插入、批次改为
`COMPLETE` 和进度更新在同一事务内提交。若进程被强制终止：

- 重启只读取持久化状态，不声称有退出报告；
- `IN_PROGRESS` 批次改为 `UNKNOWN` 并要求重放；
- 已确认样本保留，重放通过主键和断点跳过已完成批次，不重复计数；
- 恢复报告列出 `recovered_last_commit_seq`、`unknown_batch_count` 和
  `rebuilt_sample_count`。

回放恢复摘要使用逻辑记录语义摘要而非段字节摘要，因此记录级格式升级不会
使已持久化的回放进度失效；来源版本标记也保留在 v3 载荷中。

## 快照、整体替换与重放校验

每个快照保存：schema 版本、快照 ID、段 manifest（文件名、摘要、段 ID、
段格式版本、记录版本、记录数）、段集合摘要、格式版本集合、回放进度、
全部延迟样本（含未知）、回放参数、估算参数版本、统计量和段字节副本。

恢复时先比较快照段摘要/格式版本集合与当前磁盘：

- 不一致：将当前 `*.rlog` 归档，删除当前视图中的段，复制快照段字节，整体
  替换进度与样本；不是增量合并；
- 一致：不复制段文件，但仍整体替换回放进度和样本状态；
- 快照之间的版本集合、增删段、摘要变化、参数变化、样本序列摘要和统计量
  变化按固定字段顺序输出。

历史重放校验使用快照记录的速率、并发度、逻辑原点、段字节副本和估算参数
重新构建完整回放计划，逐项比较段集合摘要、格式版本集合、参数、估计参数、
样本序列摘要、样本计数及全部统计量。任何不一致返回 `E_REPLAY` 并列出具体
项；全部一致时按固定顺序输出比对摘要。

## 常用命令

```powershell
# 写入当前 v3 记录；每 4 条一个段
py -3.11 -m replication_lab write --root lab-data `
  --ops samples/deterministic_operations.json --per-segment 4

# 冷启动回放与观测
py -3.11 -m replication_lab observe --root lab-data `
  --rate 10 --concurrency 2 --reset

# 仅检查/执行升级
py -3.11 -m replication_lab upgrade --root lab-data --json

# 强制终止后的恢复
py -3.11 -m replication_lab recover --root lab-data

# 快照
py -3.11 -m replication_lab snapshot-save --root lab-data
py -3.11 -m replication_lab snapshot-list --root lab-data
py -3.11 -m replication_lab snapshot-restore --root lab-data snap-000001
py -3.11 -m replication_lab snapshot-verify --root lab-data snap-000001
py -3.11 -m replication_lab snapshot-diff --root lab-data snap-000001 snap-000002

# 三场景验收演示
py -3.11 -m replication_lab demo --output artifacts/demo

# Tkinter 界面
py -3.11 -m replication_lab gui --root lab-data
```

GUI 的固定渲染顺序为：日志段与格式版本、延迟样本、估算统计与误差界、
估算/回放参数版本、快照列表、操作编辑器、报告。支持写入 JSON 操作序列、
冷启动回放、恢复、升级、保存/恢复/校验快照。所有业务错误在对话框中显示
业务码、段名和偏移。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_FORMAT` | 段/记录非法，或记录无法升级；携带段名与偏移 |
| `E_REPLAY` | 快照参数、段集合或格式版本导致历史观测不可复现 |
| `E_STATE` | 持久化状态与当前重建参数不一致，需要显式 reset |
| `E_PARAM` | 整数速率、并发度、操作序列等参数非法 |
| `E_SNAPSHOT` | 快照 schema、bundle 或摘要非法/缺失 |
| `E_GUI` | 当前 Python 缺少 Tkinter 或无可用显示 |

## 实际验证结果

本次在 Python 3.11.0rc1、SQLite 3.x 本地环境执行：

```text
python3 -m unittest discover -s tests -t . -v
Ran 26 tests
OK
```

覆盖三版本编解码、长度/CRC/版本/序号/v3 偏移错误定位、升级备份与崩溃后
检查点续跑、SQLite 原始记录载荷、部分回放跨格式升级恢复、未知延迟保留、
强制终止批次恢复与去重、快照整视图替换、损坏视图修复、快照差异、
`E_REPLAY` 负例、CLI 旧快照恢复/校验不触发自动升级和三场景端到端演示。

`python3 -m replication_lab demo --output artifacts/demo` 实际输出：

| 场景 | 版本集合 | 样本 total/known/unknown | 估算状态 | 结果 |
| --- | --- | --- | --- | --- |
| 冷启动回放观测 | `[3]` | `8/8/0` | `COMPLETE` | mean_fixed=817500, P95=920ms |
| 格式升级后重新观测 | 升级前 `[1,2,3]` → 后 `[3]` | `10/8/2` | `DEGRADED_PARTIAL` | v1 的 2 条延迟保持未知 |
| 强制终止后恢复 | `[3]` | `8/8/0` | `COMPLETE` | 退出码 99；恢复序号 2；未知批次 1；重建样本 2 |

第三个场景还验证了：篡改磁盘视图后校验返回 `E_REPLAY`，整视图恢复后校验
通过；将速率改为 11 时再次返回 `E_REPLAY`。完整 JSON/Markdown 结果位于
`artifacts/demo/demo_report.json` 与 `artifacts/demo/demo_report.md`，各场景
还包含自己的 `scenario.json`、`observation.md`、段、备份、检查点、SQLite 和
快照文件。

## 可重复性与边界

- 回放使用虚拟整数逻辑时钟，不受机器速度、线程调度或墙上时间影响；
- 同一记录集合、速率、并发度与逻辑原点得到完全相同的批次、完成时刻、
  样本序列与统计结果；
- 未知延迟和未完成批次是有名称的观测状态，不会被填零或静默丢弃；
- 快照保存段字节副本，因此旧格式视图也可精确恢复；
- 目标 Windows 桌面 GUI 需使用包含 Tkinter 的官方 Python 安装包；本仓库
  的自动化测试不依赖显示服务器。
