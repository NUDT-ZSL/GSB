# 技术设计

## 1. 管线

```text
JSONL 查询 / v1|v2|v3 模板 / 槽位类型
        |
        v
方言分派读取器 -> ParsedTemplateSet
        |
        v
统一 IR 编译器（规则/槽位/条件/引用/来源定位/补全项）
        |
        v
条件展开 + 槽位约束 + 引用深度检查
        |
        v
候选打分与确定性排序
        |
        +--> 跨方言语义裁决
        +--> 独立验证器
        +--> 快照版本管理
```

核心包位于 `src/qrw/`：

| 模块 | 职责 |
| --- | --- |
| `readers.py` | 版本/字段集分派，格式与定位错误 |
| `compiler.py` | 统一 IR、节点 ID、来源追溯、配置校验 |
| `expansion.py` | 正则模式匹配、槽位填充、条件分支、引用递归、码点偏移 |
| `scoring.py` | 分数明细、重算公式、排序 |
| `migration.py` | v1/v2 双向迁移、v2->v3 升级、v3->v1 显式丢弃 |
| `equivalence.py` | 三方言候选与结构能力裁决 |
| `validator.py` | 独立复算与 `E_INCONSISTENT` 首个差异 |
| `snapshot.py` | 校验和、版本表、整体替换、回退/退休拒绝 |
| `pipeline.py` | 统一演示报告与注入异常 |
| `interactive.py` | 本地交互应用 |

## 2. 统一 IR

每个规则包含：

- `rule_id/weight/enabled/max_depth/priority`
- `pattern` 与 `replacement` 的序列节点
- `slots` 槽位声明
- `source`：方言、源文件、行号、列号、字段
- `fills`：v1 推断或编译器默认值的补全项

节点类型：

- `rule`
- `sequence`
- `text`
- `slot`
- `condition`，含 `then/else` 子序列
- `reference`，指向 fragment
- `slot_declaration`

节点 ID 由方言、源文件、IR 路径、节点类型和源行规范化后取 SHA-256 前缀，因此同一输入和配置下逐字节稳定。编译器完成后遍历所有带 `kind/node_id` 的节点，确认每个节点都有源行。

v1 没有槽位声明，读取时按 pattern/replacement 中的占位符出现顺序推断，槽位名到类型的对应关系由 `CompilerConfig.slot_name_types` 提供。每个推断项写入 `fills`，并保留源规则行。

## 3. 展开

模式匹配将 pattern 编译为完整匹配正则：

- 文本节点做正则转义
- 槽位节点只接受受控类型值，候选值按“长度降序、字典序升序”生成
- 每个捕获组保留查询文本的 Python 码点区间 `[start, end)`
- replacement 中槽位复制值，并输出 `output_start/output_end/query_start/query_end`

条件节点支持 `equals/not_equals/in`。引用节点递归渲染 fragment；直接规则深度为 1，每次引用加 1。满足以下任一条件返回 `E_CONFIG_INVALID`：

- 引用深度超过规则 `max_depth`
- 引用深度超过编译器 `max_depth_limit`
- 权重非正
- 规则 `max_depth` 不在允许范围

## 4. 打分与排序

```text
slot_points  = 0.25 * slot_hits
depth_penalty = 0.05 * (expansion_depth - 1)
score = weight + slot_points - depth_penalty
```

分数与各项均保留 6 位小数。`score_detail` 明确记录公式和所有输入，验证器使用 NumPy 数组独立重算。

排序键：

1. 分数降序
2. 源规则行号升序
3. 候选串字典序升序
4. 完全相同时再按规则 ID/候选 ID 稳定排序

## 5. 迁移策略

### v1 -> v2

- 保留 pattern/replacement 原文
- 补全 slots 声明
- 补全 `weight=1.0`、`enabled=true`、`max_depth=1`
- 每个补全项记录 `rule_id/source_line/field/default/origin`

### v2 -> v1

- 槽位声明展平为隐式占位符，文本不重写
- 记录被丢弃的类型约束
- 显式记录 `weight/enabled/max_depth` 的丢弃明细

### v2 -> v3

- slots 转为结构化声明
- replacement 文本拆为 text/slot 节点，拼接后与原文逐字符一致
- 补全空的 `conditions/references`，记录升级补全项

### v3 -> v1

- 包含条件分支或引用的规则不可无损降级，整条规则显式丢弃
- fragment 显式丢弃
- 丢弃项列出类别、策略、规则、源行和不可表示节点类型

## 6. 快照

快照清单包含：

- `version`
- `set_id`
- `dialect_files`：v1/v2/v3 源文件
- `slot_types_file`
- 内容校验和

校验和覆盖规范化后的 `{version,set_id,slot_types,dialects}`。加载时重新计算；不一致返回 `E_SNAPSHOT_CORRUPT`。

`SnapshotManager.install` 规则：

- 新版本 < 当前版本：`E_SNAPSHOT_ROLLBACK`
- 新版本与校验和相同：幂等，不替换
- 版本更高或同版本校验和变化：整体替换，`incremental=false`
- 传入旧编译 IR 时，先检查新槽位类型集合是否覆盖所有已编译槽位
- 若收缩导致槽位退休：`E_SLOT_TYPE_RETIRED`，且当前快照保持不变

## 7. 等价裁决与验证

裁决器比较每种方言、每个查询的：

- 候选串
- `score_detail`
- `offset_map`

同时检查 v3 条件/引用能力是否超出 v1，以及规则槽位签名是否一致。结构能力不足或候选不一致时返回 `E_SEMANTIC_DRIFT`，并给出不可还原类别与首个差异。

验证器独立执行以下检查：

- 三方言候选集合相同
- 三方言候选顺序相同
- 偏移映射相同
- 分数可由明细重算
- 补全项有规则、源行、来源和默认值

任一失败返回 `E_INCONSISTENT`，并给出 `inconsistent_category` 与 `first_difference_position`。

