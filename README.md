# 本机注入缺陷验证工具（Local Injection Scanner）

面向公司内部授权评估的**本地** Web 查询服务注入缺陷自动验证工具。工具自动探测目标查询参数、
识别注入缺陷类型、输出带 HTTP 响应证据的 JSON 报告与控制台摘要，并对修复版服务做回归验证。

> 安全边界：工具**只允许访问本机目标**（`127.0.0.1` / `localhost` / `::1` / 回环网段）。
> 任何非本机地址在发起任何网络请求之前即被拒绝并以错误码退出。全程无外部依赖、无外部网络访问、不使用容器。

## 1. 目录结构

```
project16/
├── local_injection_scanner/        # 扫描工具（仅用 Python 标准库）
│   ├── __init__.py
│   ├── cli.py                      # 命令行入口、本机限制、退出码
│   ├── scanner.py                  # 扫描编排：参数级隔离、延迟等待、报告组装
│   ├── http_client.py              # 受限 HTTP 客户端 + 全量请求记录
│   ├── detector.py                 # 响应特征判定、证据截断（<=500）
│   ├── payloads.py                 # 15 个载荷 / 8 个注入大类
│   └── logger.py                   # 带时间戳的文件+控制台日志
├── lab/
│   └── vuln_server.py              # 本地确定性靶场：vulnerable / fixed 两模式
├── tests/
│   └── test_acceptance.py          # 覆盖 14 条验收的端到端测试（10 个用例）
├── demo.ps1                        # Windows 一键演示脚本
└── demo_output/                    # 运行产物（报告 JSON、日志）
```

环境要求：Python 3.10+（实测 Python 3.14.7，Windows 11）。无第三方依赖。

## 2. 本地启动方式

### 2.1 启动靶场服务（仅监听 127.0.0.1）

```powershell
# 漏洞版：username 存在 SQL/XSS/命令/SSTI/NoSQL 注入；item 对特定载荷返回普通 500
python -I lab\vuln_server.py --mode vulnerable --port 8021 --host 127.0.0.1

# 修复版：参数化处理 + HTML 转义，无任何注入特征
python -I lab\vuln_server.py --mode fixed --port 8022 --host 127.0.0.1
```

> `-I` 为 Python 隔离模式，避免当前目录下被植入的同名模块（如 `json.py`）被加载。
> 靶场也支持 `--start-delay N`，进程启动 N 秒后才监听，用于验证延迟探测。

### 2.2 运行扫描器

```powershell
# 基本用法
python -I local_injection_scanner\cli.py `
    --target http://127.0.0.1:8021 `
    --report demo_output\vuln_report.json `
    --params username,item `
    --log demo_output\vuln_scan.log

# 延迟探测：服务暂不可用时最多等待 20 秒，恢复后自动完成
python -I local_injection_scanner\cli.py `
    --target http://127.0.0.1:8021 --report out.json --params username --delay 20
```

### 2.3 命令行选项

| 选项 | 必填 | 说明 |
|------|------|------|
| `--target` | 是 | 目标地址，仅限本机（http/https，127.0.0.1 / localhost） |
| `--report` | 是 | JSON 报告输出路径（父目录自动创建） |
| `--params` | 否 | 待检测查询参数，逗号分隔，默认 `username,item` |
| `--delay` | 否 | 服务暂不可用时的最长等待秒数，默认 0 |
| `--log` | 否 | 日志文件路径，记录带时间戳的关键事件 |

无参数运行即打印用法（含 target / report / delay 三个选项）并以退出码 2 结束。

### 2.4 一键演示与自动化测试

```powershell
pwsh -File demo.ps1                                   # 交互式覆盖 6 类场景
python -I tests\test_acceptance.py                    # 10 个端到端验收用例
```

## 3. 退出码约定

| 退出码 | 含义 |
|--------|------|
| 0 | 目标可达且**未发现缺陷** |
| 1 | **发现注入缺陷** |
| 2 | 参数或目标地址错误（含非本机目标、URL 格式非法、缺少必填项） |
| 3 | 目标服务持续不可达（不产生任何"安全"判定） |

## 4. 报告结构（JSON）

```json
{
  "scan_time": "2026-11-14 00:07:22",
  "target": "http://127.0.0.1:8021",
  "target_host": "127.0.0.1",
  "service_reachable": true,
  "conclusion": "发现缺陷：参数 username 存在注入缺陷",
  "summary": {
    "parameters_tested_count": 2,
    "vulnerable_parameters_count": 1,
    "findings_count": 12,
    "safe_parameters": [],
    "errored_parameters": ["item"]
  },
  "findings": [
    {
      "parameter": "username",
      "injection_type": "SQL注入",
      "payload_category": "sql_error",
      "payload": "'",
      "request_url": "http://127.0.0.1:8021/query?username=%27",
      "status_code": 500,
      "evidence": "<html>...sqlite3.OperationalError...SQL syntax error...",
      "evidence_length": 284
    }
  ],
  "parameter_results": [ ... ],
  "request_log": [ {"method":"GET","host":"127.0.0.1","payload":"...", ...} ]
}
```

## 5. 载荷覆盖（8 大类 / 15 个，验收要求 >=5 种）

| 大类 payload_category | 缺陷类型 | 代表载荷 |
|-----------------------|----------|----------|
| sql_error | SQL注入 | `'`、`") OR ("1"="1` |
| sql_boolean | SQL注入 | `' OR '1'='1`、`admin' AND '1'='2` |
| sql_union | SQL注入 | `' UNION SELECT NULL,NULL-- -` |
| sql_stacked | SQL注入 | `'; DROP TABLE users-- -` |
| xss_reflected | 反射型XSS | `<script>alert(1)</script>`、`"><img src=x onerror=...>` |
| cmd_injection | 命令注入 | `; id`、`| whoami`、`$(whoami)` |
| nosql | NoSQL注入 | `{"$ne": null}`、`$gt` |
| ssti | 模板注入SSTI | `{{7*7}}`、`${7*7}` |

判定原则：**载荷大类优先 + 响应特征确认**。普通 200 响应不报；只有响应中出现数据库报错、
命令执行结果（`uid=`）、模板求值（`49`）、Mongo/BSON 报错、或可执行脚本结构被原样反射时才报缺陷。
普通 5xx（无注入特征）只记为请求错误，不构成缺陷证据。

## 6. 14 条验收对照与实测结果

实测环境：Windows 11 + Python 3.14.7，全部用例通过（`Ran 10 tests ... OK`）。

| # | 验收点 | 实现位置 | 实测结果 |
|---|--------|----------|----------|
| 1 | 识别缺陷参数名与类型 | scanner/detector | 报出 `username`，含 SQL/命令/SSTI/XSS/NoSQL 五类 ✅ |
| 2 | 载荷 >=5 种且请求仅本机 | payloads.py、http_client 白名单 | 8 大类；request_log 主机集合仅 `{127.0.0.1}` ✅ |
| 3 | JSON 报告含参数/载荷/非空证据 | cli.py 写报告 | 指定路径生成，evidence 非空 ✅ |
| 4 | 延迟探测，服务恢复后完成 | scanner.wait_for_service | 端口关闭时启动，等待约 6s 后自动检出，退出码 1 ✅ |
| 5 | 持续不可达标注且不判安全 | scanner 不可达分支 | `service_reachable=false`，safe_parameters=[]，退出码 3 ✅ |
| 6 | 单参数出错不中断其他参数 | `_scan_one_param` 隔离 try/except | item=error 时 username 仍检出 ✅ |
| 7 | 修复版无缺陷且无证据 | fixed 模式 + 判定逻辑 | findings=[]，两参数 safe，退出码 0 ✅ |
| 8 | 证据为响应片段且 <=500 字符 | truncate_evidence | 超长响应截断后严格 <=500，前缀与响应一致 ✅ |
| 9 | CLI 含 target/report/delay，无参显示用法 | argparse | 无参打印用法，退出码 2 ✅ |
| 10 | 区分正常 200 与注入成功响应 | detector 特征确认 | 普通 200 不报；数据库错误才报 ✅ |
| 11 | 非本机地址拒绝 | assert_local_target | 外部地址输出"仅支持本地目标"，退出码 2 ✅ |
| 12 | 控制台摘要含参数数与缺陷数 | print_console_summary | 显示"已检测参数数 / 发现缺陷数" ✅ |
| 13 | 日志含时间戳与关键事件 | logger.TimestampLogger | 日志含 `[YYYY-mm-dd HH:MM:SS] 开始扫描` ✅ |
| 14 | 退出码 1/0/2 | cli.main | 漏洞=1、修复=0、目标错误=2（另增不可达=3）✅ |

## 7. 复现实验记录（手工实测摘录）

```
漏洞服务扫描：DELAY_EXIT=1，发现缺陷数 12，证据最大长度 284（<=500）
修复版扫描  ：FIXED_EXIT=0，findings=[]，safe=['username','item']
不可达      ：DEAD_EXIT=3，service_reachable=False，safe_parameters=[]
外部地址    ：输出"仅支持本地目标"，EXTERNAL_EXIT=2，不生成报告
延迟探测    ：扫描前端口确认关闭，ELAPSED≈6.4s 后完成，退出码 1
自动化测试  ：Ran 10 tests in 9.060s — OK
```

## 8. 合规与范围说明

- 仅用于公司内部**授权**评估，靶场与扫描目标均限定在本机回环地址。
- 不使用 Docker 或任何容器；靶场为标准库 `http.server` 实现的确定性样本。
- 不访问任何未授权或外部目标；非本机目标在建立连接前即被拦截。
