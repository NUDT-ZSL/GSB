# 实测验证记录

- 验证时间：2026-11-14
- 运行环境：Windows 11，Python 3.14.7（代码兼容 3.10+），仅标准库，无 Docker/容器
- 验证方式：`python -I -X utf8 run_demo.py` 全自动端到端编排 + `python -I -X utf8 tests/test_tool.py` 单元测试
- 样本服务全部绑定 `127.0.0.1`，端口 18080/18081/18082/18083/18099，未访问任何外部目标

## 1. 总体验收结果

| 验证项 | 结果 | 证据 |
|--------|------|------|
| 端到端编排 | **52/52 PASS** | `run_demo.py` 输出汇总 `通过 52/52` |
| 单元测试 | **18/18 OK** | `tests/test_tool.py`，Ran 18 tests OK |
| 残留服务进程 | 0 | 编排结束后 Win32_Process 查询 order_service 计数为 0 |

## 2. 11 项要求逐条对照

### 要求 1：字段缺失/类型错误即终止、零请求
- 运行 `main.py --cases samples/cases/bad_missing_fields.json`，进程退出码 **2**，输出字段级错误：
  - `$.cases[0].description: 必填字段缺失`
  - `$.cases[1].request.method: 必须为 ['GET', 'POST'] 之一，实际值 12345`
  - `$.cases[1].request.headers: 必须为对象，实际类型 array`
  - `$.cases[1].rules: 必填字段缺失（命中判定规则）`
  - `$.cases[2].case_id: 必须为非空字符串`
  - `$.cases[2].type: 必须为 ['business', 'idor', 'sqli'] 之一，实际值 'unknown_type'`
- 编排脚本同时读取服务端 `/admin/access-log`，断言校验失败期间请求数为 **0**；未生成报告文件。

### 要求 2：SQL 注入真实生效判定
- TC-01 载荷 `1001' OR '1'='1' -- ` 在漏洞实例改变 WHERE 语义，返回全部 5 条订单；
  报告 `status=hit`，`response_summary.body` 含 `"count": 5`。
- TC-02 单引号载荷触发微型 SQL 引擎 `sql_error`（HTTP 500），规则 `body_contains sql_error` 命中。
- 修复实例上同批用例均 `miss`（参数化绑定使载荷退化为普通字符串，先于查询的格式校验返回 400）。

### 要求 3：越权（IDOR）判定
- TC-03：alice 持自己的 token 读取 bob 的 1003 订单，漏洞实例返回 200 且 `data` 长度 1 → `hit`；
  报告同时记录 `request_summary`（方法/URL/脱敏头部）与 `response_summary`（状态码/响应体摘要）。
- TC-09：carol 读取 alice 的 1002 订单，同样 `hit`。
- 修复实例返回 403（属主校验），两条用例均 `miss`。

### 要求 4：报告字段、升序、null 摘要
- `results` 实际顺序：TC-01 … TC-09（按编号数字升序，TC-01 < TC-02 < … < TC-09）。
- 每条含 `case_id/type/status/hit/request_summary/response_summary/rule_detail/reject_reasons/error`。
- 不可达的 TC-08：`request_summary=null`、`response_summary=null`、`hit=null`。

### 要求 5：暂时不可达保留重试机会，分区呈现
- 首跑 TC-08 指向未启动的 18099 端口，`status=incomplete`，错误原因
  `WinError 10061 由于目标计算机积极拒绝`，进入顶层 `incomplete_cases`，不计入 miss；
  汇总 `completed=8, incomplete=1`，其余 8 条正常出结论。
- 启动 18099 实例后 `--resume` 重跑：仅 TC-08 发请求并 `hit`，其余用例沿用基准结论，
  新报告 `incomplete_cases=[]`，汇总 `completed=9`。

### 要求 6：修复对比 fixed / unfixed
- 对 fixed 实例（18081）对比：TC-01/TC-02/TC-03/TC-09 全部 `fixed`（基准 hit → 当前 miss），
  阴性对照 TC-04 为 `still_clean`，拒绝用例保持 `rejected`。
- 对另一个 vulnerable 实例（18083）对比：4 条攻击用例全部 `unfixed`（hit → hit），
  报告 summary `fixed=0, unfixed=4`。

### 要求 7：功能回退判据（正常查询结果一致性）
- fixed 实例：TC-05（alice 查 1001）响应体与基准逐字节一致，`regression=false`，
  明细"修复前后正常查询响应一致"，summary `functional_abnormal=0`。
- altered 实例（订单金额/名称被确定性改动）：同一用例 `regression=true`，
  明细"修复前后正常查询响应不一致，疑似功能回退"，summary `functional_abnormal=1`；
  其安全结论仍为 `fixed`，证明安全修复与功能回退是两条独立判据。

### 要求 8：同名报告不覆盖
- 连续两次检测生成 `detection_report_20261114_025120_01.json` 与 `..._025125_01.json` 两个不同文件；
  对旧文件做 SHA-256 前后比对，内容不变。写入侧另以排他模式 `"x"` 兜底。

### 要求 9：业务不合规参数拒绝且零请求
- TC-06（`user_id="   "`）：`status=rejected`，原因 `参数 'user_id' 为空，必须为非空字符串`。
- TC-07（`order_id="abc;DROP"`）：`status=rejected`，原因含 `必须为正整数`。
- 服务端 `/admin/access-log` 实证：两条用例均无对应请求到达（仅 6 个合规/攻击用例的 /orders 记录，
  无 400、无 `abc;DROP` 特征）。

### 要求 10：仅允许本地地址，外部主机整体拒绝
- 运行 `main.py --cases samples/cases/external_host.json`（目标 `order-db.example.invalid`），
  退出码 **3**，输出
  `目标主机 ... 无法解析且不是回环地址，拒绝发起任何请求`；
  服务端访问日志为空，零请求。策略在 socket 建立前执行，重定向跳转同样受约束。

### 要求 11：可读日志与凭据不明文
- 日志文件 `demo_output/logs/security_check.log` 含
  `================ 安全检测开始 ================` 与 `================ 安全检测完成 ================`，
  并含 WARNING（拒绝）/ERROR（不可达、策略拒绝）异常事件。
- 全文检索三个测试 token（`tok-alice-001`/`tok-bob-002`/`tok-carol-003`）均 **0 命中**；
  脱敏样例行：
  `请求头(脱敏)={'X-Auth-Token': '***REDACTED***', 'X-User-Id': 'alice'}`。
- 报告 JSON 同样检索不到 token 原文（报告层与日志层双重脱敏 + 日志 Filter 文本兜底）。

## 3. 产物清单（最近一次运行）

```
demo_output/reports/
├── detection_report_*.json   # 首跑（含 1 条 incomplete）
├── detection_report_*.json   # 第二次检测（不覆盖验证）
├── detection_report_*.json   # resume 补检后（9 条全部完成）
├── comparison_report_*.json  # fixed 实例对比（4 fixed，功能无异常）
├── comparison_report_*.json  # altered 实例对比（functional_abnormal=1）
└── comparison_report_*.json  # vulnerable 实例对比（4 unfixed）
demo_output/logs/security_check.log
```

## 4. 复现实验命令汇总

```powershell
python -I -X utf8 tests/test_tool.py     # 18 个单元测试
python -I -X utf8 run_demo.py            # 52 项端到端验收（自动启停所有本地服务）
```

手工分步操作见 `README.md` 第 3 节。
