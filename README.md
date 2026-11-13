# Python 代码库安全审计工具

仅使用 Python 标准库（3.10+），对本地目录中的 `.py` 文件按规则 JSON 做正则匹配审计，输出审计报告 JSON，供工程师复核与修复前后对比。

## 文件清单

| 文件 | 说明 |
|---|---|
| `audit_tool.py` | 审计工具 CLI 入口 |
| `rules.json` | 示例规则文件（4 条合法规则） |
| `rules_bad.json` | 含非法规则的演示文件（触发告警） |
| `make_samples.py` | 确定性样本生成脚本（before/after） |
| `test_audit.py` | 自动化测试（覆盖验收点 1-13） |

## 快速开始

```powershell
# 1. 生成确定性样本（samples/before 含缺陷，samples/after 已修复）
python make_samples.py

# 2. 审计修复前样本 -> 7 条缺陷
python audit_tool.py samples/before rules.json report_before.json

# 3. 审计修复后样本 -> 0 条缺陷（修复前后对比验证）
python audit_tool.py samples/after rules.json report_after.json

# 4. 排除目录或文件（逗号分隔）
python audit_tool.py samples/before rules.json report_excl.json --exclude third_party

# 5. 运行自动化测试
python test_audit.py
```

命令格式：`python audit_tool.py <审计目录> <规则文件.json> <输出报告.json> [--exclude a,b,c]`

## 规则文件格式

JSON 数组（或含 `rules` 数组的对象），每条规则四个必填字段：

```json
[
  {"id": "SEC001", "pattern": "\\beval\\(", "severity": "高",
   "fix": "避免使用 eval 执行动态字符串；改用 ast.literal_eval。"}
]
```

- `severity` 仅允许 `高` / `中` / `低`。
- 字段缺失、severity 非法、正则无效的规则**不生效**，并在报告 `warnings` 中记录原因，其余规则照常工作。

## 报告格式

```json
{
  "summary":  {"files_audited": 4, "findings_count": 7},
  "findings": [{"file": "app/main.py", "line": 4, "column": 12,
                "rule_id": "SEC001", "severity": "高", "fix": "..."}],
  "warnings": ["规则 BAD1 正则表达式无效(...)"]
}
```

- 仅扫描 `.py` 文件；同一行多次匹配各自独立成条，行号/列号从 1 开始。
- `findings` 按 文件名 → 行号 → 列号 升序排序；`file` 为相对审计目录路径。
- 无法读取的 `.py` 文件被跳过并记录告警，其余文件照常审计。
- 审计目录不存在或规则文件缺失：向 stderr 输出提示并以非 0 退出码结束；无缺陷时正常结束（退出码 0），`findings` 为空数组。

## 确定性样本说明（修复前触发 / 修复后不触发）

`python make_samples.py` 生成 `samples/before` 与 `samples/after`，同一台机器重复运行内容完全一致：

| 文件 | 修复前（触发规则） | 修复后 |
|---|---|---|
| `app/main.py` | `eval(user_input)`（SEC001）、`subprocess.call(cmd, shell=True)`（SEC003） | `ast.literal_eval`、参数列表无 shell |
| `app/config.py` | `password = "s3cret!"`（SEC002）、`# TODO`（SEC004） | 从环境变量读取 |
| `app/util.py` | 一行两次 `eval(x)`（SEC001，两条独立记录） | `ast.literal_eval` |
| `third_party/legacy.py` | `eval('1+1')`（演示 `--exclude third_party`） | 已修复 |
| `docs/notes.txt` | 含模式文本但非 `.py`，不进入报告 | 同左 |

## 实际验证结果（Python 3.14.7, Windows 11）

- `python test_audit.py`：12 个测试全部通过（覆盖验收点 1-13）。
- `samples/before`：4 个文件、7 条缺陷，行列号与样本预期一致（如 `app/util.py` 行 2 列 12/22 两条独立记录）。
- `samples/after`：0 条缺陷，退出码 0 —— 修复前后对比成立。
- `--exclude third_party`：缺陷从 7 条降为 6 条，报告不含该目录文件。
- `rules_bad.json`：3 条非法规则各产生一条告警且不生效，合法规则 SEC001 的 4 条匹配正常输出。
- 不存在的审计目录：stderr 输出 `错误: 审计目录不存在或不是目录: ...`，退出码 2。
- 同一命令重复运行，报告内容逐字节一致（确定性）。
