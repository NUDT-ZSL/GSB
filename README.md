# 中文意图配置跨版本互操作组件

这是一个可本地运行、可集成的 Python 领域组件与完整命令行示例。它解决三类问题：

- 同一套中文意图关键词配置以 v1 扁平行式、v2 键值行式、v3 结构化 JSON 三种形式并存时的加载、迁移与判定一致性。
- 标签体系收缩、版本升级、配置快照整体替换时的退休标签回退与回退保护。
- 读取、迁移、判定三阶段的参数化异常注入，以及可稳定复现的结构化报告。

组件不访问外部服务。样本与配置由固定脚本生成，报告 JSON 不包含时间戳，同一输入与配置版本下可逐字节复现。

## 目录

```text
intent_interop/
  config_io.py       # v1/v2/v3 读取、版本分派、校验和验证
  writers.py         # 三版确定性写出
  migration.py       # v1<->v2<->v3 迁移与损失记录
  matching.py        # 最长优先匹配、码点偏移、门限/退休回退、自检
  snapshots.py       # 不可变快照、单调版本、整体替换
  injection.py       # 三阶段异常注入
  pipeline.py        # 裁决、快照场景、注入运行器
  generator.py       # 固定中文样本与异常配置生成
  cli.py             # 命令行入口
  demo.py            # 一键验收编排
data/                # 生成的数据与配置
reports/             # 生成的 JSON 报告
tests/               # 29 个单元/集成/验收测试
scripts/             # 固定生成脚本与一键演示脚本
RUNBOOK.md           # 执行命令、参数与预期报告清单
```

## 环境准备

要求 Python 3.10+ 与 NumPy。当前环境验证版本为 Python 3.12.13、NumPy 2.5.5。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .

python --version
pip --version
python -c "import numpy; print(numpy.__version__)"
```

不安装包也可以从仓库根目录执行 `python -m intent_interop ...`。

## 快速开始

```bash
source .venv/bin/activate
python -m intent_interop generate --out-dir data
python -m intent_interop demo --data-dir data --report-dir reports
python -m unittest discover -s tests -v
```

`demo` 会执行基线裁决、四向迁移、五类异常注入、标签收缩、同版本换校验和、快照回退保护、富语义损失、资源超限与脏输入等 27 个场景，并写入 `reports/demo_summary.json`。所有场景均按预期状态通过时退出码为 `0`。

也可直接执行：

```bash
./scripts/run_demo.sh
```

## 三套配置格式

所有格式都携带 `snapshot_version`、`controlled_labels`、`fallback_intent` 与 `checksum`。校验和规则：

- v1/v2：对除 `checksum` 元数据行之外的全部文本内容计算 SHA-256。
- v3：移除顶层 `checksum` 字段后，对 `sort_keys=True` 的紧凑 JSON 计算 SHA-256。

### v1：无字段名竖线行式

```text
# version: v1
# snapshot_version: 1
# controlled_labels: fallback_unknown,refund_request
# fallback_intent: fallback_unknown
# checksum: sha256:<hex>
refund_request|申请退款,退款,退钱|1000
```

每行必须恰好三段：`意图标签|关键词串|优先级`。关键词以中文或英文逗号分隔。v1 不承载单意图置信门限与回退意图，升级时补 `0.1` 和全局回退标签。

### v2：带字段名行式

```text
# version: v2
# snapshot_version: 1
# controlled_labels: fallback_unknown,refund_request
# fallback_intent: fallback_unknown
# checksum: sha256:<hex>
intent=refund_request|keywords=申请退款,退款,退钱|threshold=0.1|fallback_intent=fallback_unknown
```

每行必须包含且只包含 `intent`、`keywords`、`threshold`、`fallback_intent` 四个键值对。缺失键、重复键、未知键或字段数异常均返回 `E_FORMAT_CORRUPT`，并带实际行号。

### v3：结构化 JSON

```json
{
  "version": "v3",
  "snapshot_version": 1,
  "controlled_labels": ["fallback_unknown", "refund_request"],
  "fallback_intent": "fallback_unknown",
  "checksum": "sha256:<hex>",
  "intents": [
    {
      "intent": "refund_request",
      "keywords": ["申请退款", "退款", "退钱"],
      "threshold": 0.1,
      "fallback_intent": "fallback_unknown",
      "label_path": ["service", "after_sale", "refund_request"],
      "slots": [{"name": "order_id", "placeholder": "{order_id}"}],
      "weight_vector": [1.0, 1.2, 1.0]
    }
  ]
}
```

`weight_vector` 与关键词按下标一一对应，用于置信度权重；默认迁移值为全 `1.0`，因此不会改变 v2 的原有判定语义。

## 迁移规则

| 路径 | 行为 | 损失处理 |
| --- | --- | --- |
| v1→v2 | 补全 `threshold=0.1` 与全局 `fallback_intent`；保留意图与关键词 | `priority` 不参与判定排序，丢弃并记录，不构成语义损失 |
| v2→v3 | 补全 `label_path=[intent]`、空 `slots`、全 1 权重 | 补全项逐条记录 |
| v3→v2 | `label_path` 可 `drop`、`flatten` 或 `reject`；槽位和权重显式丢弃 | 非默认层级、非空槽位、非全 1 权重进入 `E_SEMANTIC_LOSS` 损失明细 |
| v2→v1 | 丢弃单意图门限与回退；补写优先级 | 门限非默认值或回退非全局回退时构成语义损失 |

迁移不会重写既有意图标签或关键词。只有显式选择 `flatten` 时，标签路径才会被展平为新意图标签，并在动作记录中标记 `matching_semantics_unchanged=false`。

## 确定性判定

1. 在查询文本中枚举所有关键词出现位置，偏移使用 Python 码点下标。
2. 候选按“关键词码点长度降序、意图标签字典序、起点升序、关键词字典序”排序。
3. 按排序结果贪心选取互不重叠的命中；最终意图由第一个候选决定。
4. 置信度为 `round(clip(len(keyword) / len(query) * weight), 6)`。
5. 低于门限返回该意图的 `fallback_intent`，类别为 `below_threshold`；无命中类别为 `no_match`。
6. 命中标签不在受控集合时：
   - `--retired-policy fallback`：返回回退意图，记录 `fallback_category=label_retired`、`trigger_label=旧标签`、`code=E_LABEL_RETIRED`。
   - `--retired-policy reject`：抛出 `E_LABEL_RETIRED`，带 `query_id`。

每条结果同时输出全部非重叠 `hits` 与决定最终意图的 `top_hit`。`hits` 按起点排序，因此 `top_hit` 不一定位于 `hits` 首位，例如“申请退款并取消订单”按同分标签字典序选择 `cancel_order`。

跨版本裁决要求 v1、v2、v3 的最终意图、命中关键词、码点偏移、置信度和回退类别一致。任何分叉都会返回 `E_SEMANTIC_LOSS`，报告保留三版各自的审计结果，但 `conclusion` 置空，禁止任选一版作为结论。

## 快照版本化

- 每个快照包含单调递增 `snapshot_version`、内容校验和、受控标签集与意图配置。
- 版本增加或同版本校验和变化时，以新的不可变快照对象整体替换旧快照，`generation` 递增并给出差异清单。
- 新版本低于当前版本返回 `E_SNAPSHOT_ROLLBACK`，当前快照保持不变。
- 内容校验和缺失或损坏返回 `E_SNAPSHOT_CORRUPT`，带版本与字段字节偏移。
- 标签收缩后，未受影响查询的判定字段必须保持不变，否则自检返回 `E_INCONSISTENT`。

## 三阶段异常注入

注入由命令行参数触发，不修改源码文件。

| 阶段 | 类型 | 业务码 | 定位字段 |
| --- | --- | --- | --- |
| `read` | `field_count` | `E_FORMAT_CORRUPT` | `line_no` |
| `read` | `missing_key` | `E_FORMAT_CORRUPT` | `line_no` |
| `read` | `empty_keyword` | `E_CONFIG_INVALID` | `line_no` |
| `migrate` | `missing_key` | `E_FORMAT_CORRUPT` | `line_no` |
| `migrate` | `empty_keyword` | `E_CONFIG_INVALID` | `line_no` |
| `match` | `empty_keyword` | `E_CONFIG_INVALID` | `query_id` |
| `match` | `offset_length` | `E_INCONSISTENT` | `query_id` |
| 任一阶段 | `unexpected` | `E_UNATTRIBUTED` | 单独列入未归因异常 |

每次注入报告都包含：

- `attributed_errors`：带阶段、业务码、行号或 `query_id` 的已归因记录。
- `unattributed_errors`：非业务异常单独列出。
- `triggered` 与 `unreached`：注入触发和未到达情况。

## 资源限制与自检

可配置上限：

- `--max-keywords`：配置关键词总数。默认策略超限返回 `E_RESOURCE_LIMIT`；`--resource-policy prefix` 降级为仅查询前缀匹配，并标记 `keyword_budget_prefix_only`。
- `--max-query-codepoints`：单条查询码点长度，超限返回 `E_RESOURCE_LIMIT`。
- `--max-match-entries`：单条查询驻留匹配中间表大小，可报错或降级为前缀匹配并标记 `match_table_prefix_only`。

自检校验：

- 命中偏移位于查询码点长度内且互不重叠。
- 直接命中查询集合与回退查询集合无交集，且并集覆盖全部判定记录。
- `query_text`、回退类别、直接命中意图等字段与样本一致。
- 任一不一致返回 `E_INCONSISTENT` 并给出类别。

## 错误码

| 业务码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知配置版本或无法分派 |
| `E_FORMAT_CORRUPT` | 行字段数、键名、JSON 结构或 UTF-8 损坏 |
| `E_DUP_INTENT` | 意图标签重复，携带重复标签与行号 |
| `E_SEMANTIC_LOSS` | 表达能力不足，携带不可还原字段与涉及查询 |
| `E_LABEL_RETIRED` | 命中标签已退出受控集合 |
| `E_SNAPSHOT_ROLLBACK` | 尝试用旧版本覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | 校验和不符或快照损坏，携带版本与字段偏移 |
| `E_CONFIG_INVALID` | 配置或样本参数非法 |
| `E_DUP_QUERY` | `query_id` 重复 |
| `E_RESOURCE_LIMIT` | 关键词、查询长度或匹配表超限 |
| `E_INCONSISTENT` | 自检、偏移表或快照未受影响集合不一致 |
| `E_UNATTRIBUTED` | 未被任何业务阶段归因的异常 |

## 集成示例

```python
from intent_interop.config_io import read_config_document
from intent_interop.io_utils import read_samples
from intent_interop.matching import ResourceLimits, match_document
from intent_interop.migration import prepare_for_matching

document = read_config_document("data/config_v3.json")
canonical = prepare_for_matching(document).document
samples = read_samples("data/queries.jsonl")
batch = match_document(
    samples,
    canonical,
    limits=ResourceLimits(max_keywords=64, max_query_codepoints=256),
)

for result in batch.results:
    print(result.query_id, result.intent, result.to_dict()["offset_pairs"])
```

## 实际验证

当前工作区已执行：

- `python -m unittest discover -s tests -v`：29 项测试通过。
- `python -m intent_interop demo --data-dir data --report-dir reports`：27 个端到端场景全部通过。
- 三格式基线：25 条中文查询的意图、关键词、码点偏移、置信度与回退类别一致。
- 富语义 v3 降级：`label_path`、`slots`、`weight_vector` 被识别为不可还原字段，返回 `E_SEMANTIC_LOSS`，并对涉及查询列出 `query_id`。
- 标签收缩：6 条受影响查询回退，未受影响查询无变化；旧快照回写触发 `E_SNAPSHOT_ROLLBACK`。
- 校验和损坏：返回 `E_SNAPSHOT_CORRUPT`，报告版本与字段偏移。

具体命令、预期状态和报告文件名见 `RUNBOOK.md`。
