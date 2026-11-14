# 安全 ZIP 暂存解压工具（safe_unzip）

将外部不可信 ZIP 包安全落入本地暂存目录：解压前**逐条**判定条目路径安全性，
全安全才解压；出现任一不安全条目则**整体拒绝**，目标目录零写入；损坏包与写入
失败均有明确错误、回滚与证据保留。仅使用 Python 标准库，要求 Python 3.10+，
不依赖 Docker 或任何第三方包。

## 文件清单

| 文件 | 作用 |
|---|---|
| `safe_unzip.py` | 核心工具：条目安全判定、解压、回滚、JSON 报告、CLI |
| `make_samples.py` | 确定性生成正常 / 恶意 / 损坏三类 ZIP 样本 |
| `test_safe_unzip.py` | 覆盖 9 条验收要求的 unittest 套件（10 个用例） |
| `demo_workspace/` | 运行样本生成后出现的本地演示工作区（已 gitignore） |

## 快速启动（可复现）

```powershell
# 1. 确认 Python 版本（>= 3.10；本机实测 3.14.7）
python --version

# 2. 生成确定性样本到 demo_workspace/samples（重复运行结果字节一致）
python -I make_samples.py

# 3. 运行全部验收测试（子进程均以 python -I 隔离模式运行，不加载当前目录模块）
python -I test_safe_unzip.py
```

## 命令行用法

```
python -I safe_unzip.py <zip_path> <target_dir> [report_path]
```

- `zip_path`：待检 ZIP 路径（必填）
- `target_dir`：目标暂存目录，不存在时仅在全部条目安全后创建（必填）
- `report_path`：JSON 报告输出路径（可选，默认写在目标目录同级的
  `<目标目录名>.report.json`，保证拒绝/损坏场景下目标目录内零写入）

退出码约定：

| 码 | 含义 |
|---|---|
| 0 | 全部条目安全，解压成功 |
| 2 | 参数缺失 / 路径无效（同时打印 usage） |
| 3 | ZIP 损坏或无法读取 |
| 4 | 存在不安全条目，整体拒绝，目标目录无写入 |
| 5 | 解压写入失败，已回滚到操作前状态 |
| 6 | 解压成功但报告文件写入失败（有 stderr 警告） |

## 业务操作样例

```powershell
# 正常包：解压并生成报告
python -I safe_unzip.py demo_workspace/samples/safe.zip `
    demo_workspace/staging/safe demo_workspace/reports/safe_report.json

# 恶意包：退出码 4，目标目录不创建，全部恶意条目按 ZIP 内顺序打印
python -I safe_unzip.py demo_workspace/samples/malicious.zip `
    demo_workspace/staging/mal

# 损坏包：退出码 3，明确报错，目标目录不创建、不产生误导性报告
python -I safe_unzip.py demo_workspace/samples/corrupt.zip `
    demo_workspace/staging/broken

# 受限目录（Windows 示例）：先制造只读/拒写目录，再解压 -> 退出码 5 + 回滚
$t = "demo_workspace/staging/restricted"
New-Item -ItemType Directory $t -Force | Out-Null
icacls $t /inheritance:r
icacls $t /grant:r "$($env:USERNAME):(RX)"
icacls $t /deny "$($env:USERNAME):(WD,AD,WA)"
python -I safe_unzip.py demo_workspace/samples/safe.zip $t `
    demo_workspace/reports/restricted_report.json   # 5，目录内无残留
icacls $t /reset /T /C                                  # 演示后恢复权限
```

## 安全判定规则（逐条，解压前完成，零写入）

对每个条目先把 `\` 统一归一化为 `/`，再判定，以下任一命中即 `unsafe`：

1. 以 `/` 开头：POSIX 绝对路径或 UNC 路径（`/etc/passwd`、`//host/share/x`）
2. 带盘符前缀：Windows 绝对/盘相对路径（`C:/x`、`C:x`）
3. 路径段含 `..`：父目录跳转（`../evil`、`a/../../evil`、`..\\evil`）
4. 归一化拼接后 `realpath` 与目标目录 `commonpath` 不等于目标目录：
   纵深防御，兜住符号链接等逃逸形式

安全条目 `reason` 为空字符串；不安全条目给出具体原因。判定阶段不触碰文件
系统目标目录，因此拒绝场景天然满足"零写入"。

## JSON 报告结构

```json
{
  "zip_path": "绝对路径",
  "target_dir": "绝对路径",
  "result": "extracted | failed",
  "entry_count": 4,
  "unsafe_count": 0,
  "error": null,
  "entries": [
    { "name": "readme.txt", "status": "safe", "reason": "" }
  ]
}
```

- 每条目固定含 `name`(str)、`status`(`safe`/`unsafe`)、`reason`(str) 三字段；
- 条目顺序与 ZIP 内顺序一致，安全与不安全条目均逐条可见；
- 整体拒绝 / 损坏场景**不写报告文件**（拒绝原因完整打印在 stderr，避免在目标
  目录产生任何写入）；写入失败场景写 `result=failed` 报告作为证据；
- 报告默认放在目标目录**同级**而非目标目录内部。

## 关键设计与边界

- **先检后写**：`ZipFile.testzip()` 做 CRC/结构完整性校验，随后逐条路径判定，
  全部通过后才 `makedirs` 目标目录。
- **回滚机制**：记录本次运行新建的每个文件与目录；写入中途失败时先删文件、
  再按叶子到根的顺序 `rmdir` 空目录，预存文件与预存目录不受影响；`rmdir`
  仅在目录为空时成功，不会误删非本次创建的内容。
- **时间戳**：按 ZIP 记录的 `date_time`（UTC）通过 `os.utime` 还原文件与显式
  目录条目的修改时间；设置失败只发 stderr 警告，不影响解压与报告（要求 9）。
- **时间戳粒度**：ZIP DOS 时间戳本身为 2 秒粒度，属格式固有限制。
- **不使用 `extractall`**：自带分块流式写入（64 KiB），避免库函数对路径的
  隐式处理；不跟随/创建符号链接条目以外的特殊类型（样本只覆盖常规文件与目录）。
- **隔离运行**：所有读取不可信样本的 Python 调用均使用 `-I`，不把脚本目录/
  当前目录加入 `sys.path`，防止样本目录中被植入 `json.py` 等劫持模块。

## 验收要求与测试对照

| # | 验收要求 | 测试方法 |
|---|---|---|
| 1 | 逐条路径 containment 判定，覆盖两类恶意条目 | `test_classify_entry_rules`、`test_malicious_zip_rejected` |
| 2 | 不安全即整体拒绝、目标零写入、可见错误 | `test_malicious_zip_rejected`（含目标不存在/已存在两种） |
| 3 | 全安全按原结构解压 + 字段齐全的 JSON 报告 | `test_safe_zip_extraction_and_report` |
| 4 | 全部不安全条目按 ZIP 顺序列出，安全条目也可见 | `test_malicious_zip_rejected` |
| 5 | 损坏 ZIP 明确报错、目标不创建、无误导性报告 | `test_corrupt_zip` |
| 6 | 写入失败回滚到操作前状态并报告原因 | `test_restricted_directory_write_failure`（真实 OS 权限）、`test_mid_extraction_failure_rolls_back` |
| 7 | 报告 JSON 字段类型与取值合法 | `test_report_schema` |
| 8 | 两参数 CLI，缺参/无效路径显示用法且非零退出 | `test_cli_arguments` |
| 9 | 保留 ZIP 时间戳；时间戳失败不阻断 | `test_timestamps_preserved`、`test_timestamp_failure_tolerated` |

实测：`Ran 10 tests ... OK`（Windows 11 + Python 3.14.7，标准库）。
