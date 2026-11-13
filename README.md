# logaudit — 本地审计日志注入与敏感值检测/修复工具

针对 Windows 桌面应用本地审计日志的安全工具：扫描日志中的**换行注入**（伪造记录）、
**控制字符注入**（含 ANSI 转义、C0/C1 控制字符、多字节 UTF-8 控制序列）与**敏感值泄露**
（JWT、AWS Access Key、GitHub Token），生成 JSON 检测报告，并可输出脱敏修复后的日志及
修复前后对比。仅使用 Python 标准库，无外部依赖，输入输出均为本地纯文本文件。

## 环境要求

- Windows 10/11，Python 3.10+（`python --version` 确认）
- 无需安装任何第三方包

## 快速开始（可复现演示）

```powershell
cd D:\demo_GSB\demo\projects\project42

# 1. 确定性生成样本（含三类问题的恶意样本 + 纯净样本）
python logaudit.py gen samples

# 2. 扫描：报告输出到 stdout（合法 JSON）
python logaudit.py scan samples\sample_malicious.log

# 3. 扫描：报告写入指定文件
python logaudit.py scan samples\sample_malicious.log --report samples\report.json

# 4. 修复：生成脱敏日志 + 修复前后对比报告
python logaudit.py fix samples\sample_malicious.log --output samples\sample_fixed.log --report samples\fix_report.json

# 5. 纯净样本：问题列表为空
python logaudit.py scan samples\sample_clean.log

# 6. 一键跑全部 13 条验收项（34 项断言）
python verify.py
```

## 子命令

| 命令 | 说明 |
|---|---|
| `scan <日志> [--report 路径]` | 扫描并输出 JSON 报告；未指定 `--report` 时写标准输出 |
| `fix <日志> --output <路径> [--report 路径]` | 生成脱敏修复后的日志，报告含修复前后对比 |
| `gen <目录>` | 确定性生成 `sample_malicious.log`（三类问题各至少一处）与 `sample_clean.log` |

退出码：`0` 成功（即使发现问题）；`2` 文件不存在/不可读/报告不可写（错误信息写 stderr，不产生部分报告）。

## 报告格式

```json
{
  "tool": "logaudit",
  "mode": "scan | fix",
  "file": "输入日志路径",
  "summary": {
    "total_lines": 10,
    "problem_lines": 7,
    "by_type": {"newline_injection": 2, "control_char_injection": 2, "sensitive_value_leak": 3},
    "warning_count": 0
  },
  "issues": [
    {"line": 3, "type": "newline_injection", "snippet": "lt=success\\r2026-11-14"}
  ],
  "warnings": [
    {"line": 2, "message": "line exceeds 10000 characters (10066 chars), skipped"}
  ],
  "remediation": {                      // 仅 fix 模式
    "before": {"problem_lines": 7, "by_type": {"...": "..."}},
    "after":  {"problem_lines": 0, "by_type": {"...": "..."}}
  }
}
```

- `issues[].line`：从 1 开始的行号；`type` 区分 `newline_injection` / `control_char_injection` / `sensitive_value_leak`。
- `snippet`：匹配片段的**转义表示**——控制字符显示为 `\r`、`\x1b`、`‑` 等字面形式，敏感值以 `eyJh***MASKED***` 掩码呈现，报告中不出现完整令牌，也不含原始换行或终端控制序列。
- 超过 10000 字符的行跳过检测并列入 `warnings`，其余行检测不受影响。

## 检测与修复规则

- **换行注入**：行内 `\r`、`\x0b`、`\x0c`、U+0085、U+2028、U+2029（伪造日志记录的典型手法）。
- **控制字符注入**：C0（`\t` 除外）、DEL、C1（U+0080–U+009F，UTF-8 中为 `0xC2 0x80–0x9F` 多字节序列，如 CSI `0xC2 0x9B`）、非法 UTF-8 字节（解码占位符 U+FFFD）。正常 UTF-8 文本（如中文）不会误判。
- **敏感值**：预置三种令牌格式——JWT（`eyJ…`）、AWS Access Key（`AKIA…`）、GitHub Token（`ghp_…`）。
- **修复**：注入字符转为字面转义文本（如 `\x1b`），令牌遮蔽为 `前4字符***MASKED***`；未受影响的行**逐字节原样保留**；修复结果会再次扫描复检，确认问题数为 0。
- 含少量非法编码字节的日志不会导致中断，非法字节被标记为问题后继续处理其余行。

## 实际验证结果

`python verify.py` 输出（34/34 通过），覆盖任务全部 13 条验收项：

- 样本扫描报告三类问题齐全、行号正确（换行注入 3/4 行，控制字符 5/6 行，敏感值 7/8/9 行）；
- 纯净样本 `issues` 为空、`problem_lines` 为 0；汇总与明细的数量、类型分布一致；
- 修复后普通行与原文件逐字节一致，问题行已脱敏；`remediation.after` 全部为 0；
- 不存在文件退出码 2、stderr 有提示、stdout 无报告；
- 含超长行（>10000 字符）/非法编码字节/C1 多字节序列/中文的边界样本：超长行入警告、其余行检测正常、中文无误报、程序不中断；
- 报告所有片段与警告文本均无原始控制字符。

## 文件清单

- `logaudit.py` — 工具本体（仅标准库）
- `verify.py` — 验收脚本（34 项断言，退出码 0 表示全部通过）
- `samples/` — 演示生成的样本与报告（可由 `gen` 子命令重新确定性生成）
