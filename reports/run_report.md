# 运行报告：本地确定性候选匹配诊断工作台

日期：2026-11-22　环境：Python 3.10.12（本机无 3.11，代码兼容 3.10+）+ NumPy 1.26.4，无网络、无 GPU、无容器。

## 1. 确定性样本与逐字节复现

- 命令：`python3 scripts/gen_all.py data`（等价 `python3 -m detwb.workbench --data data gen`）
- 产出：`data/manifest.jsonl`（12 样本，固定种子 20261122，64×64×3 RGB 由 `(seed, index)` 确定性重建并校验 sha256）、`data/vocab_v1.json` / `data/vocab_v2.json`（v2 停用类别 3 bicycle、更名 5 bag→handbag、新增 6 robot）、`data/inject/`（注入清单与损坏样例）。
- 两次独立生成+运行的 `matches_v1.txt` / `matches_v2.txt` / `matches_v3.json` / `run_result.json` 经 `cmp` 比较**逐字节一致**（不依赖字典遍历顺序与系统时间）。

## 2. 匹配计算图与统计

- 命令：`python3 -m detwb.workbench --data data run`
- 结果：词表 v1、IoU 阈值 0.5，记录 58 条，TP=25 FP=33 FN=12；节点拓扑 `ingest -> group -> iou -> assign -> stats -> aggregate`。
- 阈值敏感性：阈值 0.7 时 TP=20 FP=38 FN=17（单调性由测试 `test_threshold_monotonic_tp` 保障）。

## 3. 三格式读取、迁移与跨格式裁决

- `view --format v1|v2|v3` 可切换三种视图；读取器按格式头/字段集分派。
- 迁移：v1→v2/v3 补全类别名（来源 vocab）、难度/遮挡/置信区间默认值（来源 default），逐项记录补全来源；v3→v1 按显式丢弃规则（`DROP_V1_INEXPRESSIBLE` 等）输出逐字段丢弃明细并标注可还原性；可表达字段取值不变（`expressible_fields_unchanged` 校验）。
- 裁决：`adjudicate` 产出裁决 `9ed73e451bab979e`，verdict=DRIFT，`E_SEMANTIC_DRIFT` 不可还原字段 `[conf_hi, conf_lo, difficulty, occluded]`（181 条漂移明细）；共享语义（候选指派、TP/FP/FN 集合、按类聚合）三格式完全一致。v1 不可表达字段为默认值时 verdict=EQUIVALENT（测试覆盖）。
- 复现：报告与输入侧车落盘 `data/out/adjudications/`；在后续运行覆盖输出文件后，`replay 9ed73e451bab979e` 仍逐字节复现一致。

## 4. 词表版本演进

- `vocab-demo` 实测：加载 v1→v2 整体替换（校验和 `74dc5704c455`→`0e49489a83fa`，retired=[3]）；回退 v1 被拒绝 `E_SNAPSHOT_ROLLBACK`；停用类别命中返回 `E_LABEL_RETIRED`；显式回退 `3 -> 0(person)` 并在记录中标记 `class_provenance=fallback:3->0`。

## 5. 缓存与增量失效

- 缓存键纳入图像内容哈希、候选/真值摘要、IoU 阈值、词表版本与上游节点哈希；同配置二次运行 IoU/assign 全部复用（`iou 复用12, assign 复用12`）。
- 阈值 0.5→0.6：重算 12 张（assign/stats），IoU 复用 12 张（IoU 键不含阈值）。
- 词表 v1→v2：重算 10 张、复用 2 张；两集合无交集且并集覆盖全部 12 张（`plan_invalidation` 内置断言，违反即 `E_INCONSISTENT`）。

## 6. 快照、并发与资源上限

- 快照携带单调递增版本号与内容校验和；篡改载荷后读取返回 `E_CACHE_CORRUPT`（测试覆盖）。
- 并发提交：持锁期间后到提交立即获得 `E_STAGE_BUSY`，不阻塞等待（测试覆盖）。
- 资源上限实测（`degrade-demo`）：驻留候选超限严格模式返回 `E_RESOURCE_LIMIT(category=resident_candidates)`；降级模式标记 `DEGRADE_CHUNKED_RESIDENT` 并照常产出 58 条记录。

## 7. 注入异常归因（`inject`，7/7 符合预期）

| 用例 | 期望 | 实际 | 归因 |
|---|---|---|---|
| unknown_version | E_FORMAT_VERSION_UNSUPPORTED | 同左 | 行号=1 字段=header |
| truncated_header | E_FORMAT_CORRUPT | 同左 | 行号=1 字段=header |
| field_count | E_FORMAT_CORRUPT | 同左 | 行号=2 字段=iou |
| missing_field | E_FORMAT_CORRUPT | 同左 | 行号=2 字段=iou |
| bad_number | E_FORMAT_CORRUPT | 同左 | 行号=2 字段=score |
| retired_label | E_LABEL_RETIRED | 同左 | 字段=class_id |
| resource_limit | E_RESOURCE_LIMIT | 同左 | 类别=images |

## 8. 一致性自检（`selfcheck`，全部通过）

- `cand_tp_fp_disjoint`：任一候选不得同时计入 TP 与 FP。
- `gt_tp_fn_disjoint`：任一真值框不得同时计入 TP 与 FN。
- `dag_acyclic`：计算图顶点无环（Kahn 拓扑排序）。
- `cache_hash_consistent`：缓存键与产物哈希逐条互相核对。

## 9. 测试

- `python3 -m unittest discover -s tests`：**39 个测试全部通过**（格式读写、迁移与裁决、匹配确定性与聚合守恒、词表演进、缓存键与失效、快照并发与资源上限、自检篡改检测）。
