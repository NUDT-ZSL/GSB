# 项目 67：员工信息录入与状态回读（本地 Web 表单自动化）

读取含 **姓名 / 邮箱 / 部门** 三列的 CSV（允许缺值、格式有误），通过 Playwright
在**本地模拟表单页面**逐条填写并提交，凭提交响应 JSON 中的记录标识回读最终状态，
最终输出按 **成功 / 失败 / 不可用** 三组分类、组内按源文件行号升序排列的 JSON 报告。

- 全程仅访问 `127.0.0.1` 本地服务，**不访问公网**；不使用容器。
- 模拟表单与状态接口由 Python 标准库 `http.server` 确定性模拟，故障可按行注入。
- 输入样例由任务代码（本仓库 `samples/` 与检查套件）构造。

## 1. 环境要求与准备

| 依赖 | 要求 | 本机实际 |
| --- | --- | --- |
| Python | ≥ 3.10 | 3.10.20（Anaconda 环境 `project`） |
| Playwright | ≥ 1.40 | 1.58.0 |
| 浏览器 | Chromium（或系统 Chrome/Edge 自动回退） | 已安装 Chromium 145 + 系统 Chrome 157 |

安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

> 本机网络访问 `cdn.playwright.dev` 极慢、npmmirror 旧路径 404，实际采用
> npmmirror 的 chrome-for-testing 新路径手工下载并放入 Playwright 缓存：
>
> ```powershell
> $cache = "$env:LOCALAPPDATA\ms-playwright"
> curl.exe -L -o "$env:TEMP\chrome-win64.zip" `
>   "https://cdn.npmmirror.com/binaries/chrome-for-testing/145.0.7632.6/win64/chrome-win64.zip"
> curl.exe -L -o "$env:TEMP\chrome-headless-shell-win64.zip" `
>   "https://cdn.npmmirror.com/binaries/chrome-for-testing/145.0.7632.6/win64/chrome-headless-shell-win64.zip"
> Expand-Archive "$env:TEMP\chrome-win64.zip" "$cache\chromium-1208"
> Expand-Archive "$env:TEMP\chrome-headless-shell-win64.zip" "$cache\chromium_headless_shell-1208"
> New-Item "$cache\chromium-1208\INSTALLATION_COMPLETE", "$cache\chromium_headless_shell-1208\INSTALLATION_COMPLETE"
> ```
>
> 即使不安装 Playwright 自带浏览器，程序也会自动回退到系统 Chrome / Edge
> （`--browser chrome|msedge` 可强制指定）。

## 2. 目录结构

```
project67/
├── mock_server/
│   └── server.py            # 本地模拟服务：表单页 + 提交接口 + 状态接口 + 故障注入
├── employee_importer/
│   ├── csv_reader.py        # CSV 读取，保留源文件物理行号，缺值不中断
│   ├── browser_form.py      # Playwright 表单驱动（加载/元素/提交故障处理与重试）
│   ├── status_client.py     # 按记录标识轮询最终状态（pending/故障重试）
│   ├── reporter.py          # 三组分类 JSON 报告，组内按行号升序
│   └── cli.py               # 命令行入口与逐行控制台输出
├── samples/
│   ├── data/employees.csv   # 7 行样例（含缺值、错误邮箱格式）
│   ├── expected/            # 样例预期摘要
│   └── out/                 # 运行产物（git 忽略）
├── tests/
│   ├── run_checks.py        # 14 项验收检查套件
│   └── compare_sample.py    # 报告与预期摘要对比工具
├── run_sample.ps1           # Windows 一键样例（PowerShell，已带 UTF-8 BOM）
├── run_sample.sh            # Git Bash 一键样例
└── requirements.txt
```

## 3. 快速开始（可重复运行的本地样例）

```powershell
# Windows PowerShell（自动启停模拟服务、跑填报、对比预期）
.\run_sample.ps1
# 可选：.\run_sample.ps1 -Port 8800 -Browser chrome -Headed
```

```bash
# Git Bash
bash run_sample.sh 8765
```

手动分步运行：

```powershell
# 终端 A：启动模拟服务
python -m mock_server.server --port 8765

# 终端 B：执行填报（路径使用 Windows 写法）
python -m employee_importer.cli `
  --input  "E:\Company\demo\projects\project67\samples\data\employees.csv" `
  --report "E:\Company\demo\projects\project67\samples\out\report.json" `
  --url    "http://127.0.0.1:8765"
```

命令行参数：

| 参数 | 说明 |
| --- | --- |
| `--input/-i` | 输入 CSV 路径（必填，Windows 盘符反斜杠写法） |
| `--report/-r` | 报告 JSON 输出路径（必填，父目录自动创建） |
| `--url` | 模拟服务根地址，默认 `http://127.0.0.1:8765` |
| `--browser` | `chromium`（默认，缺失自动回退系统 Chrome/Edge）/ `chrome` / `msedge` |
| `--no-headless` | 显示浏览器窗口，默认无头 |

## 4. 输入与输出约定

### CSV

- 表头固定三列：`姓名,邮箱,部门`；编码 UTF-8（兼容 BOM）。
- 空白字段视为缺值，**保留为 None，填报时该输入框留空，不写入任何占位内容**。
- 报告中的 `行号` 为数据行在源文件中的**物理行号**（含表头，从 2 开始）；
  全空白物理行跳过且不占报告条目，因此行号与源文件逐行对应。
- 任何单行的缺值或格式问题都不会中断整批处理。

### JSON 报告

顶层含输入文件、汇总计数与三个分组；每条至少包含 `行号 / 状态 / 消息`，
成功条目另附 `记录标识 / 最终状态 / 尝试次数`。状态取值固定为：
`成功`、`失败`、`不可用`。三组各自按行号升序、组间不交叉。

分类规则：

| 情形 | 分类 | 是否回读 |
| --- | --- | --- |
| 提交成功并回读到 confirmed/rejected | 成功 | 是 |
| 页面加载失败 / 必填元素缺失 | 失败 | 否 |
| 提交 2xx 但响应无 `record_id` | 失败 | 否 |
| 提交被 4xx 确定性拒绝 | 失败 | 否 |
| 提交暂时故障（5xx/网络/超时）重试耗尽 | 不可用 | 否 |
| 已提交但状态接口持续无结论/报错，重试耗尽 | 不可用 | — |

控制台对每行输出两段（开始 + 最终状态），格式 `行 N | 状态 | 消息`，
与报告内容一致；**报告文件是最终依据**。

## 5. 模拟服务与故障注入

服务端点：

- `GET /` 表单页面；`POST /api/submit` 提交（返回 `record_id`）；
  `GET /api/status/<id>` 状态回读；`GET /healthz` 健康检查。
- 控制端点（仅本地测试）：`POST /control/reset|mode|schedule`、`GET /control/snapshot`。

按行故障指令（行序与填报器打开页面的次序一一对应，故障严格限定在该行，不污染其他行）：

```json
POST /control/schedule
{"rows": [
  {"page": "normal|missing_element|fail_load",
   "submit": "success|no_id|transient",
   "status": "confirmed|pending_then_confirmed|rejected|error_500"}
]}
```

- `page=missing_element`：返回缺少姓名输入框的缺陷页面 → 元素缺失失败。
- `page=fail_load`：服务端直接关闭连接 → 加载失败，下一行新连接不受影响。
- `submit=transient`：首次 503、同页重试成功；失败尝试不落库，记录只出现一次。
- `submit=no_id`：200 但无记录标识 → 失败且不触发回读。
- `status=pending_then_confirmed`：首次 `pending` 无结论，再次查询得真实状态。
- `status=error_500`：该记录状态查询持续报错 → 不可用，其他记录照常。
- 全局模式 `POST /control/mode {"mode":"submit_500|status_500|fail_load"}` 模拟持续不可用。

## 6. 验收检查与实际结果

运行：

```powershell
$env:PYTHONUTF8="1"; python tests\run_checks.py
```

本机实际运行结果（2026-11-14，Python 3.10.20 + Playwright 1.58.0 + Chromium 145）：
**14/14 项全部通过**。

| # | 验收点 | 检查方式与实际结果 |
| --- | --- | --- |
| 1 | 缺值不中断、行号逐行对应 | 构造含全空物理行的 CSV，报告行号 `[2,3,5,6]` 与源文件一致，4 行全部成功 |
| 2 | 空字段留空提交 | 服务端快照确认空字段收到的是 `""`，无占位内容 |
| 3 | 缺记录标识判失败不回读 | 该行入失败组、无记录标识字段，服务端状态查询计数仅正常行的 1 次 |
| 4 | 无结论后再次确认 | 该记录被查询 2 次，最终 `confirmed`，进入成功组 |
| 5 | 页面故障仅影响该行 | 元素缺失行、加载失败行均失败，第三行正常成功，失败行无落库记录 |
| 6 | 暂时故障恢复且无重复 | 抖动行提交 2 次（首败次成），服务端恰好 2 条记录，成功区无重复、内容无污染 |
| 7 | 持续不可用进需跟进区 | 状态持续故障行/提交全局 500 行均入不可用组，成功组为空或仅含正常行 |
| 8 | 先前成功记录保持不变 | 中途第 2 行故障，第 1 行仍在成功组且 `confirmed` 与标识不变，第 3 行正常成功 |
| 9 | 报告结构与三字段 | 报告文件存在，条目数=输入行数，每条含行号/状态/消息，状态取值合法 |
| 10 | 三组分类且升序不交叉 | 成功 `[2,6]`、失败 `[3,5,7]`、不可用 `[4]`，各组升序、无交叉 |
| 11 | Windows 路径参数 | 以盘符反斜杠路径运行，正确读取指定 CSV 并写出指定报告 |
| 12 | 输入缺失报错无报告 | 退出码 2、stderr 提示不存在，目标报告文件未创建 |
| 13 | 控制台逐行输出 | 每行均有 `行 N | 状态` 输出，与报告逐条一致 |
| 14 | 可重复端到端样例 | `run_sample.ps1` / `run_sample.sh` 均跑通，报告与 `expected_summary.json` 完全一致 |

## 7. 设计说明与边界

- 浏览器整批复用一个实例，每行独立 Page，保证页面级故障隔离；提交暂时故障在
  **同一页面**重试（最多 3 次），状态回读最多 5 次、间隔 0.8 秒。
- 4xx 视为确定性失败（重试无意义），5xx 与网络异常视为暂时故障（保留恢复机会）。
- 模拟服务为内存态，`/control/reset` 或重启进程即清空，适合重复运行。
- 所有测试流量限定在 `127.0.0.1`；浏览器二进制下载属于一次性环境准备，不属于程序运行流量。
