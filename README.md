# 上传目录落盘核验台（upload-audit）

内部文件上传服务上线前的核验工具：读取**上传目录**与**清单 CSV**，逐条比对清单
声明信息与磁盘真实文件，产出「存在风险 / 安全 / 无法判定」三类 JSON 报告，并支持
修复前后两份报告的对比。

- 仅使用 Python 3.10+ 标准库，无第三方依赖，无网络访问。
- **只读取文件内容与元数据**，绝不执行、导入或运行任何被扫描的上传文件。
- 全部样本由代码确定性生成，同一输出目录下重复运行结果一致（报告中不写入时间戳；
  报告会如实记录上传目录与清单的绝对路径，因此换目录时路径字段会不同）。

## 快速开始

```powershell
# 1) 生成确定性样本（修复前 / 修复后 / 回归）
python -I make_samples.py --out demo_out --force

# 2) 修复前扫描
python -I upload_audit.py demo_out/before/upload demo_out/before/manifest.csv `
    --report reports/before.json

# 3) 修复后复跑并与修复前对比
python -I upload_audit.py demo_out/after/upload demo_out/after/manifest.csv `
    --report reports/after.json `
    --baseline reports/before.json `
    --compare-report reports/compare.json

# 4) 一条命令跑完整演示（含退出码与拒覆盖验证）
python -I run_demo.py --clean

# 5) 验收测试（42 项）
python -I -m unittest discover -s tests -v
```

> 所有命令都在项目根目录执行。`-I` 启动隔离模式，避免当前目录中的同名模块
> 干扰，也避免意外导入上传目录里的任何文件。

## 命令行

```
upload_audit.py <upload_dir> <manifest.csv> --report <out.json>
                [--baseline <before.json> --compare-report <compare.json>]
                [--fail-on-risk] [--force-overwrite] [--timestamp <ISO8601>]

upload_audit.py --validate-report <report.json>
```

| 参数 | 说明 |
| --- | --- |
| `upload_dir` | 上传目录；不存在或不是目录 → 退出码 4，且不生成报告 |
| `manifest` | UTF-8 CSV 清单（允许 BOM），必需列 `relative_path`、`declared_ext` |
| `--report` | 扫描报告输出路径（必填） |
| `--baseline` | 修复前报告；必须与 `--compare-report` 成对出现，否则退出码 2 |
| `--compare-report` | 对比报告输出路径 |
| `--validate-report` | 只校验既有报告，不扫描、不写文件 |
| `--fail-on-risk` | 发现任一风险时以退出码 1 结束（适合 CI 卡口） |
| `--force-overwrite` | 目标处已有计数不一致的报告时仍强制覆盖 |
| `--timestamp` | 写入 `generated_at`；默认不写，保证输出可复现 |

保护措施：`--report` 指向清单或基线文件时会以退出码 2 拒绝执行；
`--compare-report` 不能与基线或当前扫描报告同路径；基线必须是扫描报告
（传入对比报告会以退出码 3 拒绝）。

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 正常完成 |
| 1 | 指定 `--fail-on-risk` 且发现风险 |
| 2 | 命令行用法错误（缺少/多余参数、参数组合不完整） |
| 3 | 报告完整性校验失败；拒绝覆盖旧的损坏报告 |
| 4 | 上传目录不存在或不是目录（不生成报告） |
| 5 | 清单文件错误（缺失、非 UTF-8、缺列、空清单） |
| 6 | 写出报告时的 I/O 错误 |

## 判定规则

判定按下列顺序进行，命中即返回：

| 顺序 | 条件 | 结论 | `reason_code` |
| --- | --- | --- | --- |
| 1 | 清单行列数与表头不符 | 无法判定 | `malformed_row` |
| 2 | `relative_path` 为空 | 无法判定 | `empty_path` |
| 3 | 含 NUL 字符 | 存在风险（路径穿越） | `nul_byte` |
| 4 | 绝对路径 / 盘符 / UNC / 根路径 | 存在风险（路径穿越） | `absolute_path` |
| 5 | 规范化后 `..` 越过上传根 | 存在风险（路径穿越） | `path_traversal` |
| 6 | 符号链接 / 目录联接真实路径落在上传目录外 | 存在风险（路径穿越） | `symlink_escape` |
| 7 | 磁盘不存在 | 无法判定 | `missing_file` |
| 8 | 悬空链接 / 目录 / 非普通文件 | 无法判定 | `broken_symlink` / `is_directory` / `not_regular_file` |
| 9 | 读取失败（权限等） | 无法判定 | `unreadable` |
| 10 | 扩展名与 `declared_ext` 不一致（大小写不敏感） | 存在风险（扩展名伪装） | `extension_disguise` |
| 11 | 以上均不命中 | 安全 | `ok` |

补充说明：

- **规范化**同时接受 `/` 与 `\` 作为分隔符，先消除 `.` 与可消解的 `..`，再判断
  是否越界；因此 `sub/../../evil.txt` 与 `../evil.txt` 都会被识别。
- **符号链接逃逸**用 `os.path.realpath` 解析后与上传目录真实路径做公共前缀比较
  （按平台大小写语义），Windows 目录联接（junction）同样覆盖。链接指回上传目录
  内部时属于安全。
- **扩展名**采用后缀比较且大小写不敏感：`.PNG` 与 `.png` 一致；`.tar.gz`
  这类复合扩展名可正确匹配；`report.pdf.exe` 声明 `.pdf` 会判为伪装。
- **无法判定**绝不并入安全：缺失文件、不可读文件、目录目标、清单坏行都单独计入
  第三类，并在 `reasons` 中写明原因。
- **稳定排序**：结果按 `relative_path` 升序；相同路径保持清单中的原始出现顺序。
  `index` / `manifest_line` 保留原始位置，便于回溯。

## 报告结构

### 扫描报告

```json
{
  "tool": "upload-audit",
  "version": "1.0.0",
  "schema_version": 1,
  "report_type": "scan",
  "upload_dir": "...",
  "manifest": "...",
  "categories": ["存在风险", "安全", "无法判定"],
  "risk_type_labels": {"path_traversal": "路径穿越", "extension_disguise": "扩展名伪装"},
  "counts": {"存在风险": 6, "安全": 5, "无法判定": 2, "total": 13},
  "results": [
    {
      "index": 4,
      "manifest_line": 6,
      "relative_path": "../evil.txt",
      "normalized_path": "../evil.txt",
      "declared_ext": ".txt",
      "actual_ext": null,
      "status": "存在风险",
      "risk_types": ["path_traversal"],
      "reason_code": "path_traversal",
      "reasons": ["relative_path 规范化后跳出上传目录（../evil.txt）"],
      "resolved_path": null,
      "size_bytes": null
    }
  ],
  "manifest_meta": {"header": ["relative_path", "declared_ext"], "data_rows": 13, "blank_rows_skipped": 1}
}
```

`status` 只取三个值，与 `counts` 的三个中文键一一对应。

### 对比报告

```json
{
  "report_type": "comparison",
  "summary": {"baseline_risks": 6, "current_risks": 0, "new_risks": 0, "resolved_risks": 6},
  "新增风险": [],
  "已消除风险": [
    {"relative_path": "../evil.txt", "risk_type": "path_traversal", "risk_label": "路径穿越", "...": "..."}
  ]
}
```

对比按 `(规范化路径, 声明扩展名, 风险类型)` 匹配（大小写不敏感），重复条目按出现
次数一一抵消；基线中不存在的风险进 `新增风险`，当前报告中消失的进 `已消除风险`。

## 完整性校验

写出前工具会自检报告：逐条 `status` 必须合法，三类计数与逐条统计一致，
`counts.total` 等于数组长度；对比报告的两组条目与 `summary` 一致。
若目标位置**已存在**一份计数与逐条结果不一致的报告，工具**拒绝覆盖**、
以退出码 3 结束，旧文件字节保持不变（可用 `--force-overwrite` 显式覆盖）。
`--validate-report` 可独立复核任意一份报告。

## 不执行上传文件

扫描对每个文件只做 `os.stat` 与读取文件头 4096 字节（`open(..., "rb")`），
不调用任何解释器、不 import、不 `exec`。样本中的 `run_sample.py` / `run_sample.cmd`
一旦被执行就会写出 `EXECUTED_MARKER.txt`；演示脚本与测试都会断言该标记文件不存在。

## 业务操作样例（已实际运行）

```
步骤 1/7  生成样本                       exit 0
步骤 2/7  不带参数                        exit 2（打印用法）
步骤 3/7  修复前扫描                      共 13 条；存在风险 6；安全 5；无法判定 2
步骤 4/7  确认 EXECUTED_MARKER.txt 不存在  不存在 ✓
步骤 5/7  修复后扫描 + 对比                共 9 条；存在风险 0；安全 9；无法判定 0
                                        新增 0；已消除 6
步骤 6/7  篡改计数后校验/重跑              exit 3；旧报告字节未变化 ✓
步骤 7/7  上传目录不存在                   exit 4；未产生报告文件 ✓
```

修复前被判定的 6 条风险：

| relative_path | 风险 |
| --- | --- |
| `../evil.txt` | 路径穿越（`..` 越界） |
| `sub/../../evil2.txt` | 路径穿越（先进入再越界） |
| `<绝对路径>/outside/abs_target.txt` | 路径穿越（绝对路径） |
| `escape_dir/secret.txt` | 路径穿越（目录联接逃逸） |
| `payload.exe` 声明 `.png` | 扩展名伪装 |
| `dup.txt` 重复条目中声明 `.md` | 扩展名伪装 |

无法判定 2 条：`missing.dat`（磁盘缺失）、一行缺列的格式错误行。

修复后复跑，上述 6 条全部进入 `已消除风险`；`photo.png` 声明 `.PNG` 仍判安全
（大小写差异不算伪装）。

回归场景（`demo_out/regression`）演示新增风险识别：

```powershell
python -I upload_audit.py demo_out/regression/upload demo_out/regression/manifest.csv `
    --report reports/regression.json `
    --baseline reports/after.json `
    --compare-report reports/compare_regression.json
# => 新增风险 1 条：invoice.cmd 声明 .pdf
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `upload_audit.py` | 核验工具本体（CLI + 判定 + 报告 + 对比 + 校验） |
| `make_samples.py` | 确定性生成修复前/修复后/回归样本，含可执行标记样本与目录联接 |
| `run_demo.py` | 端到端演示，含退出码与拒覆盖断言 |
| `tests/test_upload_audit.py` | 42 项 unittest 验收测试 |
| `demo_out/`、`reports/` | 运行产物，已 gitignore，可随时重建 |

### 样本生成安全说明

`make_samples.py` 只清理带 `.upload-audit-sample` 标记的目录；非空且无标记的
目录会直接拒绝。清理时对目录联接只删链接本身，绝不递归进入目标目录。
