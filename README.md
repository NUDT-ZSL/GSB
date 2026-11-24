# BSL Workbench

面向自研本地二进制封装格式 BSL（Binary Segment Log）的离线解析、安全诊断、修订失效分析与检查点恢复工作台。样本由固定种子本地生成，不访问网络、账号或外部数据。

## 快速开始

Windows PowerShell / Python 3.11：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pytest==8.2.0 numpy==1.26.4
python -m pip install -e . --no-deps
python scripts\generate_samples.py --output samples\generated
pytest -q
python scripts\verify_acceptance.py
```

Linux/WSL/macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install pytest==8.2.0 numpy==1.26.4
pip install -e . --no-deps
python scripts/generate_samples.py --output samples/generated
pytest -q
```

也可以不安装包，直接在仓库根目录使用 `python -m bsl_workbench ...` 或 `python main.py ...`。

## 常用命令

```bash
# 生成固定种子样本
python -m bsl_workbench generate-samples --output samples/generated

# 解析一个或一批 BSL；目录会递归查找 *.bsl
python -m bsl_workbench parse \
  --file-id valid_basic \
  --workdir .bsl-workbench \
  --report artifacts/valid-report.json \
  samples/generated/valid/valid_basic.bsl

# 查询段状态/状态代/水位/累计摘要结论
python -m bsl_workbench query --workdir .bsl-workbench --file-id valid_basic
python -m bsl_workbench query --workdir .bsl-workbench \
  --file-id valid_basic --segment /alpha --format table

# 导出最近一轮 JSON 诊断报告
python -m bsl_workbench export-report \
  --workdir .bsl-workbench --file-id valid_basic \
  --output artifacts/exported-report.json

# 校验并可选恢复检查点/快照
python -m bsl_workbench verify-checkpoint --workdir .bsl-workbench
python -m bsl_workbench verify-checkpoint --workdir .bsl-workbench --recover

# 修订失效闭包
python -m bsl_workbench parse --file-id revision_demo \
  --report artifacts/rev-v1.json samples/generated/revisions/v1/revision_demo.bsl
python -m bsl_workbench parse --file-id revision_demo \
  --report artifacts/rev-v2.json samples/generated/revisions/v2/revision_demo.bsl
python -m bsl_workbench revise \
  --old artifacts/rev-v1.json --new artifacts/rev-v2.json \
  --file-id revision_demo --output artifacts/invalidation.json

# 从 JSON 报告生成 DOT/Mermaid 段依赖图
python -m bsl_workbench graph \
  --report samples/generated/snapshots/nested__cycle_reference.json \
  --format dot --output artifacts/cycle-dependency-graph.dot
```

CLI 子命令别名：`query-segment(s)`、`report/export`、`checkpoint/verify`、`revision`、`samples/generate`。

退出码：

- `0`：全部成功且无错误诊断。
- `1`：参数可读但存在解析/校验失败、部分文件失败或检查点不一致。
- `2`：命令行参数错误。
- `3`：工作目录锁冲突，报告码为 `E_LOCK`。

单个文件出现业务诊断不会中断同批其他文件；批量报告按 `file_id` 和路径稳定排序。

## BSL 格式实现约定

固定头字段按题面给出的小端偏移解析：

| 字段 | 类型 | 偏移 |
| --- | --- | ---: |
| magic | 4 字节，固定 `BSL1` | 0 |
| version | uint16 | 4 |
| segment_count | uint32 | 6 |
| table_offset | uint64 | 10 |
| declared_total_size | uint64 | 18 |
| reserved | 8 字节 | 26 |

题面“32 字节固定头”与字段宽度合计存在 2 字节算术差异：`4+2+4+8+8+8=34`。实现同时兼容：

- 表偏移为 `32` 的紧凑 32 字节头；
- 表偏移为 `34`、保留区完整 8 字节的字面字段布局。

生成器默认采用后者，并额外生成 `valid/header_offset32_compat.bsl` 验证 32 字节偏移兼容性。

段记录为：uint16 名称长度、UTF-8 名称、uint8 类型、uint8 压缩方式、uint64 载荷偏移、uint64 声明长度、32 字节摘要。已知段类型：

- `0x01`：普通二进制段。
- `0x10`：嵌套完整 BSL。
- `0x20`：引用依赖列表，载荷为 `{"refs":["/canonical/key","simple-name"]}`，也支持 `BSLREFS1\n` 前缀的逐行引用。

题面第 4 条中“压缩方式 0x10”与格式定义、第 3 条未知压缩方式 `E_COMPRESSION` 冲突。实现以开头的明确定义为准：**段类型 `0x10` 控制嵌套，压缩方式仍只允许 0/1/2；压缩方式 `0x10` 会返回 `E_COMPRESSION`。**

摘要位宽约定：32 字节摘要全零时表示“未声明/位宽 0”。压缩方式 1（SHA-256）或 2（HMAC-SHA256）要求非零 256 位摘要，否则返回 `E_DIGEST_WIDTH`。HMAC 需要本地密钥，工作台使用确定性默认密钥 `bsl-workbench-local-hmac-key-v1`，也可用 `--hmac-key` 覆盖；样本清单记录密钥 hex。

## 诊断码

| 业务码 | 触发条件 |
| --- | --- |
| `E_HEADER` | 魔数/版本/UTF-8 名称/段类型/头长度或头部自洽性错误 |
| `E_BOUNDS` | 段表、记录或载荷越界，或 `offset+length` 溢出 uint64 |
| `E_OVERLAP` | 两个载荷区间部分交叉、包含、被包含或完全相同 |
| `E_SELFREF` | 载荷区间引用段表自身所在区间 |
| `E_DUP_NAME` | 重复段名；最低记录偏移的首次出现作为真值 |
| `E_CHECKSUM` | SHA-256/HMAC-SHA256 摘要不匹配 |
| `E_DIGEST_WIDTH` | 压缩算法要求 256 位摘要，但声明摘要为全零/位宽不匹配 |
| `E_COMPRESSION` | 未知压缩方式 |
| `E_DEPTH` | 嵌套递归超过 `--max-depth`（默认 4） |
| `E_CYCLE` | 嵌套边与引用边构成环，报告稳定键和段名序列 |
| `E_EXPAND_LIMIT` | 累计嵌套展开字节超过 `--max-expanded-bytes` |
| `E_LOCK` | 同一工作目录已有并发写入持有文件锁 |
| `E_CHECKPOINT` | SQLite 账本摘要、检查点或快照不一致 |

所有诊断均带文件内局部偏移、嵌套深度和稳定细节；JSON 列表均排序，且不含墙钟时间，保证同输入可复现。

## 账本与检查点

SQLite 账本默认位于 `.bsl-workbench/ledger.sqlite3`：

- `segments` 主键为 `(file_id, canonical_key)`，保存轮次代 `generation`、状态代 `state_generation`、状态、水位/历史高水位、摘要状态和累计结论哈希。
- `segment_history` 逐轮保存状态演进，并可用相同链式哈希重放校验。
- `parse_rounds` 保存每轮完整 JSON 报告。
- `dependencies` 保存稳定依赖边。
- `checkpoints` 与账本更新在同一个 `BEGIN IMMEDIATE` 事务中提交。

头部/段表前置校验失败时不会调用账本提交，因此不会写入段状态或检查点。账本格局版本由 `LEDGER_LAYOUT_VERSION` 与策略版本共同控制；版本不匹配时打开账本会整体删表重建，不做增量续接。

每次文件事务提交后：

1. 原子写 `checkpoints/checkpoint-latest.json`（同目录临时文件 + `os.replace`）。
2. 原子写 `snapshots/checkpoint-latest.json`（同目录临时文件 + `os.replace`）。

恢复时以 SQLite 中最后一个完整检查点为准，重算账本摘要和历史哈希链，核对检查点文档与快照；发现 `*.tmp` 半写残留可在 `--recover` 下清理并从 SQLite 重写文档。同一工作目录的写入由 `workbench.lock` 非阻塞排他锁串行化。

## 修订失效

`revise` 读取两份诊断报告：

1. 按 `canonical_key` 对齐段。
2. 比较实际内容 SHA-256、摘要或声明摘要；新增/删除键也作为变化来源。
3. 依赖边 `source -> target` 表示 source 依赖 target。
4. 从变化段沿反向边做确定性 BFS，得到传递失效集合。
5. 输出 `changed_segments`、`invalidated_segments`、`recompute_segments` 和来源路径。

样本中 `/base` 变化后，闭包为 `/base`、`/mid`、`/top`。算法只依赖集合和稳定排序，打乱报告中的段或边顺序不会改变结果。

## 确定性样本

`python -m bsl_workbench generate-samples` 默认种子 `20261125`，生成：

- 合法 SHA-256/HMAC/不校验样本；
- 畸形头、非法 UTF-8 名称、未知段类型；
- 越界长度与 uint64 溢出；
- 部分重叠、完全包含、自引用、重复段名；
- 摘要不符、位宽不符、未知压缩方式；
- 深层嵌套、环引用、展开配额样本；
- v1/v2 修订批次；
- 三个用于强制终止恢复测试的有效文件。

`samples/generated/manifest.json` 记录每个样本的期望业务码和专用选项。
`samples/generated/snapshots/` 由同一生成脚本写出每份样本的稳定 JSON 诊断快照。

## 实际验证结果

当前执行环境为 Ubuntu/WSL、Python 3.10.12；仓库要求的固定依赖已安装在 WSL `.venv`。另外通过 uv 安装的 Windows CPython 3.11.16（`py -0p` 显示为 `Astral/CPython3.11.16`）创建了独立 Windows venv 并安装相同固定依赖，30 个测试在 Windows Python 3.11 下同样全部通过。

```text
pytest 8.2.0
numpy 1.26.4
30 passed
```

最终以实际运行为准：

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/verify_acceptance.py
```

`artifacts/acceptance-report.json` 保存 24 个样本的业务码核对、修订闭包和检查点摘要；`artifacts/validation-result.txt` 同时记录 WSL Python 3.10 与 Windows Python 3.11 的测试结果。Windows 默认 `python` 是 3.13.14，但 Python 3.11.16 可通过上述 uv 解释器路径使用。

## 代码结构

- `bsl_workbench/parser.py`：严格解析、冲突/摘要/嵌套/环检测。
- `bsl_workbench/builder.py`：测试与样本使用的确定性二进制构造器。
- `bsl_workbench/generator.py`：固定种子样本生成器。
- `bsl_workbench/revision.py`：修订比对与传递失效闭包。
- `bsl_workbench/graph.py`：DOT/Mermaid 段依赖图渲染。
- `bsl_workbench/ledger.py`：SQLite 状态代、水位、结论哈希和检查点事务。
- `bsl_workbench/checkpoint.py`：原子快照、一致性重放和恢复。
- `bsl_workbench/locking.py`：POSIX/Windows 非阻塞文件锁。
- `bsl_workbench/cli.py`：命令行入口。
- `tests/`：30 个 pytest 用例，包括子进程强杀恢复和 `E_LOCK`。
