# 关系与事件抽取工程数据编辑器

这是一个不依赖容器、账号或外部服务的本地 Python 3.11 应用。它把定宽 v1 文本工程文件和 JSON v2 工程文件解析为统一中间表示（IR），支持事务化编辑、双版本序列化、降级损失追踪、原子快照恢复、并发稳定排序、异常注入隔离以及确定性诊断报告。

## 快速开始

```bash
python -m pip install -r requirements.txt
python -m domain_editor generate --output samples --seed 20261117 --force
python -m unittest discover -s tests -v
python -m domain_editor run --bundle samples/manifest.json --workspace build/run_v1
python -m domain_editor run --bundle samples/manifest_v2.json --workspace build/run_v2
python -m domain_editor verify --samples samples --output reports --seed 20261117
```

Windows PowerShell 可使用同样的 `py -3.11 -m ...` 命令。`build/` 和 `reports/workspaces/` 是可重建目录；最终验收证据在 `reports/VERIFICATION.md` 与 `reports/verification.json`。

## 目录

```text
domain_editor/
  model.py         统一 IR、损失项、失败项、编辑日志
  v1.py            定宽 v1 解析与序列化
  v2.py            JSON v2 解析与序列化、角色命名表、v1 旁路元数据
  documents.py     标注文档 JSONL 并发解析
  edits.py         五类事务化编辑及后置校验
  degradation.py   目标版本损失重算
  snapshots.py     快照代目录、CURRENT 指针、原子替换
  engine.py        命令执行、恢复、退出、报告、回放
  diagnostics.py   双版本往返校验与确定性报告
  samplegen.py     固定种子样本与异常注入样例
  verification.py  59 项本地验收
samples/           确定性双版本工程、命令流、异常样例
reports/           实际验收结果
tests/             单元与集成测试
```

## 样本与命令

`samples/documents.jsonl` 每行包含 `doc_id`、空格分词后的 `tokens`、整数词位置 `token_positions` 和可选 `metadata`。样本由固定种子 `20261117` 生成。

正常命令流依次执行：

1. `load_project`
2. `adjust_trigger`
3. `merge_relations`
4. `split_relation`
5. `shift_offsets`
6. `replace_argument_role`
7. `switch_version` 到 v1
8. `switch_version` 到 v2
9. `exit`

`commands.jsonl` 加载 v1 源，`commands_v2.jsonl` 加载 v2 源。异常命令流把第 5 步平移量改为 `100` 秒，以触发越界拒绝。

## v1 定宽格式

每个非空行是一条记录。字符串字段 UTF-8 左对齐、空格补齐；整数字段十进制右对齐、前导零补齐。关系记录为 135 字节，事件记录在关系字段后追加 `arg_count` 个 68 字节论元段。

| 字段 | 宽度 | 说明 |
| --- | ---: | --- |
| kind | 1 | `R` 关系，`E` 事件 |
| doc_id | 16 | 文档 ID |
| entry_id | 20 | 条目 ID |
| relation_type | 20 | 关系类型；事件行存放事件类型 |
| head_start/head_end | 8+8 | 头部或事件起止整数位置 |
| tail_start/tail_end | 8+8 | 尾部位置；事件行写 0 |
| confidence_permille | 4 | 0..1000 千分比置信度 |
| trigger | 20 | 触发词 |
| event_type | 20 | 事件类型 |
| arg_count | 2 | 事件论元段数 |

每个论元段：`role` 24、`start` 8、`end` 8、`source_relation` 20、`entity_type` 8。位置通过 `(integer_units - origin) / units_per_second` 转为统一秒偏移，默认 `origin=0`、`units_per_second=1000`。

解析失败会按条目隔离并进入失败清单。超长行、短行、置信度越界、角色非法、论元悬空和位置逆序分别使用稳定业务码。

## v2 JSON 格式

```json
{
  "schema_version": "v2",
  "role_names": {"R_EMPLOYEE": "employee"},
  "documents": [
    {
      "doc_id": "doc-001",
      "metadata": {"lang": "en"},
      "relations": [
        {
          "entry_id": "rel-001",
          "relation_type": "works_for",
          "head": {"start": 0.0, "end": 0.005},
          "tail": {"start": 0.015, "end": 0.019},
          "confidence": 0.939,
          "trigger": "works"
        }
      ],
      "events": [
        {
          "entry_id": "evt-001",
          "event_type": "employment",
          "span": {"start": 0.0, "end": 0.019},
          "confidence": 0.771,
          "trigger": "works",
          "arguments": [
            {
              "role": "R_EMPLOYEE",
              "start": 0.0,
              "end": 0.005,
              "source_relation": "rel-001",
              "entity_type": "PERSON"
            }
          ]
        }
      ]
    }
  ],
  "metadata": {}
}
```

JSON 使用 `parse_float=Decimal`，因此解析、快照和报告不会依赖二进制浮点显示差异。输出使用稳定键序、无多余空白和固定 Decimal 拼写。

## 版本互转与降级

- v1 → v2：整数位置按配置换算为秒；千分比置信度除以 1000；从配置角色表生成 `role_names`；原始整数位置、千分比置信度和原始字段写入 `project.v2.v1-origin.json` 旁路文件。
- v2 → v1：位置按 `v2_to_v1_rounding` 量化；置信度量化到千分比；位置/置信度精度损失、文档元数据丢失、超长角色名截断和合并来源丢失进入损失清单。
- 损失清单按文档、条目索引、类型、字段和详情稳定排序，其哈希写入快照，并参与降级产物指纹。
- 诊断报告分别标记可逆变更与不可逆损失。v1 整数位置/千分比、角色表生成和旁路元数据属于可逆变更；元数据丢弃和超长角色截断属于不可逆损失。

## 编辑语义

- `adjust_trigger`：向关系或事件触发词表加入/移除词条。
- `merge_relations`：仅允许同文档、同类型关系；保留组件来源，合并位置取联合跨度，置信度取保守最小值，并重映射事件论元的关系引用。
- `split_relation`：`boundary_token_index` 必须落在文档内部词边界。带合并组件的关系按组件恢复，因此 merge → split 可还原原 ID、位置、置信度和事件引用；普通关系按边界裁剪跨度。
- `shift_offsets`：平移文档内全部关系、事件和论元偏移，词位置不动；负值或超出文档标注边界即拒绝。
- `replace_argument_role`：角色必须属于该事件类型角色表，论元仍须位于事件范围内。

所有编辑在副本上执行，后置校验和损失重算成功后才提交；任何失败都返回 `E_EDIT_INVALID`，IR 和损失清单保持原样，失败项仍被记录。

## 快照与恢复

每次命令处理后写入 `snapshots/gen-<global_version>-op<operation>-<hash>/snapshot.json`。写入顺序为：

1. 在同级临时目录内原子写入快照；
2. 完整目录 rename 为不可变代目录；
3. 原子替换 `snapshots/CURRENT` 指针；
4. 从快照内嵌产物原子写出目标文件与旁路文件；
5. 原子写入 `progress.json`。

快照包含全局版本、来源版本、来源文件束指纹、IR 指纹、损失清单哈希、最后操作序号、完整 IR、编辑日志、失败项和内嵌产物。恢复命中时跳过源解析并可从快照修复输出；来源版本或指纹变化时输出 `E_SNAPSHOT_STALE`，完全重解析新来源，不把旧来源条目混入新状态。

SIGINT/SIGTERM 会置停止标志，当前命令完成快照后退出 0。`progress.json` 记录最后完成操作和快照版本，下一次 `--resume` 跳过已处理操作。强制终止后使用 `recover` 只读取已完整提交的 `CURRENT` 快照，不承诺存在退出报告。

## 并发与确定性

文档 JSONL 解析、v1/v2 文档条目解析、编辑后每文档校验、目标版本序列化均通过线程池执行，线程数来自配置或 `--max-workers`。每个任务带 `(doc_id, entry_index, stage)` 稳定键；完成顺序只用于测试观察，不参与输出。关系/事件、损失、日志、失败与报告均按稳定排序键输出。

`python -m domain_editor verify` 会比较 1 线程与 4 线程的 `final_state.json`、`losses.json`、`failures.json`、`edit_log.jsonl`、目标产物和报告是否逐字节一致。

## 业务码

| 业务码 | 场景 |
| --- | --- |
| `E_FORMAT_TRUNCATED` | v1 字段不足、行短于声明宽度、数值字段损坏 |
| `E_FORMAT_OVERLONG` | v1 行或字段超过定宽 |
| `E_JSON_INVALID` | v2 JSON 损坏、根对象或缺键错误 |
| `E_SPAN_ORDER` | 起止位置逆序、论元越出事件、文档词位置重叠 |
| `E_CONF_RANGE` | v1 千分比超出 0..1000，或 v2 置信度超出 0..1 |
| `E_ARG_DANGLING` | 论元引用了不存在的关系 |
| `E_ROLE_UNKNOWN` | 角色键/角色名不在角色命名表或事件角色表 |
| `E_EDIT_INVALID` | 编辑事务校验失败；详情码给出根因 |
| `E_SNAPSHOT_STALE` | 快照来源版本或来源指纹不匹配，已重解析 |
| `E_DOCUMENT_UNKNOWN` | 工程条目引用未标注文档 |
| `E_DUPLICATE_ID` | 文档、关系或事件 ID 重复 |

## 异常注入

`samples/anomaly_injection.json` 和 `samples/anomaly/anomaly_injection.json` 定义了五个稳定编号：

- `INJ-001` 定宽行超长
- `INJ-002` 千分比置信度越界
- `INJ-003` 事件论元悬空
- `INJ-004` 位置平移越界
- `INJ-005` v2 角色名非法

v1 异常包可命中 001–004；v2 异常包可命中 004–005，并在 v2→v1 往返中展示超长角色截断、文档元数据损失以及位置/置信度量化精度损失。正常包与异常包分离，异常只影响对应条目，其余条目继续处理。失败清单非空时进程退出码为 1。

## 诊断报告

`report.json` 对同一 IR 分别序列化为 v1 与 v2 再解析，包含：

- 输入/输出关系与事件计数、缺失/多余条目；
- 每个条目的位置偏差明细、总直方图和按字段直方图；
- 最大位置偏差、最大置信度偏差；
- 论元角色还原总数、失败明细；
- 可逆变更、不可逆损失、转换失败和解析失败；
- 业务码摘要、注入命中证据和运行日志摘要。

报告不含时间戳、绝对路径或线程完成顺序。相同输入连续两次运行通过 `reports/verification.json` 中的 SHA-256 比较证明逐字节一致。

## 实际验收结果

本工作区已执行：

```text
python -m unittest discover -s tests -v       # 7 tests OK
python -m domain_editor verify ...             # 59 checks passed
```

实际结果见 `reports/VERIFICATION.md`、`reports/verification.json`、`reports/diagnostic_report.json`、`reports/run_v1_report.json` 和异常报告。关键结果：

- 正常 v1 / v2 命令流退出 0，无失败项；
- 1 线程与 4 线程全部验收产物逐字节一致；
- 批处理与逐条回放最终 IR 指纹一致；
- 两次诊断报告逐字节一致；
- SIGTERM 完成当前快照后退出 0，并可继续恢复；
- 强制终止后无退出报告，恢复点固定为最后完整替换的操作 3；
- 五个异常注入编号全部命中，业务码符合预期。

## 已知边界

- 无合并来源的普通关系在非端点词边界拆分时，缺失的一侧端点会生成零宽端点；合并来源关系优先按组件恢复。
- v1 不承载文档级元数据和完整角色命名表，因此降级会明确记录不可逆损失与可逆扁平化。
- 快照来源指纹覆盖标注文档、规则配置和当前来源工程文件；输出文件以快照内嵌产物为准，恢复时会修复不一致的外部输出。
