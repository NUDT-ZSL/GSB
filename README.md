# 项目47：本地文件上传网关绕过检测工具

仅使用 **Python 3.10+ 标准库**（实测 Python 3.14.7），不使用 Docker、不访问任何外部目标。
所有样本由代码确定性构造（固定边界、固定魔数、固定脚本片段，无随机数据）。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `gateway.py` | 本地网关模拟程序，`--mode permissive`（放行/加固前）与 `--mode hardened`（加固后）一键切换，`--drop-on` 支持故障注入 |
| `upload_scanner.py` | 检测工具：校验地址与配置 → 构造三类伪装载荷 → 逐一投递 → 判定 → 写 JSON/摘要/日志 |
| `config.json` | 表单字段配置（上传路径、文件字段名、普通文本字段） |
| `run_demo.py` | 一键验证脚本，覆盖全部 9 项验收点（56 项断言），产物在 `demo_output/` |

## 固定载荷清单（三类，缺一不可）

| 编号 | 类别 | 严重程度 | 文件名 | 声明 MIME | 伪装手法 |
| --- | --- | --- | --- | --- | --- |
| P01 | double_extension（双扩展名） | high | `shell.php.jpg` | image/jpeg | 以 `.jpg` 结尾但内嵌 `.php`，内容为 PHP 脚本 |
| P02 | mime_content_mismatch（MIME 与内容不符） | medium | `avatar.jpg` | image/jpeg | 声明 JPEG，实际内容是 PNG 魔数 |
| P03 | scripted_filename（带脚本内容的文件名） | low | `notes<script>alert(1)</script>.png` | image/png | 文件名包含 `<script>` 片段 |

判定规则：网关返回 HTTP 响应且 JSON 中 `accepted=true` → `accepted`；
其余响应 → `rejected`；连接类故障（拒绝、断连、超时等）→ `incomplete`，
故障项不参与接受/拒绝统计，**不污染其他载荷结论**。

## 一、一键复现（推荐）

```powershell
python -I run_demo.py
```

脚本自动完成：动态申请空闲端口（避免与本机其他服务冲突）、
依次启动放行/加固/故障注入三种网关场景、运行检测并校验 JSON、摘要、日志。
末尾输出 `总计 56 项检查，通过 56 项，失败 0 项` 即全部验收点通过。

## 二、手动分步演示

### 1. 启动放行模式网关（加固前，存在缺陷）

```powershell
python -I gateway.py --host 127.0.0.1 --port 18099 --mode permissive
```

另开一个终端运行检测：

```powershell
python -I upload_scanner.py http://127.0.0.1:18099/upload `
  --config config.json `
  --output result_permissive.json `
  --summary summary_permissive.txt `
  --log-file scan_permissive.log -v
# 退出码 1 = 发现绕过（accepted 项非 0）
```

预期：P01/P02/P03 全部 `accepted`，每项带可复核 `evidence`。

### 2. 切换加固模式（同一配置重跑）

停掉网关后以加固模式重启：

```powershell
python -I gateway.py --host 127.0.0.1 --port 18099 --mode hardened
```

```powershell
python -I upload_scanner.py http://127.0.0.1:18099/upload `
  --config config.json `
  --output result_hardened.json `
  --summary summary_hardened.txt `
  --log-file scan_hardened.log -v
# 退出码 0 = 全部被拒绝，无绕过
```

预期：三类全部 `rejected`，`accepted_total=0`，`evidence` 均为 `null`。

### 3. 故障注入（网关对指定载荷直接断连）

```powershell
python -I gateway.py --host 127.0.0.1 --port 18099 --mode permissive --drop-on P02
```

```powershell
python -I upload_scanner.py http://127.0.0.1:18099/upload --config config.json `
  --output result_drop.json --summary summary_drop.txt --log-file scan_drop.log
# 退出码 3 = 存在未完成项
```

预期：P02 为 `incomplete` 且 `error` 非空（如
`RemoteDisconnected: Remote end closed connection without response`），
P01/P03 仍为 `accepted`，结论互不污染。

### 4. 前置校验失败（不发起任何请求，退出码非 0）

```powershell
# 省略参数
python -I upload_scanner.py
# 外部地址（非回环，直接拒绝，退出码 2，无网络请求）
python -I upload_scanner.py http://203.0.113.5/upload --config config.json
# 配置缺失 / 字段非法
python -I upload_scanner.py http://127.0.0.1:8080/upload --config no_such.json
```

## 退出码约定

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部载荷完成且无一项被接受（加固有效） |
| 1 | 存在被接受项（发现绕过） |
| 2 | 前置校验失败（地址非回环、配置缺失/非法、参数错误），未发起请求 |
| 3 | 检测完成但存在未完成（连接故障）项 |

## 输出产物

- **结果 JSON**：顶层含 `target / config / payload_total / accepted_total /
  incomplete_total / results`；每项含 `payload_id、category、severity、verdict、
  basis（判定依据）、evidence（被接受项为可复核证据字符串，其余为 null）、
  status_code`，故障项额外含 `error`。被接受项按 high → medium → low 排序在前。
- **摘要 TXT**：按严重程度降序的漏洞行，以及形如
  `Severity counts of accepted findings: High: 1, Medium: 1, Low: 1`
  和 `Verdict counts: accepted: 3, rejected: 0, incomplete: 0` 的统计行。
- **日志文件**：记录每次投递（deliver）、判定（judge）与故障（ERROR/WARNING）；
  加 `-v` 后切到 DEBUG，额外记录请求头与原始响应，条目明显增多。

## 加固模式的校验点（对应三类载荷的拒绝依据）

1. 文件名不得包含 `.php/.asp/.jsp/.html` 等脚本扩展名标记（拦截 P01 双扩展名）；
2. 文件名不得包含 `; | \` $ < > 换行` 等特殊字符（拦截 P03 脚本文件名）；
3. 扩展名必须在图片白名单内；
4. 内容必须以受支持图片魔数开头（JPEG/PNG/GIF）；
5. 声明 MIME 必须与内容魔数匹配（拦截 P02 MIME 伪造）；
6. 内容中不得出现 `<?php / <script / <% / jsp:` 等脚本片段（拦截 P01 脚本内容）。

## 实际验证结果（2026-11-14，Python 3.14.7 / Windows 11）

`python -I run_demo.py` 实测：**56 项断言全部 PASS**，关键结论：

- 前置校验：省略参数、外部地址 `203.0.113.5`、配置缺失、`file_field` 非法
  均以非零码退出且无结果文件（未发起请求）；
- 放行模式：P01/P02/P03 全部 `accepted`，证据字符串非空，排序为
  P01(high) → P02(medium) → P03(low)，摘要统计 `High: 1, Medium: 1, Low: 1`；
- 加固模式：同一配置重跑，三类全部 `rejected`（HTTP 403，依据含具体加固规则），
  `accepted_total=0`，摘要统计 `High: 0, Medium: 0, Low: 0`；
- 故障注入（`--drop-on P02`）：P02 = `incomplete`，
  `error="RemoteDisconnected: Remote end closed connection without response"`，
  P01/P03 结论保留，`accepted_total=2`；
- 日志：默认 12 条记录，`-v` 后 18 条且含 DEBUG 请求头/原始响应。

## 安全与边界说明

- 工具内置回环地址白名单（`127.0.0.0/8`、`localhost`、`::1` 等），
  对任何非回环目标在发起网络请求前即报错退出；
- 载荷为确定性构造的静态样本，不落地、不执行、不回连，脚本片段仅用于特征检测；
- 仅适用于授权范围内的本地网关加固验证，禁止用于未授权目标。
