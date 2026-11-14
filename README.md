# token_guard：本地令牌检测与加固验证工具

对内部工具使用的本地签发令牌进行**风险检测**与**加固校验**的命令行程序，覆盖三类疑似绕过
手法：载荷篡改、算法标识替换（如 `alg=none`）、签名剥离。仅依赖 Python 标准库
（`hmac` / `hashlib` / `base64` / `json` / `argparse`），无需安装第三方包、无需网络。

## 1. 环境要求

- Python 3.10+（开发与实测环境：Windows 11 + Python 3.14.7）
- 无额外依赖；测试令牌由 `token_guard/samples.py` 确定性构造（固定时间戳 `2026-11-14T00:00:00Z`，无随机数）

## 2. 目录结构

```
project49/
├── token_guard/
│   ├── __init__.py      # 公共 API 导出
│   ├── core.py          # 常量、异常类型、Base64URL 工具、长度上限(1000)
│   ├── detector.py      # 令牌类型识别（JWT / 自定义 payload.signature）
│   ├── verifier.py      # 加固后的校验函数（修复实现）
│   ├── analyzer.py      # 风险分析器，输出结构化 JSON 报告
│   ├── samples.py       # 确定性测试样本构造
│   └── cli.py           # 命令行入口（analyze / verify / demo）
├── tests/
│   └── test_token_guard.py   # 36 个 unittest 用例，逐条对应 9 项验收点
├── run_demo.py          # 端到端演示入口
└── README.md
```

## 3. 本地启动方式（可复现）

所有命令在项目根目录 `project49/` 下执行。

### 3.1 一键演示（推荐先跑这个）

```powershell
python run_demo.py
# 等价于：
python -m token_guard.cli demo
```

演示会对 14 个确定性样本逐个输出完整 JSON 报告。退出码：`0` 全部通过；
`1` 存在被拒令牌（演示的预期结果，因为含篡改样本）；`2` 输入级错误。

### 3.2 运行验收测试

```powershell
python -m unittest discover -s tests -v
# 实测结果：Ran 36 tests ... OK
```

### 3.3 单令牌操作样例

```powershell
# 完整检测报告（风险列表 + 加固校验结论）
python -m token_guard.cli analyze --token "<令牌>" --key "demo-shared-secret-2026"

# 仅运行加固后的校验函数
python -m token_guard.cli verify  --token "<令牌>" --key "demo-shared-secret-2026"

# 从文件读取令牌（以路径传参，不执行文件内容）；也可管道输入
python -m token_guard.cli analyze --token-file sample_jwt.txt --key "demo-shared-secret-2026"
Get-Content sample_custom.txt | python -m token_guard.cli analyze --key "demo-shared-secret-2026"

# 报告同时落盘
python -m token_guard.cli analyze --token "<令牌>" --key "..." --file report.json
```

退出码约定：`0` 校验通过；`1` 令牌可识别但校验不通过/有风险；`2` 输入级错误（超长、空串、格式不支持）。

### 3.4 Python API 调用

```python
from token_guard import analyze_token, hardened_verify

report = analyze_token(token, "demo-shared-secret-2026")   # 完整 JSON 可序列化报告
result = hardened_verify(token, "demo-shared-secret-2026")  # {"passed": bool, "reason": ...}
```

## 4. 支持的令牌格式

| 格式 | 结构 | 签名方式 |
|---|---|---|
| JWT | `base64url(header).base64url(payload).base64url(HMAC-SHA256)`，3 段 | 对 `header.payload` 原文做 HMAC-SHA256 |
| 自定义 | `base64url(payload).hex(HMAC-SHA256)`，2 段 | 对载荷段原文做 HMAC-SHA256，签名为 64 位十六进制 |

类型识别规则：3 段 → JWT；2 段且首段能解出含字符串 `alg` 字段的 JSON 头 → 按“被剥离签名的
JWT”处理（以便报告风险）；其余 2 段 → 自定义格式；空串、含空白、段数不符 →
返回 `unsupported_token` 错误。

## 5. 风险项与严重等级

报告中的风险按严重程度**从高到低**稳定排序（同级保持检测顺序）：

| 严重度 | 风险 code | 含义 |
|---|---|---|
| critical | `alg_none` | JWT 算法标识被改为 `none`，企图跳过签名验证 |
| critical | `signature_stripped` | 签名段缺失/为空（JWT 与自定义格式均检测） |
| high | `alg_missing` / `alg_unsupported` | 算法字段缺失、类型非法或不在白名单 |
| high | `payload_tampered` | JWT 载荷改动后沿用旧签名，HMAC 重算失配 |
| high | `signature_invalid` | 自定义令牌载荷或签名被篡改，HMAC 失配 |
| high | `malformed` | Base64/JSON/十六进制编码损坏、`exp` 类型非法 |
| medium | `token_expired` | `exp` 早于当前时间 |

JSON 报告字段：`token_type`、`token_length`、`algorithm`、`risk_count`、`risks`
（每项含 `code/severity/title/detail/detected_at`）、`verification`、`conclusion`，
解析成功时附 `decoded_header` / `decoded_payload`。

## 6. 加固校验函数的修复要点（verifier.py）

针对怀疑的三个绕过点，修复实现与脆弱写法的区别：

1. **算法白名单而非盲信头部**：JWT 只接受 `alg == "HS256"`；`none`、缺失、其它算法
   一律返回 `alg_tampered`。不会根据令牌自报的算法动态选择校验路径。
2. **签名必须存在**：2 段 JWT、空签名段直接返回 `signature_stripped`，不进入后续流程。
3. **用给定密钥独立重算 + 常量时间比较**：使用 `hmac.compare_digest` 对比重算的
   HMAC-SHA256 与令牌签名，载荷任何字节改动都会失配（`payload_tampered` /
   `signature_invalid`）；错误密钥同样无法通过。
4. **先验签后看内容**：签名通过后才解析载荷、检查 `exp`，过期返回 `token_expired`；
   `exp` 只接受数值型时间戳（布尔值被显式排除）。
5. **输入上限**：超过 1000 字符直接抛 `TokenTooLongError`（CLI 输出错误 JSON、退出码 2），
   不做任何解析，杜绝超长输入造成的资源/解析异常。
6. **自定义格式同等级保护**：签名必须是 32 字节 HMAC-SHA256 的十六进制编码，
   长度/字符集不符按 `malformed` 拒绝，再做常量时间比较与过期检查。

## 7. 验收点对照与实测结果

实测命令：`python -m unittest discover -s tests -v` → **36 tests OK**；
`python run_demo.py` 的完整输出见 `demo_output.txt`。

| # | 验收点 | 对应测试 | 实测结果 |
|---|---|---|---|
| 1 | 识别 JWT/自定义；空串与结构不符报不支持 | `Test1TokenTypeDetection`（6 例） | 通过 |
| 2 | `alg=none` 报告算法篡改（critical） | `Test2AlgorithmTampering` | 通过 |
| 3 | 无签名段报告签名剥离（含空第三段、自定义格式） | `Test3SignatureStripping`（3 例） | 通过 |
| 4 | 改载荷留旧签名报告载荷篡改 | `Test4PayloadTampering`（2 例） | 通过 |
| 5 | 合法 JWT 通过；三类篡改 + 错误密钥全部拒绝 | `Test5HardenedJwtVerification`（5 例） | 通过 |
| 6 | 自定义格式 HMAC-SHA256 校验，改载荷/改签名失败 | `Test6HardenedCustomVerification`（5 例） | 通过 |
| 7 | JSON 字段齐全、风险按严重度降序（含复合攻击样本） | `Test7JsonReportAndOrdering`（5 例） | 通过 |
| 8 | 过期 JWT 拒绝且报告含过期项（JWT 与自定义均覆盖） | `Test8Expiration`（2 例） | 通过 |
| 9 | >1000 字符返回明确超长错误（含 1000/1001 边界、CLI 退出码） | `Test9LengthLimit`（4 例） | 通过 |

补充实测（真实系统时钟，非固定样本）：手工签发 `exp=当前时间-1s` 的 JWT，
`analyze_token` 结论为 `fail` 且风险含 `token_expired`，`hardened_verify` 返回
`reason=token_expired`；无 `exp` 的合法令牌正常通过。

## 8. 执行约束说明

- 不使用 Docker 或任何容器；纯本地进程运行。
- 样本全部由代码确定性生成，不访问网络、不接触未授权目标。
- 仅使用 Python 标准库；密钥通过命令行参数显式传入，不落盘、不写日志。
