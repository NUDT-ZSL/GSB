# pathscan —— 文件下载接口路径穿越自检工具

对自研文件下载接口（`GET <endpoint>?filename=...`）做路径穿越安全自检。
工具从命令行或 JSON 文件读取测试路径，逐条发起探测，输出带**逐条判定明细
与汇总计数**的 UTF-8 JSON 报告。

- 仅使用 Python 标准库，要求 Python 3.10+（实测 3.14）
- 不使用 Docker 或任何容器、第三方依赖
- 全部样本确定性生成，仅访问本地授权模拟服务，不触达未授权目标

## 1. 目录结构

```
project15/
├── pathscan/
│   ├── __init__.py
│   ├── scanner.py            # 扫描器：输入校验、探测、判定、报告、CLI
│   ├── mock_server.py        # 本地授权模拟服务（vulnerable / fixed + 故障注入）
│   └── generate_samples.py   # 确定性样本生成器（基目录、标记、路径、损坏JSON）
├── tests/
│   └── test_scanner.py       # 26 个 unittest 用例
├── run_demo.py               # 一键端到端验收脚本（覆盖全部 9 条验收）
└── workspace/                # 运行后生成的样本与报告（确定性，可重复生成）
```

## 2. 判定模型

| 判定 | 条件 | 明细记录 |
|---|---|---|
| `high` 高风险 | 响应体包含基目录外**标记文件的完整内容**（越界文件被成功读出） | 命中的标记文件名 `marker_file_name`、标记内容摘录、响应摘录，供人工复核 |
| `low` 低风险 | 请求得到明确 HTTP 响应但未泄露标记内容；含服务端明确拒绝越界（403）与正常/404 响应 | HTTP 状态码 `http_status` 与原因文本 `reason` |
| `unknown` 未知 | 连接失败 / 连接重置 / 超时等网络层异常 | 结构化错误 `error`；**绝不并入安全（low）** |

关键设计：

- **原始编码保留（不二次编码）**：filename 值通过字符串拼接进入查询串，
  全程不调用 `quote/urlencode`。模拟服务把收到的**原始查询串**写入日志，
  可逐字复核 `%2f`、`%2e%2e`、`%252f` 等与输入完全一致。
- **异常路径下报告仍可用**：单条探测的异常被隔离捕获，其余路径继续完成；
  报告经同目录临时文件 + 原子替换写入，不会产生残缺报告。
- **稳定排序**：明细按 `high → low → unknown` 排列，同层级内按测试路径
  字母序；序列化参数固定，重复运行报告字节级一致。

## 3. 快速开始（可复现本地启动方式）

### 3.1 一键验收（推荐首次运行）

```powershell
# 在项目根目录 project15/ 下
$env:PYTHONUTF8 = 1
python -I run_demo.py
```

脚本自动生成样本、启停 vulnerable/fixed 两个模拟服务，并依次断言 9 条验收
项，结尾打印三个场景的汇总计数。全部通过时输出 `全部验收场景通过 ✔`。

### 3.2 单元测试

```powershell
python -I -m unittest discover -s . -t . -v
```

26 个用例覆盖：输入校验（缺失/损坏/结构非法/空列表）、URL 拼接不二次编码、
高/低/未知三类判定、超时、drop-after 已完成判定保留、排序、汇总计数一致性、
原子报告与 UTF-8 JSON 合法性。

### 3.3 手动分步演示（业务操作样例）

终端 A —— 生成样本并启动模拟服务：

```powershell
$env:PYTHONUTF8 = 1
python -m pathscan.generate_samples --workspace workspace
# 缺陷服务（可读出基目录外标记）
python -m pathscan.mock_server --port 9091 `
  --base-dir workspace/base_dir --mode vulnerable `
  --log workspace/demo_out/raw_query.log
# 另一终端可启动修复服务：--mode fixed --port 9092
```

终端 B —— 运行扫描器：

```powershell
# 来源一：命令行参数（-p 可重复）
python -m pathscan.scanner -u http://127.0.0.1:9091 `
  -p '../outside/SECRET_MARKER.txt' `
  -p '..%2foutside%2fSECRET_MARKER.txt' `
  -p 'docs/readme.txt' `
  -m workspace/outside/SECRET_MARKER.txt `
  -o workspace/demo_out/report.json

# 来源二：JSON 文件（路径数组，或 {"paths": [...]} 对象）
python -m pathscan.scanner -u http://127.0.0.1:9092 `
  -f workspace/samples/test_paths.json `
  -m workspace/outside/SECRET_MARKER.txt

# 不带 -o 时默认输出当前目录 report.json；-t 指定单条超时秒数
```

复核服务端收到的原始查询串（验证未二次编码）：

```powershell
Get-Content workspace/demo_out/raw_query.log
# ... /download?filename=..%2foutside%2fSECRET_MARKER.txt   （与输入逐字一致）
```

故障注入（验证 unknown）：

```powershell
# 第 N 个请求处理期间放下连接闸门：前 N-1 个请求确定完成，之后新连接确定失败
python -m pathscan.mock_server --port 9093 --base-dir workspace/base_dir `
  --mode fixed --drop-after 2
# 或：挂起所有请求，配合扫描器 -t 超时
python -m pathscan.mock_server --port 9093 --base-dir workspace/base_dir `
  --mode fixed --hang
```

错误处理：

```powershell
python -m pathscan.scanner -u http://127.0.0.1:9091 `
  -f workspace/samples/corrupt.json -m workspace/outside/SECRET_MARKER.txt
# 退出码 2，stderr 输出可读错误（含行列号），不生成 report.json
```

## 4. CLI 参数

| 参数 | 说明 |
|---|---|
| `-u, --base-url` | 目标基地址，如 `http://127.0.0.1:9091`（必填） |
| `-e, --endpoint` | 接口路径，默认 `/download` |
| `-p, --path` | 测试路径，作为 filename 原始值不二次编码；可重复 |
| `-f, --paths-file` | JSON 路径文件，可重复，与 `-p` 按出现顺序合并 |
| `-m, --marker` | 基目录外标记文件路径，内容作为高风险命中依据（必填） |
| `-o, --output` | 报告输出路径，默认 `./report.json` |
| `-t, --timeout` | 单条请求超时秒数，默认 5.0 |

退出码：`0` 正常出报告（含 unknown）；`2` 输入错误（JSON 缺失/损坏、
无路径、标记不可读），此时不写任何报告。

## 5. 报告结构

```json
{
  "tool": "pathscan",
  "version": "1.0.0",
  "target": {"base_url": "...", "endpoint": "/download"},
  "summary": {"high": 3, "low": 4, "unknown": 0, "total": 7},
  "details": [
    {
      "test_path": "..%2foutside%2fSECRET_MARKER.txt",
      "url": "http://.../download?filename=..%2foutside%2fSECRET_MARKER.txt",
      "verdict": "high",
      "http_status": 200,
      "reason": "HTTP 200 响应体命中基目录外标记文件内容",
      "evidence": {
        "type": "marker_content_match",
        "marker_file_name": "SECRET_MARKER.txt",
        "matched_marker_excerpt": "PATHSCAN-SECRET-MARKER-7f3a91 ...",
        "response_excerpt": "..."
      },
      "error": null
    }
  ]
}
```

- 汇总各判定数与明细逐条计数严格一致（测试与演示均做断言）。
- low 记录的 403 拒绝示例：`"http_status": 403,
  "reason": "path traversal rejected: resolved path is outside base directory"`。
- unknown 记录：`"http_status": null, "error": "socket.timeout: request timed out"`，
  错误描述不依赖操作系统本地化代码页，跨环境复核不乱码。

## 6. 关于双重编码样本的说明

样本含 `..%252foutside%252fSECRET_MARKER.txt`（双重百分号编码）。服务端只做
一次 URL 解码，得到含字面 `%2f` 的普通文件名，不构成穿越：

- vulnerable 服务：按该字面名找文件失败 → 404 → low（无标记泄露）；
- fixed 服务：路径未越界 → 404 → low。

这恰好验证扫描器**不自行二次编码/解码**：判定完全基于服务端对原始输入的
真实响应，而不是工具替服务端“脑补”出一条穿越。

## 7. 安全与隔离注意

- 模拟服务与样本仅监听 `127.0.0.1`，仅供本地自检。
- 处理下载的外部数据时，建议用 `python -I` 运行，避免工作目录中被植入的
  同名模块（如 `json.py`）被解释器加载；演示脚本以子进程方式运行扫描器时
  通过 `PYTHONPATH` 显式定位本项目包。
- 扫描器对单条响应体限制读取 1 MiB，防止异常响应拖垮进程。
