# 本地订单查询接口 SQL 注入自查工具

面向自建本地订单查询接口的**授权防御性** SQL 注入自查工具。纯 Python 标准库实现，
仅探测回环地址目标，使用确定性构造的 SQLite 合成订单库，**不访问公网、不使用真实凭据、
不依赖 Docker 或任何第三方包**（Python 3.10+）。

## 1. 能力概览

| 能力 | 说明 |
| --- | --- |
| 配置严格校验 | 字段缺失/类型错误逐字段报错，校验通过前不发任何请求 |
| 基线可达性 | 先用正常订单号探测，不可达即说明原因并以非零码结束 |
| 逐参数注入探测 | 每个查询参数独立施加固定载荷，定位改变查询语义/越权返回的参数与位置 |
| 回显差异降噪 | 仅把越权泄露或逻辑改变判为可疑，页面正常回显不算注入 |
| 二次确认 | 对可疑参数做布尔差异 + UNION 哨兵确认，标注确认状态与证据 |
| 确定性报告 | JSON 键序固定、无时间/耗时（固定占位值），同输入两次运行逐字节一致 |
| 修复对照 | 同批参数/载荷复测修复版，输出前后对照与残留风险单列 |
| 故障韧性 | 单探测失败不中断，标注未完成；恢复后续扫只补未完成项、不重复计数 |
| 业务摘要 | 面向业务人员的文本结论，存在未完成项时明确提示覆盖不全、不宣称安全 |
| 自检子命令 | 内置确定性样本验证全部能力，输出各阶段通过数量 |

## 2. 目录结构

```
project23/
├── sqli_audit/              # 工具包（仅标准库）
│   ├── __main__.py          # CLI 入口：setup / scan / compare / selfcheck
│   ├── config.py            # 目标描述文件加载与严格校验（回环限制、逐字段报错）
│   ├── payloads.py          # 固定探测载荷目录（无注释、引号自平衡，顺序恒定）
│   ├── client.py            # 受限本地 HTTP 客户端（禁重定向、无凭据、异常结构化）
│   ├── analysis.py          # 响应指纹、注入判定、二次确认评估
│   ├── scanner.py           # 扫描编排（基线→探测→确认，单失败不中断）
│   ├── state.py             # 断点续扫状态（只缓存成功结论）
│   ├── report.py            # 确定性 JSON 报告与业务文本摘要
│   ├── compare.py           # 修复前后对照与残留识别
│   ├── lab.py               # 确定性 SQLite 样本库 + 五种模式本地接口
│   ├── setup.py             # 一键生成实验环境（库/独立接口脚本/目标配置）
│   └── selfcheck.py         # 自检（10 阶段 42 项断言）
├── tests/                   # unittest 测试（30 个，配置/判定/端到端）
├── demo.ps1 / demo.sh       # 一键演示（Windows / POSIX）
└── lab_ws/                  # 运行 setup 后生成（已在 .gitignore，可随时重建）
```

## 3. 快速开始（可复现）

要求：Python 3.10+，在项目根目录执行。

### 3.1 一键演示（推荐，覆盖全部验收场景）

```powershell
# Windows PowerShell（5.1 与 7+ 均可）
powershell -ExecutionPolicy Bypass -File .\demo.ps1
```

```bash
# git-bash / WSL / macOS / Linux
bash demo.sh
```

脚本会自动生成环境、起停本地接口、跑完 14 类验收场景并在结尾打印报告目录。

### 3.2 分步操作（业务人员视角）

**第一步：生成确定性实验环境**

```bash
python -m sqli_audit setup lab_ws
```

生成内容（固定数据，重复生成同名文件逐字节一致）：

| 文件 | 含义 |
| --- | --- |
| `sample_orders.db` | 合成 SQLite 订单库（3 个客户、10 条订单） |
| `server_vulnerable.py` :8031 | 历史缺陷：`order_no`、`customer_id` 均字符串拼接 |
| `server_fixed.py` :8032 | 修复版：两个参数全部参数化 |
| `server_partial.py` :8033 | 部分修复：仅 `customer_id` 参数化，`order_no` 仍可注入 |
| `server_echo.py` :8034 | 正常接口：只回显订单号、不查库（验证不误报） |
| `server_flaky.py` :8035 | 同缺陷版，带故障门控（环境变量 `SQLI_AUDIT_FAIL_AFTER=N`） |
| `target_<mode>.json` | 各接口对应的目标描述文件 |

**第二步：另开一个终端启动待测接口**（以缺陷版为例，保持窗口运行）

```bash
python lab_ws/server_vulnerable.py
# => lab server mode=vulnerable listening on http://127.0.0.1:8031/orders
```

**第三步：执行自查扫描**

```bash
python -m sqli_audit scan lab_ws/target_vulnerable.json
```

输出业务摘要，并在 `lab_ws/reports/` 生成 `*_report.json`（机读）与 `*_summary.txt`（人读），
在 `lab_ws/state/` 生成续扫状态文件。退出码：`0` 完成（可能含未完成项，见报告）、
`1` 配置/用法错误、`2` 基线不可达。

**第四步：修复后复测并对照**

```bash
python lab_ws/server_fixed.py            # 另开终端启动修复版
python -m sqli_audit compare lab_ws/target_fixed.json \
    --before lab_ws/reports/target_vulnerable_report.json \
    --output lab_ws/reports/comparison_fixed.json
```

**第五步：自检（无需先启动任何服务，内部自起临时回环服务）**

```bash
python -m sqli_audit selfcheck
# => 10 个阶段全部 PASS，合计 42/42 通过，退出码 0
```

> 工作目录无关性：在任意目录运行时设置 `PYTHONPATH` 指向项目根即可，例如
> `set PYTHONPATH=D:\demo_GSB\demo\projects\project23`（Windows）或
> `export PYTHONPATH=/path/to/project23`（POSIX）。报告内容只取决于目标描述与载荷，
> 与工作目录无关。

## 4. 目标描述文件格式

```json
{
  "target_name": "lab-order-api-vulnerable",
  "base_url": "http://127.0.0.1:8031",
  "endpoint_path": "/orders",
  "method": "GET",
  "baseline": {"order_no": "ORD-1001", "customer_id": "C001"},
  "params": [
    {"name": "order_no", "location": "query", "type": "string", "label": "订单号参数"},
    {"name": "customer_id", "location": "query", "type": "string", "label": "客户号参数"}
  ],
  "data_path": ["data", "orders"],
  "order_id_field": "order_id",
  "timeout_seconds": 5
}
```

校验规则（任一不满足即整体拒绝，**不会发出请求**）：

- 顶层 `target_name/base_url/endpoint_path/method` 必须为非空字符串；`baseline` 必须为对象；
  `params` 必须为非空数组；`timeout_seconds` 必须为正整数；
- `base_url` 必须是 `http` 且主机为回环地址（`127.0.0.1` / `localhost` / `::1`）；
- 每个参数必须含非空字符串 `name/location/type/label`，`location` 仅支持 `query|body`，
  `GET` 不允许 `body` 参数，参数不得重复；
- `baseline` 的键必须能在 `params` 中找到；
- 兼容带 UTF-8 BOM 与不带 BOM 的配置文件。

## 5. 判定方法（为什么正常回显不会误报）

1. **基线指纹**：用正常订单号请求一次，记录数据子树（默认 `data.orders`）的订单号集合、
   行数与规范化 SHA-256。
2. **逐参数探测**：每次只替换一个参数为探测载荷，其余参数保持基线值。判定只比较
   **数据子树**，不比较输入在页面上的回显：
   - 行数增多且出现基线外订单 → 越权泄露，判 **suspicious**；
   - UNION 结果出现哨兵订单号 `LEAK` → 可注入额外行，判 **suspicious**；
   - 仅结果变少、仅服务端 500、仅语法差异而无数据扩张 → 只记 `informational`，不判疑；
   - 数据子树与基线完全一致（含“把输入原样显示出来”的回显接口）→ `no_effect`。
3. **二次确认**：对初筛可疑参数依次施加布尔真、布尔假、UNION 哨兵三个确认载荷，
   布尔差异（真扩张/假归零）或哨兵行任一复现即标记 `confirmed`，并附响应差异摘要；
   确认阶段有未连通项则标 `incomplete`，绝不把证据不足当作安全。
4. **载荷设计**：全部载荷不依赖 SQL 行注释，采用字符串引号自平衡写法，因此对
   “参数后仍有 AND 子句/仍有绑定占位符”的查询同样有效；在参数化查询中它们只是普通字符串值。

## 6. 报告说明

### 6.1 扫描报告 `*_report.json`（固定排序、可复现）

- 顶层字段：`report_schema_version / generated_at / elapsed / run_kind / target /
  scan_summary / baseline / parameters / business_summary`；
- `generated_at` 与 `elapsed` 恒为占位值 `OMITTED_FOR_REPRODUCIBILITY`，报告中没有任何时钟读数；
- `parameters` 按 `(name, location)` 固定排序；每个参数含 `status`、`confirmation_status`、
  `evidence_summary`、`suspicious_probe_ids`、`incomplete_reasons` 及每个载荷的请求结论与响应指纹；
- `status` 取值：`vulnerable_confirmed`（已确认注入）、`suspicious_unconfirmed`（可疑但确认未成立）、
  `not_suspicious`（无可疑）、`incomplete`（存在未完成探测）。

### 6.2 对照报告 `comparison_*.json`

- `comparisons`：逐参数的 `before_status/after_status/conclusion`（`fixed` /
  `remains_vulnerable` / `retest_incomplete` / `previously_clean`）与双方证据；
- `residual_vulnerable_params`：**修复后仍可越权的参数单独清单**，不会被已修复项掩盖。

### 6.3 业务文本摘要 `*_summary.txt`

- 覆盖完整时给出可用结论，并声明结论仅对本批载荷与样本有效；
- 存在未完成项时明确写出“覆盖不全提示”与“未覆盖不等于安全，请勿宣称全部参数无风险”。

## 7. 故障与续扫机制

- 任何单次请求的传输异常（连接被拒、连接被断开、超时）都被捕获为 `probe_error`，
  扫描继续推进其余参数，受影响项标记未完成并写入 `incomplete_reasons`；
- 状态文件只缓存**成功**结论；恢复后对同一状态文件重跑同一命令，已完成项直接复用
  （不发请求、不重复计数），仅未完成项重新探测；
- 目标描述或载荷集合变化会改变目标指纹，旧状态自动失效，避免错误复用；
- 演示故障：`server_flaky.py` 在 `SQLI_AUDIT_FAIL_AFTER=N` 时，第 N+1 次请求起直接断开
  连接（确定性模拟服务中途停止）；删除该环境变量即恢复，无需更换端口。

## 8. 14 条任务要求与验收对照（均已实际运行验证）

| # | 任务要求 | 验证方式与实测结果 |
| --- | --- | --- |
| 1 | 缺字段/错类型明确提示且不探测 | 提交缺字段配置，退出码 1，列出 `target_name/params`，自检断言校验期零请求 |
| 2 | 基线不可达即说明原因并结束 | 未启服务扫描，退出码 2，输出 `WinError 10061` 连接拒绝与目标 URL |
| 3 | 定位可疑参数与位置 | 缺陷接口报告 `order_no`/`customer_id` 为 `vulnerable_confirmed`，位置 `query` |
| 4 | 正常回显不误报 | echo 接口两参数均 `not_suspicious`，零可疑 |
| 5 | 二次确认状态与证据 | 可疑项带 `confirmed`，证据含“真 10 行 > 基线 1 行、假 0 行、哨兵 LEAK” |
| 6 | 报告 JSON 固定排序、可复现 | 两次运行 SHA-256 完全相同（如 `761F2713…6E971`） |
| 7 | 修复对比、字段完整 | fixed 复测两参数结论均 `fixed`，对照含前后状态与双方证据 |
| 8 | 残留风险单列 | partial 对照中 `order_no=remains_vulnerable`，仅它进入残留清单 |
| 9 | 单失败不中断、标注未完成 | 门控断连后 customer_id 正常完成，order_no 4 项标记未完成，流程不中断 |
| 10 | 恢复续扫、不重复计数 | 重跑后 8/8 探测、6/6 确认；状态缓存 14 条唯一键，已完成结论不变 |
| 11 | 业务摘要不把缺失当安全 | 未完成时摘要含“覆盖不全提示/未覆盖不等于安全” |
| 12 | 时间耗时固定/省略 | 两字段恒为 `OMITTED_FOR_REPRODUCIBILITY` |
| 13 | 顺序载荷固定、与工作目录无关 | 两个不同 cwd 产出报告哈希一致，且与默认目录结果一致 |
| 14 | 自检子命令 | `selfcheck` 输出 10 阶段 42/42 通过，退出码 0 |

测试与自检命令：

```bash
python -m unittest discover -s tests -v   # 30 个单元/端到端测试
python -m sqli_audit selfcheck           # 42 项确定性自检
```

## 9. 安全边界与适用范围

- 工具在配置层强制只允许回环 HTTP 目标，禁止重定向、不携带 Cookie/凭据；
- 所有数据为合成样本，接口与库均由任务代码确定性生成，可随时删除 `lab_ws/` 后用
  `setup` 原样重建；
- 结论仅对目标描述中的参数、固定载荷集合与当前样本数据有效，不替代完整的安全评估或人工审计；
- 仅应用于你拥有或已获授权测试的本地系统。
