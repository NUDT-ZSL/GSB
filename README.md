# Markdown 隐藏文本 / 提示注入检测工具（mdscan）

面向本地产品知识库的安全审计工具：递归扫描指定目录下的 Markdown 文档，
依据可配置的 JSON 规则识别**对人不可见、但会被下游问答系统当作指令执行**的
隐藏文本（HTML 注释、零宽字符夹带、Unicode Tag 走私、全角/实体混淆、显式提示注入），
输出 JSON 报告。

* 仅使用 Python 标准库，要求 **Python 3.10+**（本机实测 3.14.7）
* 不依赖 Docker、不联网、不修改被扫描文件
* 单个文件读取失败不会中断整体扫描，且**无法确认内容的文件按不可信处理**

---

## 1. 快速开始（可复现）

```powershell
cd D:\demo_GSB\demo\projects\project18

# 1) 生成确定性样本（含真实零宽字符 / Unicode Tag 字符）
python examples\make_samples.py

# 2) 扫描并输出报告
python scan_md.py --dir examples\samples --config rules.json --output report.json
```

控制台输出（实际运行结果）：

```
scanned 8 markdown file(s) in examples\samples: 6 suspicious, 2 clean
report written to report.json
```

```powershell
# 3) 一键跑完 11 条验收场景（自建临时工作区，含真实不可读文件）
python examples\run_demo.py

# 4) 单元 + 验收测试（28 个用例）
python -m unittest discover -s tests -v
```

> `run_demo.py` 会在系统临时目录中复制样本、用 `icacls /deny`（Windows）或
> `chmod 000`（POSIX 非 root）制造一个真正不可读的文件，跑完整流程后自动清理；
> 若平台无法强制拒绝读取，该项标记为 **SKIPPED** 而非伪造结果。

### 命令行参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--dir` | 是 | 要递归扫描的目录，只有扩展名为 `.md`（大小写不敏感）的普通文件参与检测 |
| `--config` | 是 | JSON 规则配置文件 |
| `--output` | 是 | 报告输出路径（父目录不存在会自动创建；先写临时文件再原子替换） |
| `--version` | 否 | 打印版本 |

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 扫描完成、报告写入成功（**发现可疑文档不会改变退出码**，结果看报告） |
| `1` | 未预期的内部错误 |
| `2` | 参数错误 / 配置缺失 / JSON 非法 / 规则非法 |
| `3` | 目标目录不存在或不是目录（不生成报告） |
| `4` | 报告写入失败 |

设计说明：可疑文档属于**正常业务结果**，用退出码区分"检出"和"工具失效"，
便于 CI 中把 `exit != 0` 当作工具故障而不是发现项。

---

## 2. 规则配置格式

```json
{
  "rules": [
    {
      "name": "explicit-code-execution",
      "keywords": ["eval", "exec(", "os.system"],
      "patterns": ["(?i)\\b(?:eval|exec)\\s*\\(", "__import__\\s*\\("]
    }
  ]
}
```

* `name`：规则名，必填且全局唯一，命中时原样写入 `matched_rules`
* `keywords`：字符串数组，**不区分大小写**，命中任一即视为该规则命中
* `patterns`：字符串数组，Python `re` 语法的正则，**不自动忽略大小写**——
  需要时请显式写 `(?i)`，以保留区分大小写的精确匹配能力
* 单条规则 `keywords` 与 `patterns` 至少提供一项；二者都为空的规则视为配置错误
* 规则数组为空（`{"rules": []}`）也视为配置错误：空配置会把全部文档报成"干净"，
  这几乎一定是误配置，宁可直接失败（退出码 2）
* 也可以直接用 JSON 数组作为配置根（`[ {...}, {...} ]`）

仓库内 `rules.json` 提供 5 条可直接使用的规则：显式代码执行、提示注入（中英文）、
HTML 注释载荷、凭据外泄、Unicode Tag 走私。

### 匹配发生在哪些文本上

工具**不做 Markdown 渲染，也不丢弃 HTML 注释**——渲染恰恰是隐藏文本生效的原因。
每条规则依次作用于以下视图，**任一视图命中即标记可疑**：

| 视图 | 作用 |
| --- | --- |
| 原始文本 | `<!-- ... -->`、`[//]: # (...)`、代码块、front matter 全部可见 |
| 去不可见字符 | 删除全部 Unicode `Cf` 字符（零宽空格/连接符、BOM、双向控制符等）及 Hangul 填充符 |
| Unicode Tag 解码 | `U+E00xx` 逐字符映射回 ASCII（`ev<tag>al` → `eval`） |
| NFKC 归一 | 全角/兼容形式折叠（`ｅｖａｌ` → `eval`） |
| HTML 实体反转义 | `&#101;val` / `&commat;` 之类在纯文本管线中不会被渲染的写法 |

---

## 3. 报告格式

报告是**一个 JSON 数组**，每个元素对应一个 `.md` 文件，字段固定为
`file` / `suspicious` / `matched_rules` / `error`：

```json
[
  {
    "file": "B.md",
    "suspicious": true,
    "matched_rules": ["prompt-injection"],
    "error": null
  },
  {
    "file": "a.md",
    "suspicious": false,
    "matched_rules": [],
    "error": null
  }
]
```

* `file`：相对 `--dir` 的路径，统一使用 `/` 分隔
* `suspicious`：`true` 表示命中至少一条规则，**或该文件内容无法读取**
* `matched_rules`：命中的规则名，按配置文件中的顺序排列，每条规则只出现一次
* `error`：读取/解码失败时给出原因，正常为 `null`
* 排序：按 `file` 的**码点字典序**（不是大小写不敏感排序），因此 `B.md` 排在
  `a.md` 之前；排序在 `--dir` 全量递归后统一执行，可疑与非可疑混排，
  相同输入多次运行报告字节级一致
* 编码 UTF-8、缩进 2 空格、`ensure_ascii=False`（中文文件名可读）

### 失败即不放行（fail-closed）

`read_document()` 只把**读取/解码**失败（`PermissionError`、`OSError`、无法解码）
视为不可信：此时 `suspicious=true`、`matched_rules=[]`、`error` 写明原因。
理由：一份没能审计的文档，其风险未知，不能报"干净"。

有限范围、并显式写入 `error` 的情形：

* 目录遍历时无法列出的子目录：在 stderr 打印 warning，扫描继续（不静默吞掉）
* 符号链接：文件与目录均不跟随，避免逃逸出目标树读取设备/外部文件
* 超大文件：单个视图的正则扫描窗口上限 4 MiB（`MAX_REGEX_INPUT`），
  关键词匹配不受限；这是对 `re` 无超时机制的成本上界，不是内容截断报告

---

## 4. 验收要求与实际验证

| # | 要求 | 验证方式 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 只扫描 `.md` | `mixed/` 含 `a.md` + `b.txt`（b.txt 内含 `eval`） | 报告仅 `["a.md"]`，`b.txt` 未收录；`c.markdown`、`d.md.txt` 亦不收录，`.MD` 参与扫描 |
| 2 | JSON 配置读取规则（名称/关键词/正则） | `tests.TestRules` + `rules.json` | `eval` 关键词命中 `explicit-code-execution` |
| 3 | 关键词不区分大小写 | `case-insensitive.md` 含 `EVAL`，关键词为 `eval` | 可疑，`matched_rules=["explicit-code-execution"]` |
| 4 | 正则命中并记录规则名 | `test_regex_rule_records_rule_name` | 命中 `injection`，规则名出现在报告 |
| 5 | 覆盖 HTML 注释与零宽字符 | `hidden-comment.md`、`zero-width.md`、`unicode-tags.md` | 三者均 `suspicious=true`；零宽样本原始字节中**不含** `eval` 子串 |
| 6 | 报告字段 `file/suspicious/matched_rules/error` | `test_schema_fields_and_ordering` | 字段顺序与类型正确；正常文件 `error=null`、命中文件 `matched_rules` 非空 |
| 7 | 按路径字典序稳定排序 | `B.md` + `a.md` + `nested/C.md` | 顺序 `B.md` / `a.md` / `nested/C.md`；两次运行报告字节相同 |
| 8 | 配置缺失 / JSON 非法 → 非零退出 | `run_demo.py` B 段 | 两种情形均 `exit 2`，stderr 给出原因，不生成报告 |
| 9 | 目录不存在 → 非零退出且无报告 | `run_demo.py` C 段 | `exit 3`，`report.json` 未创建 |
| 10 | 单文件读取失败不影响其余文件 | `run_demo.py` A 段（ACL deny）+ `TestUnreadableFile` | `unreadable.md` 的 `error="permission denied: ..."`，其余 8 个文件正常完成 |
| 11 | `--dir/--config/--output` + 摘要计数一致 | `run_demo.py` A 段 | 控制台 `9 markdown file(s) ... 7 suspicious`，与报告数组逐项对账一致 |

完整测试：**29 个用例全部通过**（`Ran 29 tests ... OK`），
`run_demo.py` **11/11 checks passed**。

### 已实际观测到的限制

* **HTML 注释不会被剥离，但也不会单独告警**：规则命中只看内容（关键词/正则）。
  若注释里只写了无关备注，不会误报；若写了注入内容，则与正文同样命中——
  这正是要求 5 想要的语义。
* **零宽二进制载荷**：工具会尝试把连续 ≥8 个零宽字符按二进制解出 ASCII
  （支持 2/4 字符字母表两种常见变体），但为避免把普通 emoji ZWJ 序列误判，
  只在解码结果"看起来像文本"（全可打印且含 ≥2 个字母）时采用。
  未内置 Base64/自定义映射等更复杂的隐写解码，属于已知边界。
* **不可读文件按可疑计**：这是刻意的 fail-closed 选择，会抬高可疑数；
  报告中可用 `error != null` 把这些条目与"规则命中"区分开。
* **GB18030 兜底**：无 BOM 且非 UTF-8 的文档会再尝试 GB18030（GBK/GB2312 超集），
  因此非 UTF-8 的中文样本不会被误报为读取失败；UTF-16/32 仅在带 BOM 时识别。
* 正则来自配置文件，工具不提供 `re` 超时；已用 4 MiB 扫描窗口与 4096 字符
  模板长度上限约束成本，但恶意构造的灾难性回溯正则仍需配置侧把关。

---

## 5. 目录结构

```
scan_md.py                  # 启动脚本（显式设置 sys.path，可在任意 cwd 运行）
mdscan/
  __init__.py
  cli.py                    # 参数解析、退出码、原子写报告、摘要输出
  config.py                 # 规则加载与校验（ConfigError）
  documents.py              # 文件发现、解码、去混淆视图
  detector.py               # 规则匹配、单文件扫描、汇总
rules.json                  # 5 条内置规则
examples/
  make_samples.py           # 生成 examples/samples 下的确定性样本
  samples/                  # 提交到仓库的固定样本（不含不可读文件）
  run_demo.py               # 端到端演示：11 条验收逐条打印证据
tests/test_acceptance.py    # 28 个用例，覆盖 11 条验收 + 单元测试
```

样本设计（`examples/samples/`）：

| 文件 | 内容 | 预期 |
| --- | --- | --- |
| `a.md` | 正常产品文档 | 干净，`error=null` |
| `B.md` | 正文直接写入 `Ignore previous instructions...` | 可疑 `prompt-injection` |
| `visible-injection.md` | 中文可见提示注入 | 可疑 `prompt-injection` |
| `hidden-comment.md` | HTML 注释内藏注入指令与 `api_key` | 可疑（规则命中 3 条） |
| `zero-width.md` | `ev\u200bal` 零宽拆分 + 注释 | 可疑 `explicit-code-execution` |
| `unicode-tags.md` | Unicode Tag 字符走私整句英文指令 | 可疑 `unicode-smuggling` + `prompt-injection` |
| `case-insensitive.md` | 正文大写 `EVAL` | 可疑 `explicit-code-execution` |
| `nested/deep.md` | 嵌套目录正常文档 | 干净，验证递归 |
| `notes.txt` | 非 Markdown | 不参与检测 |

> 演示用的 `unreadable.md` **不提交到仓库**，由 `run_demo.py` 在临时工作区
> 现场创建并加锁，结束后清理，避免仓库中出现无法读取的文件。
