# pysentry — 模板批量展开服务离线静态检测工具

面向内部模板批量展开服务的本地静态扫描器，针对历史上出现过的三类缺陷：

| 规则标识 | 缺陷类型 | 检测方式 |
|---|---|---|
| `SEC001` | 外部输入拼接后直接传入路径打开调用（越权读取） | 单文件轻量污点流 + `open`/`*.open` 调用判定 |
| `SEC002` | 递归展开缺少终止条件（资源耗尽） | AST 函数自递归识别 + 终止结构扫描 |
| `SEC003` | 敏感配置值明文写入日志/标准输出（信息泄露） | 输出调用参数中的敏感标识符匹配 |

## 环境要求

- Windows 本机 Python **3.10+**（实际验证环境 Python 3.14.7）
- **仅标准库**（`ast`、`argparse`、`json`、`pathlib`、`unittest` 等），无需 pip 安装任何第三方包
- 不使用 Docker 或其他容器；全程离线，不访问网络

## 目录结构

```
project36/
├── pysentry/
│   ├── __init__.py        # 包标识与规则清单
│   ├── model.py           # Finding 数据模型、规则说明
│   ├── rules.py           # 三条规则的 AST 检测逻辑（核心）
│   ├── scanner.py         # 目录扫描、文件发现、稳定排序、跳过标注
│   ├── compare.py         # 修复前后对比（相对路径+规则标识匹配）
│   ├── samples.py         # 确定性样例生成（无随机、可重复）
│   └── cli.py             # 命令行入口（scan / compare）
├── tests/
│   └── test_acceptance.py # 21 项 unittest，逐条覆盖 12 条验收要求
├── run_scanner.py         # 根目录启动器（支持 python -I 隔离模式）
├── generate_samples.py    # 样例生成启动器
├── demo.ps1               # 一键演示（PowerShell 5.1 / 7 均可）
└── README.md
```

## 快速开始（可复现启动方式）

所有命令均在项目根目录 `project36\` 下执行。

### 1. 一键演示（推荐先跑）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1
# 自定义输出目录：
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -DemoDir D:\pysentry_demo
```

### 2. 分步操作

```powershell
# (1) 生成确定性样例（不指定目录时写入 %TEMP%\pysentry_samples）
python -I generate_samples.py "$env:TEMP\pysentry_demo"

# (2) 扫描一个目录，JSON 报告写入文件，控制台打印计数摘要
python -I run_scanner.py scan "$env:TEMP\pysentry_demo\before" `
    -o "$env:TEMP\pysentry_demo\before_report.json"

# (3) 修复前后对比
python -I run_scanner.py compare `
    "$env:TEMP\pysentry_demo\before" "$env:TEMP\pysentry_demo\after" `
    -o "$env:TEMP\pysentry_demo\diff.json"

# (4) 不指定 -o 时：JSON 走标准输出，摘要走标准错误，便于管道处理
python -I run_scanner.py scan "$env:TEMP\pysentry_demo\before" 2>err.txt > report.json

# (5) 运行验收测试（两种方式等价）
python -I tests\test_acceptance.py
python -I -m unittest discover -s tests -v
```

> 关于 `python -I`（隔离模式）：忽略 `PYTHON*` 环境变量、不注入当前工作目录与用户 site-packages，
> 可防止被扫描目录中植入的同名模块（如伪造 `json.py`）被导入。Python 3.11 起 `-I` 隐含 `-P`，
> 脚本目录不再自动进入 `sys.path`，因此两个根目录启动器会显式把**自身所在的受信任项目目录**加入路径。

## 命令与输出说明

### `scan <目录> [-o 报告.json]`

输出 JSON 字段：

```json
{
  "scan_root": "扫描根目录",
  "scanned_files": ["相对路径，正斜杠"],
  "scanned_count": 3,
  "skipped_files": [{"file": "broken_syntax.py", "reason": "SyntaxError: invalid syntax"}],
  "issue_count": 6,
  "issues": [
    {
      "rule_id": "SEC001",
      "file": "path_open_demo.py",
      "line": 8,
      "message": "外部输入经字符串拼接后直接用于路径打开调用，存在越权读取风险"
    }
  ],
  "notice": "仅在无扫描目标时出现"
}
```

每条问题固定包含四个字段：`rule_id`（规则标识）、`file`（相对路径）、`line`（行号）、`message`（一句话说明）。
排序规则：**文件相对路径字典序 → 行号升序 → 规则标识 → 说明**，保证同目录两次运行字节一致。

### `compare <修复前目录> <修复后目录> [-o diff.json]`

- 匹配键：`(相对路径, 规则标识)`；**行号变化不视为新问题**。
- `fixed_issues`：修复前存在、修复后消失（同键按多重集计数，支持同规则多条）。
- `remaining_issues`：修复后仍存在，记录修复后的新行号（`line_number_source: "after"`）。
- `introduced_issues`：修复后新引入的问题（附加信息，不计入两类主结果）。

## 样例布局（由 `samples.py` 确定性生成）

| 目录 | 内容 | 预期结果 |
|---|---|---|
| `before/` | `path_open_demo.py`（SEC001）、`recursion_demo.py`（SEC002）、`secret_leak.py`（4 条 SEC003，其中一行两个敏感变量） | 6 条问题 |
| `after/` | 同名三文件：SEC001 改白名单查表、SEC002 加终止分支；SEC003 保留且因新增注释**行号整体 +1** | 4 条 SEC003 |
| `syntax_err/` | 一个语法错误文件 + 一个正常文件 | 坏文件标注跳过，好文件问题照常输出 |
| `empty/` | 空目录 | 空问题列表 + “未发现可扫描目标”提示 |

## 检测规则实现要点

### SEC001 越权读取

- **污染源（source）**：`input()`、`sys.argv[...]`、`os.environ[...]`/`os.getenv()`、HTTP 请求对象取值（`request.args/form/values/headers/cookies/...`，含 `.get(...)`）。
- **传播**：字符串拼接（`+`、f-string）、`os.path.join`/`Path(...)`、`str/strip/replace/encode` 等内置转换沿参数传播；赋值经不动点迭代收敛。
- **汇聚点（sink）**：内置 `open(...)` 与任意 `*.open(...)` 的第一个参数。
- **防误报设计**：字典下标只继承“容器”污染、不继承“键”污染，因此 `SAFE_TEMPLATES.get(user_input)` 白名单查表不会被判定为污染路径。

### SEC002 递归资源耗尽

- 识别函数体内对自身函数名的调用（含 `self.expand(...)` 形式），嵌套函数定义不参与外层判定。
- 终止结构：`if` 分支中的 `return`/`raise`/`break`，或循环中的 `break`。
- 函数末尾无条件的 `return` **不**算终止条件——无终止递归通常也以 `return` 收尾，计入会漏报。

### SEC003 敏感信息泄露

- 敏感命名（大小写不敏感子串）：`token`、`secret`、`password`、`passwd`、`apikey`/`api_key`。
- 输出 sink：`print(...)`、`logger.<debug|info|warning|error|exception|critical|log>(...)`、`sys.stdout/stderr.write(...)`。
- 参数中出现几个敏感标识符节点就报几条，**不做合并去重**；纯固定文案（无敏感标识符）不报。

## 12 条验收要求对照（tests/test_acceptance.py）

| # | 验收要求 | 对应测试 |
|---|---|---|
| 1 | 三文件目录恰好覆盖三文件 | `test_01_covers_all_three_files` |
| 2 | 路径拼接打开被检出且行号一致 | `test_02_path_traversal_detected_with_correct_line` |
| 3 | 无终止递归检出、有终止不检出 | `test_03_recursion_detection` |
| 4 | 令牌输出检出、固定文案不检出 | `test_04_secret_leak_detection` |
| 5 | 四字段齐全、相对路径 | `test_05_finding_has_four_fields_and_relative_path` |
| 6 | 两次运行字节一致、排序正确 | `test_06_deterministic_and_sorted` |
| 7 | 空/缺失目录空列表+提示 | `test_07_empty_and_missing_directory` |
| 8 | 语法错误隔离并标注跳过 | `test_08_syntax_error_isolated` |
| 9 | 对比输出已修复/仍存在两类 | `test_09_compare_fixed_classification` |
| 10 | 相对路径+规则匹配，行号漂移不影响归类 | `test_10_line_shift_still_remaining` |
| 11 | UTF-8 合法 JSON 文件、控制台计数一致 | `test_11_output_file_and_console_count` |
| 12 | 同规则重复出现分别报告 | `test_12_duplicate_same_rule_reported_separately` |

另有 9 项规则细节测试（污点传播、`argv` 拼接、白名单安全查表、良性递归、f-string 日志等）。

## 已知边界（与“仅单文件分析、不跨文件追踪符号”的约束一致）

- 流不敏感：同一变量在任意赋值中被污染即视为污染，不区分分支条件。
- 不做跨过程/跨文件的形参实参传播，不解析被导入模块的实现。
- 不内置净化函数模型；安全用法推荐采用白名单查表模式（样例 `after/path_open_demo.py` 已示范）。
- SEC003 基于命名约定识别敏感值，未按命名规范命名的敏感变量不在覆盖范围内。
- 仅解析 UTF-8 编码的 `.py` 文件；其他编码会作为跳过文件标注原因。
