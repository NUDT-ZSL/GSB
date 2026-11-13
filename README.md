# 本机文件上传服务安全审计实验（Windows / Python 标准库）

在 Windows 本机搭建一个**仅监听 127.0.0.1** 的自制文件上传服务（含存在漏洞版与已修复版），
并提供一个安全审计工具：构造授权范围内的确定性探测请求，判定

1. **路径遍历写入**（文件名含 `../`、`..\`、绝对路径时是否写到上传目录之外）；
2. **扩展名/内容不一致的类型绕过**（是否只按扩展名放行、忽略文件真实内容）。

工具输出 UTF-8 编码的结构化 JSON 报告，支持对修复后版本做回归验证。
**不使用 Docker 或任何容器，不访问 127.0.0.1 之外的任何目标，仅依赖 Python 3.10+ 标准库。**

---

## 1. 目录结构

```
project40/
├─ vuln_server/
│  └─ server_vuln.py        # 存在漏洞的上传服务（默认 127.0.0.1:8000）
├─ fixed_server/
│  └─ server_fixed.py       # 已修复的上传服务（默认 127.0.0.1:8001）
├─ auditor/
│  └─ auditor.py            # 安全审计工具（探测 + 判定 + JSON 报告 + 清理）
├─ examples/
│  └─ manual_upload.py      # 手工业务操作样例（正常/遍历/伪装三类上传）
├─ tests/
│  └─ test_e2e.py           # 端到端自动化测试（4 个用例，覆盖全部验收点）
├─ run_demo.ps1             # 一键演示脚本（漏洞版→不可达→修复版回归）
└─ README.md
```

运行后自动产生（已在 `.gitignore` 中忽略）：

```
demo_runtime/              # 漏洞版运行沙箱
├─ uploads/                #   上传目录（--dir 指定，工具自动创建）
├─ report_vuln.json        #   漏洞版审计报告
└─ auditscan_*.png         #   越界写入证据（仅在未加 --clean-evidence 时保留）
demo_runtime_fixed/        # 修复版运行沙箱（结构同上，预期无任何越界文件）
```

---

## 2. 环境要求

- Windows 10/11，Python 3.10+（本机实测 Python 3.14.7）
- 无第三方依赖，全部使用标准库（`http.server`、`urllib`、`json`、`argparse` 等）
- 建议用 `python -I`（隔离模式）运行，避免当前目录恶意模块（如 planted `json.py`）干扰

---

## 3. 快速开始

### 3.1 一键演示（推荐）

在项目根目录打开 PowerShell（5.1 或 7+ 均可）：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
```

脚本自动完成三个场景并打印预期值对照：

| 场景 | 目标服务 | 预期结果 |
|---|---|---|
| A 漏洞版审计 | 127.0.0.1:8000 | 高危路径遍历 + 中危类型绕过；目录外生成 3 个证据文件；上传目录内 0 残留 |
| B 服务不可达 | 端口无监听 | 打印中文连接错误；退出码 1；不生成报告 |
| C 修复版回归 | 127.0.0.1:8001 | 高/中危均为 0；目录外 0 新增文件；仅保留 1 条低危提示 |

### 3.2 手工分步启动

**第一步：启动服务（两个终端，或用 Start-Process 后台启动）**

```powershell
# 终端 1：漏洞版（端口 8000）
$env:UPLOAD_DIR = "D:\demo_GSB\demo\projects\project40\demo_runtime\uploads"
python -I .\vuln_server\server_vuln.py

# 终端 2：修复版（端口 8001）
$env:UPLOAD_DIR = "D:\demo_GSB\demo\projects\project40\demo_runtime_fixed\uploads"
python -I .\fixed_server\server_fixed.py
```

服务仅绑定 `127.0.0.1`，局域网/外部网络不可达。可用环境变量 `PORT` 改端口、`UPLOAD_DIR` 改上传目录。

**第二步：运行审计工具**

```powershell
# 对漏洞版（自定义上传目录 + 自定义报告路径）
python -I .\auditor\auditor.py `
  --url http://127.0.0.1:8000 `
  --dir  "D:\demo_GSB\demo\projects\project40\demo_runtime\uploads" `
  --report "D:\demo_GSB\demo\projects\project40\demo_runtime\report_vuln.json"

# 对修复版回归
python -I .\auditor\auditor.py `
  --url http://127.0.0.1:8001 `
  --dir  "D:\demo_GSB\demo\projects\project40\demo_runtime_fixed\uploads" `
  --report "D:\demo_GSB\demo\projects\project40\demo_runtime_fixed\report_fixed.json"
```

**第三步：手工业务样例（不依赖审计工具，直接发 multipart 请求）**

```powershell
python -I .\examples\manual_upload.py --url http://127.0.0.1:8000 all
# 可选动作：normal（正常 PNG）/ traversal（../ 遍历）/ fake（PHP 内容伪装 PNG）/ all
```

**第四步：自动化测试**

```powershell
python -I .\tests\test_e2e.py
# 注意：-I 隔离模式下 cwd 不在 sys.path，故采用“直接运行脚本”而非 -m unittest
```

---

## 4. 审计工具命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--url` | `http://127.0.0.1:8000` | 上传服务基址，仅允许指向本机授权目标 |
| `--dir` | `./uploads` | 服务上传目录，工具自动创建；越界判定与受影响绝对路径均以此为基准 |
| `--sandbox-root` | 上传目录的父目录 | 测试沙箱根；任何探测的预期落点超出该目录会**拒绝发送** |
| `--report` | `./report.json` | UTF-8 JSON 报告输出路径 |
| `--timeout` | `5` | HTTP 超时秒数 |
| `--clean-evidence` | 关 | 结束时连上传目录之外的漏洞证据一并删除（默认保留证据供人工核验） |

退出码：`0` 审计流程正常完成（报告中可能有漏洞）；`1` 服务不可达等连接错误；`2` 服务响应异常/基线失败。
**任何失败路径都会先删除旧报告，绝不留下误导性 `report.json`。**

---

## 5. JSON 报告结构（UTF-8）

```json
{
  "report_version": "1.0",
  "generated_at": "2026-11-14T07:21:04+08:00",          // ISO 8601（带时区偏移）
  "scan_token": "20261114072104_36c42be7",              // 每次扫描唯一标识，也是探测文件标记
  "target": { "base_url": "...", "upload_dir": "...", "sandbox_root": "..." },
  "summary": { "total_findings": 3, "high_risk": 1, "medium_risk": 1, "low_risk": 1,
               "vulnerable": true },
  "findings": [
    {
      "id": "PATH_TRAVERSAL_WRITE",
      "vulnerability_type": "路径遍历写入",
      "risk_level": "高",                                // 高 / 中 / 低
      "title": "...",
      "status": "confirmed",
      "affected_paths": ["D:\\...\\auditscan_..._traversal_unix.png"],  // 绝对路径
      "evidence": [ { "technique": "...", "submitted_filename": "...",
                      "http_status": 200, "file_verified_on_disk": true,
                      "written_outside_upload_dir": true } ],
      "detected_at": "2026-11-14T07:21:04+08:00",
      "remediation": {
        "path_normalization": "basename 剥离 + realpath 规范化 + commonpath 边界校验 …",
        "content_type_validation": "扩展名白名单 + 魔数(magic bytes) 双重校验 …"
      }
    }
  ],
  "probe_checks": [ /* 每条探测的 HTTP 状态与判定结果，便于逐项复核 */ ],
  "warnings": [],
  "confinement": { "all_probe_paths_within_sandbox": true },
  "cleanup": { "removed_stale_files_at_start": [], "removed_test_files_at_end": [],
               "retained_evidence_outside_upload_dir": [] }
}
```

- `findings` 严格按风险等级**降序**排列（高 → 中 → 低）。
- 每个漏洞条目都带 `remediation.path_normalization` 与 `remediation.content_type_validation`
  两类可操作修复要点，可直接定位到服务端待修改的校验逻辑。
- 漏洞类型与风险等级：路径遍历写入=**高**；类型绕过=**中**；响应回传绝对路径信息泄露=**低**。

---

## 6. 两版服务的漏洞与修复对照

### 漏洞版 `vuln_server/server_vuln.py`

```python
# 漏洞 2：只看扩展名，不看内容
ext = os.path.splitext(filename)[1].lower()
if ext not in ALLOWED_EXTENSIONS:
    return 400
# 漏洞 1：未规范化、未做边界校验，直接拼接客户端文件名
destination = os.path.join(UPLOAD_DIR, filename)
open(destination, "wb").write(content)
```

### 修复版 `fixed_server/server_fixed.py`

```python
# 修复 1：basename 剥离目录成分 → realpath 规范化 → commonpath 强制边界
safe_name = os.path.basename(filename.replace("\\", "/"))
target = os.path.realpath(os.path.join(upload_root, safe_name))
if os.path.commonpath([upload_root, target]) != upload_root:
    return 400  # path_traversal_blocked

# 修复 2：扩展名白名单 + 魔数校验，内容与扩展名不一致即拒绝
if ext not in ALLOWED_EXTENSIONS:
    return 400
if not content_matches_extension(ext, content):   # PNG: 89 50 4E 47、JPG: FF D8 FF …
    return 400  # content_type_mismatch
```

修复版实测行为：`../x.png` 被收编为上传目录内的 `x.png`（不越界）；
PHP/PE/纯文本内容伪装成 `.png/.jpg/.pdf` 一律 HTTP 400 拒绝。

---

## 7. 实际验证结果（2026-11-14 本机实测）

| 验收点 | 实测结果 |
|---|---|
| 1. 路径遍历探测 | 漏洞版 3 种向量（`../`、`..\`、盘符绝对路径）全部 HTTP 200，磁盘核实 3 个文件写到 `uploads` 之外，报告标记**高危** |
| 2. 类型绕过探测 | 漏洞版 3 个伪装文件（PHP→.png、PE→.jpg、文本→.pdf）全部被错误接受，报告列出文件名，标记**中危** |
| 3. JSON 报告 | UTF-8、字段齐全、ISO 8601 时间戳、高/中/低降序，`datetime.fromisoformat` 可解析 |
| 4. 修复要点 | 每条 finding 均含路径规范化、内容类型校验两类说明文本 |
| 5. 沙箱与清理 | 上传目录内测试文件运行结束 0 残留；目录外默认只保留带 token 的证据，`--clean-evidence` 后同样为 0 |
| 6. `--dir` 自定义 | 以绝对路径指定自定义目录，受影响路径均为该目录内/外的真实绝对落点 |
| 7. 服务不可达 | 中文错误 `错误：无法连接上传服务…[WinError 10061]…`，退出码 1，无 `report.json`（旧报告也被删除） |
| 8. 修复版回归 | 高/中危为 0，`vulnerable=false`，上传目录外无新增文件；仅低危信息泄露提示 |
| 自动化测试 | `python -I tests\test_e2e.py` → **Ran 4 tests … OK** |

---

## 8. 安全边界与合规说明

- 两个服务硬编码绑定 `127.0.0.1`，不提供对外监听能力；审计工具默认目标也是 127.0.0.1。
- 所有探测文件名带统一前缀 `auditscan_` 与每次运行唯一 token；发送前强制断言预期落点位于
  `--sandbox-root`（默认上传目录父目录）之内，超出则抛错中止，杜绝写到系统其他位置。
- 探测样本为确定性的本地构造内容（PNG/JPG/PDF 魔数、占位文本），不包含真实恶意载荷，
  不发起任何网络攻击、不访问未授权目标。
- 本项目仅用于授权范围内的安全教学与防御验证。
