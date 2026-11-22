# NER 标注数据治理组件

面向多标注者 NER 标注的本地领域组件与完整示例，覆盖：

- v1 竖线行式、v2 键值行式、v3 JSON 结构化声明的严格读取与分派。
- v1/v2/v3 双向迁移、默认补全、降级丢弃/展平及逐源行明细。
- 多数投票与标注者优先级合并，同票按 `annotator_id` 字典序裁决。
- 受控类型、类型别名与 BIO 序列校验。
- 停用类型的替代、显式拒绝、整文档退回。
- 三格式语义等价检查、逐 token 独立追溯重放。
- 版本化快照整体替换、校验和、回滚防护与资源上限。
- 跨模块一致性自检、CLI 示例、JSONL 样本与自动化测试。

## 环境

代码要求 Python 3.10+，核心组件仅使用标准库；样本生成器使用 `numpy`。

当前实际验收环境：

- Python 3.10.12
- numpy 1.26.4
- pytest 8.3.3
- WSL2 上的 Ubuntu-22.04 / Linux 6.18

任务描述中的 Windows 环境在当前执行机中未提供；实现未使用 Linux 专属路径或容器，`pathlib`、`os.replace`、`fsync` 等均可在 Windows 运行。实际报告会在 `environment.note` 中明确记录此差异。

## 快速运行

```bash
python3 scripts/generate_samples.py
python3 -m ner_governance.cli \
  --data-dir data \
  --snapshot-dir artifacts/snapshots \
  --migration-dir artifacts/migrations \
  --report artifacts/report.json
python3 -m pytest -q
```

Windows PowerShell 可将 `python3` 替换为 `python`。安装为可编辑包：

```bash
python -m pip install -e ".[dev]"
```

## 目录

- `ner_governance/`：领域组件。
- `scripts/generate_samples.py`：固定种子 `20261123` 的确定性样本生成器。
- `data/`：生成的 8 个文档、32 条多标注者记录、三套类型集和配置。
- `tests/`：46 个自动化测试，覆盖正常链路与主要失败码。
- `artifacts/report.json`：完整示例报告。
- `artifacts/migrations/`：六组有向迁移的实际输出。
- `artifacts/snapshots/`：类型集与合并结果的当前快照。
- `TECHNICAL.md`：算法、错误映射、资源与快照语义说明。

## 类型集格式

### v1

格式头固定三列，数据行也固定三列；空行和 `#` 注释忽略。

```text
#NER_TYPE_SET|v1|ls-ner-3
TYPE|PERSON|true
TYPE|LOCATION|false
ALIAS|PER|PERSON
```

- `TYPE|name|enabled`：BIO 前缀要求和别名启用状态由 v1 格式隐含为 `true`。
- `ALIAS|alias|target`。
- 抬头行不是三列、数据行列数不为 3、布尔值不可解析、字段为空、别名目标不存在等均返回 `E_TYPE_SET_CORRUPT`，并携带 `field` 与 `line`。

### v2

```text
#NER_TYPE_SET|v2|ls-ner-3
type=PERSON
bio_prefix_required=true
enabled=true
alias=PER>PERSON>true
type=LOCATION
bio_prefix_required=true
enabled=false
alias=LOC>LOCATION>true
```

每个 `type=` 开始一个类型块；块内必须有 `bio_prefix_required` 与 `enabled`。别名固定为 `alias>target>enabled` 三段。

### v3

```json
{
  "format": "NER_TYPE_SET_V3",
  "label_set_version": "ls-ner-3",
  "types": [
    {
      "name": "PERSON",
      "bio_prefix_required": true,
      "enabled": true,
      "aliases": [
        {
          "name": "PER",
          "target": "PERSON",
          "enabled": true,
          "metadata": {}
        }
      ]
    }
  ]
}
```

读取器优先按格式头分派；无格式头时按 JSON 对象字段、`key=value` 字段集或 v1 行类型字段集分派。未知版本返回 `E_VERSION_UNSUPPORTED`。任一格式解析失败都不会返回部分结果。

## 迁移语义

- v1 升级到 v2/v3：显式补全每个类型的 `bio_prefix_required=true`、每个别名的 `enabled=true`；升级到 v3 时补全 `metadata={}`。每条补全记录包含 `item`、`value`、`source` 和 `source_line`。
- v3/v2 降级到 v1：嵌套别名行会展平并记录 `representation_flattened`；不可表达的元数据按 `nested_aliases` 的 `flatten`/`discard` 处理；禁用别名按 `disabled_aliases` 的 `flatten`/`discard` 处理；`bio_prefix_required=false` 按 `bio_prefix_required` 的 `flatten`/`reject` 处理。
- 每条丢弃/展平记录包含源行 `source_line` 和 `lossy` 标记。
- 每次迁移都会重新读取生成的文本并比较语义指纹；回读不一致返回 `E_SEMANTIC_DRIFT`。

## 合并与裁决

- 按 `doc_id` 分组，并要求各标注者的 `doc_id` 集合、token 序列和标签长度一致。
- `majority`：先经别名归一，再统计规范化标签；同票选择“支持该标签的最小 `annotator_id`”对应的标签。
- `annotator_priority`：按配置优先级选择；未列入配置的标注者排在已列项之后，并按 `annotator_id` 字典序稳定处理。
- 最终标签必须属于启用类型；`I-X` 前一个最终标签必须是 `B-X` 或 `I-X`。
- 停用类型按 `retirement_policy` 处理：
  - `substitute`：改写为替代类型，产生 `E_TYPE_RETIRED` 告警，类别为 `retired_substituted`。
  - `reject`：返回 `E_TYPE_RETIRED`。
  - `doc_reject`：返回 `E_DOC_REJECTED`。
- 所有排序、字典键、文档、位置和 JSON 序列化顺序均显式确定，不依赖字典插入顺序或系统时间。

## 组件接口

```python
from pathlib import Path

from ner_governance import NerGovernance
from ner_governance.snapshots import SnapshotStore

api = NerGovernance()
views = api.load_type_set_views({
    "v1": "data/entity_types_v1.txt",
    "v2": "data/entity_types_v2.txt",
    "v3": "data/entity_types_v3.json",
})
config = api.load_config("data/annotation_config.json")
records = api.load_annotations("data/annotations.jsonl")

migration = api.migrate(views["v1"], "v3", config)
bundle = api.merge(records, views["v1"], config)
trace = api.trace(bundle, records, views["v1"], config, "doc-001", 1)
semantic = api.compare_views(views, records, config)
check = api.self_check(records, views, config)

store = SnapshotStore(Path("artifacts/snapshots"))
snapshot = api.write_merge_snapshot(store, bundle)
```

业务失败抛出 `GovernanceError`，使用 `exc.code`、`exc.details` 和 `exc.to_dict()` 获取机器可读结果。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知或无法判定的类型集版本 |
| `E_TYPE_SET_CORRUPT` | 类型集格式、字段、行号或布尔值损坏 |
| `E_CONFIG_INVALID` | 配置字段、策略或别名冲突 |
| `E_ANNOTATION_CORRUPT` | JSONL 记录或字段结构损坏 |
| `E_TYPE_NOT_ALLOWED` | 归一化后的类型不在启用集合 |
| `E_ALIAS_UNRESOLVED` | 已知别名被禁用或目标无效 |
| `E_BIO_INVALID` | BIO 标签格式错误或 `I-` 缺少同类型前缀 |
| `E_TYPE_RETIRED` | 命中停用类型；替代时为告警，拒绝时为失败 |
| `E_DOC_REJECTED` | 因停用类型整文档退回 |
| `E_SEMANTIC_DRIFT` | 三格式语义或迁移回放不一致 |
| `E_TRACE_BROKEN` | 追溯缺失、篡改或独立重放不一致 |
| `E_SNAPSHOT_ROLLBACK` | 试图用旧版本覆盖新快照 |
| `E_SNAPSHOT_CORRUPT` | 快照缺失、JSON、版本或校验和损坏 |
| `E_RESOURCE_LIMIT` | token、类型或驻留投票超限 |
| `E_INCONSISTENT` | 自检发现跨模块不一致 |

## 实际验收结果

固定种子生成结果：

- 8 个文档、4 个标注者、32 条 JSONL、134 个 token 位置。
- 类型语义指纹：`59f08af9b926dc67fe383ee42816ce47928d2d60e19af1dce135cff56708f8ec`。
- 合并摘要：`b2fb5d52ef35b45c230c8ec31d36e80949fd74d13dd3690ac10dd707b8ba39f6`。
- 16 条 `E_TYPE_RETIRED` 替代告警，均归入 `retired_substituted`。
- 六组非恒等迁移全部 `replay_ok=true`、`lossless=true`。
- 134 个位置全部追溯重放成功。
- 自检 `code=OK`，三视图启用集合、别名映射和合并结论一致。
- 类型集快照从 v1 整体替换到 v2；回退尝试返回 `E_SNAPSHOT_ROLLBACK`。

两次运行样本生成器后，五个样本文件 SHA-256 完全一致。完整结果见 `artifacts/report.json`。
