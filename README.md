# MoE 专家路由与容量均衡离线核对组件

本工作目录提供一套仅依赖 Python 3 标准库的离线演示与验收组件。它把同一工程的
三个 schema 版本归一为统一的字节制模型，逐请求完成专家路由与副本分派，核算
每副本每专家负载账目，处理迁移/扩缩容事件，执行最小最大再均衡，并用一个不导入
主路由代码的独立参考分配器逐请求复核。

## 快速运行

环境要求：Linux/WSL 上的 Python 3，不需要安装任何第三方包。

生成并验收默认样本（固定种子 42、24 个请求）：

```bash
python3 demo.py --seed 42 --requests 24 --out artifacts/demo
```

使用另一组固定参数复现：

```bash
python3 demo.py --seed 7 --requests 10 --out /tmp/moe-demo-seed7
```

对已有三版本 schema 只做独立验收：

```bash
python3 verify.py --schemas-dir artifacts/demo/schemas --out artifacts/verify
```

运行标准库测试：

```bash
python3 -m unittest discover -s tests -v
```

等价模块入口：

```bash
python3 -m moe_capacity demo --seed 42 --requests 24 --out artifacts/demo
python3 -m moe_capacity verify --schemas-dir artifacts/demo/schemas --out artifacts/verify
```

同一 `--seed` 与 `--requests` 会生成字节级一致的 schema 和路由结果。所有时间、
集合遍历、并列决胜和排序都使用固定顺序，不依赖 Python 的哈希随机化。

## 输出产物

`demo.py` 在 `--out` 目录下写入：

```text
schemas/project_v1.json
schemas/project_v2.json
schemas/project_v3.json
normalized/v1.json
normalized/v2.json
normalized/v3.json
routes/v1_route.json
routes/v1_reference.json
routes/v1_reference_check.json
routes/v2_route.json
routes/v2_reference.json
routes/v2_reference_check.json
routes/v3_route.json
routes/v3_reference.json
routes/v3_reference_check.json
verification.json
summary.txt
manifest.json
```

`summary.txt` 是人类可读演示，包含逐请求最终路由、候选专家、每副本每专家账目、
再均衡调整、事件受影响请求、参考分配器比对和跨版本结论。

`verification.json` 是机器验收证据，关键字段包括：

- `acceptance.passed`：全部验收项是否通过。
- `versions.<v>.route.request_traces`：逐请求候选筛选、目标副本选择和历史变化。
- `versions.<v>.route.event_results`：每个事件的受影响请求、依赖专家、缺口、
  重路由结果与容量恢复情况。
- `versions.<v>.reference_check`：参考分配器的逐请求、账本和指标比对结果。
- `cross_version.observable_differences`：可观察差异清单；每项均带阶段归因
  （`normalization` 或 `routing`）与证据。

## Schema 归一语义

| 项目 | v1 | v2 | v3 | 统一表示 |
|---|---|---|---|---|
| 专家表 | 扁平 `experts[]` | 副本内嵌套 `replicas[].experts[]` | 拓扑节点内嵌套 `branches[]` | 扁平专家记录 + `home_replica` |
| 通信 | 十进制 `MB` | 二进制 `MiB` | 二进制 `MiB` | 整数 `bytes` |
| 请求负载 | `sample_count` | `payload_bytes` | `work.samples` | `sample_units` + `equivalent_bytes` |
| 容量 | `capacity_bytes` | `{value, unit}` | `limits.weighted_load_bytes` | 整数加权负载字节 |
| 精度 | `bf16/fp8` 字符串 | 精度数组 | `precision_profiles` | 支持精度元组 |

默认样本使用 `1 sample = 4096 equivalent bytes`，序列量化长度为 128。计算负载为：

```text
compute_bytes =
    sample_units
    * ceil(sequence_length / 128)
    * expert.per_sample_cost_bytes
```

通信量先按专家所在副本与目标副本共同计算：

- 目标副本就是专家 home 副本：原始通信量为 0。
- 目标副本与专家 home 同机架但不同副本：记录 `same_rack_avoided_bytes`，
  实际计费为 0。
- 目标副本与专家 home 跨机架：`billed_communication_bytes` 等于
  `expert.cross_rack_comm_bytes * sequence_blocks`。

加权容量占用为 `compute_bytes + billed_communication_bytes`。同一副本上的多个
专家分片会先按专家聚合，再汇总到副本；账本不会重复计算同机架通信。

归一指纹只包含源版本无关的规范字段，不含 `source_schema`、`source_path` 或
归一证据，因此三版本若真正等价必须得到相同 SHA-256 指纹。

## 主路由策略

1. 按逻辑时刻处理请求和事件；同一时刻先应用事件，再接纳该时刻请求。
2. 候选专家必须声明请求领域，且至少存在一个支持该专家精度的目标副本。
3. 每个可行路由选项计算投影后的：

   ```text
   (最大副本负载, 副本负载平方和, 计费跨机架通信, 目标副本 id, 专家 id)
   ```

4. 单请求接纳选择该元组最小的容量可行项。
5. 最终再均衡尝试四种确定性请求顺序：
   `min-work-desc`、`max-work-desc`、`arrival`、`sample-desc`。
   每种顺序用相同的加权目标做容量感知放置，再在四组可行结果中选全局目标最小者，
   从而避免单一顺序的局部死路。

主路由输出逐请求 `arrival_selection`、最终 `selected`、候选专家、所有目标副本
选项、候选过滤依据、目标选择依据、事件历史和最终目标元组。

## 事件重算

- `migrate_expert`：更新专家 home 副本；受影响请求为当前路由到该专家的请求。
  依赖专家集合包含被迁移专家。
- `scale_replica`：更新副本容量；受影响请求为当前分派到该副本的请求，依赖专家
  集合包含该副本上的 home 专家和受影响请求实际使用的专家。
- 缩容时计算 `shortfall_bytes = max(0, 旧目标负载 - 新容量)`。
- 受影响请求按降序计算量重新选择。若增量重路由无法满足容量，则执行确定性的全局
  fallback，输出失败候选的剩余容量/缺口、fallback 顺序、是否 reassign 未受影响
  请求以及恢复后容量状态。

## 独立参考分配器

`moe_capacity/reference.py` 不导入 `moe_capacity.router`。它独立实现：

- 事件应用与最终拓扑计算；
- 领域/精度候选和目标副本选项；
- 四种确定性排序及容量感知加权放置；
- 每副本每专家账本和不均衡指标。

`compare_with_reference` 先比较逐请求 `(request_id, expert_id, target_replica)`，
再比较完整账本和指标。失败时报告首个不一致请求、双方选择、选择规则和字段差异。

## 不均衡指标

`coefficient_of_variation` 使用总体标准差除以均值；`normalized_range` 为
`(max - min) / mean`；主指标 `imbalance_ratio_max_over_mean` 为
`max / mean`。`bottleneck_replicas` 先报告利用率不低于 90% 的副本；若没有副本
超过阈值，则报告利用率最高的副本。计算、通信和总加权负载分别输出指标。

## 公开 Python API

```python
from moe_capacity import (
    generate_project_set,
    normalize_document,
    run_allocation,
    run_reference,
    verify_project_set,
)
from moe_capacity.reference import compare_with_reference

documents = generate_project_set(seed=42, request_count=24)
project = normalize_document(documents["v1"])
main = run_allocation(project)
reference = run_reference(project)
check = compare_with_reference(main, reference)
verification = verify_project_set(documents)
```

## 目录结构

```text
demo.py                        演示命令包装
verify.py                      验收命令包装
moe_capacity/generator.py      确定性三版本样本生成器
moe_capacity/model.py          统一数据模型
moe_capacity/normalize.py      v1/v2/v3 归一与规范指纹
moe_capacity/router.py         主路由、事件重算、账本、再均衡
moe_capacity/reference.py      独立参考分配器与逐请求比对
moe_capacity/verify.py         跨版本与端到端验收
moe_capacity/cli.py            命令行与摘要输出
tests/test_acceptance.py       标准库验收测试
```

## 验收项

`verify_project_set` 会检查：

- 三版本规范指纹、逐请求路由、账本、指标、事件结果是否一致；
- 主路由与独立参考分配器是否逐请求一致；
- 最终分派和每次事件重算后是否满足容量；
- 每副本每专家账本是否与请求明细可重算一致；
- 同机架通信是否只在诊断字段中记录、不进入计费负载；
- 缩容事件是否给出缺口和重新选择依据；
- 再均衡后最大副本负载是否不劣于再均衡前；
- 逐请求轨迹字段是否完整；
- `observable_differences` 是否为空；若不为空则列出阶段与证据。

默认样本应输出 `Acceptance: PASS`。
