# 文档预览服务路径穿越漏洞 · 本地演示环境

安全团队内部授权演示：同一套确定性本地样本上，对"按文档名返回 uploads 内容"的预览服务做越权读取检测，并对比加固前后行为。**全程仅访问 127.0.0.1，不接触任何线上目标，不依赖 Docker / 第三方包 / 外部网络。**

## 环境要求

- Python 3.10+（已在 Python 3.14.7 / Windows 11 验证）
- 仅使用标准库：`http.server`、`urllib`、`json` 等（见各文件 import，无任何第三方依赖）

## 文件说明

| 文件 | 作用 |
|---|---|
| `common.py` | 路径常量 + 确定性样本生成（固定内容，无随机/时间戳） |
| `vulnerable_server.py` | 未加固服务：直接拼接 `uploads + 文档名`，存在路径穿越 |
| `hardened_server.py` | 加固服务：文件名校验 + realpath/commonpath 目录约束 + 通用拒绝文案 |
| `detector.py` | 检测入口：对授权本地服务尝试越权读取，输出固定字段 JSON 报告 |
| `run_all.py` | 一键编排：生成样本 → 起两个服务 → 11 项验收 → 检测 → 对比报告 |
| `samples/` | 生成的样本（`uploads/` 内公开文件 + `outside/secret.txt` 目录外机密样本） |
| `reports/` | 检测报告与对比报告 |

## 一键复现

```powershell
python run_all.py
```

输出 11 项验收结果（当前全部 PASS），并生成：

- `reports/vulnerable_report.json` — 结论 `VULNERABLE`
- `reports/hardened_report.json` — 结论 `NOT_VULNERABLE`
- `reports/comparison_report.json` — 未加固在前、加固在后，字段顺序一致

报告无时间戳等不确定字段，多次运行逐字节一致（已验证）。

## 分步演示（业务操作样例）

```powershell
# 1. 生成确定性样本
python common.py

# 2. 启动服务（两个窗口，或后台）
python vulnerable_server.py 18081   # 未加固
python hardened_server.py 18082     # 加固

# 3. 正常预览：200，正文与样本一致
curl.exe "http://127.0.0.1:18081/preview?name=hello.txt"

# 4. 不存在的文档：404 + "document not found"
curl.exe "http://127.0.0.1:18081/preview?name=no_such.txt"

# 5. 越权读取（未加固：200 且返回目录外机密内容；加固：403 access denied）
curl.exe "http://127.0.0.1:18081/preview?name=../outside/secret.txt"
curl.exe "http://127.0.0.1:18082/preview?name=../outside/secret.txt"

# 6. 边界输入（加固服务：均 4xx，服务不崩溃）
curl.exe "http://127.0.0.1:18082/preview?name="                  # 空名 -> 400
curl.exe "http://127.0.0.1:18082/preview?name=aaaa...5000个"     # 超长名 -> 400
curl.exe "http://127.0.0.1:18082/preview?name=binary.bin"        # 非UTF-8 -> 415

# 7. 检测入口（退出码：0=不可越权，1=可越权）
python detector.py http://127.0.0.1:18081 --out reports/vulnerable_report.json
python detector.py http://127.0.0.1:18082 --out reports/hardened_report.json
```

## 加固要点

1. 文档名必须非空、≤128 字符、且为纯文件名（`os.path.basename` 比对，拒绝 `..` 与路径分隔符）；
2. `realpath` 解析后用 `os.path.commonpath` 确认落在 uploads 真实路径内；
3. 拒绝响应统一为 `access denied` / `invalid document name`，不回显绝对路径、不泄露目标文件内容；
4. 按 UTF-8 解码，非 UTF-8 文件返回 415 可读错误，进程保持运行。

## 实际验证记录（2026-11-14，本机 Python 3.14.7）

- `python run_all.py`：11/11 验收 PASS（对应任务验收 1–10，其中验收 8 对两个服务各验一次）；
- 连续两次运行，`comparison_report.json` 逐字节一致（确定性）；
- 未加固服务穿越请求实测返回 `OUTSIDE-SECRET-7f3d9a2c`（HTTP 200），加固服务返回 403 `access denied`；
- 全部 import 为标准库或本地模块，服务仅绑定 127.0.0.1，断网可运行（验收 11）。
