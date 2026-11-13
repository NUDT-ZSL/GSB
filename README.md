# 本地目录穿越验证工具（Windows / Python 3.10+）

在 Windows 上自带的**已知缺陷**文档预览服务 + **验证工具**，用于验证预览服务对目录穿越的防护是否可靠，输出结构化 JSON 报告。

- 全程只绑定 `127.0.0.1`，不访问外网，不使用 Docker/容器。
- 只用 Python 标准库，无第三方依赖。
- 本地确定性样本：目录内正常资料 + 目录外标记文件。

## 布局与业务操作

```
<sample>/
  share/                 # 共享目录：服务只应能读到这里的文件
    welcome.txt          # 正常资料（业务操作样例：GET /preview/welcome.txt）
    notes.md
  secrets/
    marker.txt           # 标记文件（在共享目录之外，内容含 TRAVERSAL-MARKER-7f3a9c）
```

服务端点：

| 请求 | 期望（修复后） |
| --- | --- |
| `GET /preview/welcome.txt` | `200`，返回共享目录内文档 |
| `GET /preview/..%252fsecrets%252fmarker.txt` | `403`，不返回目录外内容 |
| `GET /healthz` | `200 ok`（探活） |

## 文件

| 文件 | 作用 |
| --- | --- |
| `preview_server.py` | 预览服务：`vulnerable`（双重解码缺陷）/ `fixed`（修复）/ `partial`（只拦一种写法的残缺修复，用于自测验证工具）|
| `verify_traversal.py` | 验证工具 CLI：多种等价路径探测 → JSON 报告 |
| `run_demo.py` | 一键复现演示：建样本、起停服务、跑 7 个场景、落盘报告 |
| `tests/test_verification.py` | 18 个端到端验收测试（真实子进程 + 真实 CLI）|

## 快速复现

```powershell
# 1) 跑全部验收测试
python -I -m unittest discover -s tests -v

# 2) 一键演示（自动创建 demo_runs/<时间戳>/sample 与 reports）
python -I run_demo.py
```

手动演示（三个终端）：

```powershell
# 终端 A：创建样本
New-Item -ItemType Directory -Force sample\share, sample\secrets | Out-Null
"Welcome to the internal document preview service." | Set-Content sample\share\welcome.txt
"TRAVERSAL-MARKER-7f3a9c`nCONFIDENTIAL" | Set-Content sample\secrets\marker.txt

# 终端 B：启动带缺陷服务
python -I preview_server.py --share-dir sample\share --mode vulnerable --port 8765

# 终端 C：验证（报告写入 report.json）
python -I verify_traversal.py --share-dir sample\share --report report.json

# 修复后复测
python -I preview_server.py --share-dir sample\share --mode fixed --port 8766
python -I verify_traversal.py --share-dir sample\share --report report-fixed.json `
  --base-url http://127.0.0.1:8766
```

直接用浏览器/`curl` 演示业务操作：

```powershell
curl.exe -s -o NUL -w "normal=%{http_code}`n" http://127.0.0.1:8765/preview/welcome.txt
curl.exe -s http://127.0.0.1:8765/preview/..%252fsecrets%252fmarker.txt
curl.exe -s -o NUL -w "traversal=%{http_code}`n" http://127.0.0.1:8766/preview/..%252fsecrets%252fmarker.txt
```

> 注意：`curl.exe` 会规范化部分路径；`run_demo.py` / 验证工具用 `http.client` 发送**原始**百分号编码字节，才是准确复现。

## CLI

```
python verify_traversal.py --share-dir <目录> --report <报告.json>
                           [--base-url http://127.0.0.1:8765]
                           [--timeout 10.0] [--per-attempt 1.5]
```

- 缺参数：argparse 打印 `usage:` 并以非零码（2）退出。
- 退出码：`0` 存在穿越；`1` 已修复（全部探测均被拒）；`2` 结果不确定/错误；`3` 报告无法写入。

## 报告字段

```json
{
  "vulnerable": true,
  "attempted": ["../secrets/marker.txt", "..%2fsecrets%2Fmarker.txt", "..."],
  "evidence": "TRAVERSAL-MARKER-7f3a9c\nCONFIDENTIAL: ...",
  "error": null,
  "conclusion": "vulnerable",
  "checks": [{"attempt": "...", "status": 200, "outcome": "leaked", "detail": "..."}],
  "details": {"base_url": "...", "share_dir": "...", "completed_attempts": 13, ...},
  "generated_at": "2026-11-14T..."
}
```

- `vulnerable`：`true` / `false` / `null`。`null` 表示**无法判定**（服务不可达、超时、样本缺失），绝不等同于安全。
- `attempted`：本次发送的等价写法字符串数组。
- `evidence`：读到目录外标记内容时的原文，否则 `null`。
- `error`：失败原因，成功时为 `null`。

## 设计要点

1. **判定为穿越只需要一种写法成功**：任一探测返回 200 且响应体含标记内容即 `vulnerable: true`；其余写法被拦不影响结论（验收 3）。
2. **泄漏证据比对标记内容**，而不是只看状态码：避免把 404 页面或错误页误判为泄漏；返回 200 但不含标记内容记为 `unexpected_200`，3xx/5xx 记为 `abnormal_http`，两者都只导致「不确定」，不会判为安全。
3. **单个探测有硬超时**（`--per-attempt`），超时记为一次 `timeout` 失败尝试，继续跑剩余写法；同时有整体预算 `--timeout`，超预算的写法记为 `skipped`，最终 `vulnerable: null`（验收 8）。
4. **服务不可达/响应异常不判安全**：基线文档请求失败或内容与共享目录不一致时，报告 `error` 非空、`vulnerable: null`（验收 5）。
5. **样本不具备验证条件**：共享目录为空或标记文件缺失时，直接 `vulnerable: null` + 明确 `error`（验收 9）。
6. **修复逻辑**：只解码一次；拒绝残留 `%`、反斜杠、盘符/ADS 冒号、控制字符与空/`.`/`..` 段；解析真实路径后强制 `relative_to(share_root)`，越界即 `403`。目录内正常资料不受影响（验收 6、7）。
7. 报告采用「临时文件 + `os.replace`」原子写入，先写报告再以退出码表达结论。

## 实际验证结果（本机 Windows 11, Python 3.14.7）

`python -I -m unittest discover -s tests -v` → **Ran 18 tests — OK**

| 验收项 | 场景 | 结果 |
| --- | --- | --- |
| 1 | 无参数运行 CLI | 退出码 2，打印 `usage:` ✅ |
| 2 | 缺陷服务：`..%252fsecrets%252fmarker.txt` | `200` + 标记内容；正常资料 `200` ✅ |
| 3 | 残缺修复只拦 `..%252f...` 一种写法 | 报告仍 `vulnerable: true` ✅ |
| 4 | 报告 JSON 字段类型/取值 | `bool/list/str-or-null` 全部符合 ✅ |
| 5 | 未启动服务 | `error` 非空、`vulnerable: null`、`conclusion: inconclusive` ✅ |
| 6 | 修复后复测 | `vulnerable: false`；正常资料仍 `200` ✅ |
| 7 | 修复后遍历全部 13 种写法 | 全部 `403`，无标记内容泄漏 ✅ |
| 8 | 交替延迟（0.8 s）超 0.25 s 单次限额 | 7 次 `timeout`，其余 6 次仍完成；`error` 非空、`vulnerable: null` ✅ |
| 9 | 删除标记文件 | `error` 非空、`vulnerable: null` ✅ |

最近一次 `python -I run_demo.py` 生成报告摘要：

```
01-vulnerable.json   vulnerable=True   evidence=TRAVERSAL-MARKER-7f3a9c  error=null
02-partial-fix.json  vulnerable=True   evidence=TRAVERSAL-MARKER-7f3a9c  error=null
03-fixed.json        vulnerable=False  evidence=null                    error=null
04-timeout.json      vulnerable=null   evidence=null   error=7 probe(s) timed out at 0.25s
05-unreachable.json  vulnerable=null   evidence=null   error=service unreachable ...
06-no-marker.json    vulnerable=null   evidence=null   error=marker file missing ...
```

报告位于 `demo_runs/<时间戳>/reports/`。`demo_runs/` 已加入 `.gitignore`（演示产物不入库）。

## 修复前 / 后行为对照

| 请求 | 缺陷服务 | 修复后 |
| --- | --- | --- |
| `/preview/welcome.txt` | 200 | 200 |
| `/preview/..%252fsecrets%252fmarker.txt` | 200（泄漏） | 403 |
| `/preview/..%2fsecrets%2Fmarker.txt` | 200（泄漏） | 403 |
| `/preview/..%5csecrets%5Cmarker.txt` | 200（泄漏） | 403 |
| `/preview/C%3a/Windows/win.ini` | 404 | 403 |
