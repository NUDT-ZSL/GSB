# Sampling Compatibility Lab

面向 LLM 推理采样与解码链路的本地确定性应用，覆盖采样配置 v1/v2/v3
格式互操作、tokenizer 词表绑定、参数归一化、逐 step 解码、跨版本裁决、
版本化快照和一致性自检。

应用只使用 Python 标准库和 NumPy，不访问外部账号、站点或容器环境。

## 快速开始

项目已在 Python 3.11.15 + NumPy 2.4.6 下验证。

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 完整非交互演示
.venv/bin/python app.py demo

# 本地交互菜单
.venv/bin/python app.py menu

# 或使用环境检查包装脚本
./run.sh demo

# 其他单次命令
.venv/bin/python app.py list
.venv/bin/python app.py show req-0001 --version v3
.venv/bin/python app.py run req-0002 --version v2
.venv/bin/python app.py adjudicate req-0002
.venv/bin/python app.py migrate req-0002
.venv/bin/python app.py errors
.venv/bin/python app.py self-check
.venv/bin/python app.py snapshots

# 46 项单元/集成测试
.venv/bin/python -m unittest discover -s tests -v

# 幂等重建固定种子样本
.venv/bin/python scripts/generate_samples.py
```

默认快照写入 `runtime/snapshots.json`，该目录被 `.gitignore` 排除。

## 项目结构

```text
app.py                         # 命令行入口
sampling_compat/
  config_io.py                  # v1/v2/v3 读取、分派与序列化
  migration.py                  # v1 <-> v2 迁移及补全/丢弃/量化明细
  normalization.py              # 参数受控域与 E_PARAM_RANGE
  sampler.py                    # float64 确定性采样与候选轨迹
  reference.py                  # 独立 Python 标量 float64 参考实现
  adjudication.py               # 跨版本一致性与 E_SEMANTIC_GAP
  requests.py                   # 请求 JSONL 读取
  vocabulary.py                 # 词表版本表与校验和
  snapshots.py                  # 整体替换快照、版本和校验和
  engine.py                     # 应用服务与业务错误演示编排
  selfcheck.py                  # 重算、参考对齐和迁移可追溯性自检
  cli.py, render.py             # 本地菜单、命令和文本视图
scripts/generate_samples.py     # 固定种子样本与异常夹具生成器
samples/                        # 已提交的确定性样本
tests/                          # 标准库 unittest 测试
```

## 请求样本

`samples/requests.jsonl` 每行一个请求，主要字段为：

```json
{
  "request_id": "req-0001",
  "prompt_tokens": [1, 5, 9, 3, 5],
  "tokenizer_version": "tok-2024-01",
  "sampling_seed": 240101,
  "sampling_profile": {
    "v1": "<v1 原始文本>",
    "v2": "<v2 原始文本>",
    "v3": "<v3 原始文本>"
  },
  "logits": [[0.1, -0.2, "... 词表宽度的 float64 数值 ..."]]
}
```

`sampling_profile` 支持三种形态：版本到原始文本的映射、单段原始文本，或
`{"format": "v2", "body": "<原始文本>"}`。本地样本使用三版本映射，因此 UI
可以在 v1/v2/v3 视图间直接切换。

`samples/vocabularies.json` 保存词表版本、大小、token 名称列表和内容校验和。
`samples/expected.json` 保存三个请求、三个视图的 token 序列及决策哈希。

## 配置格式

### v1：扁平 JSON

仅允许 `version`、`temperature`、`top_k`。温度按文本保存，解析时执行以下
容错规则：去除首尾空白、允许下划线、允许正负号和小数/科学计数法；原始
字面量始终保存在 `temperature_literal`。

```json
{
  "version": "v1",
  "temperature": " 7.00e-1 ",
  "top_k": 8
}
```

### v2：带单位对象的 JSON

必填字段为 `temperature`、`top_k`、`top_p`、`repetition_penalty` 和
`vocab_version`。采样数值可使用 `{"value": ..., "unit": ...}` 单位对象：

```json
{
  "version": "v2",
  "temperature": {"value": "7.00e-1", "unit": "ratio"},
  "top_k": {"value": 8, "unit": "tokens"},
  "top_p": {"value": "1.0", "unit": "probability"},
  "repetition_penalty": {"value": "1.0", "unit": "multiplier"},
  "vocab_version": "tok-2024-01"
}
```

单位不匹配返回 `E_FORMAT_CORRUPT`，字段名形如 `temperature.unit`。读取器也
接受省略单位对象的普通数值，并记录 `implicit:<expected-unit>`。

### v3：行式键值文本

第一行必须是版本头，数值字段必须带单位，支持科学计数法、空行和 `#` 注释：

```text
!sampling-config v3
temperature = 7.00e-1 ratio
top_k = 8.0e0 tokens
top_p = 1.0e0 probability
repetition_penalty = 1.0e0 multiplier
vocab_version = tok-2024-01
```

读取器先检查版本头，再检查该版本的字段集合。未知版本返回
`E_VERSION_UNSUPPORTED`；截断头、缺失必填字段、重复字段、字段集不符返回
`E_FORMAT_CORRUPT` 并指出字段名。

## 迁移规则

### v1 -> v2

- `top_p` 补为 `1.0 probability`，语义为不截断。
- `repetition_penalty` 补为 `1.0 multiplier`，语义为中性。
- `vocab_version` 绑定为调用方显式传入的 `default_vocab_version`。
- 每项补全都写入 `added_fields`，含字段名、值、单位和规则名。
- 迁移输出会立即由 v2 读取器重新读取，确保格式可回读。

### v2 -> v1

- `temperature` 使用 `ROUND_HALF_UP` 量化到 6 位小数，量化前后值写入
  `quantized_fields`。
- `top_p`、`repetition_penalty`、`vocab_version`、单位标记显式丢弃并写入
  `dropped_fields`。
- 当 `top_p != 1.0` 或 `repetition_penalty != 1.0` 时标记
  `semantic_loss=true`，同时写入 `losses`。
- `vocab_version` 的丢弃是绑定元数据丢失，需由
  `request.tokenizer_version` 重新水合。
- 迁移输出会立即由 v1 读取器重新读取。

## 参数归一化

归一化在采样前执行，归一化结果和默认/截断明细完整写入请求决策记录。

| 参数 | 受控域 | 越界行为 |
| --- | --- | --- |
| `temperature` | `(0, 5]` | `E_PARAM_RANGE` |
| `top_k` | 正整数，最大为词表大小 | 非正/非整数 `E_PARAM_RANGE`，大于词表则显式截断 |
| `top_p` | `(0, 1]`；下界开，`1.0` 是显式“不截断”哨兵 | `E_PARAM_RANGE` |
| `repetition_penalty` | `(0, 10]` | `E_PARAM_RANGE` |

v1 缺失的 `top_p=1.0` 和 `repetition_penalty=1.0` 会进入
`defaulted_fields`，不会成为不可见默认值。所有范围错误都带有字段名和原始
字面量。

## 确定性采样

每个 step 的处理顺序固定：

1. logits 转为 NumPy `float64` 并除以 temperature。
2. 对 prompt 与已生成 token 的历史集合施加 repetition penalty：
   正 logit 除以 penalty，非正 logit 乘以 penalty。
3. 按 `(-scaled_logit, token_id)` 排序，截取 top-k；并以 token id 打破平局。
4. 在 top-k 候选上计算稳定 softmax，按累计概率保留 top-p 前缀并重新归一化。
5. 由 `sha256(algorithm|seed|request_id|step)` 取高 53 bit，得到精确的
   float64 `uniform in [0,1)`，再用逆 CDF 抽取 token。
6. 将 token 加入历史，进入下一 step。

每步决策记录包含 `top_k_candidate_ids`、top-p 后 `candidate_ids`、每个候选的
`scaled_logit`、归一化 `weight`、`cumulative_weight`、`uniform` 和
`chosen_token`。token 序列另以规范化 JSON 的 SHA-256 标识。

同一请求、seed、logits 和配置重复运行，决策记录逐字节一致。测试与自检会把
主实现和 `reference.py` 的独立标量 float64 循环逐 step 对齐。

## 跨版本裁决

- v1、v2、v3 各自读取并归一化后，比较采样语义指纹与 token 序列。
- v2/v3 的非默认 `top_p` 或 `repetition_penalty` 无法由 v1 表达时，返回
  `E_SEMANTIC_GAP`，列出 `unrestorable_fields`、全部 `affected_steps` 和实际
  `divergent_steps`，不会静默使用 v1 默认值继续裁决。
- 可表达字段不一致或 token 序列不一致返回 `E_INCONSISTENT`，并标注类别。
- `req-0001`、`req-0003` 的三视图一致；`req-0002` 的 v2/v3 一致，但 v1 与
  v2/v3 之间按预期返回 `E_SEMANTIC_GAP`。

## 词表与请求校验

- 请求 `tokenizer_version` 不在词表版本表：`E_VOCAB_VERSION_MISMATCH`。
- v2/v3 配置绑定版本与请求 `tokenizer_version` 不一致：
  `E_VOCAB_VERSION_MISMATCH`，归因到 request id 和 `vocab_version` 字段。
- prompt token 超出 `[0, vocab_size)`：`E_TOKEN_RANGE`，包含 token 下标。
- 任一 logits 行宽度不等于词表大小：`E_LOGITS_SHAPE`，包含具体 `step`。
- logits 非数值或含 NaN/Inf：`E_FORMAT_CORRUPT`，包含字段和 step。

## 版本化快照

`SnapshotRepository` 在单一文件中保存四类快照：`vocab_table`、
`normalized_params`、`sampling_decisions`、`migration_records`。

每个条目包含：

```json
{
  "kind": "sampling_decisions",
  "version": 2,
  "checksum": "sha256:...",
  "content": {}
}
```

- 版本号按类别单调递增，校验和覆盖 `kind + version + content`。
- 每次提交重建完整快照文档，通过临时文件 + `os.replace` 整体替换，不做增量
  修补。
- 加载时重新验证每个条目；文件签名、版本号或校验和变化时整体替换内存缓存。
- 校验和不符、缺失字段、非法版本、损坏 JSON 返回 `E_SNAPSHOT_CORRUPT`，
  并指出 `entry` 与 `version`。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知配置版本或请求缺少目标视图 |
| `E_FORMAT_CORRUPT` | 头部截断、JSON/行格式损坏、字段缺失或单位错误 |
| `E_PARAM_RANGE` | 归一化参数越界，带字段与原始值 |
| `E_VOCAB_VERSION_MISMATCH` | 词表版本未知或配置绑定与请求不一致 |
| `E_TOKEN_RANGE` | prompt token 超出词表范围 |
| `E_LOGITS_SHAPE` | logits 维度/宽度不符，带 step |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符，带 entry/version |
| `E_SEMANTIC_GAP` | v1 表达能力不足，列出字段和受影响 step |
| `E_INCONSISTENT` | 重算、参考、迁移追溯或跨版本结果不一致，带类别 |

`app.py errors` 会运行 12 个固定异常夹具并断言实际错误码。异常样本位于
`samples/errors/`，包括截断头、未知版本、缺失字段、四类参数越界、越界 token、
logits 宽度错误、词表绑定不一致、损坏校验和快照和语义缺口。

## 自检

`app.py self-check` 会整体提交快照并检查：

1. 每个存储决策可由同一请求、视图、seed 和 logits 重新计算，且规范化 JSON
   完全一致。
2. token 序列与独立标量 float64 参考实现一致，候选集和权重在容差内一致。
3. 迁移补全项、丢弃项、量化项都能追溯到字段或显式规则。
4. 三版本裁决结论符合预期，语义缺口必须带字段与 step。
5. 快照内容、版本和校验和与内存状态一致。

任一检查失败返回 `E_INCONSISTENT` 并指出类别和 request id。项目测试还包含
三种故意篡改场景：决策 token、迁移丢字段明细、快照决策内容。

## 实际验证

当前工作区已执行：

```text
Python 3.11.15
NumPy 2.4.6
46 tests in 0.733s: OK
```

完整 demo 结果：

```text
req-0001: [3, 12, 21, 7, 0, 25]   v1 = v2 = v3
req-0002: [45, 47, 16, 15, 30, 16, 12]   v2 = v3; v1 -> E_SEMANTIC_GAP
req-0003: [27, 10, 16, 23, 10]   v1 = v2 = v3
12/12 error demo cases PASS
max reference weight delta: 1.110e-16
```

更完整的命令输出和验收记录见 `VERIFICATION.md`。
