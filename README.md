# 本地授权 Web 漏洞扫描器

对本地授权的 Flask 样本服务检测 **SQL 注入 / 跨站脚本（XSS）/ 目录遍历** 三类问题，
输出按严重性（高 → 中 → 低）排序的 JSON 报告、JSONL 明细，并支持前后报告对比、
记录查询与补丁修复验证。

## 约束与特性

- **仅使用 Python 标准库**，要求 Python ≥ 3.10（实测 3.14），无网络、无外部账号、无 Docker。
- 样本服务按 Flask 风格编写：检测到真实 Flask 时使用 Flask，否则自动回退到
  内置的标准库 WSGI 兼容层（`vuln_scanner/flask_compat.py`），两种运行时行为一致。
- 全部检测针对本机确定性样本，样本以固定标记回显，检测可重复、无误报，不访问任何外部目标。
- 目标不可达（等待上限内无响应 / 连接被拒绝）只影响该目标，已完成结果不被污染。

## 目录结构

```
project27/
├─ vuln_scanner/
│  ├─ __main__.py          # python -m vuln_scanner 入口
│  ├─ cli.py               # 统一 CLI：serve/lab/scan/compare/query/verify
│  ├─ config.py            # 目标清单与策略的加载、校验（缺失字段/非法 URL/上限 10）
│  ├─ http_client.py       # 标准库 HTTP 客户端，区分超时/拒绝/异常状态
│  ├─ checks.py            # 三类检测逻辑、漏洞模型与修复建议
│  ├─ scanner.py           # 扫描编排、取消处理、去重、摘要、报告组装
│  ├─ records.py           # JSONL 明细查询（URL 前缀 + 严重性）
│  ├─ report_compare.py    # 两份报告对比：新增/已修复/未变化
│  ├─ patch.py             # 统一 diff 补丁应用/回滚（含 .bak 备份）
│  ├─ lab.py               # 确定性生成目标清单/策略/非法输入样例
│  ├─ sample_app.py        # 含三类漏洞的本地样本服务（可被补丁修复）
│  └─ flask_compat.py      # Flask 子集的标准库实现（离线回退）
├─ patches/
│  └─ fix_vulnerabilities.patch   # 确定性修复补丁（参数化查询/HTML 转义/路径白名单）
├─ tools/generate_patch.py        # 补丁生成器（difflib，可重复生成）
├─ tests/acceptance_test.py       # 15 条任务要求的端到端验收（17 个断言）
└─ lab/                            # lab 命令生成的输入样例
```

## 输入文件格式

目标清单（最多 10 个目标，URL 必须是 http/https）：

```json
{
  "targets": [
    {
      "name": "local-vulnerable-sample",
      "base_url": "http://127.0.0.1:8765",
      "endpoints": [
        {"path": "/login",  "checks": ["sql_injection"], "parameter": "username", "placeholder": null},
        {"path": "/search", "checks": ["xss"], "parameter": "q", "placeholder": null},
        {"path": "/files/{file}", "checks": ["path_traversal"], "parameter": null, "placeholder": "{file}"}
      ]
    }
  ]
}
```

- `parameter` 为 `null` 表示路径型注入点，报告条目的参数名同样为 `null`。
- 省略 `endpoints` 时使用样本服务的默认注入点画像。

扫描策略：

```json
{
  "timeout_seconds": 2.0,
  "enabled_checks": ["sql_injection", "xss", "path_traversal"]
}
```

- 检测项 ID：`sql_injection`（高）、`xss`（中）、`path_traversal`（低）。
- 从 `enabled_checks` 移除某项即排除该检测：报告中不会出现对应漏洞，
  但会在 `skipped_checks` 里标注该项未检测及原因。

校验失败（缺必需字段、非法 URL、超过 10 个目标等）会一次性打印全部问题，
进程以**退出码 1** 结束，不启动扫描。

## 本地启动与业务操作样例

以下命令均在项目根目录执行。

### 1. 生成输入样例并启动样本服务

```powershell
python -m vuln_scanner lab --lab-dir lab --port 8765
python -m vuln_scanner serve --port 8765      # 另开一个终端，保持运行
# 健康检查：GET http://127.0.0.1:8765/health -> {"status":"ok","runtime":"stdlib-shim"}
```

### 2. 执行扫描（报告 + JSONL 明细）

```powershell
python -m vuln_scanner scan `
  --targets lab/targets.json --policy lab/policy.json `
  --report runs/report.json --records runs/records.jsonl
```

样例输出摘要：高中低漏洞数 `1 / 3 / 2`；不可达目标 `scan_completed=false`，
`/error` 的 500 出现在 `warnings` 中且其余路径结果照常保留。

### 3. 查询 JSONL 明细（按目标前缀 + 严重性，降序返回）

```powershell
python -m vuln_scanner query --records runs/records.jsonl --severity high
python -m vuln_scanner query --records runs/records.jsonl `
  --url-prefix http://127.0.0.1:8765 --severity low
```

### 4. 对比修复前后两份报告

```powershell
python -m vuln_scanner compare `
  --before runs/before.json --after runs/after.json --output runs/comparison.json
# 输出 counts: {added, fixed, unchanged} 及三类明细，明细按严重性降序
```

### 5. 修复验证一键入口（应用补丁 → 重启复扫 → 对比 → 自动恢复）

```powershell
python -m vuln_scanner verify --output-dir runs/verify
# 实测：counts = {"added": 0, "fixed": 6, "unchanged": 0}
# 默认自动从 .bak 恢复样本文件；--keep-patched 可保留修复后状态
```

### 6. 中途取消（取消文件出现即停止，已完成结果落盘）

```powershell
python -m vuln_scanner scan --targets runs/cancel_demo/targets.json `
  --policy runs/cancel_demo/policy5.json `
  --report runs/cancel_demo/report.json --records runs/cancel_demo/records.jsonl `
  --cancel-file runs/cancel_demo/STOP
# 扫描过程中创建 STOP 文件即可触发：
New-Item runs/cancel_demo/STOP -ItemType File
# 报告中：已完成目标保留结果，当前目标 status=cancelled，未处理目标 status=not_scanned
```

### 7. 非法输入的失败演示

```powershell
python -m vuln_scanner scan --targets lab/invalid/targets_bad_url.json `
  --policy lab/policy.json --report runs/x.json --records runs/x.jsonl
# 一次性列出全部非法 URL，退出码为 1
```

### 8. 一键运行全部验收

```powershell
python tests/acceptance_test.py
# 17 项断言全部 PASS，覆盖任务书 15 条要求
```

## 报告结构说明

```json
{
  "cancelled": false,
  "targets": [
    {
      "name": "...",
      "target_url": "http://...",
      "scan_completed": true,
      "scan_status": "completed | timeout | unreachable | cancelled | not_scanned",
      "status_detail": null,
      "findings": [
        {"type": "sql_injection", "type_label": "SQL 注入", "url": "...",
         "parameter": "username", "severity": "high",
         "remediation": "使用参数化查询/预编译语句..."}
      ],
      "warnings": [{"path": "/error", "url": "...", "message": "... HTTP 状态 500 ..."}],
      "skipped_checks": [{"check": "sql_injection", "label": "SQL 注入",
                          "path": "/login", "parameter": "username",
                          "reason": "扫描策略已禁用该检测项"}]
    }
  ],
  "summary": {
    "total_targets": 2, "completed_targets": 1, "incomplete_targets": 1,
    "vulnerabilities_by_severity": [
      {"severity": "high", "label": "高", "count": 1},
      {"severity": "medium", "label": "中", "count": 3},
      {"severity": "low", "label": "低", "count": 2}
    ],
    "total_vulnerabilities": 6,
    "total_warnings": 1
  }
}
```

## 检测与去重规则

- 漏洞身份键为 `(类型, URL, 参数名)`：同目标同类型同参数的重复命中只保留一条；
  参数名不同（如 `/search` 的 `q` 与 `lang`）视为不同漏洞，分别保留。
- 路径型注入点参数名为 `null`，与查询参数型条目天然区分。
- 每个目标先做 `/health` 预检：超时或拒绝则整目标标记未完成，不再逐点探测，
  因此不可达目标不会产生任何警告或漏洞污染。
- 单路径返回 5xx 仅记录警告并继续其余路径；扫描中目标失联则保留此前已完成结果。
