# 离线 SQL 注入检测与修复验证工具（sqli_check）

内部团队本地自建登录服务的注入检测工具。仅用 Python 标准库（3.10+，实测 3.14.7），
Windows 直接运行，不依赖 Docker，不访问任何外部目标——只检测本工具拉起的本地样本服务。

## 文件

| 文件 | 说明 |
|---|---|
| `samples/vuln_server.py` | 漏洞版登录服务（`http.server` + 内存 SQLite，SQL 字符串拼接） |
| `samples/fixed_server.py` | 修复版登录服务（同一接口，参数化查询） |
| `sqli_check.py` | 检测工具：拉起两版服务 → 就绪等待 → 注入检测 → 清理进程 → 输出 JSON 报告 |

样本服务接口：`GET /health`（就绪探针）、`GET /login?username=..&password=..`。
内置用户：`admin / S3cret!Admin`、`alice / wonderland123`、`bob / builder456`。

## 启动方式（在项目根目录执行）

```powershell
# 完整检测：两版对比 + 修复有效性结论
python sqli_check.py --vuln-cmd  "python samples/vuln_server.py --port {port}" `
                     --fixed-cmd "python samples/fixed_server.py --port {port}" `
                     --report report.json

# 不带参数：生成仅含 null 占位的 report.json
python sqli_check.py

# 手动体验样本服务（业务操作样例）
python samples/vuln_server.py --port 18301
# 正常登录:  http://127.0.0.1:18301/login?username=admin&password=S3cret!Admin
# 注入演示:  http://127.0.0.1:18301/login?username='%20UNION%20SELECT%20username,%20password%20FROM%20users--%20&password=x
```

启动命令中的 `{port}` 由工具替换为实际端口（`--vuln-port` / `--fixed-port` 可改，默认 18301/18302）。

## 检测载荷

| 载荷 ID | 类型 | 判定依据 |
|---|---|---|
| `union_select_users` | 联合查询 | `' UNION SELECT username, password FROM users-- ` 响应中出现未登录用户的口令标记 |
| `boolean_blind_and` | 布尔盲注 | `admin' AND '1'='1'-- ` 与 `admin' AND '1'='2'-- ` 响应存在稳定真假差异 |

任一载荷检出即判定该版本「存在漏洞」。每个请求有 `--request-timeout`（默认 5s）约束；
超时或出错的载荷标注「未完成」，不影响其他载荷结果。

## 报告结构（report.json）

四个固定字段，未执行/不可用项为 `null`：

- `vulnerable_version` / `fixed_version`：状态（可用/不可用+原因）、结论、逐载荷结果（含响应证据片段）、清理信息（警告与残留 PID）
- `comparison_summary`：两版结论对比与 `fix_effective`（是/否/null）
- `evidence`：全部载荷证据汇总列表

## 测试钩子（用于验收模拟）

- `--simulate-timeout-payload <载荷ID>`：将该载荷请求发往本机黑洞端口，确定性触发超时 → 验证「未完成」标注
- `--simulate-cleanup-failure`：模拟清理失败 → 报告写入警告与残留 PID（随后仍尽力真实清理，不留实际残留）

## 实际验证记录（2026-11-14，Windows 11 + Python 3.14.7）

| 验收项 | 命令要点 | 结果 |
|---|---|---|
| 1/2/4 双版检测 | 完整命令运行 → `report_full.json` | 漏洞版「存在漏洞」（union+布尔两载荷均检出，含响应片段）；修复版「未检出漏洞」；`fix_effective: 是` |
| 3 无效启动命令 | `--vuln-cmd "python samples/no_such_server.py ..."` → `report_unavailable.json` | 漏洞版状态「不可用」，原因「服务进程已退出，退出码 2」；修复版正常 |
| 5 载荷超时 | `--simulate-timeout-payload boolean_blind_and --request-timeout 2` → `report_incomplete.json` | 该载荷「未完成」(TimeoutError)，union 载荷仍「检出」，漏洞版结论不受影响 |
| 6 清理失败 | `--simulate-cleanup-failure` → `report_cleanup.json` | 两版 cleanup 均含警告与残留 PID；`netstat` 确认 18301/18302 无真实残留监听；正常运行的报告无警告 |
| 7 报告四字段 | 不带参数运行 → `report.json` | 四字段存在，值均为 null |
| 8 参数错误 | `python sqli_check.py --bogus-flag` | 输出 usage 提示，退出码 2，不崩溃 |
