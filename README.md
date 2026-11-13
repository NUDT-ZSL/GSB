# secscan — 离线 JSON 配置安全扫描与修复工具

面向离线 Windows 审计场景的本地命令行工具。只依赖 **Python 3.10+ 标准库**，
不联网、不引入第三方包、不使用容器。

功能：

* 扫描 JSON 配置中的**弱口令**（高危）、**明文敏感值**（中危）、
  **危险反序列化设置**（高危）；
* 输出可复核的确定性 JSON 报告（含规则类型、严重级别、配置路径、脱敏证据、
  被跳过路径）；
* `--fix` 对弱口令 / 明文敏感值做安全替换后**再次扫描验证**；
* 配置文件缺失或非法时以非零码失败，**不产生残缺报告**；
* 规则支持可选 JSON 规则文件，缺失时告警并回退内置规则。

## 1. 环境与启动

```powershell
# 确认解释器（3.10 或更高）
python --version

# 在本项目根目录直接运行
python secscan.py --help

# 或以模块方式运行（等价）
python -m secscan --help
```

无需 `pip install`，无需虚拟环境。Windows PowerShell / cmd / Git Bash 均可。

## 2. 命令行

```
secscan --config PATH --output PATH [--rules PATH] [--fix] [--fixed-config PATH]

  --config        待扫描 JSON 配置（必填）
  --output        报告输出 JSON（必填）
  --rules         可选规则文件；文件不存在 => stderr 警告 + 内置默认规则
  --fix           修复弱口令/明文敏感值，并自动复扫验证
  --fixed-config  修复结果写出路径；仅配合 --fix，默认 <config>.fixed.json
```

退出码：

| 码 | 含义 |
|----|------|
| 0 | 扫描/修复成功 |
| 2 | 参数缺失或用法错误（usage 打到 stderr） |
| 3 | 配置不存在、不是文件、非 UTF-8、JSON 非法、顶层非对象/数组 |
| 4 | 规则文件存在但非法（JSON 错、字段错、正则错、id 重复等） |
| 5 | 输出路径非法或写入失败 |
| 6 | 修复后复扫仍存在可修复项（防御性检查） |

失败语义：**先校验输入、再写文件**，报告与修复配置都用临时文件 +
`os.replace` 原子写入，失败时不会留下半截文件。

## 3. 业务操作样例

> 工具不会隐式创建目录：请先确保报告输出目录存在（如 `mkdir reports`），
> 否则会以退出码 5 明确报错。`demo.cmd` / `demo.sh` 会自动创建 `reports\demo`。

```powershell
# 1) 基线扫描（示例配置含弱口令 / 明文 secret+token / yaml.load / 未知嵌套 foo）
python secscan.py --config samples\vulnerable.json --output reports\report.json

# 2) 使用企业自定义规则
python secscan.py --config samples\vulnerable.json --output reports\report.json --rules samples\rules.json

# 3) 规则文件不存在（应告警并使用默认规则，仍成功）
python secscan.py --config samples\vulnerable.json --output reports\report.json --rules samples\no-such-rules.json

# 4) 修复并复扫验证（输出修复后配置 + 修复清单）
python secscan.py --config samples\vulnerable.json --output reports\report-fixed.json --fix --fixed-config reports\vulnerable.fixed.json

# 5) 对修复后的文件再次独立扫描（仅剩 loader 条目）
python secscan.py --config reports\vulnerable.fixed.json --output reports\report-rescan.json

# 6) 失败场景
python secscan.py --config samples\missing.json --output reports\x.json   # 退出码 3，无报告
python secscan.py                                                        # 退出码 2，打印 usage
```

一键演示：

* Windows：`demo.cmd`（cmd.exe）
* Git Bash / WSL / Linux：`bash demo.sh`（可用 `PY=python3 bash demo.sh`）

脚本会依次执行 10 个验收步骤，末尾输出 `ALL DEMO STEPS COMPLETED`。

## 4. 报告结构

```jsonc
{
  "tool": "secscan", "version": "1.0.0", "mode": "scan",   // 或 "fix"
  "config": "samples\\vulnerable.json",
  "rules": { "source": "builtin-default", "name": "...", "path": null, "count": 3 },
  "total": 4,                         // == vulnerabilities.length
  "summary": { "by_severity": {"高危":2,"中危":2,"低危":0},
               "by_type": {...}, "skipped": 17 },
  "vulnerabilities": [                // 按配置路径稳定排序
    {
      "rule_id": "SEC-WEAK-PASSWORD-001",
      "rule_type": "weak_password",
      "operator": "weak_password",
      "severity": "高危", "severity_code": "high",
      "path": "$.app.database.password",     // JSONPath
      "bare_path": "app.database.password",  // 点号路径，便于人工定位
      "key": "password",
      "description": "检测到密码类字段使用了常见弱口令……",
      "evidence": "1****6",                  // 脱敏，不回显原始凭据
      "fixable": true
    }
  ],
  "skipped": [                        // 未知字段/嵌套结构，供人工复核
    { "path": "$.foo", "bare_path": "foo", "kind": "nested",
      "reason": "未知/未匹配规则的嵌套结构…… " }
  ],
  "skipped_paths": ["$.foo", ...],
  // 仅 --fix 时出现：
  "fixes": [{ "path": "...", "rule_id": "...", "action": "replaced_with_vault_reference" }],
  "fixed_config": "reports/vulnerable.fixed.json",
  "pre_fix_total": 4, "post_fix_total": 1,
  "pre_fix_findings": [ ... ]
}
```

排序规则：按路径分段稳定排序（对象键按小写字典序、数组下标按数值），
同一路径再按 `rule_id`，因此同一输入两次运行字节级一致。

### 检测规则（内置默认）

| 规则 ID | 类型 | 级别 | 匹配字段 | 判定 |
|---|---|---|---|---|
| SEC-WEAK-PASSWORD-001 | `weak_password` | 高危 | `password/passwd/pwd/pass/…` | 值在常见弱口令字典内（`123456`、`password`、`admin` 等） |
| SEC-PLAINTEXT-SECRET-001 | `plaintext_secret` | 中危 | `secret/token/api_key/private_key/…` | 非空、非 `${ENV}`/`vault://`/`ENC()` 等保护形式 |
| SEC-DANGEROUS-DESERIALIZATION-001 | `dangerous_deserialization` | 高危 | `loader/deserializer/…`，另扫描任意字段中的裸可调用表达式 | `pickle.loads`/`dill.loads`/`marshal.loads`，或 `yaml.load` 未带 SafeLoader |

误报抑制：`yaml.SafeLoader`、`yaml.safe_load`、`yaml.load(..., Loader=yaml.SafeLoader)`、
`json.loads` 不报；`secret` 为 `${VAULT_SECRET}` / `vault://…` / `ENC(...)` 时不报明文；
非字典内口令（如强密码）不报。

### 自定义规则文件

```jsonc
{
  "schema_version": 1,
  "name": "custom-corp-rules",
  "rules": [
    {
      "id": "CUSTOM-WEAK-001",
      "type": "weak_password",
      "severity": "high",                 // high | medium | low
      "operator": "weak_password",        // weak_password | plaintext_secret | dangerous_loader
      "field_pattern": "^(password|svc_password)$",   // 或 "field_names": ["..."]
      "description": "……",
      "weak_passwords": ["Company@2024"]  // 追加到默认字典
    }
  ]
}
```

`match_value_anywhere: true` 会让该规则在任意字段名上尝试匹配"裸可调用表达式"
（用于反序列化检测，避免漏掉 `format: pickle.loads` 这类改名字段）。

## 5. 修复语义

| 命中类型 | 替换方式 |
|---|---|
| 弱口令 | 24 位随机强口令（`secrets`，保证大小写/数字/符号齐全） |
| 明文敏感值 | `${SECSCAN_SECRET_FROM_VAULT}` 密钥托管引用（可被扫描器识别为已保护） |
| 危险反序列化 | **不自动改写**，保留在报告中供人工决策（自动改 `loader` 可能改变运行时行为） |

修复基于深拷贝（迭代实现，避免深嵌套爆栈），只替换命中路径上的值；
键顺序、嵌套层级、数组顺序及所有无关值保持不变。修复后立即在同一进程内复扫，
若仍存在可修复项则退出码 6；同时把修复后配置写到 `--fixed-config`，便于再次独立扫描。

## 6. 实际验证结果

以下为本机实测（Windows 11，Python 3.14.7；另在 WSL Python 3.11.0rc1 重复通过）。

| # | 验收项 | 命令 | 实测结果 |
|---|--------|------|----------|
| 1 | 文件缺失：非零码、stderr 提示、无报告 | `--config samples\missing.json` | `EXIT=3`，stderr `配置文件不存在 (config file not found)`，报告文件不存在 |
| 1b | 格式非法 | 尾部逗号样本 | `EXIT=3`，stderr `配置文件格式非法（JSON 解析失败, invalid JSON）: line 1 column 21`，无报告 |
| 2 | 弱口令高危 + 路径 | `samples\vulnerable.json` | `$.app.database.password` → `severity=高危`，`rule_type=weak_password` |
| 3 | 明文敏感值中危 + 描述 | 同上 | `$.app.integrations.payment_gateway.secret`、`$.app.integrations.notification.token` → `中危`，描述含“明文存储” |
| 4 | 危险反序列化高危 | 同上 | `$.app.serialization.loader` → `高危`，`rule_type=dangerous_deserialization` |
| 5 | total 一致、顺序稳定 | 连续两次扫描 | 两次报告 `fc /b` 字节一致；`total=4`，条目数 4 |
| 6 | 修复后仅剩 loader | `--fix` + 独立复扫 | 修复后 `total=1`，唯一条目为 `$.app.serialization.loader` |
| 7 | 其余字段与层级不变、仍是合法 JSON | `--fix` | 逐叶子对比：除 3 个漏洞字段外全部相等；文件可被 `json.load` 解析 |
| 8 | 未知嵌套跳过且继续 | `samples\vulnerable.json` 的 `foo.bar` | `skipped_paths` 含 `$.foo`，其余 4 条漏洞完整 |
| 9 | 规则缺失告警 + 默认规则 | `--rules samples\no-such-rules.json` | stderr `[WARN] 规则文件不存在，使用内置默认规则继续扫描`，报告正常生成、`rules.source=builtin-default` |
| 10 | 缺参打印用法并非零退出 | 无参数 | `EXIT=2`，stderr 打印 usage 及 `--config, --output, ...` |

补充健壮性实测：

* 400 层嵌套配置：不崩溃，超深子树记入 `skipped` 并注明 `MAX_SCAN_DEPTH=256`；
  5000 层文本视解释器解析上限而定：能解析时按深度上限处理并正常出报告，
  不能解析时以退出码 3 干净失败并说明“嵌套层级过深”，两种情况都不打印 traceback、
  不产生残缺报告；
* Unicode 键（`配置`）、重复键、数组内标量、含点号键：路径均正确；
* 报告输出路径等于配置文件 / 修复输出等于原配置：拒绝执行（退出码 5）；
* 自定义规则文件非法（JSON 错、缺 `rules` 数组、正则错、id 重复、路径为目录）：退出码 4 且无报告。

### 自动化测试

```powershell
python -m unittest discover -s tests -v
```

实测：Windows Python 3.14.7 与 WSL Python 3.11.0rc1 均为
`Ran 32 tests ... OK`（覆盖上述 1–10 全部验收点及边界场景）。

## 7. 目录

```
secscan.py              入口（python secscan.py ...）
secscan/                实现包
  cli.py                参数解析、退出码、流程编排
  rules.py              内置规则 + 规则文件加载校验
  scanner.py            递归扫描、排序、修复、报告构建
  jsonio.py             JSON 读取与原子写入
  errors.py             错误类型/退出码
samples\                确定性样本：vulnerable.json / clean.json / rules.json
tests\test_secscan.py   32 个端到端测试
demo.cmd / demo.sh      一键验收演示
reports\                演示输出（可删除，运行时重建）
```
