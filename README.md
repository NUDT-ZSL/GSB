# SQL 注入离线检测与参数化修复工具

针对本地订单系统遗留的 JSON 查询描述，离线检测**模板拼接**与**恶意载荷**两类 SQL 注入风险，输出 JSON 检测报告与参数化后的安全查询描述，并对含占位符语法的模板验证修复前后模拟执行结果一致。

- 仅使用 Python 标准库（需 Python 3.10+），不连接数据库、不访问网络，Windows 下直接运行
- 样本由内置生成器构造，全部为本地确定性数据，不访问任何未授权目标

## 文件结构

```
tool.py          检测工具（含内置样本生成器）
test_tool.py     验收测试（unittest，19 个用例）
samples/         内置生成器构造的样本（13 个）
reports/         工具对样本生成的检测报告
```

## 快速开始

```bat
REM 1. 生成内置样本（拼接漏洞 / 载荷注入 / 安全查询三类 + 错误边界样本）
python tool.py --generate-samples samples

REM 2. 分析单个查询描述，生成 JSON 报告
python tool.py samples\sample_payload_quote.json reports\report.json

REM 3. 运行验收测试
python test_tool.py
```

## 输入格式

```json
{
  "query_template": "SELECT * FROM orders WHERE username = 'alice'",
  "params": {"username": "alice"},
  "safe": false
}
```

- `query_template`：SQL 模板。`safe=false` 的遗留模板常把参数值直接渲染进文本（f-string / 加号拼接的运行结果）；`safe=true` 的模板使用 `{参数名}` 占位符
- `params`：参数键值
- `safe`：是否参数化安全查询

## 检测规则与严重程度

严重程度仅使用 `high` 与 `low` 两个值：

| severity | 含义 |
|---|---|
| `high` | 可直接利用的注入风险：模板拼接（参数值直接出现在 SQL 文本中），或参数值含强载荷特征（闭合引号 `' OR '1'='1`、注释符 `--` `/*`、堆叠查询分号 `; DROP`） |
| `low` | 可疑但不足以直接利用的弱特征：UNION SELECT 关键字组合、恒真条件 `OR 1=1`、危险关键字（DROP TABLE / SLEEP）、孤立单引号 |

检测项结构：

- `type`：`template_concatenation`（模板拼接）或 `parameter_payload`（参数载荷）
- `location`：拼接项为**模板偏移**（0 起始字符位置）；载荷项为**参数名**
- `reason`：以"模板拼接：…"/"参数载荷：…"前缀区分两类检测
- 载荷项附 `payload_type`：`quote_escape` / `comment` / `stacked_query` / `suspicious_keyword` / `lone_quote`

排序规则：`high` 全部在 `low` 之前；同级中拼接项按模板偏移升序、载荷项按参数名升序（拼接项排在载荷项之前）。

## 输出报告

```json
{
  "input": "…",
  "error": null,
  "safe": false,
  "detections": [ … ],
  "counts": {"high": 3, "low": 0},
  "fixed_query": {
    "query_template": "SELECT * FROM orders WHERE username = '{username}' AND status = '{status}'",
    "params": {"username": "' OR '1'='1", "status": "paid"},
    "safe": true
  },
  "validation": null
}
```

- `fixed_query`：仅对 `safe=false` 生成。模板中的参数原值全部替换为 `{参数名}` 占位符，参数集中存放且保留原键值；`safe=true` 或无修复时为 `null`
- `validation`：仅对已含占位符语法的模板输出。模拟执行（占位符替换为参数值）修复前后的 SQL，字段值相等则 `consistent=true`
- `error`：字段缺失、JSON 损坏、模板引号不配对、模板引用未提供参数时给出说明，此时 `detections=[]`、`fixed_query=null`

## 内置样本与实测结果

`python tool.py --generate-samples samples` 生成 13 个样本，实测输出（`reports/`）：

| 样本 | 类别 | 实测结果 |
|---|---|---|
| sample_concat_fstring / concat_plus | 拼接漏洞 | high=2，location 为模板偏移，已生成参数化 fixed_query |
| sample_payload_quote | 载荷注入 | high=3，含 `payload_type=quote_escape`（`' OR '1'='1`） |
| sample_payload_comment | 载荷注入 | high=3，`payload_type=comment` |
| sample_payload_stacked | 载荷注入 | high=2，`payload_type=stacked_query` |
| sample_safe | 安全查询 | detections=[]，fixed_query=null |
| sample_safe_malicious_param | 安全查询+恶意参数 | detections=[]，fixed_query=null（参数化执行，载荷无法逃逸） |
| sample_mixed_severity | 排序验证 | high=2 在前、low=2 在后；同级拼接按偏移、载荷按参数名升序 |
| sample_placeholder_mixed | 一致性验证 | 修复前后模拟执行 SQL 完全一致（consistent=true） |
| sample_unbalanced_quote | 错误样本 | error="模板引号不配对： 单引号未闭合" |
| sample_missing_param | 错误样本 | error="缺少参数： email" |
| sample_missing_field | 错误样本 | error="缺少必需字段： safe" |
| sample_bad_json | 错误样本 | error="JSON 解析失败： …" |

## 验证记录（2026-11-14，Windows 11，Python 3.14.7）

- `python test_tool.py`：19 个用例全部通过（覆盖验收点 1–10、14、15 及 CLI）
- `python tool.py --generate-samples samples` 后对 13 个样本逐一执行 `python tool.py <in> <out>`，全部生成报告，检测项与样本类别吻合（上表）
- 在 Windows 命令提示符（cmd.exe）下执行 `python tool.py samples\sample_concat_fstring.json reports\report_cmd_demo.json` 成功生成报告
- 全程离线，未访问网络与数据库
