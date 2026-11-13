# PDF 附件入库前安全检查（pdf-preflight）

面向内网文档网关的本地命令行工具：递归扫描收件目录中的 PDF 附件，识别**脚本、自动动作、嵌入可执行内容**三类风险特征，输出 JSON 报告与摘要，供运营人员决定隔离或放行。

- 纯 Python 标准库实现，无第三方依赖，Windows / Linux / macOS 均可运行
- 只读取字节，**不执行、不提取**任何嵌入内容
- 样本由 `demo_samples.py` 确定性构造，无需外部附件
- 不使用 Docker 或任何容器环境

## 1. 环境要求与启动

需要 Python 3.10+（已在 Python 3.14.7 / Windows 11 上验证）。

```powershell
# 以下命令均在项目根目录执行
py --version                 # 确认 >= 3.10
py demo.py                   # 一键演示：构造样本 -> 扫描 -> 打印报告摘要
```

`demo.py` 会生成 `demo_workspace/incoming/` 下的确定性样本，并写出
`demo_workspace/report.json`。

单独使用扫描器（适用于网关实际接入）：

```powershell
py pdf_preflight.py <输入目录> -o <报告路径>

# 示例
py pdf_preflight.py D:\gateway\incoming -o D:\gateway\reports\report.json
```

退出码：`0` 成功；`1` 报告写入失败；`2` 输入目录不存在或不是目录。

## 2. 实际运行结果（已验证）

`py demo.py` 的实际输出：

```
扫描完成: 共 12 个 PDF (high=4, medium=3, low=3, unknown=2)
  [high] all_three.pdf (javascript,auto_action,embedded_executable)
  [high] modern_xref_stream.pdf (javascript,auto_action)
  [high] packed_object_stream.pdf (javascript,embedded_executable)
  [high] script_and_auto.pdf (javascript,auto_action)
  [unknown] broken.pdf (truncated)
  [unknown] encrypted.pdf (encrypted)
报告已写入: demo_workspace\report.json
```

报告正文摘录（`demo_workspace/report.json`，完整 12 条见文件）：

```json
{
  "scanner": { "name": "pdf-preflight", "version": "1.0.0" },
  "input_dir": "...\\demo_workspace\\incoming",
  "files": [
    {
      "path": "all_three.pdf",
      "level": "high",
      "features": ["javascript", "auto_action", "embedded_executable"],
      "status": "parsed",
      "action": "quarantine",
      "advice": "建议隔离，待人工确认后再处理",
      "size": 1108
    },
    {
      "path": "auto_action.pdf",
      "level": "medium",
      "features": ["auto_action"],
      "status": "parsed",
      "action": "review",
      "advice": "建议人工复核后放行或隔离",
      "size": 698
    },
    {
      "path": "a.PDF",
      "level": "low",
      "features": [],
      "status": "parsed",
      "action": "allow",
      "advice": "未发现风险特征，可放行",
      "size": 600
    },
    {
      "path": "broken.pdf",
      "level": "unknown",
      "features": [],
      "status": "unparsed",
      "reason": "truncated",
      "detail": "文件缺少 %%EOF 结束标记",
      "action": "manual_review",
      "advice": "无法解析，请人工检查后再决定",
      "size": 144
    }
  ],
  "summary": { "total": 12, "low": 3, "medium": 3, "high": 4, "unknown": 2 }
}
```

排序规则：`high → medium → low → unknown`，同级按路径升序；`summary.total`
恒等于各类之和，也恒等于 `files` 条目数。

## 3. 业务操作样例（可直接演示）

### 样例 A：混合收件目录（`py demo.py`）

`demo_workspace/incoming` 内构造的文件与预期：

| 文件 | 构造内容 | 特征 | 等级 | 建议动作 |
| --- | --- | --- | --- | --- |
| `a.PDF` | 正常 PDF | 无 | low | allow（放行） |
| `b.txt` | 非 PDF | 被忽略，不入报告 | — | — |
| `sub/c.pdf` | 子目录正常 PDF | 无（路径记为 `sub/c.pdf`） | low | allow |
| `sub/notes.txt` | 非 PDF | 被忽略 | — | — |
| `script_only.pdf` | 两处 `/JavaScript` 标记 | javascript（去重一次） | medium | review |
| `auto_action.pdf` | `/OpenAction` + `/S /GoTo` | auto_action | medium | review |
| `embedded_exe.pdf` | 附件名 `A.EXE` | embedded_executable | medium | review |
| `embedded_txt.pdf` | 附件名 `readme.txt` | 无 | low | allow |
| `script_and_auto.pdf` | 脚本 + 自动动作 | javascript, auto_action | high | quarantine |
| `all_three.pdf` | 脚本 + 自动动作 + `payload.DLL` | 三种特征 | high | quarantine |
| `modern_xref_stream.pdf` | PDF 1.5 `/XRef` 流交叉引用 | javascript, auto_action | high | quarantine |
| `packed_object_stream.pdf` | 特征藏在 Flate 压缩 `/ObjStm` 中 | javascript, embedded_executable | high | quarantine |
| `broken.pdf` | 截断，无 xref/trailer/EOF | 不臆测，空特征 | unknown | manual_review |
| `encrypted.pdf` | 带 `/Encrypt`（内部含脚本也不解析） | 不臆测，空特征 | unknown | manual_review |

### 样例 B：单独验证某项验收（临时目录）

```powershell
# 只验证「一个文件含两种特征 -> high」
py demo_samples.py D:\tmp\caseB
py pdf_preflight.py D:\tmp\caseB\incoming -o D:\tmp\caseB\r.json
```

### 样例 C：把扫描器接入网关脚本

```powershell
py pdf_preflight.py .\incoming -o .\report.json
if ($LASTEXITCODE -ne 0) { throw "扫描失败" }
# 运营侧按 level/action 分流：high -> 隔离区，unknown -> 人工复核
```

## 4. 特征判定规则

| 特征值 | 判定依据 | 说明 |
| --- | --- | --- |
| `javascript` | `/JavaScript` 名称树键、`/JS` 值键或 `/S /JavaScript` 动作 | 同一文件多处出现只记录一次 |
| `auto_action` | 文档级 `/OpenAction` 或附加动作字典 `/AA` | 与脚本特征独立记录 |
| `embedded_executable` | 文件规范字典（`/Type /Filespec` 或含 `/EF`）中的 `/F`、`/UF` 等名字以 `.exe`/`.dll` 结尾，不区分大小写 | 支持字面字符串、十六进制字符串、UTF-16BE 及名称转义 |

风险等级只由**特征种类数**决定：0 种 `low`，1 种 `medium`，≥2 种 `high`。
`unknown` 仅用于无法解析的文件，且 `features` 恒为空数组（“不知道”不等于“无特征”）。

## 5. 无法解析文件的处理（`unknown`）

`inspect_structure()` 在读取任何风险特征之前先做结构校验；失败即记为
`unknown`，并给出 `reason`：

| reason | 含义 |
| --- | --- |
| `empty` | 文件为空 |
| `not_pdf` | 前 1024 字节内无 `%PDF-` 文件头 |
| `truncated` | 文件尾部无 `%%EOF` |
| `broken_xref` | `startxref` 越界，或交叉引用表/`/XRef` 流结构损坏 |
| `no_trailer` | 经典交叉引用表后缺少 `trailer` |
| `no_root` | 所有 trailer 中均无 `/Root` 目录引用 |
| `encrypted` | trailer 中声明 `/Encrypt`；不解析内容 |
| `unreadable` | 文件读取失败 |
| `too_large` | 超过 200 MB 上限，未解析 |

单个文件失败不影响其他文件：每个文件独立处理，逐条产出报告项。

## 6. 测试

```powershell
py -m unittest -v
```

**实际结果：18 个测试全部通过（Ran 18 tests ... OK）。** 覆盖八项验收：

1. 递归扫描与大小写不敏感扩展名（`a.PDF`、`b.txt`、`sub/c.pdf` → 仅 2 条）
2. 两处脚本标记 → 特征仅 `javascript` 一次
3. 脚本 + 自动动作并存 → 两者都在
4. `A.EXE`/`lib.Dll` 命中，`readme.txt` 不命中；十六进制 UTF-16 文件名亦命中
5. 0/1/3 种特征 → low/medium/high；并校验等级与特征数量全局一致
6. `x.pdf`、`y.pdf` 在 medium 之前且 x 在 y 前
7. 损坏、加密、伪 PDF 均为 unknown 且特征为空，正常文件结果不受影响
8. 三类各一 + 损坏样本 → `total/low/medium/high/unknown = 4/1/1/1/1`；并校验 `total == 各类之和 == len(files)`

此外还覆盖：PDF 1.5 交叉引用流、压缩对象流内隐藏特征、经典 xref 多子段表、CLI
端到端与退出码。

确定性验证：两次独立构造的 14 个样本文件 SHA-256 完全一致；从不同工作目录调用 CLI 结果一致。

## 7. 实现与限制说明

- 特征扫描采用与 `pdfid` 同类的结构化字节扫描；仅在结构校验通过且未加密的文件上执行，避免把损坏/加密内容误判为“无特征”。
- 对 PDF 1.5+ `/ObjStm` 压缩对象流做 Flate 解码后再扫描（上限 8 MB，防解压炸弹），因此藏进对象流的特征同样能被发现。
- 嵌入文件名只从真实文件规范字典或 `/EmbeddedFiles` 名称树读取，避免把字体资源 `/F` 等无关键误报为可执行附件。
- 这是**静态预检**：不渲染、不执行脚本、不展开嵌入载荷。加密 PDF 一律 `unknown` 交人工，是刻意策略而非解析缺陷。
- 若网关需要更强的内容级判定（如解码后确认真实可执行格式），可在同一接口上扩展 `detect_features()`，报告 schema 无需改动。

## 8. 文件清单

| 文件 | 作用 |
| --- | --- |
| `pdf_preflight.py` | 扫描器主程序（CLI、结构校验、特征识别、报告生成） |
| `demo_samples.py` | 确定性 PDF 样本构造器（正常/脚本/自动动作/附件/加密/损坏等） |
| `demo.py` | 一键演示脚本 |
| `test_pdf_preflight.py` | 验收测试（18 项） |
| `demo_workspace/` | 演示输出（样本 + `report.json`），可随时删除重建 |
