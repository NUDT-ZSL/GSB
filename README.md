# 本地 CI/CD 流水线制品缓存管理器

这是一个仅使用 Python 标准库实现的离线缓存管理器，用于合并两代 CI/CD 缓存清单、裁决制品是否可复用、沿流水线依赖传播失效，并在进程被强制终止后恢复未完成事务。实现不依赖网络、Docker 或任何第三方运行时包。

## 环境与快速开始

目标环境为 Windows + Python 3.11 或以上，并确认标准库 `sqlite3` 可用。本仓库在 Python 3.10.12、SQLite 3.37.2 上完成实际验证，代码只使用 3.10+ 标准库能力，可直接在 3.11/3.12 上运行。

```bat
py -3.11 -m unittest discover -v
py -3.11 scripts\run_demo.py
```

Linux/macOS 对应命令：

```sh
python3 -m unittest discover -v
python3 scripts/run_demo.py
```

演示脚本会自动清空 `.demo/state`，连续运行两遍并比较完整输出字节。固定样本也可手动执行：

```bat
py -3.11 -m cacheman --state-dir .cacheman merge ^
  --v1 samples/v1/cache.json ^
  --v2 samples/v2/meta.sqlite3 ^
  --now-ms 1760000200000

py -3.11 -m cacheman --state-dir .cacheman judge ^
  --pipeline samples/pipelines/app-ci.json ^
  --pipeline samples/pipelines/docs-ci.json ^
  --now-ms 1760000200000
```

`python -m cacheman` 不要求先安装项目。也可以在联网前准备本地 setuptools wheel 后执行 `pip install --no-index .`，项目自身没有运行时依赖。

## 目录

- `cacheman/v1.py`、`cacheman/v2.py`：两代清单解析、摘要和分片校验。
- `cacheman/indexer.py`：确定性跨版本合并、冲突裁决、迁移记录、统一索引。
- `cacheman/engine.py`：复用裁决、替换分类、失效闭包、恢复和验证。
- `cacheman/store.py`：SQLite 快照、WAL、暂存表、文件锁、幂等存储。
- `cacheman/pipeline.py`：流水线 DAG 校验和稳定拓扑排序。
- `cacheman/cli.py`：`load/merge/judge/replace/resume/status/verify` 命令。
- `samples/`：固定 v1、v2、替换包、流水线和制品字节。
- `scripts/generate_samples.py`：确定性重建全部样本。
- `scripts/run_demo.py`：两轮演示及逐字节比较。
- `tests/`：非法清单、合并、裁决、替换、恢复、并发和可复现性测试。

## 输入格式

### cache-v1

`samples/v1/cache.json` 是单文件清单：

```json
{
  "format": "cache-v1",
  "manifest_id": "legacy-v1-main",
  "manifest_version": 1,
  "logical_clock_ms": 1760000100000,
  "artifacts": [
    {
      "task": "legacy/lint",
      "artifact": "lint.txt",
      "sha256": "sha256:...",
      "relative_written_seconds": 120,
      "size_bytes": 9,
      "content_path": "../artifacts/v1/lint.txt"
    }
  ],
  "task_artifacts": {
    "legacy/lint": ["lint.txt"]
  }
}
```

校验包括：`(task, artifact)` 唯一、摘要与文件字节一致、大小一致、相对秒数为非负整数、`task_artifacts` 与制品声明完全一致、相对换算后时间不为负。

### cache-v2

v2 由 `meta.sqlite3` 和分片 JSON 组成。SQLite 表为：

- `v2_meta`：格式、`manifest_id`、清单版本、逻辑时钟、缓存代数下限。
- `v2_generations`：缓存代数、创建毫秒、分片 ID/路径、分片原始 SHA-256、规范化内容 SHA-256。
- `v2_artifacts`：规范任务键、制品名、摘要集合、绝对写入毫秒、代数、命中标签、大小、内容路径。
- `v2_tombstones`：任务键、制品名、目标摘要、失效毫秒、墓碑代数。

分片文件名形如 `shards/shard-0007.json`，`content_sha256` 是对除该字段外整个分片对象做规范 JSON 后的 SHA-256。解析器同时校验文件原始哈希、规范内容哈希、分片与 SQLite 元数据逐字段一致、代数下限和创建时间单调、绝对时间为整数毫秒、墓碑目标和摘要存在且匹配。

### 流水线

```json
{
  "format": "cacheman-pipeline-v1",
  "pipeline_id": "app-ci",
  "pipeline_version": 1,
  "tasks": [
    {
      "task": "source/fetch",
      "depends_on": [],
      "artifacts": [{"name": "source.tar", "ttl_seconds": 3600}]
    },
    {
      "task": "app/build",
      "depends_on": ["source/fetch"],
      "artifacts": [{"name": "app.tar", "ttl_seconds": 3600}]
    }
  ]
}
```

任务键先做 NFC、反斜杠到 `/`、连续 `/` 折叠和路径段校验；v2 中必须已经是规范形式。DAG 必须在加载时证明无环，拓扑排序使用字典序稳定队列。

### 缓存包

`replace --package` 读取包描述：

```json
{
  "format": "cacheman-package-v1",
  "package_id": "replacement-cache-package",
  "manifest_version": 3,
  "cache_generation_floor": 9,
  "logical_clock_ms": 1760000300000,
  "sources": [
    {"type": "v1", "path": "../../v1/cache.json"},
    {"type": "v2", "path": "../../v2-replacement/meta.sqlite3"}
  ]
}
```

## 核心语义

### 时间迁移

v1 的相对秒数在注入逻辑时钟 `T` 下换算。CLI 以 `--now-ms` 注入 `T`；未指定时使用来源集合可推导的逻辑时钟。v1 清单自身的 `logical_clock_ms` 继续作为跨版本冲突排序依据，并同时记录在迁移上下文中。

```text
absolute_written_ms = T - relative_written_seconds * 1000
scale_ms_per_second = 1000
rounding = floor
```

每个 v1 条目保留原值，并记录迁移映射：统一代数 `0`、命中标签 `legacy-unknown`、换算标度和方向。v2 绝对毫秒保持原值，标度为 `1`、舍入为 `none`。复用有效期统一按 `absolute_written_ms + ttl_seconds * 1000` 计算，因此时间模式迁移不改变事实上的过期结论。

### 跨版本冲突

同一 `(task, artifact)` 的候选按以下稳定顺序取最大者：

1. 清单声明的逻辑时钟；
2. 统一缓存代数（v1 迁移为 0，v2 使用原值）；
3. 绝对写入时间；
4. 来源格式优先级（v2 高于 v1）；
5. 清单版本、来源 ID、摘要集合等稳定诊断字段。

索引条目保留 `source_version`、`source_id`、原始时间对象、`original_digests`、规范化 `digests` 和迁移精度标签。每个冲突输出 `merge_decisions`，每个 v1/v2 条目输出 `migration_decisions`。相同注入时钟和来源集合重复合并会命中幂等 `noop`，返回同一索引版本与摘要。

### 复用裁决

裁决按流水线拓扑序执行。任务先继承上游失败，再检查自身制品：

1. 任一依赖不可复用时，原因记为 `upstream_not_reusable`，路径从首个根因任务连续延伸到当前任务；
2. 否则按流水线中制品声明顺序检查缺失、墓碑、过期；
3. 全部通过才记为 `reusable`。

制品摘要与墓碑摘要精确匹配，过期边界为 `now_ms >= expires_at_ms`。输出包含每个任务的 `reason`、`first_trigger`、`dependency_path`、来源清单版本/代数、`relative_seconds_migrated` 或 `absolute_milliseconds` 精度标记。

任务裁决以语义输入哈希写入 `judgement_cache`。索引变化后，只有制品摘要/时间/墓碑或依赖结论变化的下降闭包需要重判；其余任务复用旧裁决，并在 JSON 中显示 `decision_reused=true` 与原 `cached_from_index_version`。

### 替换识别

`replace` 依次比较清单版本、缓存代数下限和分片哈希：

- 清单版本或代数下限回退：返回业务错误，不发布。
- 来源集合完全相同：`noop`。
- 新分片集合保留当前全部分片且代数范围可衔接：`incremental`，未在包中重发的旧来源继续保留。
- 新代数下限高于当前上限且分片完全不相交：`full_replacement`，旧来源整体退出快照。
- 其他删除/替换来源场景：`full_replacement`。

输出 `added_shard_hashes`、`removed_shard_hashes`、替换原因，以及每条流水线中由变化任务沿反向依赖边计算的精确失效闭包。

## 持久化、并发与恢复

状态目录默认 `.cacheman`，内含 `index.sqlite3` 和 `cacheman.lock`。数据库使用 WAL、`BEGIN IMMEDIATE`、`synchronous=FULL` 和跨平台文件锁。读者只读取 `current_index_version` 指向的完整已提交行。

发布流程：

1. 在 `pending_operations` 写入确定性的 operation ID、来源哈希、基线和目标版本。
2. 将完整快照、来源、条目、墓碑和裁决写入 `stage_*` 临时表。
3. 校验暂存行数、JSON 和双层摘要。
4. 在一个事务中写入版本化 `snapshots`/`snapshot_*`，更新快照指针和逻辑时钟水位，最后清理暂存行。

诊断参数 `--fault-point after_prepare|after_stage` 会在阶段提交后调用 `os._exit(75)`，用于模拟强制终止。`resume` 会：

- 对完整 `staged` 操作只执行发布，不重新计算；
- 对 `prepared` 操作从持久化来源重放未完成阶段；
- 若其他实例已提交同一输入，标记完成而不重复写入；
- 若指针已前进到其他输入，标记 superseded，绝不回退已提交版本；
- 清理无主暂存行。

并发合并同一缓存包时，文件锁和 operation ID 保证只产生一个快照；另一实例返回 `noop`。重复 `judge` 使用报告哈希和任务裁决主键保证幂等。

## CLI

```text
cacheman [--state-dir DIR] [--clock-tolerance-ms N] COMMAND ...

load     --v1 MANIFEST --v2 META_DB [--now-ms N]
merge    --v1 MANIFEST --v2 META_DB [--now-ms N]
judge    --pipeline JSON [--pipeline JSON ...] [--now-ms N]
replace  --package PACKAGE [--pipeline JSON ...] [--now-ms N]
resume
status
verify   [--v1 MANIFEST] [--v2 META_DB] [--pipeline JSON ...]
```

所有成功输出均为按键排序、固定缩进的稳定 JSON。失败输出：

```json
{
  "ok": false,
  "error": {
    "code": "CACHE_ARTIFACT_MISSING",
    "message": "v1 artifact content file is missing",
    "fields": {
      "task_key": "app/build",
      "artifact": "app.tar"
    }
  }
}
```

业务错误退出码为 2；内部错误为 3；诊断故障注入退出码为 75。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `CACHE_MANIFEST_INVALID` | 清单结构、整数、规范键、摘要格式或 JSON 非法 |
| `CACHE_ARTIFACT_MISSING` | v1/v2 制品文件或 v2 分片文件缺失 |
| `CACHE_DIGEST_MISMATCH` | 制品字节、大小与声明摘要不一致 |
| `CACHE_SHARD_HASH_MISMATCH` | 分片文件哈希或规范内容哈希不一致 |
| `CACHE_TOMBSTONE_INVALID` | 墓碑重复、目标不存在或摘要不属于目标制品 |
| `CACHE_GENERATION_REGRESSION` | 缓存代数低于下限、时间不单调或提交时回退 |
| `CACHE_MANIFEST_VERSION_ROLLBACK` | 新清单版本低于已提交索引 |
| `CACHE_CLOCK_ROLLBACK` | 逻辑时钟回退超过容差 |
| `CACHE_PIPELINE_INVALID` | 流水线字段、任务键、依赖、环或制品声明非法 |
| `CACHE_STATE_CORRUPT` | 快照指针、暂存行、摘要或未知 operation 损坏 |
| `CACHE_LOCK_TIMEOUT` | 等待文件锁超时 |
| `CACHE_INDEX_EMPTY` | 尚未提交索引却执行 `judge` |
| `CACHE_INTERNAL_ERROR` | 无法在并发的稳定版本上完成或未预期内部错误 |

非法来源清单在创建状态数据库之前完成解析，因此错误路径不会留下索引目录或 SQLite 文件。

## 固定样本与验证结果

`scripts/generate_samples.py` 会重建：

- v1 三个制品；
- v2 两个代数的三个分片/十个制品/一个墓碑；
- `app-ci` 七个任务与 `docs-ci` 四个任务；
- 当前包（应为 noop）和代数 9 的完整替换包。

本次实际验证（Linux 容器内的本地 Python 3.10.12、SQLite 3.37.2）：

- `python3 -m unittest discover -v`：21 项测试全部通过。
- 连续两次运行 `scripts/generate_samples.py` 后，整个 `samples/` 目录树摘要均为 `07b7c5884104ff2019a7b4574cbb991f7f784f091d85e6f556ddbe481f91578b`。
- 初始合并：索引版本 1、11 个统一制品、2 个跨版本冲突、1 个墓碑。
- 初始裁决：6 个可复用、5 个阻断；`app/test` 因墓碑阻断，`app/release` 路径为 `["app/test","app/release"]`；`docs/build` 因 60 秒 TTL 过期，`docs/preview` 路径为 `["docs/build","docs/publish","docs/preview"]`。
- 完整替换：从版本 1 进入版本 2，识别为 `full_replacement`；精确失效闭包为 app 的三个下游任务和 docs 的三个下游任务。
- 替换后裁决：11 个任务全部可复用，5 个语义未变任务复用旧裁决，6 个受影响任务重新裁决。
- 并发双实例合并：一次 `committed`、一次 `noop`，最终只有一个版本且 pending 数为 0。
- 强制终止恢复：`after_stage` 退出码 75，`status` 显示无快照但有 pending，`resume` 后版本 1 完整提交且 pending 归零。
- `scripts/run_demo.py` 两轮输出逐字节一致；当前 `demo_output/comparison.json` 记录 SHA-256 为 `f0d59825724054867686c25830a5a4d36127ac4522bf0699096c27e9ea7f3f26`。
