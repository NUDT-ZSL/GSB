# 离线危险命令拦截规则审计与修复验证工具

## 1. 背景与问题模型

内部运维团队的本地危险命令拦截模块从 JSON 配置读取规则（`id` / `pattern` /
`action`），对输入命令做拦截判定。历史实现把**规则文本与命令文本直接拼接**
后做前缀匹配，等价于：

```python
(pattern + " " + command).startswith(command_head)   # command_head = 首个 ; & | 之前的部分
```

该实现存在两类确定性绕过，本工具对其进行离线检测并验证修复：

| 绕过类型 | 构造命令示例 | 历史实现行为 |
|---|---|---|
| 分隔符绕过 | `echo "start"; rm -rf /tmp/data` | 只比较分号前的良性命令头，危险尾部被忽略 |
| 引号绕过 | `curl "https://evil.example/x"` | 引号作为普通字面量字符参与比较，带引号的载荷无法命中 |

**修复方式**：不再拼接任何命令文本，将 `pattern` 编译为正则表达式，对
**完整命令文本**执行 `re.search`（见 `cmd_guard/matching.py`）。

## 2. 环境要求

- Python 3.10+（实测 Windows 11 + Python 3.14.7），仅标准库，**无第三方依赖、无网络访问**
- Windows 命令行可直接运行 `python`；所有命令均带 `-I`（隔离模式），
  避免当前目录被植入的 `json.py` 等模块劫持

## 3. 目录结构

```
project32/
├── auditor.py                  # 入口：python -I auditor.py ...
├── cmd_guard/
│   ├── matching.py             # 历史拼接匹配器（legacy）与修复后语义匹配器（fixed）
│   ├── engine.py               # 加载/校验/去重/编译/检测/判定/报告（核心引擎）
│   └── cli.py                  # 参数解析、退出码、文件落盘
├── samples/
│   ├── input/                  # 确定性样本：合法/缺字段/坏pattern/重复id/混合/损坏 配置 + 命令清单
│   └── output/                 # 运行产物（git 忽略，可随时重建）
├── tests/test_auditor.py       # 19 个测试，逐条对应 14 项验收
├── examples/demo_retry.py      # 瞬时不可读→重试恢复演示
├── run_demo.cmd / run_demo.sh  # 一键演示（Windows / POSIX）
└── README.md
```

## 4. 快速开始

### 4.1 一键演示

```powershell
# Windows（PowerShell 或 cmd）
run_demo.cmd

# Git Bash / Linux / macOS（自动选择 python 或 python3）
bash run_demo.sh
```

### 4.2 手动执行

```powershell
# 检测阶段：复现历史拼接匹配并输出绕过检测报告
python -I auditor.py `
  --config samples/input/rules_valid.json `
  --commands samples/input/commands.txt `
  --outdir samples/output/demo_audit

# 修复阶段：按规则语义对完整命令逐条判定
python -I auditor.py `
  --config samples/input/rules_valid.json `
  --commands samples/input/commands.txt `
  --outdir samples/output/demo_fixed `
  --stage remediated
```

### 4.3 参数说明

| 参数 | 说明 |
|---|---|
| `--config PATH` | 规则 JSON，可重复传入多个来源；单条失败不阻断其它来源 |
| `--commands PATH` | 命令清单：JSON（`{"commands":[...]}` 或裸数组）或每行一条的文本（支持 `#` 注释） |
| `--outdir DIR` | 输出目录，不存在时自动创建 |
| `--stage audit\|remediated` | `audit`=历史拼接检测绕过（默认）；`remediated`=修复后语义判定 |
| `--retry N` | 配置暂不可读时的重试次数，默认 1；设 0 可快速验证缺失输入路径 |

### 4.4 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 全部输入成功读取，两个结果文件已生成 |
| 1 | 存在持续缺失/损坏输入（报告 `missing_inputs` 非空），已成功部分照常落盘 |
| 2 | 参数缺失/非法，已向 stderr 打印用法 |

## 5. 输出文件

固定生成两个 JSON 文件（键排序、固定缩进、`\n` 换行，保证逐字节可复现）：

**`detection_report.json`**

```json
{
  "stage": "audit",
  "summary": { "commands_evaluated": 5, "rules_evaluated": 4, ... },
  "vulnerable_rules": ["R001", "R004"],   // 历史实现下可被绕过的规则 id（字典序）
  "safe_rules": ["R002", "R003"],         // 完整评估且未发现绕过的规则 id（字典序）
  "error_rules": [ { "id": "E001", "reason": "pattern 无法编译: ...", "source": "..." } ],
  "invalid_rules": [ { "id": "B001", "reason": "缺失字段: pattern", "source": "..." } ],
  "duplicate_ids": ["D001"],              // 重复 id 中被忽略的后续条目
  "missing_inputs": ["..."],              // 持续缺失/损坏的输入路径
  "retried_paths": ["..."]                // 经历过瞬时失败、重试后恢复的路径
}
```

**`command_judgements.json`** —— 修复后逐条判定，未命中时 `rule_id` 与
`action` 均为 `null`：

```json
{
  "stage": "remediated",
  "results": [
    { "command": "ls -l", "rule_id": null, "action": null },
    { "command": "reboot", "rule_id": "R002", "action": "block" }
  ]
}
```

## 6. 14 项验收对照与实测结果

测试命令：`python -I tests/test_auditor.py`（实测 **19/19 通过**，
耗时约 0.09 s，全程离线）。

| # | 验收点 | 覆盖测试 | 实测 |
|---|---|---|---|
| 1 | 正常输入退出码 0 且生成两个结果文件 | `test_01_*` | 通过，EXIT=0 |
| 2 | 缺字段规则列入非法规则并说明缺失字段 | `test_02_*` | B001→缺失 pattern，B002→缺失 action |
| 3 | 引号+分号命令的绕过规则进 vulnerable_rules | `test_03_*` | R001、R004 被识别 |
| 4 | 两数组均存在且 id 字典序升序 | `test_04_*` | 升序且两数组互不相交、完整划分 |
| 5 | 修复后原绕过规则不再出现在 vulnerable_rules | `test_05_*` | remediated 阶段 vulnerable=[] |
| 6 | 未命中时 rule_id/action 均为 null | `test_06_*` | `ls -l` 双 null |
| 7 | 坏 pattern 进 error_rules，其余照常评估 | `test_07_*` | E001 报错，E002/E003 正常评估 |
| 8 | 重复 id 保留首条、duplicate_ids 列后续 | `test_08_*` | 首条 action=block 生效，warn 被忽略 |
| 9 | 暂不可读提示重试，其它来源继续处理 | `test_09_*` + `examples/demo_retry.py` | 重试恢复；缺失路径不阻断合法来源 |
| 10 | 持续缺失/损坏进 missing_inputs，退出码非 0 | `test_10_*` | 缺失与损坏 JSON 均 EXIT=1 |
| 11 | 合法数据不被后续坏输入污染 | `test_11_*` | 混合来源前后合法规则 id 完全一致 |
| 12 | 多次运行修复阶段逐字节一致 | `test_12_*` | 两次 SHA256 完全相同（实测见下） |
| 13 | 未完成/出错规则不得计入 safe_rules | `test_13_*` | E001 与非法规则均不在 safe 中 |
| 14 | 无参数打印用法且退出码非 0 | `test_14_*` | 打印用法，EXIT=2 |

实测哈希（验收 12，两次独立运行）：

```
detection_report.json   SHA256 = 3E14B2B39D39E61DC57A6949087DB80D63898248340571DE54EA6CEC0F185F6B（两次一致）
command_judgements.json SHA256 = 545CE5BF398A07E1F745F3598CB5F539B9132FEF632332F26D50425F1747ADC0（两次一致）
```

## 7. 关键设计说明

1. **legacy/fixed 双匹配器并存**（`matching.py`）：检测阶段对每条规则同时
   跑两种匹配，凡存在「legacy 漏判而 fixed 命中」的命令即判定该规则可被
   绕过；判定完全确定性，不依赖网络或启发式猜测。
2. **输入隔离**：每个成功来源独立保存（`LoadedSource`），后续来源的 JSON
   损坏、结构非法或规则字段错误都无法修改已解析数据（验收 11）。
3. **规则处理流水线**：字段校验 → 首条 id 去重 → 正则编译。任一阶段失败
   的规则进入对应分类（`invalid_rules` / `error_rules` / `duplicate_ids`），
   绝不进入 `safe_rules`，报告显式区分「完整评估」与「未完成部分」（验收 13）。
4. **可复现性**：规则按 id 排序后评估、多规则命中取最小 id、JSON 以
   `sort_keys=True` + 固定缩进 + `\n` 写出，同输入任意次运行逐字节一致。
5. **安全运行姿态**：仅做静态文本分析，不执行任何输入命令；推荐一律使用
   `python -I`，防止样本目录中的同名模块劫持解释器。
