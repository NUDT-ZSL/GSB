# 订单查询接口安全自测工具（SQL 注入 / 越权）

对本地运行的订单查询 HTTP 接口，按 JSON 用例发起检测请求，**基于响应内容判定 SQL 注入与越权是否真实生效**，
输出结构化 JSON 报告；随后对修复实例复用同一批用例做对比，确认漏洞消除且正常订单查询结果不变。

- 仅依赖 **Python 3.10+ 标准库**，无第三方依赖、无 Docker、无容器；
- 全程只访问配置中的**本地回环地址**，外部主机在建立连接前整体拒绝；
- 样本服务与用例均为本地确定性构造，可重复演示。

## 1. 目录结构

```
project25/
├── main.py                     # 检测工具入口（python -I main.py ...）
├── run_demo.py                 # 一键端到端验收编排（11 项要求，52 条断言）
├── order_sec_check/            # 检测工具包
│   ├── config.py               # 用例 JSON 结构校验（字段缺失/类型错误 -> 字段级报错并终止）
│   ├── netpolicy.py            # 本地回环地址白名单策略
│   ├── validate.py             # 业务参数合规校验（空用户标识/非数字订单号等）
│   ├── detector.py             # HTTP 检测 + 声明式命中规则引擎
│   ├── report.py               # 报告生成（升序/null/分区/不覆盖同名文件）
│   ├── redact.py               # 凭据脱敏（结构化 + 日志兜底过滤器）
│   ├── logging_setup.py        # 控制台与文件日志
│   └── cli.py                  # 命令行主流程（检测 / resume / 修复对比）
├── samples/
│   ├── order_service.py        # 本地订单服务样本（vulnerable / fixed / altered 三模式）
│   └── cases/
│       ├── order_cases.json    # 9 条正式用例（SQLi/IDOR/业务/拒绝/不可达重试）
│       ├── bad_missing_fields.json   # 故意缺字段/类型错误的用例（验收要求1）
│       └── external_host.json        # 故意指向外部主机的用例（验收要求10）
├── tests/test_tool.py          # 18 个标准库 unittest 单元测试
└── demo_output/                # 运行产物（报告/日志，已 gitignore）
```

## 2. 一键复现（推荐）

```powershell
# Windows PowerShell（macOS/Linux 去掉 -X utf8 亦可）
python -I -X utf8 run_demo.py
```

脚本会自动启停 5 个本地服务实例（端口 18080/18081/18082/18083/18099，均绑定 127.0.0.1），
依次执行 8 个阶段并打印 `[PASS]/[FAIL]`，最终汇总（当前实测 **52/52 PASS**）。
产物写入 `demo_output/reports/` 与 `demo_output/logs/`。

单元测试：

```powershell
python -I -X utf8 tests/test_tool.py
```

> `-I`（隔离模式）屏蔽用户站点包与环境变量注入，避免解释器从 cwd/环境加载到不可信模块；
> `-X utf8` 强制 UTF-8 输出，避免 Windows 控制台中文乱码。

## 3. 手工业务操作样例

### 3.1 启动样本服务（三个终端，或用后台进程）

```powershell
# 终端 A：漏洞版（SQL 拼接 + 无属主校验）
$env:MODE="vulnerable"; $env:PORT="18080"; python -I -X utf8 samples/order_service.py
# 终端 B：修复版（参数化 + 属主校验 + 入参校验）
$env:MODE="fixed";      $env:PORT="18081"; python -I -X utf8 samples/order_service.py
# 终端 C：数据变异版（修复代码 + 业务数据被改动，用于演示功能回退判据）
$env:MODE="altered";    $env:PORT="18082"; python -I -X utf8 samples/order_service.py
```

健康检查：`GET http://127.0.0.1:18080/health`。

样本内置测试身份（仅本地演示数据，非真实凭据）：

| X-Auth-Token      | 认证身份 |
|-------------------|----------|
| `tok-alice-001`   | alice    |
| `tok-bob-002`     | bob      |
| `tok-carol-003`   | carol    |

### 3.2 对漏洞实例做检测

```powershell
python -I -X utf8 main.py --cases samples/cases/order_cases.json `
    --report-dir demo_output/reports --log-dir demo_output/logs
```

预期：TC-01/TC-02（SQLi）、TC-03/TC-09（IDOR）标记 `hit`；TC-06/TC-07 因业务参数不合规标记
`rejected` 且**请求不发出**；TC-08 指向未启动的 18099 端口，标记 `incomplete` 并进入未完成分区。

### 3.3 对暂时不可达用例再次检测（resume）

```powershell
# 另起一个 18099 端口的实例后，只重跑上一份报告里的未完成用例
$env:MODE="vulnerable"; $env:PORT="18099"; python -I -X utf8 samples/order_service.py
python -I -X utf8 main.py --cases samples/cases/order_cases.json `
    --resume demo_output/reports/detection_report_YYYYMMDD_HHMMSS_01.json `
    --report-dir demo_output/reports --log-dir demo_output/logs
```

其余用例直接沿用基准结论，TC-08 补检完成，新报告 `incomplete_cases` 为空。

### 3.4 对修复实例复用同一批用例做对比

```powershell
python -I -X utf8 main.py --cases samples/cases/order_cases.json `
    --base-url http://127.0.0.1:18081 `
    --compare-baseline demo_output/reports/detection_report_YYYYMMDD_HHMMSS_01.json `
    --report-dir demo_output/reports --log-dir demo_output/logs
```

攻击用例基准 `hit`、当前 `miss` → `fixed`；若对另一个漏洞实例对比则为 `unfixed`；
阴性对照用例（`attack_intent: negative_control`）维持 `still_clean`；
`functional_regression_checks` 中正常查询 TC-05 与基准逐字节比对，一致则 `regression=false`。

### 3.5 两个"必须拒绝"的负向演示

```powershell
# 字段缺失/类型错误：退出码 2，打印字段级错误，不发起任何请求，不生成报告
python -I -X utf8 main.py --cases samples/cases/bad_missing_fields.json `
    --report-dir demo_output/reports --log-dir demo_output/logs

# 外部主机：退出码 3，提示拒绝原因，不发起任何请求
python -I -X utf8 main.py --cases samples/cases/external_host.json `
    --report-dir demo_output/reports --log-dir demo_output/logs
```

## 4. 用例文件格式

```json
{
  "target": { "base_url": "http://127.0.0.1:18080" },
  "cases": [
    {
      "case_id": "TC-01",
      "type": "sqli | idor | business",
      "attack_intent": "attack | negative_control（可选，默认 attack）",
      "description": "用例说明",
      "base_url": "可选，按用例覆盖目标（仍须为回环地址）",
      "request": {
        "method": "GET | POST",
        "path": "/orders",
        "headers": { "X-Auth-Token": "tok-alice-001" },
        "params": { "order_id": "1001", "user_id": "alice" }
      },
      "param_rules": { "order_id": ["positive_int"], "user_id": ["not_empty"] },
      "rules": [
        { "type": "status_code_equals", "expected": 200 },
        { "type": "json_len_gte", "path": "data", "expected": 2 },
        { "type": "json_path_equals", "path": "count", "expected": 5 },
        { "type": "body_contains", "expected": "sql_error" }
      ],
      "compare_ignore_fields": ["可选，功能对比时豁免的易变字段名"]
    }
  ]
}
```

- 命中判定规则（任一满足即命中）：`status_code_equals` / `json_path_equals` /
  `json_len_gte` / `json_len_equals` / `body_contains` / `body_not_contains`；
- 业务参数规则（不通过即拒绝、不发请求）：`not_empty` / `numeric` / `positive_int`。

## 5. 报告说明

- 文件名：`detection_report_时间戳_序号.json`、`comparison_report_时间戳_序号.json`；
  目录中同名文件**永不覆盖**（先查存在性，再以排他模式 `"x"` 落盘双保险）。
- 检测报告 `results` 按 `case_id` 编号升序，每条含 `case_id/type/status/hit/
  request_summary/response_summary/reject_reasons/error`，摘要缺失处为 `null`；
  顶层区分 `completed_case_ids` 与 `incomplete_cases`，不可达用例 `hit=null`，不计未命中。
- 对比报告 `verdict`：`fixed`（基准命中、当前未命中）/ `unfixed`（仍命中）/
  `still_clean`（本就干净且保持）/ `rejected` / `incomplete` / `business_checked`；
  `functional_regression_checks` 给出正常订单查询的功能回退判据。
- 凭据（含 token 类头部值与已知凭据原文）在报告与日志中一律为 `***REDACTED***`。

## 6. 安全边界

1. 仅允许 `127.0.0.0/8`、`::1`、`localhost` 等回环目标；域名先解析再判断，解析失败默认拒绝；
   重定向跳转目标同样过策略。任何外部主机 → 整体拒绝、零请求、退出码 3。
2. 结构校验与业务合规校验全部发生在 socket 建立之前；被拒绝用例不会产生网络流量
   （`run_demo.py` 通过服务端 `/admin/access-log` 实证）。
3. 不使用 Docker/容器，不访问任何未授权目标；样本端口全部绑定 `127.0.0.1`。
