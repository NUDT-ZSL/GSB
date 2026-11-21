# 架构与算法说明

## 数据与确定性

样本生成器包含 `single_column`、`two_column`、`mixed_columns` 三类固定模板。画布固定为 `[1600, 1200]`，每个坐标仅受已声明的 PCG64 种子序列扰动。样本字段为：

```text
page_id, canvas_shape, blocks[]
block: block_id, x1, y1, x2, y2, block_type, text_len, reading_order_true
```

可选 `column_index/zone_id/nesting_path` 用于三格式视图；缺省时由归一化中心点和类型派生。`reading_order_true` 不参与几何、候选边、代价和求解缓存键，只参与指标节点。

规范化 JSON 使用：

- `sort_keys=true`
- 紧凑分隔符
- 8 位小数归一化，零统一为 `0.0`
- SHA-256 内容寻址

因此同一页、同一输入与配置可以逐字节复现归一化块和候选边。

## 候选边

对每对有序块计算：

- `same_column` / `cross_column`
- `vertical_adjacent`
- `cross_zone`
- `target_below` / `backward`
- `horizontal_overlap`
- 归一化垂直间距和中心差

候选选择顺序依次为：目标在下方、同栏、垂直间距、水平失配、水平中心差、目标 `block_id`。最后按来源 `block_id`、选择键、目标 `block_id` 全序排列。默认每块保留 16 条候选；边集合必须让每个块同时作为 source 和 target 出现。单块页生成显式覆盖自环，由求解器按单块路径处理。

资源策略触发时，退化为确定性基线链加闭环自环；该产物可能降低精度，但保持块全覆盖并在报告中标记降级类别。

## 代价

每条边记录五个分项：

```text
type_order_violation
column_transition
vertical_gap
horizontal_misalignment
cross_zone
```

总代价定义为按固定分项顺序求和并规范化到声明精度。类型违规单位由 `type_order` 排名差计算，再乘 `type_violation_penalty` 与 `weights.type_order_violation`。向更早列移动比向更晚列移动具有更高列惩罚；向上移动至少增加一个画布高度单位的垂直惩罚。

所有成本重算、顺序校验和自检都只使用这五个分项，不额外注入启发式偏置。

## 求解

设页面块数为 `n`，候选边作为有向图。Held-Karp 状态：

```text
dp[mask][last] = (minimum_cost, lexicographically_smallest_path_prefix)
```

从任意单块路径开始扩展到全掩码，最终在所有终点中按 `(总代价, block_id 序列)` 打破平局。输出后执行：

1. 序列长度等于块数。
2. 每个块恰出现一次。
3. 每对相邻块存在于候选代价边。
4. 总代价等于选中边总代价。
5. 每条边总代价等于分项和。

任何失败返回 `E_ORDER_INCONSISTENT` 和首个可定位块号。默认单页块上限为 14，确保精确动态规划驻留数据可控。

## 增量缓存

每个节点键：

```text
SHA256({
  node,
  page_id,
  dependencies,
  config
})
```

依赖粒度：

- `normalize`：页面块内容（排除 `reading_order_true`）。
- `edges`：归一化几何指纹，不包含块类型，因此类型-only 修正可复用边。
- `costs`：候选边内容指纹 + 类型指纹 + 代价配置。
- `solve`：代价边内容指纹。
- `metrics`：求解结果内容指纹 + 真值顺序指纹。

执行报告同时输出：

- `recomputed_nodes`
- `reused_nodes`
- `recomputed_pages`
- `reused_pages`
- `affected_pages`
- `geometry_affected_pages`
- `downstream_only_pages`
- 分区、无交集和受影响页覆盖检查

内容指纹相同即命中缓存，不依赖快照版本号本身，因此版本号递增但内容不变时仍可复用。

## 快照并发

`SnapshotStore.commit()` 获取非阻塞 stage lease：

1. 进程内 `threading.Lock.acquire(blocking=False)`。
2. 锁文件 `fcntl.flock(LOCK_EX | LOCK_NB)`。
3. 校验三个分区存在、版本为正、内容校验和一致。
4. 逐分区比较当前版本；旧版本或同版本不同内容直接拒绝。
5. 原子写入并通过 `os.replace` 发布整个快照。

`stage_lease()` 可用于显式占用提交窗口；占用期间后到提交立即返回 `E_STAGE_BUSY`。

## 自检覆盖

`run_self_check()` 生成并验证：

- DAG 无环及拓扑节点数一致。
- 五页基线候选覆盖、成本分项、单链和指标。
- 三格式视图解析后序列与块集合一致。
- v1 -> v3 补全记录、v3 -> v1 丢弃/展平明细、严格降级 drift 保护。
- 页面几何修正、权重调整、类型顺序变化三次增量。
- 每次增量与独立全量重算的内容指纹一致。
- 快照整体替换、回退、损坏、stage busy。
- 字段数、必填字段、非法编号、未知版本、非法权重、越界顺序、资源上限。
- 降级策略与类别标记。
- 两次固定种子独立基线报告哈希一致。

