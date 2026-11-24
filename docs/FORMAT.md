# 输入、状态与快照格式

所有 JSON 均使用 UTF-8；数值必须为有限数；摘要采用规范化 JSON（键排序、无多余空白）的 SHA-256，并带 `sha256:` 前缀。

## 1. 回放输入 `anim-replay-input-v1`

```json
{
  "format": "anim-replay-input-v1",
  "seed": 20261125,
  "skeleton": {
    "bones": [
      {
        "name": "root",
        "parent": null,
        "rest": {
          "translation": [0.0, 0.0, 0.0],
          "rotation": [0.0, 0.0, 0.0, 1.0],
          "scale": [1.0, 1.0, 1.0]
        }
      }
    ]
  },
  "clips": [
    {
      "id": "idle",
      "duration_ms": 960,
      "loop_count": 0,
      "tracks": {
        "hips": {
          "translation": [{"t": 0, "value": [0.0, 1.0, 0.0]}],
          "rotation": [{"t": 0, "value": [0.0, 0.0, 0.0, 1.0]}],
          "scale": [{"t": 0, "value": [1.0, 1.0, 1.0]}]
        }
      }
    }
  ],
  "parameters": {
    "speed": {
      "default": 0.0,
      "track": [{"t": 0, "value": 0.0}, {"t": 320, "value": 0.8}]
    }
  },
  "state_machine": {
    "initial_clip": "idle",
    "transitions": [
      {
        "id": "idle_to_walk",
        "from": "idle",
        "to": "walk",
        "when": [{"param": "speed", "op": ">=", "value": 0.5}],
        "priority": 10,
        "interrupt": true,
        "loop_count": 0,
        "min_hold_ms": 0,
        "on_finish": false
      }
    ]
  },
  "timeline": {
    "duration_ms": 2400,
    "frame_step_ms": 16,
    "chunk_ms": 96
  }
}
```

### 骨架

- 骨骼名非空且唯一，不允许 `.`（v1 扁平键使用点号分隔）。
- `parent` 必须是已声明骨骼或 `null`；父链必须无环，可拓扑排序。
- `rest.translation` 和 `rest.scale` 为 3 分量有限向量；`rest.rotation` 为 4 分量有限非零四元数 `[x,y,z,w]`。
- `rest.scale` 可省略，默认 `[1,1,1]`。

### 片段与轨道

- 片段 `id` 唯一，`duration_ms > 0`，`loop_count >= 0`（0 表示无限循环）。
- 每个骨骼通道只支持 `translation`、`rotation`、`scale`。
- 关键帧 `t` 为非负整数，片段内严格递增且不超过 `duration_ms`。
- 每个通道的值是绝对局部 TRS 分量，不是相对 rest 的增量；未提供通道时使用 rest。
- 旋转值必须是非零有限四元数，采样时归一化。

### 参数与转移

- 参数类型可为有限数、布尔或字符串；轨道值类型必须与 `default` 一致。
- 数值参数在轨道键之间线性采样；布尔/字符串在键之间保持左侧值。
- 条件算子：`< <= > >= == !=`。有序比较要求参数和阈值都是数值。
- `when` 中的条件为逻辑 AND；条件引用的参数必须已声明。
- 来源可为片段 id 或 `"*"`，也可是片段 id 数组。
- `loop_count` 覆盖目标片段的默认循环次数；未给出时用目标片段值。
- `on_finish=true` 要求当前片段已完成目标循环次数。
- `interrupt=false` 只在片段完成后允许；`interrupt=true` 在满足 `min_hold_ms` 后随时可打断。
- 多个候选规则按 `(priority, id)` 取最大，确保同一逻辑时刻最多一条转移。

### 时间轴

- `duration_ms` 必须是 `frame_step_ms` 的整数倍，帧包含 `0` 和 `duration_ms`。
- `chunk_ms` 必须是 `frame_step_ms` 的整数倍。

## 2. 编辑脚本 `anim-replay-edits-v1`

```json
{
  "format": "anim-replay-edits-v1",
  "edits": [
    {
      "op": "set_keyframe_value",
      "clip": "walk",
      "bone": "chest",
      "channel": "rotation",
      "t": 200,
      "value": [0.0, 0.0, 0.0, 1.0]
    }
  ]
}
```

支持的顺序操作：

| `op` | 必填字段 | 说明 |
|---|---|---|
| `set_keyframe_value` | `clip,bone,channel,t,value` | 修改已有关键帧值 |
| `add_keyframe` | `clip,bone,channel,t,value` | 新增关键帧；同刻不得重复 |
| `delete_keyframe` | `clip,bone,channel,t` | 删除已有关键帧 |
| `move_keyframe` | `clip,bone,channel,t,new_t` | 移动关键帧时间 |
| `set_clip_duration` | `clip,duration_ms` | 修改片段时长 |
| `set_clip_loop_count` | `clip,loop_count` | 修改片段默认循环数 |
| `set_transition_condition` | `transition,when[,on_finish]` | 修改转移条件 |
| `add_transition` | `transition` | 添加完整转移对象 |
| `delete_transition` | `transition` | 删除转移 |

每一步应用后都重新执行完整 schema 校验；非法编辑返回 `E_SCHEMA`，且不产生任何回放输出文件。

## 3. v1 快照 `anim-replay-snapshot-v1`

```json
{
  "format": "anim-replay-snapshot-v1",
  "created_generation": 0,
  "canonical_clip_map": {"idle": "anim://clip/idle"},
  "clip_origins_ms": {"idle": 0},
  "frames": [
    {
      "clip": "idle",
      "relative_s": 0.016,
      "state_generation": 0,
      "bones": {
        "hips.translation": [0.0, 1.0, 0.0],
        "hips.rotation": [0.0, 0.0, 0.0, 1.0],
        "hips.scale": [1.0, 1.0, 1.0]
      }
    }
  ],
  "frame_count": 1,
  "content_digest": "sha256:..."
}
```

- 绝对逻辑毫秒 = `clip_origins_ms[clip] + round(relative_s * 1000)`。
- `relative_s` 必须为有限非负数，换算结果用于与 v2 合并。
- `content_digest` 是对除该字段外整个 JSON 对象规范化后的摘要。
- `frame_count` 必须等于 `frames` 实际长度。

## 4. v2 快照分片

目录结构：

```text
v2/
  meta.sqlite
  shards/
    g1_idle.json
    g2_idle.json
    ...
```

分片：

```json
{
  "format": "anim-replay-snapshot-v2-shard",
  "snapshot_generation": 2,
  "shard_id": "g2_idle",
  "canonical_clip_id": "anim://clip/idle",
  "kind": "live",
  "records": [
    {
      "canonical_clip_id": "anim://clip/idle",
      "logical_ms": 0,
      "state_generation": 0,
      "tombstone": false,
      "bones": {"root.translation": [0.0, 0.0, 0.0]}
    }
  ],
  "frame_count": 1,
  "content_digest": "sha256:..."
}
```

SQLite 表：

```sql
CREATE TABLE shards(
  shard_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  snapshot_generation INTEGER NOT NULL,
  digest TEXT NOT NULL,
  frame_count INTEGER NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  canonical_clip_id TEXT NOT NULL
);

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

`meta` 中保存规范化元数据体的 `meta_digest` 和 `meta_body`。加载时同时验证：

- `meta_digest` 存在且匹配；
- SQLite 行与 `meta_body` 一致；
- 每片自己的 `content_digest` 存在且与 SQLite 记录一致；
- 分片、SQLite 两处声明帧数均等于实际记录数；
- 分片路径不逃逸快照目录。

某一 `snapshot_generation` 中任一分片失效时，整代不再参与合并；报告 `fallback_generation` 为最近完整有效代。禁止静默使用损坏数据。

## 5. 合并裁决

记录键为 `(canonical_clip_id, floor(logical_ms / bucket_ms))`。候选排序从高到低：

1. `state_generation` 更大；
2. 同代数时墓碑优先于无墓碑；
3. 绝对逻辑毫秒更晚；
4. v2 优先于 v1；
5. v2 的 `snapshot_generation` 更大；
6. 路径和记录索引的稳定字典序。

每个非胜出且内容不同的候选产生一条冲突项，包含：

- 规范片段标识、逻辑毫秒、裁决依据；
- 胜出/失败来源的版本、分片文件、记录位置；
- `bone_channel` 首差异通道，以及 `differing_channels` 全部通道及双侧值。

## 6. 持久化块 `anim-replay-chunk-v1`

```json
{
  "format": "anim-replay-chunk-v1",
  "version_id": "sha256:...",
  "chunk_index": 0,
  "logical_start_ms": 0,
  "logical_end_ms": 96,
  "state_at_start": {
    "active_clip": "idle",
    "enter_ms": 0,
    "target_loop_count": 0,
    "generation": 0
  },
  "state_at_end": {"...": "..."},
  "frame_count": 6,
  "frames": [
    {
      "t": 0,
      "clip": "idle",
      "local_ms": 0,
      "state_generation": 0,
      "bones": {"...": "..."},
      "dependencies": {
        "tracks": [
          {"bone": "chest", "channel": "rotation", "lo_ms": 0, "hi_ms": 240}
        ]
      }
    }
  ],
  "events": [
    {
      "type": "transition",
      "t": 432,
      "rule_id": "idle_to_walk",
      "from": "idle",
      "to": "walk",
      "priority": 10,
      "state_generation": 1
    }
  ],
  "dependencies": {
    "clips": {"idle": {"frames": [0], "local_min_ms": 0, "local_max_ms": 0}},
    "tracks": [
      {
        "clip": "idle",
        "bone": "chest",
        "channel": "rotation",
        "intervals": [[0, 240]],
        "frames": [0],
        "local_min_ms": 0,
        "local_max_ms": 0
      }
    ],
    "bones": ["chest"],
    "transition_rules": [
      {
        "id": "idle_to_walk",
        "source": ["idle"],
        "target": "walk",
        "priority": 10,
        "frames": [0],
        "selected": false
      }
    ],
    "selected_transition_rules": []
  },
  "content_digest": "sha256:..."
}
```

状态目录另有 `manifest.json`，记录版本标识、帧步长、块大小、最后提交块、帧数和事件数。恢复时只接受从块 0 开始、摘要和边界状态均有效的连续前缀。

## 7. 客户端缓存 `anim-replay-client-cache-v1`

```json
{
  "format": "anim-replay-client-cache-v1",
  "version_id": "sha256:...",
  "frames": [
    {
      "t": 0,
      "clip": "idle",
      "state_generation": 0,
      "bones": {"root.translation": [0.0, 0.0, 0.0]}
    }
  ],
  "applied_event_count": 1,
  "events": []
}
```

重连时先校验重叠帧和已应用事件前缀。无偏差时，操作序列为所有缺失帧按 `t` 升序的 `fill_pose`，之后才是按序号的 `replay_event`。

## 8. 错误结构

```json
{
  "ok": false,
  "error": {
    "code": "E_REPLAY",
    "message": "client cache diverges from authoritative replay",
    "details": {
      "first_mismatch": {
        "logical_ms": 800,
        "clip": "run",
        "bone_channel": "hips.rotation",
        "component": 0,
        "reason": "pose_tolerance",
        "expected": 0.0,
        "actual": 0.25,
        "abs_diff": 0.25
      }
    }
  }
}
```

