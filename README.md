# 航班保障推理编译缓存（flight_compile_cache）

面向航空公司航班保障调度平台的本地可复现编译缓存组件。项目只使用 Python 3
标准库，不依赖第三方包、数据库或外部服务。

演示负载包含两类模型：

- `delay_predict`：延误预测，输入形状随当日航班数 `B` 变化。
- `resource_alloc`：地面保障资源分配，输入形状同时随航班数 `B` 与保障任务数
  `T` 变化。

组件把“模型版本 + 规范化输入张量形状”编码为编译键。缓存命中时直接复用编译
产物；未命中时只编译受具体形状影响的算子，形状无关算子复用已有产物。LRU
容量、模型版本更新、形状突变、全量编译数值基准和快照恢复都在同一套确定性
工程与请求序列上验证。

## 快速开始

```bash
# 一键生成工程、运行主负载、验证重启恢复、运行边界样本
python3 main.py demo

# 也可以分步执行
python3 main.py generate --out-dir artifacts/generated
python3 main.py validate --project artifacts/generated/project.json \
  --out artifacts/generated/validation.json
python3 main.py run --project artifacts/generated/project.json \
  --out-dir artifacts/run
python3 main.py edge --project artifacts/generated/edge_project.json \
  --out-dir artifacts/edge
python3 main.py snapshot-check \
  --project artifacts/generated/project.json \
  --snapshot artifacts/run/cache_checkpoint.json
```

也可以使用模块入口：

```bash
python3 -m flight_compile_cache demo
```

`python3 main.py` 无子命令时等价于 `python3 main.py demo`。

## 输出文件

`demo` 默认写入 `artifacts/demo/`：

```text
artifacts/demo/
├── generated/
│   ├── project.json          # 主推理工程（含请求和模型更新事件）
│   ├── edge_project.json     # 图引用缺失、非正维度、张量不闭合等边界工程
│   └── manifest.json         # 固定种子生成清单与解析真值摘要
├── run/
│   ├── report.json           # 逐请求记录、逐出、更新、命中率、数值基准
│   ├── request_log.jsonl     # 逐请求 JSONL
│   ├── event_log.jsonl       # 更新事件 JSONL
│   ├── cache_checkpoint.json # 中断点快照（用于重启恢复验证）
│   ├── cache_final.json      # 完整运行结束快照
│   ├── latest_snapshot.json  # 最近一次完整运行快照
│   └── restart_verification.json
└── edge/
    ├── edge_report.json
    └── edge_request_log.jsonl
```

快照使用“临时文件 + `os.fsync` + `os.replace`”整体替换，读取方不会看到半写
状态。

## 命令行退出码

- `generate`、`validate`、`snapshot-check`：成功为 `0`。
- `run`、`edge`、`demo`：所有验收检查通过为 `0`，否则为 `1`。
- `validate` 的主工程允许包含预期内的坏请求；只要工程级结构和事件定义没有
  致命错误，`executable` 为 `true`，退出码为 `0`。坏请求会在结果中给出业务
  错误码并被运行时跳过。

## 工程结构

```text
flight_compile_cache/
├── generator.py      # 固定种子工程/请求/事件/解析真值生成器
├── graph.py          # 工程解析、图引用检查、拓扑排序、形状闭合、版本 diff
├── shapes.py         # 符号与具体形状推导、广播、输入符号绑定
├── tensors.py        # 标准库张量运行时与逐元素误差比较
├── keys.py           # 规范化编译键
├── compiler.py       # 单元产物编译、全量编译基准、专门化执行
├── cache.py          # LRU 缓存、版本失效、增量重编译、快照
├── verification.py   # 请求序列推进、边界样本、重启一致性验证
├── validation.py     # 对外校验接口
└── cli.py            # 命令行入口
tests/                # 22 个 unittest 回归测试
```

## 关键设计

### 1. 编译键

输入形状按输入名排序，维度按原顺序规范化。编译键覆盖模型 ID、模型版本和全部
输入形状：

```text
payload = {
  "schema": "flight-compile-key/v1",
  "model_id": "...",
  "model_version": "...",
  "input_shapes": [["flight_features", [3, 12]], ...]
}
compile_key = "ck_" + sha256(canonical_json(payload))[:24]
```

因此：

- 同模型、同版本、同规范化形状 -> 相同键。
- 形状或版本任一变化 -> 不同键。
- 输入字典顺序不影响键。

键首次出现时，`key_registry` 记录 `first_seen_request`、
`first_registration_source`、首次登记时刻和首次产物标识。来源可能是
`request:<id>` 或 `event:<id>`；快照恢复后继续保留。

### 2. 形状专门化单元与产物标识

计算图按算子单元编译：

- 形状敏感算子：`matmul`、`reduce_sum`、`reshape`、`transpose`、`concat`、
  `slice`、`softmax`。其产物包含具体输入/输出形状及轴、归约维、转置顺序等
  静态内核参数。
- 形状无关算子：常量、逐元素算术、`relu`、`scale` 等。其静态内核不包含具体
  请求形状，可在不同形状和未变更版本之间复用。

完整编译键不能被形状无关单元直接使用，否则新形状必然产生新标识，无法复用。
实现中把完整键投影为**单元键**：

```text
unit_descriptor = 模型 ID + 单元 ID + 算子 + 结构摘要 (+ 形状敏感单元的具体形状)
unit_key = "uk_" + sha256(unit_descriptor)[:24]
artifact_id = "art_" + sha256({unit_key, unit_id, artifact_abi})[:24]
```

结构摘要递归包含上游结构和静态参数。于是：

- 编译键命中/逐出后重编译同一键 -> 同一产物标识。
- 新形状下形状无关单元 -> 沿用原产物标识。
- 模型更新未影响的单元 -> 沿用原产物标识。
- 更新影响到的单元 -> 结构摘要变化，产生新产物标识。

每个请求记录 `compile.unit_reports`，逐单元给出 `scope`、`action`、
`artifact_id`、`compile_work` 和 `compile_ms`。

### 3. LRU 与逐出

`cache_config.capacity` 表示可驻留的编译键条目数。每次命中移动到 LRU 尾部；
未命中新增条目后若超限，从头部逐出。逐出记录包含：

- 被逐出的 `compile_key`、模型、版本、形状段；
- 该键的完整 `key_subgraph_scope`；
- 实际释放的 `released_subgraph_scope` 和 `freed_artifact_ids`；
- 与其他键共享而未释放的单元；
- 逐出后的缓存大小与策略。

产物注册表按引用检查释放：只要其他缓存键仍引用同一产物，就不会被删除。再次
请求被逐出的键会重新编译；`key_registry` 中记录
`artifact_ids_repeatable=true/false` 并保留首次产物标识，用于复核一致性。

### 4. 模型版本更新

生成器在 `t=120` 更新 `delay_predict:v1 -> v2`，只改变 `delay_mean` 的静态
缩放参数；在 `t=260` 更新 `resource_alloc:v1 -> v2`，只改变
`resource_task_norm`。

事件处理流程：

1. 对该模型全部旧编译键标记 `invalidated` 并从驻留缓存移除，其他模型条目不动。
2. 比较新旧计算图，得到 changed/affected/unaffected 单元集合。
3. 对更新前已有缓存形状立即增量重编译：受影响单元生成新产物，未受影响单元
   沿用旧产物标识。
4. 若事件发生时该模型尚无缓存形状，则记录 `applied_deferred`：增量范围已确定，
   下一次具体请求到达时执行。

事件记录包含 `incremental_recompile_scope`、逐键重编译范围、重编译耗时、
`unaffected_artifact_ids_stable` 和 `other_model_keys_preserved`。

### 5. 形状专门化统计

请求记录同时给出：

- `shape_specialization_events`：请求级未命中次数；
- `request_novel_key_compiles`：首次出现形状键的编译次数；
- `request_recompiles_after_eviction`：逐出后重编译次数；
- `shape_specialized_kernel_compiles`：实际生成的形状专用内核数；
- 按 `model@version` 与 `B=...,T=...` 形状段聚合的键数和请求次数。

验收会把请求序列中唯一编译键数量、形状段分布与生成器真值比较。

### 6. 全量编译数值基准

`FullCompileBaseline` 对每个有效请求重新编译完整计算图，不读取编译缓存。专门化
路径使用缓存中的单元产物执行。两条路径的输出按输出名逐元素比较：

- 默认容差来自 `cache_config.numeric_tolerance`；
- 每请求记录 `max_abs_error`、`max_location`、超差元素位置和值；
- 超差时在 `mismatch` 中给出请求 ID、输出名、扁平索引和多维索引；
- 汇总中给出全量编译工作量、专门化编译工作量、节省量和降低比例。

### 7. 快照与恢复

快照包含：

- 编译键条目及 LRU 顺序；
- 产物注册表和键映射；
- 驻留模型运行时版本；
- 命中/编译/逐出统计、事件日志、计数器；
- 工程指纹和各模型版本图指纹。

恢复流程：

1. 校验快照 schema 和内容哈希 `snapshot_id`。
2. 逐条目对照当前工程图指纹；图已变化、版本被删除或模型被删除的条目直接
   失效并报告 `E_SNAPSHOT_VERSION_CHANGED` 原因。
3. 从条目重建产物注册表，不触发全量编译。
4. 恢复运行时模型版本、LRU 顺序和统计。

`run` 会在逻辑时间轴中段写入检查点快照，继续跑完剩余序列，然后创建全新引擎
从检查点恢复并重放剩余序列。逐请求比较编译键、命中判定、产物标识和逐出决策，
结果写入 `restart_verification`。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_PROJECT_SCHEMA` | 工程顶层结构错误 |
| `E_MODEL_NOT_FOUND` | 请求或事件引用未知模型 |
| `E_MODEL_VERSION_MISSING` | 模型版本清单/图缺失 |
| `E_VERSION_NOT_FOUND` | 请求或事件引用未知版本 |
| `E_STALE_VERSION` | 版本更新后仍请求旧版本 |
| `E_GRAPH_SCHEMA` | 图结构字段错误 |
| `E_GRAPH_DUPLICATE_NODE` | 节点 ID 重复或与输入同名 |
| `E_GRAPH_REFERENCE_MISSING` | 算子引用不存在的张量 |
| `E_GRAPH_CYCLE` | 图存在依赖环 |
| `E_GRAPH_OUTPUT_MISSING` | 图输出未定义 |
| `E_OP_UNKNOWN` / `E_OP_UNSUPPORTED` | 未知/不支持算子 |
| `E_OP_SHAPE_NOT_CLOSED` | 算子声明输出与推导形状不闭合 |
| `E_INVALID_DIMENSION` | 张量维度非正或不是整数 |
| `E_CONST_SHAPE_MISMATCH` | 常量值形状非法 |
| `E_REQUEST_SCHEMA` | 请求字段结构错误 |
| `E_REQUEST_DUPLICATE_ID` | 请求 ID 重复 |
| `E_MISSING_INPUT` | 请求缺少图输入 |
| `E_UNKNOWN_INPUT` | 请求包含图外输入 |
| `E_INPUT_RANK_MISMATCH` | 请求输入秩与图声明不一致 |
| `E_INPUT_SHAPE_MISMATCH` | 静态维度不匹配 |
| `E_SHAPE_SYMBOL_CONFLICT` | 同一符号被绑定为不同值 |
| `E_EVENT_INVALID` | 不支持的事件类型 |
| `E_SNAPSHOT_SCHEMA` | 快照 schema 不支持 |
| `E_SNAPSHOT_INTEGRITY` | 快照内容哈希不匹配 |
| `E_SNAPSHOT_PROJECT_MISMATCH` | 快照与工程不匹配 |
| `E_SNAPSHOT_VERSION_CHANGED` | 恢复时检测到图/版本变化 |

异常请求会写入 `status=rejected`、`skipped=true`、`error_code` 和
`expected_match`，不会进入缓存查询或全量编译基准。

## 复现与测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 生成器同种子完全一致、不同种子不同；
- 编译键稳定性与版本/形状区分性；
- 形状无关产物跨形状复用、未受影响产物跨版本保持标识；
- 缓存命中、LRU 顺序、逐出后重编译标识一致；
- 模型更新增量范围、其他模型产物保留；
- 全量基准逐元素比较与超差定位；
- 快照原子替换、完整性校验、版本变化失效；
- 检查点恢复后命中/产物/逐出决策一致；
- CLI 生成、校验、运行、边界样本与快照检查全链路。

编译耗时是确定性的“编译工作量 × `ms_per_work_unit`”模型时间，便于在固定种子
下逐字节复现；它不依赖机器当前负载。实际数值推理和全量基准仍在本地执行。
