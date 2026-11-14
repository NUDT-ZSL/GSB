# 本地恶意特征扫描与隔离工具

纯 Python 标准库实现（无需任何第三方依赖，Python 3.10+，已在 Windows 11 / Python 3.14 实测）。
递归扫描目录、计算 SHA-256、按模拟特征判定恶意文件、移入隔离区，并输出结构化 JSON 报告。

## 1. 文件说明

| 文件 | 作用 |
|---|---|
| `malware_scanner.py` | 扫描器主体：递归扫描、哈希、判定、隔离、JSON 报告、`--dry-run` |
| `make_samples.py` | 确定性生成测试样本树（正常/单关键字/双关键字/空文件/隐藏项/链接） |
| `lock_holder.py` | 通过 Windows 独占句柄模拟“文件被占用、无法读取”场景 |
| `demo_run.py` | 端到端验证脚本，覆盖全部 10 项验收点（137 项断言） |
| `verification_log.txt` | 最近一次完整验证的实测日志（137 项全部 PASS） |
| `demo_workspace/` | 各场景的样本、隔离区与报告产物（已加入 `.gitignore`） |

## 2. 判定与状态模型

- **判定规则**：文件内容（二进制安全、大小写不敏感）**同时**出现 `EVIL` 与 `MALWARE` 即判恶意；仅出现其一或均不出现为正常。流式分块（64 KB）读取，关键字横跨块边界仍可正确识别（已专项测试）。
- **状态取值**：

| 状态 | 含义 |
|---|---|
| `clean` | 正常文件（含 0 字节文件） |
| `malicious` | dry-run 模式下确认恶意、未做处置 |
| `quarantined` | 确认恶意且已成功移入隔离区 |
| `unquarantined` | 确认恶意但隔离失败（如同名冲突），文件保留原位，`error` 附原因 |
| `unreadable` | 无法读取/鉴定（权限拒绝、被独占等），`error` 附原因，其他文件不受影响 |

- **遍历规则**：递归子目录；跳过符号链接、Windows junction 等重解析点，以及名称以 `.` 开头的隐藏文件和目录；报告路径为相对输入根目录的 posix 形式（`/` 分隔），按字典序升序排列。
- **退出码**：`0` 正常；`1` 报告中存在 `unreadable` 或 `unquarantined` 文件；`2` 参数错误或扫描根目录不存在。

## 3. 本地启动方式（可复现）

所有命令均在项目根目录的 PowerShell 中执行。建议统一加 `-X utf8 -I`：
`-X utf8` 保证中文输出在任何控制台代码页下不乱码；`-I` 隔离用户站点包与当前目录，防止同名模块（如 `json.py`）被意外加载。

### 3.1 一键全量验证（推荐演示入口）

```powershell
python -X utf8 -I demo_run.py
```

依次执行 6 个场景（标准扫描、隔离冲突、ACL 拒绝读取、文件独占、dry-run、参数校验），
结束时打印 `ALL CHECKS PASSED`，产物保留在 `demo_workspace/<场景>/`。

### 3.2 手动业务操作样例

```powershell
# 1) 确定性生成样本树
python -X utf8 -I make_samples.py --root demo_workspace\manual\samples

# 2) 仅检测不隔离（原文件不动，隔离字段为 null）
python -X utf8 -I malware_scanner.py `
  --root demo_workspace\manual\samples --dry-run `
  --report demo_workspace\manual\dryrun.json

# 3) 正式扫描并隔离（隔离区不存在会自动创建）
python -X utf8 -I malware_scanner.py `
  --root demo_workspace\manual\samples `
  --quarantine demo_workspace\manual\quarantine `
  --report demo_workspace\manual\report.json

# 4) 报告直接输出到标准输出（省略 --report）
python -X utf8 -I malware_scanner.py --root <任意目录> --quarantine <隔离区>
```

### 3.3 故障场景的手工构造

```powershell
# 无法读取（ACL 拒绝当前用户读取；验证后用 /remove:d 恢复）
icacls <文件> /deny "*S-1-1-0:(R)"
python -X utf8 -I malware_scanner.py --root <目录> --quarantine <隔离区>  # 退出码 1
icacls <文件> /remove:d "*S-1-1-0"

# 文件被其他进程独占（另开一个终端运行，保持 30 秒独占句柄）
python -X utf8 -I lock_holder.py --file <文件> --seconds 30

# 无法隔离（预先在隔离区放置同名文件）
New-Item -ItemType Directory -Force <隔离区> | Out-Null
Set-Content <隔离区>\payload.bin "PRE-EXISTING PLACEHOLDER"
```

## 4. JSON 报告结构

```json
{
  "scan_root": "绝对路径",
  "quarantine_dir": "隔离区绝对路径（dry-run 时仍回显参数，未使用则为 null）",
  "dry_run": false,
  "generated_at": "2026-11-14T00:53:02Z",
  "summary": {
    "total": 6, "clean": 4, "malicious": 2,
    "quarantined": 2, "unquarantined": 0, "unreadable": 0
  },
  "files": [
    {
      "path": "sub/deeper/payload.bin",
      "sha256": "58d1e8341cc5ca259cf14ea142bd0ad6a26f39e01bb5470f7d7473650899e200",
      "size": 46,
      "status": "quarantined",
      "original_path": "原绝对路径",
      "quarantined_path": "隔离后绝对路径",
      "error": null
    }
  ]
}
```

字段约定：`sha256` 为 64 位小写 hex（不可读时为 `null`）；`size` 为字节数；
`original_path` / `quarantined_path` 仅 `quarantined` 状态非空，其余为 `null`；
`error` 仅 `unquarantined` / `unreadable` 状态非空。空文件哈希固定为
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。

## 5. 验收点对照与实测结果

实测环境：Windows 11（10.0.26300）、Python 3.14.7，无第三方库、无容器。
完整断言输出见 `verification_log.txt`（137 项检查，0 失败）。

| # | 验收点 | 覆盖场景 | 实测结果 |
|---|---|---|---|
| 1 | 递归扫描、SHA-256、JSON 含哈希/大小/状态 | S1 | 6 个常规文件均含 64 位 hex 哈希、字节大小与状态 |
| 2 | 仅含 EVIL 或仅含 MALWARE 判 clean | S1 | `evil_only.txt`、`malware_only.txt` 均为 clean |
| 3 | 恶意文件移入隔离区并记录原/新路径，其余不动 | S1 | 2 个恶意文件原位置消失、隔离区同名出现，哈希一致，4 个 clean 文件原位保留 |
| 4 | 隔离区自动创建；同名冲突标记 unquarantined 并附错误 | S2 | 预放 `payload.bin` 后，原文件保留原位、状态 unquarantined、error 为 FileExistsError，另一恶意文件正常隔离 |
| 5 | 不可读文件标记 unreadable 附错误，其他文件正常 | S3 | `icacls /deny` 后该文件 unreadable（PermissionError），其余 5 个正常；测试后自动恢复权限 |
| 6 | 0 字节文件 clean，空串 SHA-256 | S1 | `empty.dat` 哈希 `e3b0c442…b855`，状态 clean |
| 7 | 递归子目录、相对路径、跳过隐藏项与链接 | S1 | 2 个隐藏项与 junction（无符号链接权限时的降级链接）均跳过，路径为相对形式 |
| 8 | 路径字典序排列，摘要计数与明细一致 | 全部场景 | 每个场景均校验升序与 6 项计数逐项吻合 |
| 9 | 单文件失败不中断；不可读/不可隔离时退出码非 0 | S2/S3/S4 | 独占句柄下锁定文件 unreadable，其余正常隔离/判定，退出码为 1 |
| 10 | `--dry-run` 只检测不隔离，隔离字段 null，状态准确 | S5 | 2 个文件状态 malicious、隔离字段 null、隔离区未创建、扫描前后样本树哈希逐一相同 |

补充实测：
- **跨块边界**：构造关键字恰好横跨 64 KB 读取边界的样本，双关键字仍判 malicious、单关键字判 clean。
- **链接健壮性**：实测发现 Windows junction 不被 `os.path.islink` 识别（返回 False），
  会导致 `os.walk` 跟随联接重复扫描；扫描器已通过重解析点属性（`FILE_ATTRIBUTE_REPARSE_POINT`）
  显式排除该类目录与文件。
- **参数校验**：非 dry-run 缺少 `--quarantine` 时退出码 2 并打印用法提示。

## 6. 设计要点与边界说明

- 哈希与判定在一次流式读取中完成，内存占用与文件大小无关（固定 64 KB 缓冲 + 7 字节跨块回溯窗口）。
- 隔离采用“目标已存在即失败”的保守策略，绝不覆盖隔离区中的已有文件；移动失败时源文件保持原位。
- 每个文件的鉴定与处置均有独立异常捕获，任何单文件异常只影响自身记录。
- 隔离区为扁平结构（按 basename 存放），因此不同子目录下的同名恶意文件第二个起会标记
  `unquarantined`，该行为在报告中显式可见、可审计；如需保留目录层级可在 `quarantine_file` 中扩展。
