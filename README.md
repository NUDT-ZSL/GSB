# 本地授权越权靶场与修复判定工具

纯 Python 标准库实现的**水平越权（IDOR）漏洞验证环境**，在隔离的本地回环地址上运行
两种（实际提供三种）应用形态，对同一份场景清单执行双形态对比，并输出结构化修复判定报告。

- 仅使用 Python 3.10+ 标准库（实测 Python 3.14.7 / Windows 11），**无第三方依赖、无容器**
- 全部数据为本地确定性样本（`data.json`），不访问任何外部或未授权目标
- 支持暂时性故障重试、持续不可用隔离、三类判定、1000 条场景并发执行

## 1. 目录结构

```
project35/
├── data.json                  # 账号、资源与敏感字段定义（确定性本地样本）
├── scenarios.json             # 默认场景清单（7 条，含越权/属主/未知资源）
├── checker.py                 # 判定工具：清单校验、双形态执行、判定、JSON 报告
├── run_demos.py               # 13 项验收的自动化编排脚本
├── run_vulnerable.bat         # 一键启动未修复形态（默认 8081）
├── run_fixed.bat              # 一键启动已修复形态（默认 8082）
├── run_checker.bat            # 一键运行判定工具
├── range/
│   ├── __init__.py
│   ├── data.py                # 数据加载与敏感字段视图
│   └── server.py              # 靶场 HTTP 服务（三种形态 + 故障注入）
├── scenarios/
│   ├── invalid_manifest.json  # 验收1：缺字段/类型错误样本
│   ├── empty_manifest.json    # 验收12：空数组样本
│   └── unordered_manifest.json# 验收10：乱序清单样本
└── demo_output/               # 验收运行产物（报告、日志、结果汇总）
```

## 2. 靶场三种运行形态

| 形态 (`--mode`) | `/api/resource` | `/api/account` | 用途 |
|---|---|---|---|
| `vulnerable` | 仅校验令牌，不校验归属，任意登录用户可读他人资源全量字段 | 同左 | 未修复基线 |
| `fixed` | 非所有者访问返回 `403`，敏感字段不泄露 | 同左 | 已修复目标 |
| `fixed-partial` | 已修复（403） | **仍越权** | 构造“未完全修复”混合判定 |

故障注入开关（仅用于验证工具的容错能力）：

| 参数 | 行为 |
|---|---|
| `--flaky-first N` | 服务启动后前 N 个业务请求统一返回 `503`，之后恢复（暂时性故障） |
| `--flaky-always RES_ID` | 指定资源的请求始终返回 `503`（单场景持续不可用） |

接口与认证：

- `GET /api/resource?resource_id=res-1001`：资源详情
- `GET /api/account?user_id=alice`：账号详情
- `GET /health`：健康检查（不消耗 flaky 配额）
- 认证头：`X-Auth-Token: <token>`，令牌与账号的对应关系见 `data.json`

## 3. 快速开始（两个终端 + 一次判定）

```powershell
# 终端 A：未修复形态
python -I range\server.py --port 8081 --mode vulnerable

# 终端 B：已修复形态
python -I range\server.py --port 8082 --mode fixed

# 终端 C：运行判定（默认读 scenarios.json，写 report.json）
python -I checker.py
# 或使用封装脚本
run_checker.bat
```

手工对比单个越权请求（bob 访问 alice 的资源 res-1001）：

```powershell
# 未修复形态：HTTP 200，响应体包含 medical_record 等敏感字段
Invoke-WebRequest "http://127.0.0.1:8081/api/resource?resource_id=res-1001" `
  -Headers @{ "X-Auth-Token" = "token-bob-002" }

# 已修复形态：HTTP 403，响应体仅含 forbidden 错误，无敏感字段
Invoke-WebRequest "http://127.0.0.1:8082/api/resource?resource_id=res-1001" `
  -Headers @{ "X-Auth-Token" = "token-bob-002" } -SkipHttpErrorCheck
```

## 4. 判定规则（以资源归属为准）

1. 从 `data.json` 解析场景目标（资源/账号）及其所有者，与 `actor` 比对得出
   `is_cross_owner_access`（非所有者访问他人资源）。
2. 对每个场景分别请求两种形态，收集状态码与响应摘要；网络故障 / 502 / 503 / 504
   按 `--retries` 自动重试（默认 2 次，即最多 3 次尝试）。
3. 跨所有者访问场景：
   - 已修复形态响应中检出该目标敏感字段（字段名 + 字段值双重证据）→ **漏洞仍存在**
   - 已修复形态无泄露（如 403）→ **漏洞已修复**（未修复侧的泄露作为对照证据写入依据）
   - 已修复形态持续不可用 → **无法判定**
4. 属主访问自身资源：可用形态返回 200 与属主数据 → 已修复（归属控制未误伤合法访问）；
   全部异常 → 无法判定。
5. 目标在数据中不存在、两种形态均不可用等归属无法建立的情况 → 无法判定，
   **绝不以空响应冒充成功**。

整体结论：存在“漏洞仍存在”或“无法判定”即 **未完全修复**（无法判定项单列）；
全部已修复才是 **完全修复**；全部无法判定时为 **无法形成整体判定**。

### 退出码约定

| 退出码 | 含义 |
|---|---|
| 0 | 全部场景判定为已修复 |
| 2 | 清单校验失败（缺字段/类型不符/重复 id/JSON 非法），不执行任何场景 |
| 3 | 清单为空数组，无场景可判定，不写报告 |
| 4 | 未完全修复（含漏洞仍存在或无法判定场景） |
| 5 | 全部场景均无法判定 |
| 1 | 其他运行期错误 |

### 常用命令行参数

```
python checker.py [清单路径] [-o 报告路径]
                  [--vuln-url URL] [--fixed-url URL] [--data PATH]
                  [--retries N] [--retry-delay 秒] [--timeout 秒]
                  [--workers N] [--quiet]
```

## 5. 一键复现全部 13 项验收

```powershell
python -I run_demos.py
```

脚本会自动在独立端口上启动/清理各形态服务、注入暂时性与持续性故障、构造全部样本，
并将逐条结果写入 `demo_output/demo_run.log` 与 `demo_output/results_summary.json`。
**最近一次实测结果：13/13 全部通过，1000 条场景端到端耗时约 1.6 秒。**

## 6. 验收要求与实测对照

| # | 验收点 | 实测方式 | 结果 |
|---|---|---|---|
| 1 | 缺字段/类型不符报具体错误、非零退出、不执行场景 | `invalid_manifest.json`（缺 actor/path、query 为字符串、值为 int），退出码 2，无报告 | PASS |
| 2 | 默认清单 + 路径覆盖 | 无参数生成 `report.json`；自定义路径加载 3 条乱序清单 | PASS |
| 3 | 双形态状态码与响应摘要 | 报告中每场景含两侧 `status_code` 与 `body_excerpt` | PASS |
| 4 | 修复前泄露、修复后不泄露 | S01：未修复 200 泄露 `medical_record`，修复后 403 无泄露 | PASS |
| 5 | 三类结论 + 人类可读依据 | 默认清单产出 fixed 与 cannot_determine，D9 产出 vulnerable，每条带 `reason` | PASS |
| 6 | 暂时故障后重试不丢弃 | `--flaky-first 2`：尝试序列 503→503→200，场景正常出结论 | PASS |
| 7 | 持续不可用形态显式列出、其余照常 | fixed 指向无服务端口：`unavailable_forms` 列出，属主场景仍出结论 | PASS |
| 8 | 失败场景判无法判定而非冒充成功 | `--flaky-always res-1001`：3 次 503 后标记 cannot_determine | PASS |
| 9 | 混合三类的整体汇总与单列 | fixed-partial + flaky-always：1 条 vulnerable、2 条 cannot，整体未完全修复 | PASS |
| 10 | 输出顺序与清单一致 | 乱序清单 ZETA/ALPHA/MID 输出顺序不变，`order` 连续 | PASS |
| 11 | 合法 JSON 报告含时间/版本/两类结论 | 报告含 `generated_at`、`tool_version=1.0.0`、逐场景与整体结论 | PASS |
| 12 | 空清单非零退出且不写报告 | 退出码 3，提示“空数组”，无报告文件 | PASS |
| 13 | Win10+ 标准库、1000 条完整判定 | 1000 条 / 2000 请求，8 线程约 1.6s，退出码 0，无缺失库错误 | PASS |

## 7. 报告结构（report.json 摘要）

```json
{
  "generated_at": "2026-11-14T07:13:11",
  "tool_version": "1.0.0",
  "manifest_path": "...",
  "target_forms": { "vulnerable": "...", "fixed": "..." },
  "scenario_count": 7,
  "unavailable_forms": [],
  "scenarios": [
    {
      "order": 1,
      "id": "S01-bob-reads-alice-resource",
      "actor": "bob",
      "path": "/api/resource",
      "query": { "resource_id": "res-1001" },
      "ownership": { "target_owner": "alice", "is_cross_owner_access": true },
      "vulnerable_response": { "status_code": 200, "body_excerpt": "...", "attempts": 1, "attempts_detail": [...] },
      "fixed_response": { "status_code": 403, "body_excerpt": "...", "attempts": 1, "attempts_detail": [...] },
      "verdict": { "code": "fixed", "label": "漏洞已修复", "reason": "...", "evidence": { ... } }
    }
  ],
  "overall_conclusion": {
    "code": "not_fully_fixed",
    "label": "未完全修复",
    "summary": "共 7 条场景：已修复 6 条，漏洞仍存在 0 条，无法判定 1 条。",
    "vulnerable_scenario_ids": [],
    "cannot_determine_scenario_ids": ["S07-reads-unknown-resource"]
  }
}
```

## 8. 安全与边界说明

- 所有服务仅绑定 `127.0.0.1`，不对外暴露；数据为虚构样本，无真实凭据。
- 推荐以 `python -I` 运行：隔离模式忽略环境变量与当前目录的恶意模块（如 planted `json.py`）。
- 子进程编排时服务日志重定向到文件而非管道，避免日志写满 OS 管道导致服务阻塞。
- 判定工具默认串行，`--workers` 可开启标准库线程池并发，结果始终按清单顺序汇总。
