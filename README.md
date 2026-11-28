# 区域医疗信息交换存储运维模拟器

这是一个仅使用 Python 3 标准库的确定性存储规划工程。它读取一份交换存储工程清单，完成对象放置、容量水位控制、逻辑时刻事件演化、故障后增量重定位、逐对象迁移进度和引用/摘要对账，并输出 JSON 与 Markdown 报告。

样本完全由固定整数种子在本地生成，不访问真实医疗机构、线上服务或外部账号。

## 快速运行

环境为 Linux/WSL Ubuntu 与 Python 3.10+，不需要安装任何第三方包。

```bash
# 省略 --project 时，按种子生成样本并运行
python3 main.py --seed 20261128 --output reports/seed_20261128

# 运行已有工程清单
python3 main.py \
  --project samples/sample_project_seed_20261128.json \
  --seed 20261128 \
  --output reports/from_project

# 只生成样本
python3 main.py --seed 42 --sample-only

# 将完整 JSON 报告同时打印到 stdout
python3 main.py --seed 20261128 --output reports/seed_20261128 --print-json
```

等价的模块入口：

```bash
python3 -m exchange_storage --seed 20261128 --output reports/seed_20261128
```

非法清单会输出 `validation_errors.json`、拒绝运行规划，并以退出码 `2` 结束。合法运行输出：

- `report.json`：完整机器可读结果。
- `report.md`：便于人工复核的摘要。
- `samples/sample_project_seed_<seed>.json`：省略清单时生成的固定种子样本。

## 目录结构

```text
exchange_storage/
  models.py       数据模型与错误定位结构
  validation.py   清单解析、引用完整性和业务约束校验
  engine.py       得分、放置、水位修复、事件、重定位与对账
  sample.py       固定种子合成样本生成器
  cli.py          命令行入口与报告渲染
main.py           便捷入口
samples/          固定种子样本
reports/          运行报告
tests/            标准库 unittest 独立核算测试
```

## 工程清单格式

最小结构如下；`events` 必须按 `at` 非降序排列。

```json
{
  "schema_version": 1,
  "project_id": "synthetic-regional-exchange-20261128",
  "seed": 20261128,
  "policy": {
    "watermark_ratio": 0.8,
    "max_latency_ms": 180,
    "urgency_weights": {
      "routine": 1.0,
      "urgent": 2.0,
      "critical": 4.0
    }
  },
  "institutions": [
    {
      "id": "HOSP-A",
      "name": "甲院（合成）",
      "required_compliance_level": 1,
      "max_latency_ms": 160
    }
  ],
  "regions": [
    {
      "id": "R-LOCAL",
      "name": "本地机房",
      "capacity_bytes": 180000,
      "used_bytes": 25000,
      "health": "healthy",
      "latency_ms": 4,
      "compliance_level": 3,
      "watermark_ratio": 0.8
    }
  ],
  "objects": [
    {
      "id": "EX-001",
      "institution_id": "HOSP-A",
      "test_category": "生化",
      "urgency": "routine",
      "bytes": 10000,
      "created_at": 0,
      "expires_at": 10,
      "digest": "sha256:...",
      "reference_count": 1
    }
  ],
  "events": [
    {
      "at": 2,
      "type": "access",
      "object_id": "EX-001",
      "delta": 1
    },
    {
      "at": 9,
      "type": "expire",
      "object_id": "EX-001",
      "delta": 1
    }
  ],
  "incidents": [
    {
      "at": 5,
      "type": "health_degraded",
      "region_id": "R-EAST",
      "health": "degraded"
    },
    {
      "at": 5,
      "type": "health_degraded",
      "region_id": "R-WEST",
      "health": "degraded",
      "after_migration_step": 1
    }
  ]
}
```

### 字段与约束

- `institutions[].required_compliance_level`：非负整数，表示机构要求的最低区域合规级别。
- `institutions[].max_latency_ms`：可选；实际时延上限取它与 `policy.max_latency_ms` 的较小值。
- `regions[].used_bytes`：区域已有外部占用，水位统计为“已有占用 + 活跃对象字节”。
- `regions[].health`：`healthy`、`degraded` 或 `offline`。只有 `healthy` 区域可作为放置或迁移目标。
- `regions[].watermark_ratio`：可选区域覆盖值；缺省使用全局 `policy.watermark_ratio`。
- `objects[].urgency`：必须存在于 `policy.urgency_weights` 中，因此等级集合可扩展。
- `objects[].bytes`：非负整数；`expires_at` 不得早于 `created_at`；`reference_count` 为正整数。
- `events[].type`：`access` 增加引用计数，`expire` 减少引用计数；`delta` 缺省为 `1`。
- `incidents[].type`：支持 `health_degraded`、`health_change`、`watermark_change`、`watermark_exceeded`。
- `incidents[].health`：健康事件的新状态，缺省为 `degraded`。
- `incidents[].watermark_ratio`：`watermark_change` 必须提供；`watermark_exceeded` 可省略（按当前阈值检查），也可提供新阈值。取值要求 `0 < ratio <= 1`。
- `incidents[].after_migration_step`：可选正整数。提供时表示该故障在同时刻重定位计划的第 N 个对象迁移完成后生效，用于验证“迁移途中目标区域降级”。

校验不通过时，错误条目包含 `code`、`message`、`path`，能定位时还包含 `object_id`、`institution_id`、`region_id` 和 `event_index`。只要存在错误，规划器不会继续处理。

## 确定性公式

### 放置得分

对满足过滤条件的对象 `o` 与区域 `r`：

```text
score(o, r) = urgency_weights[o.urgency] * 1000 / (r.latency_ms + 1)
```

结果四舍五入到 6 位小数，分数越高越优先；同分按区域 `id` 升序决胜。

### 候选过滤

区域必须同时满足：

```text
r.health == "healthy"
AND r.latency_ms <= min(policy.max_latency_ms, institution.max_latency_ms)
AND r.compliance_level >= institution.required_compliance_level
```

报告的每个对象候选列表包含得分、是否合格、`status` 和逐项 `reason_code`。常见未入选原因包括：

- `EXCLUDED_LATENCY_LIMIT`
- `EXCLUDED_COMPLIANCE_LEVEL`
- `EXCLUDED_HEALTH_DEGRADED`
- `CAPACITY_WATERMARK`
- `LOWER_SCORE`
- `TIE_BREAK_REGION_ID`

### 容量水位

```text
limit_bytes(r) = floor(r.capacity_bytes * r.watermark_ratio)
effective_used(r) = r.used_bytes + sum(active object bytes assigned to r)
watermark(r) = effective_used(r) / r.capacity_bytes
```

### 加权时延满足率

```text
100 * sum(weight(o) for assigned o with r.latency_ms <= latency_limit(o))
    / sum(weight(o) for all active o)
```

未指派对象计入分母、不计入分子。报告按 `routine`、`urgent`、`critical` 等实际等级分别统计。

## 求解流程

1. 按急缓权重降序、字节数降序、对象 `id` 升序，将每个对象放到得分最高的合格区域，得到无容量约束偏好。
2. 对越限区域执行贪心修复：每次选择“单位迁出字节得分损失最小”的对象迁至同样合格且有水位余量的区域。
3. 执行局部提升：在不突破目标区域水位的前提下，将对象迁移到得分更高的区域，直到无改进。
4. 若仍无法消除越限，报告该区域需要迁出的对象集合、迁出字节与迁出后预测水位。
5. 按逻辑时刻处理事件；同刻先按急缓权重降序，再按 `expire` 先于 `access`、对象 `id`、清单序号稳定排序。
6. 区域健康降级时，受影响对象为降级区域当前承载集合中的全部对象；水位越限时，从该区域按低急缓优先选择足以消除超量的对象。只对这些对象重新求解。
7. 重定位计划逐对象给出顺序、源/目标区域、目标字节增量、目标预测水位和状态历史。
8. 每个逻辑时刻输出所有区域的对象数、对象承载字节、已有占用、有效已用字节、容量、水位上限、水位、健康状态和越限标记。
9. 最终按区域引用清单、对象规范摘要、对象引用计数和指派归属进行对账。

### 引用计数语义

`reference_count` 表示对象当前持有的逻辑引用数。初始放置时，区域引用条目镜像该计数；`access` 增加计数，`expire` 减少计数，归零时从区域承载集合和引用清单中移除。对象在一个逻辑时刻只有一个承载区域，因此活跃且已指派对象的区域引用合计应等于其计数。

若对象因为没有任何健康、合规、时延和容量都合适的区域而未指派，它仍可用于事件与满足率统计，但区域引用清单为 0，不会被误报为摘要缺失。

### 迁移失败与最终一致性

- 目标在计划生成后、实际迁移前健康状态不是 `healthy`：失败码 `TARGET_HEALTH_DEGRADED`，源引用保留。
- 目标实际水位将越限：失败码 `TARGET_CAPACITY_EXCEEDED`，源引用保留。
- 无健康/合规/时延合格目标：失败码 `NO_ELIGIBLE_TARGET`。
- 源引用在迁移途中变化：失败码 `SOURCE_REFERENCE_CHANGED`。

每个计划条目的状态历史从 `pending` 开始，最终进入 `migrated` 或 `failed`。最终 `region_reference_lists`、`reconciliation` 和 `execution_audit` 会检查执行结果是否一致。

## 报告内容

`report.json` 的主要顶层字段：

- `validation`：合法清单的零错误结果。
- `formula`：本次运行采用的公式说明。
- `assignment.objects`：逐对象指派、选取得分、时延上限及全部候选的合格状态与落选原因。
- `capacity_control`：容量修复迁移、局部提升迁移、初始与最终未解决越限区域、迁出集合和预测水位。
- `events_processed`：逐事件引用计数变化。
- `incidents_processed`：健康和水位故障事件。
- `timeline`：逐逻辑时刻区域承载演化与对账状态。
- `relocations`：逐批次受影响对象、迁移顺序、目标、字节增量、预测水位和进度。
- `relocation_progress`：汇总 `pending`、`migrated`、`failed` 三类逐对象进度。
- `reconciliation`：最终摘要/引用计数对账、不一致条目和首个不一致对象及所在区域。
- `initial_reconciliation`：初始放置后的对账基线。
- `distribution_comparison`：放置前、无约束偏好、容量指派后、时间线结束后的区域对象数、承载字节、越限变化和加权满足率。
- `execution_audit`：迁移进度、失败时源引用保留、最终引用清单与指派一致性。
- `region_reference_lists`：各区域逐对象引用、摘要、引用计数和字节数。

## 测试与验收

```bash
python3 -m unittest discover -s tests -v
```

测试使用手工构造的小型区域与对象清单，独立核算：

- 急缓权重与时延得分的精确数值。
- 合规、时延和健康过滤及逐项落选原因。
- 贪心容量迁出、预测水位和仍然越限时的迁出集合。
- 同刻事件排序、引用计数递减和归零移除。
- 健康降级后未受影响对象保持不变，受影响对象与全量重解逐对象一致。
- 迁移目标在迁移途中降级时的失败原因码。
- 加权时延满足率。
- 摘要篡改和引用计数差异的首个不一致定位。
- 非法清单拒绝处理、CLI 退出码和报告生成。
- 固定种子样本的可重复性。

固定种子并不表示输出永远与仓库内报告逐字节相同；实现升级时报告结构或算法描述可能变化。样本生成本身和同一版本内的规划结果都是确定性的。
