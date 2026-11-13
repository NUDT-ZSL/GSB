# 本地授权账目查询服务 · 未授权访问检测工具

仅使用 Python 标准库（3.10+，实测 3.14.7），不依赖 Docker/容器，全部在本机
`127.0.0.1` 回环地址运行。

仓库包含四部分：

| 目录 | 作用 |
| --- | --- |
| `authsvc/` | 两版本地授权查询服务（`legacy` 含缺陷 / `fixed` 已修复） |
| `detector/` | 确定性探测器，输出 JSON 报告，支持单服务扫描与新旧版对比 |
| `tests/` | 16 项自动化测试，覆盖未授权拒绝、合法通过、畸形输入、新旧版对比 |
| `examples/` | 演示脚本与子进程编排工具 |

## 1. 快速开始

```powershell
# 在项目根目录
$env:PYTHONPATH = "."
```

### 启动两版服务（各占一个终端）

```powershell
# 旧版（含缺陷），端口 8765
python -m authsvc --version legacy --port 8765

# 修复版，端口 8766
python -m authsvc --version fixed --port 8766
```

`--port 0` 会由系统分配空闲端口，并可通过 `--ready-file reports/ready.json`
把 `host/port/base_url` 写出，便于脚本拉起。

### 业务操作样例

```powershell
$body = '{"token":"demo-shared-token-2026","account_id":"ACC-1001"}'

# 合法请求：200 + 账目业务字段
Invoke-WebRequest -Uri http://127.0.0.1:8766/api/accounts/query `
  -Method Post -Body $body -ContentType 'application/json'

# 缺失令牌：修复版 401，旧版 200 且泄漏数据
Invoke-WebRequest -Uri http://127.0.0.1:8766/api/accounts/query `
  -Method Post -Body '{"account_id":"ACC-1001"}' -ContentType 'application/json' `
  -SkipHttpErrorCheck
```

响应（合法）：

```json
{
  "ok": true,
  "account_id": "ACC-1001",
  "account_name": "Northwind Reserve",
  "owner": "finance-team",
  "currency": "CNY",
  "balance": "184320.50",
  "status": "active",
  "ledger": [
    {"date": "2026-10-01", "type": "credit", "amount": "72000.00", "reference": "INV-2026-1001"}
  ]
}
```

请求字段：`token`（共享令牌）、`account_id`（账目 ID，样本含 `ACC-1001`、`ACC-1002`）。

### 运行检测

```powershell
# 单版扫描（发现问题时退出码为 1）
python -m detector scan --base-url http://127.0.0.1:8765 --label legacy --out reports/legacy_report.json

# 修复版（退出码 0）
python -m detector scan --base-url http://127.0.0.1:8766 --label fixed --out reports/fixed_report.json

# 先后独立检测两版并输出对比
python -m detector compare `
  --legacy-url http://127.0.0.1:8765 `
  --fixed-url  http://127.0.0.1:8766 `
  --legacy-out reports/legacy_report.json `
  --fixed-out  reports/fixed_report.json `
  --out        reports/comparison_report.json
```

### 一键演示（自动起停服务）

```powershell
python examples/demo.py
```

### 运行测试

```powershell
python -m unittest discover -s tests -t . -v
```

## 2. 检测项与判定语义

探测器通过 HTTP 与目标交互，不导入目标服务的内部实现。共有 7 项检查：

| id | 类别 | 期望 |
| --- | --- | --- |
| `legitimate.valid_token` | 合法 | 200 且包含全部账目业务字段 |
| `unauthorized.missing_token` | 未授权 | 4xx，无业务数据、无内部细节 |
| `unauthorized.empty_token` | 未授权 | 同上 |
| `unauthorized.wrong_token` | 未授权 | 同上 |
| `malformed.invalid_json` | 畸形输入 | 4xx，无业务数据、无内部细节 |
| `malformed.oversized_field` | 畸形输入 | 4xx，无业务数据、无内部细节 |
| `consistency.legitimate_data_stable` | 稳定性 | 全部探测结束后合法数据与探测前完全一致 |

判定规则：

- 未授权场景只有 **4xx 且响应不含业务字段/业务值/内部细节** 才记 `outcome=rejected`，
  否则记 `not_rejected` 且 `status=vulnerability`，绝不记通过。
- 判定“业务数据泄漏”时同时检查 JSON 键名（`account_id`/`account_name`/`owner`/
  `currency`/`balance`/`status`/`ledger`）和已知业务值字面量，避免字段改名的绕过。
- 判定“内部细节泄漏”时检查异常栈、`File "...", line`、`.py` 路径与调试字段。
- 报告 `summary` 显式区分 `unauthorized_rejected` 与 `unauthorized_not_rejected`。

## 3. 实际验证结果（本机运行）

`python examples/demo.py` 输出：

```
== 报告 legacy ==
  [pass         ] legitimate.valid_token               合法请求通过（HTTP 200），响应包含账目业务字段
  [vulnerability] unauthorized.missing_token           未拒绝（HTTP 200），响应泄漏账目业务数据
  [vulnerability] unauthorized.empty_token             未拒绝（HTTP 200），响应泄漏账目业务数据
  [vulnerability] unauthorized.wrong_token             未拒绝（HTTP 200），响应泄漏账目业务数据
  [vulnerability] malformed.invalid_json               未正确拒绝（HTTP 500），响应泄漏内部异常细节
  [pass         ] malformed.oversized_field            已拒绝（HTTP 404），响应不含业务数据或内部细节
  [pass         ] consistency.legitimate_data_stable   探测前后合法账目数据一致，未被探测污染
  通过=3 漏洞=4 未授权已拒绝=0 未授权未拒绝=3
  总体: 发现漏洞/异常：unauthorized.missing_token, unauthorized.empty_token,
        unauthorized.wrong_token, malformed.invalid_json

== 报告 fixed ==
  [pass] legitimate.valid_token
  [pass] unauthorized.missing_token          已拒绝（HTTP 401）
  [pass] unauthorized.empty_token            已拒绝（HTTP 401）
  [pass] unauthorized.wrong_token            已拒绝（HTTP 401）
  [pass] malformed.invalid_json              已拒绝（HTTP 400）
  [pass] malformed.oversized_field           已拒绝（HTTP 413）
  [pass] consistency.legitimate_data_stable
  通过=7 漏洞=0 未授权已拒绝=3 未授权未拒绝=0
  总体: 无漏洞：所有探测项均通过
```

`python -m unittest discover -s tests -t .` → `Ran 16 tests ... OK`。

命令行方式（两个真实子进程 + 独立 CLI 调用）同样得到：
`scan legacy` 退出码 1（vulnerability_found），`scan fixed` 退出码 0（no_vulnerability），
`compare` 报告显示 4 个旧版漏洞项在修复版全部 `pass` 且 `resolved=true`。

## 4. 两版差异

`authsvc/legacy_server.py` 刻意保留的缺陷：

1. 缺失/空令牌被当作匿名内部调用直接放行；
2. 令牌用普通 `!=` 比较（非恒定时间）；
3. 错误令牌触发“降级模式”，仍返回完整账目；
4. 非法 JSON 返回 500 + 完整异常栈（含源码路径）。

`authsvc/fixed_server.py` 的修复：

- 缺失/空/错误令牌统一 401，响应仅为通用错误信封；
- 令牌比较只经 `authsvc/security.py::constant_time_token_match`，内部使用
  `hmac.compare_digest`，且比较前先做 SHA-256 归一化长度；固定代码中不存在
  对令牌的普通相等/不等比较（`tests/test_detector.py` 用反射 + 正则断言）；
- 请求体上限 4096 字节（413），非法 JSON/非对象/超长 `account_id` 返回 400，
  错误响应不含异常栈或路径；
- 所有拒绝路径都在读取账目样本之前返回。

> 说明：修复版对超长请求先 `_discard_body` 再返回 413。Windows 的 TCP 栈在仍有
> 未读入站数据时关闭连接会发 RST，客户端可能看不到 413；丢弃最多 1 MiB 既能
> 保证客户端稳定收到错误码，又不会无限读取。

## 5. 复现性与隔离性

- 样本数据是代码内固定常量，无随机、无外部文件，两次检测结果逐字节一致。
- `detect()` 只依赖传入的 `base_url`，不保存跨调用状态；对比模式先后调用两次
  独立 `detect()`，任一版结论不受另一版影响。
- 每项检查都重新发请求，且合法基线在探测前、探测后再各取一次做全量 JSON 对比，
  证明探测未污染合法数据。
- 服务仅绑定 `127.0.0.1`，不访问任何外部目标。
