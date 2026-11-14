# 本地模板注入（XSS）检测与修复对比工具

对自建模板渲染服务做**本地、确定性**的安全验证：构造一批含模板字符串与数据字典的
样本，分别以「未修复（原样填充）」和「修复（HTML 实体转义）」两种模式渲染，
检测未修复输出中的可执行脚本片段，并对比修复效果，产出业务可读的 JSON 报告。

- 仅使用 Python 标准库，Python 3.10+（实际验证环境：Python 3.14.7，Windows 11）
- 不使用 Docker / 容器；不访问任何网络目标
- 单文件异常不影响整体流程；报告不含样本绝对路径

## 一、目录与文件

| 文件 | 作用 |
|---|---|
| `xss_audit.py` | 命令行入口与全部核心逻辑（渲染、检测、报告） |
| `build_samples.py` | 确定性构造样本目录（10 个样本，无随机内容） |
| `test_xss_audit.py` | 标准库 `unittest` 验收测试，覆盖全部 14 条验收点 |
| `samples/` | 运行构造脚本后生成的样本目录 |
| `reports/` | 报告输出目录（已在 `.gitignore` 忽略） |

## 二、本地启动方式（可复现）

```powershell
# 1. 构造确定性样本（默认输出到 samples，可传参指定目录）
python build_samples.py samples

# 2. 执行检测与修复对比，生成 JSON 报告
python xss_audit.py samples reports/report.json

# 3. 运行全部验收测试
python -m unittest -v test_xss_audit
```

> 处理来自他人、不可信的样本目录时，建议加 `-I` 隔离模式运行，避免样本目录中的
> 同名模块（如伪造的 `json.py`）被解释器加载：
> `python -I xss_audit.py <样本目录> <报告路径>`

### 命令行参数约定

```
python xss_audit.py <样本目录> <报告输出路径>
```

- 两个参数均为必填；缺失时任一参数，`argparse` 打印用法提示并以**退出码 2** 结束。
- 报告父目录不存在时自动创建。

## 三、样本约定

样本目录下每个顶层 `.json` 文件为一个用例，**用例标识即文件名**（不含路径）。
约定字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string | 模板字符串，占位符语法 `{{ key }}` |
| `data` | object | 数据字典，键名支持字母/数字/`_`/`.`/`-` |

- 字段缺失或类型不符 → 标记为**不合法用例**，不参与漏洞判定。
- 文件不可读、编码错误、JSON 解析失败 → 进入报告 `problems` 问题列表，不中断处理。
- 模板引用了 `data` 中不存在的键 → 渲染抛 `KeyError`，该用例标记**处理失败**并附原因，其余用例继续。
- 非 `.json` 文件静默忽略。

构造的 10 个确定性样本：

| 文件 | 类型 | 预期结论 |
|---|---|---|
| `01_script_basic.json` | `<script>` 标签注入 | 漏洞 → 已修复 |
| `02_event_attr.json` | 双引号事件属性 `onerror` | 漏洞 → 已修复 |
| `03_event_attr_single_quote.json` | 单引号事件属性 `onmouseover` | 漏洞 → 已修复 |
| `04_onload_attr.json` | SVG `onload` 事件属性 | 漏洞 → 已修复 |
| `05_plain_text.json` | 纯文本 | 无漏洞 |
| `06_benign_brackets_and_number.json` | 含 `<` `>` 的非标签文本、数值 | 无漏洞 |
| `07_missing_field.json` | 缺 `data` 字段 | 不合法 |
| `08_wrong_type.json` | 字段类型不符 | 不合法 |
| `09_render_exception.json` | 引用不存在的键 | 处理失败（KeyError） |
| `10_unreadable_broken.json` | JSON 语法损坏 | 处理失败 + 问题列表 |

## 四、两种渲染模式与检测规则

### 渲染

- **未修复模式 `render_raw`**：数据值 `str()` 后原样填入占位符，不做任何转义，用于复现风险。
- **修复模式 `render_escaped`**：使用 `html.escape(value, quote=True)` 转义
  `& < > " '`（分别变为 `&amp; &lt; &gt; &quot; &#x27;`）后再填入。

### 检测规则 `detect_xss`

先从渲染结果中提取**原始 HTML 标签**（正则 `<[^<>]*>`，转义后的 `&lt;` 不会被提取），
仅在原始标签内部匹配两类规则：

1. **script 标签**：`<script` 后接空白或 `>`；
2. **事件属性片段**：`on` 开头的属性名 + `=`，且等号后紧跟引号或属性值首字符；
   首字符为 `&`（实体转义形态）不命中。

该设计使规则天然具备「转义感知」：修复输出中注入内容要么不再构成原始标签，
要么属性语法已被实体打断，因此漏洞用例修复后不会再命中。

> **范围说明（技术限制）**：`javascript:` 伪协议属于 **URL 上下文**风险，
> HTML 实体转义无法消除（字符串中没有尖括号/引号可转义）。工程上应在向
> `href`/`src` 赋值前增加**协议白名单校验**（仅允许 `http`/`https` 等），
> 不能仅依赖输出编码。本工具的检测范围按任务要求限定为 script 标签与事件属性两类。

## 五、报告结构（字段含义稳定）

```jsonc
{
  "summary": {                 // 汇总，数值与明细逐条统计一致
    "total_records": 10,       // 明细总条数
    "valid_case_count": 7,     // 合法用例数
    "invalid_case_count": 3,   // 不合法 + 处理失败条数
    "vulnerable_case_count": 4,// 漏洞用例数（合法且命中规则）
    "fixed_case_count": 4,     // 已修复用例数（漏洞用例修复后不再命中）
    "failed_case_count": 2,    // 处理失败条数
    "problem_file_count": 1    // 问题列表条目数
  },
  "notices": [],               // 目录为空/不存在等提示
  "problems": [                // 不可读文件问题列表，仅含文件名与原因
    {"file": "10_unreadable_broken.json", "reason": "JSONDecodeError: 无法读取或解析文件"}
  ],
  "cases": [                   // 按 case_id 升序，多次运行顺序一致
    {
      "case_id": "01_script_basic.json", // 用例标识，仅文件名
      "valid": true,                      // 是否合法
      "vulnerable": true,                 // 漏洞判定（基于未修复输出）
      "matched_rules": ["script标签"],    // 命中的检测规则
      "fix_conclusion": "已修复",         // 已修复 / 修复未生效 / 无漏洞无需修复 / 不合法用例 / 处理失败 / 不适用
      "raw_rendered": "<div>...<script>...</script></div>",   // 未修复渲染文本
      "fixed_rendered": "<div>...&lt;script&gt;...</div>",    // 修复渲染文本
      "error": null                       // 失败/不合法原因，正常为 null
    }
  ]
}
```

## 六、验收点对照与实际验证结果

实际运行命令与结果（Python 3.14.7 / Windows 11，PowerShell）：

| # | 验收点 | 验证方式 | 结果 |
|---|---|---|---|
| 1 | 两参数生成报告；缺参数非零退出且有提示 | 零参数/单参数退出码均为 2，含 usage 提示 | ✅ |
| 2 | 缺字段/类型不符标为不合法且不参与判定 | 样本 07、08 `valid=false`，无渲染结果 | ✅ |
| 3 | 同一用例含未修复与修复两段渲染文本 | 合法用例均含 `raw_rendered`、`fixed_rendered` | ✅ |
| 4 | 未修复原样保留尖括号与引号 | 样本 01 原文出现 `<script>alert('xss')</script>` | ✅ |
| 5 | 修复后尖括号/引号变实体、原标签消失 | 输出含 `&lt;script&gt;`、`&#x27;`、`&quot;`，无原始标签 | ✅ |
| 6 | script 样本判漏洞、纯文本判无漏洞 | 样本 01 命中；05、06 不命中 | ✅ |
| 7 | 漏洞用例修复后不命中，结论已修复 | 4 个漏洞用例结论均为 `已修复` | ✅ |
| 8 | 报告可被 JSON 解析，字段稳定 | `ConvertFrom-Json`/`json.load` 解析通过，字段固定 | ✅ |
| 9 | 明细按标识升序，多次运行顺序一致 | 两次运行 case_id 序列完全相同且为升序 | ✅ |
| 10 | 渲染异常标记失败并附原因，其余继续 | 样本 09 记 `KeyError: 'missing_key'`，其余正常 | ✅ |
| 11 | 空/不存在目录输出空明细报告，退出码 0 | 两种场景 `cases=[]`、含提示、退出码 0 | ✅ |
| 12 | 报告无绝对路径，标识仅文件名 | 全文无盘符/无上级目录片段，`case_id` 不含分隔符 | ✅ |
| 13 | 汇总计数与明细一致 | 7 合法 / 4 漏洞 / 4 已修复，与逐条统计相等 | ✅ |
| 14 | 不可读文件入问题列表且不中断 | 样本 10 出现在 `problems`，其余 9 条全部完成 | ✅ |

测试套件：`python -m unittest test_xss_audit` → **Ran 18 tests … OK**。

## 七、业务操作样例

```powershell
# 构造样本
python build_samples.py samples

# 生成报告
python xss_audit.py samples reports/report.json
# 控制台输出：
# 报告已生成: reports/report.json
# 汇总: 明细 10 条 | 合法 7 | 不合法 3 | 漏洞 4 | 已修复 4 | 处理失败 2 | 问题文件 1

# 业务侧快速核对某条用例（PowerShell）
(Get-Content reports/report.json -Raw | ConvertFrom-Json).cases |
    Where-Object case_id -eq '01_script_basic.json' |
    Format-List case_id, vulnerable, fix_conclusion, raw_rendered, fixed_rendered
```

## 八、设计说明

- **确定性**：样本内容、文件名、排序规则（按文件名升序）全部固定，报告无时间戳/随机数，重复运行可直接 diff。
- **容错隔离**：文件读取、JSON 解析、结构校验、渲染分别在单条用例粒度 try/except，异常原因（不含路径）写入 `error` 与 `problems`。
- **路径安全**：报告只记录文件名；异常原因对 `OSError`/`JSONDecodeError` 做脱敏，避免系统路径随报告外泄。
- **纯文本渲染**：渲染结果始终为字符串，非字符串数据经 `str()` 归一化后再转义。
