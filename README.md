# SSTI 本地授权端点离线检测工具

安全团队自建本地演示端点的模板注入（SSTI）响应评估工具。纯 Python 标准库实现，
无需第三方依赖、无需 Docker，仅访问 127.0.0.1 上的授权目标。

## 环境

- Python 3.10+（已在 Python 3.14.7 / Windows 11 验证）

## 文件

| 文件 | 说明 |
|---|---|
| `ssti_probe.py` | 检测工具：读 JSON 清单 → 探测 → 输出 JSON/CSV 报告与日志 |
| `demo_server.py` | 本地授权演示端点（正常/漏洞/疑似/500/2MB/超时/请求头回显） |
| `targets.json` | 样本清单（含正常、漏洞、疑似、异常、超时、缺字段目标） |
| `targets_empty.json` | 空清单（验证退出码 0 与空报告） |
| `verify_reports.py` | 产物自动验收脚本 |

## 可复现启动方式

```powershell
# 终端1：启动本地演示端点（仅监听 127.0.0.1:8377）
python demo_server.py

# 终端2：运行检测
python ssti_probe.py --targets targets.json --json-out report.json --csv-out report.csv --log probe.log

# 自动验收
python verify_reports.py

# 空清单场景（警告 + 空 targets 数组 + 退出码 0）
python ssti_probe.py --targets targets_empty.json --json-out report_empty.json --csv-out report_empty.csv
```

## 目标清单格式

```json
[
  {"id": "t-vuln", "url": "http://127.0.0.1:8377/vuln", "param": "input", "payloads": ["${7*7}"]}
]
```

每个目标含 `id`、`url`、`param`（参数名）、`payloads`（载荷列表）四个字段；
缺字段目标被跳过并告警，不影响其余目标。

## 判定规则

- 载荷 URL 编码后放入指定查询参数（`${7*7}` → `%24%7B7%2A7%7D`）；仅携带约定
  User-Agent，无其他自定义头。
- 状态码非 200 或响应体 >1MB → **响应异常**，不参与漏洞判定。
- 响应含载荷原文或其 URL 编码形式 → 该载荷 **未执行**（正常反射不误判）。
- `${A*B}` 的乘积作为独立数字出现（如 `结果:49`）→ **存在漏洞**。
- 非运算载荷无反射且与基线（无害值请求）长度偏差 >10% → **疑似漏洞**。
- 连接/读取超时（3 秒）→ **超时**，其余目标继续。
- 目标最终状态仅由各载荷结果得出，取值限于：存在漏洞 / 疑似漏洞 / 未发现漏洞 /
  响应异常 / 超时。

## 实测结果（2026-11-14）

| 目标 | 端点行为 | 状态 |
|---|---|---|
| t-normal | 原样反射输入 | 未发现漏洞 |
| t-vuln | 返回 `结果:49` | 存在漏洞 |
| t-suspect | 基线 100 → 响应 115（+15%），无反射 | 疑似漏洞 |
| t-error | HTTP 500 | 响应异常 |
| t-big | 2MB 响应体 | 响应异常 |
| t-slow | 睡眠 6s | 超时（其余目标不受影响） |
| t-headers | 回显请求头 | 未发现漏洞（用于验收请求头约定） |
| t-no-url | 缺 url 字段 | 跳过并告警，不进入报告 |

服务端日志可见 `raw-query=input=%24%7B7%2A7%7D`（要求2）；`captured_headers.json`
中仅 User-Agent 为约定值，其余为 urllib 标准传输头（要求11）；`probe.log` 含每个
目标起止行且不写入响应体（要求12）。注：/slow 触发客户端超时后，服务端写响应时
出现 ConnectionAbortedError 属预期现象（客户端已断开）。
