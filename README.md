# YAML 部署配置安全扫描器

对本地 YAML 部署配置快照进行静态安全扫描，识别 **硬编码凭据、危险命令、调试端口暴露**
三类缺陷，输出结构化 JSON 报告，并支持修复前 / 修复后两份快照的比对，输出新增、
已消除、仍存在、不可比对四类清单，便于业务人员判断修复是否完成。

- 纯 Python 标准库实现，**无需安装任何第三方依赖**（含自研带行号的 YAML 子集解析器）
- 运行环境：Python 3.10+（实际验证环境 Python 3.14.7，Windows 11）
- 不使用 Docker / 容器；样本由代码确定性生成，不访问任何网络目标

## 目录结构

```
project12/
├── yaml_scanner.py          # 扫描器主程序（单文件，可直接拷贝使用）
├── make_samples.py          # 确定性测试样本生成器
├── tests/
│   └── run_acceptance.py    # 15 项验收测试（端到端子进程方式）
├── demo_workspace/          # 演示工作区（运行生成器后出现，已被 .gitignore 忽略）
│   ├── before/              # 修复前快照（5 个 YAML）
│   ├── after/               # 修复后快照（5 个 YAML）
│   ├── basic/               # 基础双文件目录
│   ├── report_before.json   # 扫描报告示例
│   ├── report_after.json
│   └── report_compare.json  # 比对报告示例
└── README.md
```

## 一、本地启动方式

```powershell
# 0. 确认 Python 版本（需 3.10+）
python --version

# 1. 生成确定性演示样本（重复运行结果逐字节一致）
python make_samples.py demo_workspace

# 2. 扫描单个目录（报告写 stdout）
python yaml_scanner.py scan demo_workspace/before

# 3. 扫描并把 UTF-8 报告写入文件
python yaml_scanner.py scan demo_workspace/before --output report_before.json

# 4. 修复前后比对
python yaml_scanner.py --compare demo_workspace/before demo_workspace/after `
    --output report_compare.json

# 5. 运行全部 15 项验收测试
python -I tests/run_acceptance.py
```

> `scan` 是可选关键字，`python yaml_scanner.py <目录>` 与其等价。
> Linux/macOS 把反引号换行换成 `\` 即可。
> 建议以 `python -I -X utf8 yaml_scanner.py ...` 方式运行：`-I` 隔离当前目录，
> 防止样本目录中被植入同名模块；`-X utf8` 保证 Windows 下报告按 UTF-8 输出。

## 二、检测规则

| 规则名 | 级别 | 判定逻辑 |
| --- | --- | --- |
| `hardcoded-credential` | high | 键名（不区分大小写）含 `password` / `secret` / `token`，且值为非 null、非空字符串、非 `${VAR}` 环境变量引用（支持 `${VAR:-default}`） |
| `dangerous-command` | high | 任意字符串值匹配 `rm -rf` 类模式（含 `rm -fr`、`rm --recursive --force` 变体）或 `curl/wget ... \| bash/sh/zsh` 管道模式；报告 `pattern` 字段给出命中模式名 `rm -rf` / `curl-pipe-bash` |
| `debug-port-exposure` | medium | 键名为 `debug_port` 或 `debug`（不区分大小写），值为 1–65535 的整数或纯数字字符串；`0`、`70000`、非数字、布尔值均不标记 |

每条漏洞记录 5 个定位字段：`line`（从 1 开始的物理行号）、`key_path`（点分键路径，
序列用 `[i]`）、`rule`、`severity`、`pattern`。注释内容在词法阶段即被剥离，
`# password: '123'` 这类注释不会产生漏洞。

## 三、报告结构

### 单目录扫描报告

```json
{
  "scan_root": "/abs/path/before",
  "message": "scan completed | no files found",
  "summary": {
    "total_files": 5,
    "files_with_vulnerabilities": 4,
    "total_vulnerabilities": 13,
    "by_severity": {"high": 9, "medium": 4, "low": 0}
  },
  "files": [
    {
      "path": "app/deploy.yml",
      "status": "ok",
      "vulnerabilities": [
        {"line": 5, "key_path": "service.password", "rule": "hardcoded-credential",
         "severity": "high", "pattern": null}
      ]
    },
    {"path": "broken.yml", "status": "error", "reason": "第 4 行: 缩进异常……",
     "vulnerabilities": []}
  ]
}
```

- 文件按路径字典序排列，文件内漏洞按 `(行号, 规则名, 键路径)` 升序，
  因此对同一输入连续扫描结果**逐字节相同**。
- 单个 YAML 语法错误 / 文件不可读只影响该文件（`status: error` 并附中文原因），
  其他文件正常扫描，进程整体退出码仍为 0。
- 路径不存在或目录内无 YAML 文件时返回空报告，`message` 为 `no files found`，退出码 0。

### 前后比对报告（--compare）

按 **规则名 + 点分键路径** 在同名文件内对齐，分四类输出：

```json
{
  "mode": "compare",
  "summary": {"introduced": 1, "resolved": 8, "remaining": 3, "uncomparable_files": 3},
  "introduced":  [ { "file": "web/gateway.yml", "line": 11, "...": "..." } ],
  "resolved":    [ ... ],
  "remaining":   [ ... ],
  "uncomparable": [
    {"path": "legacy/old-service.yml", "side": "before", "reason": "文件仅存在于修复前快照……"},
    {"path": "new-service/new-service.yml", "side": "after", "reason": "……仅存在于修复后……"},
    {"path": "broken.yml", "side": "both", "reason": "before: 第 4 行……; after: 第 4 行……"}
  ]
}
```

- `resolved`（已消除）、`remaining`（仍存在）、`introduced`（新增）只统计两侧都存在
  且都解析成功的文件。
- 仅一侧存在的文件、任一侧解析失败的文件进入 `uncomparable`，**不会**被计为漏洞新增
  或消除。

## 四、命令行参数与退出码

| 参数 | 说明 |
| --- | --- |
| `directory`（位置参数，可选） | 待扫描目录，递归扫描所有 `.yaml` / `.yml`（含子目录） |
| `--min-severity {high,medium,low}` | 最低级别过滤，大小写不敏感；如 `medium` 仅保留 high/medium |
| `--output PATH` | UTF-8 报告文件；不指定则写 stdout，两者内容完全一致 |
| `--compare BEFORE AFTER` | 比对两个快照目录 |
| `--help` / `-h` | 用法与全部参数说明 |

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常完成（含发现漏洞、单文件错误、无 YAML 文件等情况） |
| 2 | `--min-severity` 取值非法、比对目录不存在、报告文件无法写入等参数/IO 错误 |

## 五、业务操作样例（修复判定流程）

```powershell
# 1) 修复前留底扫描
python yaml_scanner.py scan .\snapshots\before --output before.json

# 2) 团队完成修复后扫描
python yaml_scanner.py scan .\snapshots\after --output after.json

# 3) 出比对结论
python yaml_scanner.py --compare .\snapshots\before .\snapshots\after --output diff.json

# 4) 只看 high 级别的修复情况
python yaml_scanner.py --compare .\snapshots\before .\snapshots\after --min-severity high
```

判定建议：

- `summary.resolved` 为修复消除项；`summary.remaining > 0` 说明修复不彻底，不能关单；
- `summary.introduced > 0` 说明修复引入了新问题，需退回整改；
- `summary.uncomparable_files > 0` 时先人工核对文件缺失 / 语法错误，再下结论。

## 六、实际验证结果

`python -I tests/run_acceptance.py` 在本机（Python 3.14.7 / Windows 11）实测
**15 项验收全部通过**，对应任务要求逐条覆盖：

| # | 验收点 | 覆盖方式 |
| --- | --- | --- |
| 1 | 递归扫描并逐文件列出漏洞 | basic 双文件目录 |
| 2 | 凭据规则及 null / `${VAR}` 豁免 | 专项 cred 目录 7 个取值 |
| 3 | 危险命令 + 模式名 | rm -rf/rm -fr/curl\|bash/wget\|sh 五组取值 |
| 4 | 调试端口范围与类型判定 | 0/1/65535/70000/字符串/布尔 8 个取值 |
| 5 | 行号与点分键路径（含序列嵌套） | nested 专项文件 |
| 6 | 两次扫描逐字节相同 | 哈希比对 stdout |
| 7 | YAML 语法错误隔离，退出码 0 | broken.yml + 正常文件混扫 |
| 8 | 不存在路径 / 空目录 no files found，退出码 0 | 两类输入分别验证 |
| 9 | 注释不产生漏洞 | gateway.yml 注释行断言 |
| 10 | min-severity 过滤与非法值退出码 2 | MEDIUM 大写 + critical 非法值 |
| 11 | --output 与 stdout 一致 | 文件内容逐字符比对 |
| 12 | 文件不可读标 error，其余继续 | icacls 拒绝读取（含句柄占用回退方案） |
| 13 | compare 三类清单内容正确 | 8 消除 / 3 仍存在 / 1 新增精确断言 |
| 14 | 单侧文件与解析失败进不可比对 | 3 个不可比对条目精确断言 |
| 15 | --help 完整且退出码 0 | 参数名存在性断言 |

## 七、YAML 解析器支持范围与边界

自研解析器面向部署配置场景，支持：块式 / 流式 map 与 sequence（含嵌套、跨行流式）、
单双引号字符串（含转义）、`|` 字面量与 `>` 折叠块标量、`---` 文档分隔、
锚点 / 别名（最小支持）、null/bool/int/float 类型推断、注释剥离。
不支持 YAML 标签（`!!`）、多文档合并、复杂锚点合并等高级特性；遇到无法解析的结构会
返回 `status: error` 并给出物理行号，而不是静默忽略。
