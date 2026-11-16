# LogStore：Windows 本地日志结构存储引擎

这是一个可直接构建、测试和演示的 Rust 日志结构键值存储组件。它只使用本地文件与 stdin/stdout，不依赖网络、数据库、容器或外部服务。工程包含：

- 带长度前缀、版本号、操作类型、逻辑时间戳和 CRC-32 校验和的追加式日志段格式。
- 从显式快照点开始、只重放后续记录的逻辑时钟恢复。
- 可声明基键/前缀/视图依赖的派生视图图，按确定拓扑顺序传播缓存失效。
- 尾部不完整记录修复、中间段校验和损坏阻止恢复、依赖成环拒绝和快照缺段拒绝。
- 视图重建与一致性校验、完整恢复会话快照、结构化 JSONL 业务日志。
- 可复用 Rust 库 API、stdin 命令行、确定性样本、Shell/PowerShell 演示脚本及自动化测试。

## 1. 环境与构建

要求 Rust 稳定工具链 1.75 或更高版本。Windows 上需要可用的 Rust MSVC 工具链和 Visual Studio Build Tools 链接器。

```bat
rustc --version
cargo --version
cargo build --offline
cargo test --offline
```

依赖已放在 `vendor/`，`.cargo/config.toml` 使用本地源替换，因此构建与测试不需要访问 crates.io。运行时同样不访问网络。

Rust 1.75/Linux/WSL 参考验证结果见 `docs/VERIFICATION.md`。

## 2. 快速演示

生成确定性样本：

```bash
cargo run --offline -- generate-samples samples
```

Linux/macOS/WSL：

```bash
bash scripts/demo.sh
bash scripts/anomalies.sh
```

Windows PowerShell：

```powershell
cargo build --offline
.\scripts\demo.ps1
.\scripts\anomalies.ps1
```

`demo.sh`/`demo.ps1` 顺序执行三个场景，并把稳定报告写到 `demo-output/`：

1. 从样本快照点首次恢复，只重放快照后的逻辑时间戳 3 记录。
2. 恢复后写入新键，创建新的快照点，保存会话并执行视图一致性校验。
3. 从样本副本移除最后一个段文件尾部 8 字节，恢复时保留完整前缀并返回 `E_LOG` 报告。

`anomalies.sh`/`anomalies.ps1` 额外验证 `E_CORRUPT`、`E_DEPEND` 和 `E_SNAPSHOT` 链路。

## 3. 磁盘记录格式

段文件按文件名字典序扫描，例如 `000001.log`、`000002.log`。每条记录的顺序由段文件顺序和段内字节顺序唯一决定。

完整帧：

| 偏移 | 长度 | 字段 |
| ---: | ---: | --- |
| 0 | 4 | `record_body_len`，u32 little-endian |
| 4 | 2 | 格式版本，u16 little-endian；当前支持集合为 `{1}` |
| 6 | 1 | 操作类型：`1=put`、`2=delete`、`3=expire` |
| 7 | 1 | 保留标志，必须为 0 |
| 8 | 4 | `key_len`，u32 little-endian |
| 12 | 4 | `value_len`，u32 little-endian |
| 16 | 8 | 毫秒级逻辑时间戳，u64 little-endian |
| 24 | 4 | CRC-32/IEEE 校验和，u32 little-endian |
| 28 | `key_len` | UTF-8 键 |
| 28 + `key_len` | `value_len` | UTF-8 值 |

约束：

- `record_body_len = 24 + key_len + value_len`，其中 24 字节为版本到校验和的固定头。
- 校验和覆盖 body 中除校验和字段外全部内容；计算时校验和字段视为 4 个零字节。
- `delete` 与 `expire` 的记录值必须为空。
- 非法版本、未知操作、长度字段不自洽、保留位非零或 UTF-8 非法均返回 `E_LOG`。
- 完整帧 CRC 不一致返回 `E_CORRUPT`。最终段尾部不完整帧由恢复过程截断并产生 `E_LOG` 报告，不跳过后继续读取。

## 4. 逻辑时钟与恢复

- 无快照时，首条记录的逻辑时间戳必须为 1，之后每条必须严格加 1。
- 快照点保存 `segment`、`offset`、`logical_timestamp` 与 `state_digest`；快照前状态从会话快照加载，不从日志重新计算。
- 从快照恢复时，第一条待重放记录必须是 `snapshot.timestamp + 1`；出现空隙返回 `E_SNAPSHOT`，不会静默回退到更早位置。
- 同一进程内重复调用恢复会从上次已应用位置继续，不会重复应用记录。
- 恢复先使用临时状态计算；任何 `E_LOG`/`E_CORRUPT`/`E_SNAPSHOT` 失败都不提交部分内存状态。

## 5. 派生视图与依赖图

图 JSON：

```json
{
  "graph_version": 7,
  "views": [
    {
      "id": "user_count",
      "kind": { "type": "prefix_count", "prefix": "user:" },
      "dependencies": [{ "type": "prefix", "prefix": "user:" }]
    },
    {
      "id": "user_country_index",
      "kind": {
        "type": "secondary_index",
        "index": "by_country",
        "prefix": "user:",
        "delimiter": ":",
        "field": 1
      },
      "dependencies": [{ "type": "prefix", "prefix": "user:" }]
    },
    {
      "id": "total_count",
      "kind": { "type": "prefix_count", "prefix": "" },
      "dependencies": [
        { "type": "view", "view": "user_count" },
        { "type": "view", "view": "order_count" }
      ]
    }
  ]
}
```

支持的依赖：

- `key`：精确基键。
- `prefix`：基键前缀。
- `view`：视图到视图的间接依赖边。

图加载时按视图 ID 和依赖标签规范化。拓扑排序使用字典序稳定的 Kahn 算法；加载或声明视图时发现环，整体拒绝并返回 `E_DEPEND`。

写入、删除或过期后，先更新候选基状态，再按拓扑顺序计算候选视图内容摘要。只有内容摘要实际变化时才把缓存标记为失效并递增版本号；传播轨迹记录每一步的触发源、受影响视图、依赖关系和动作。这样视图版本与内容摘要保持一致，避免无意义版本变化。

### 重建与校验

- `view-rebuild`/`rebuild_view` 始终从当前已恢复基键状态和视图定义重新计算，不读取旧缓存作为输入。
- `validate` 对每个视图独立重算期望内容，比较上次构建版本/摘要与当前版本/摘要。
- 若“版本变化但摘要不变”或“摘要变化但版本不变”，返回 `E_VIEW`；错误消息稳定包含首个差异视图 ID 和首个差异内容键。

## 6. 会话快照

会话 JSON 是完整恢复会话，不只是位置指针，包含：

- `snapshot_version` 与会话格式版本。
- `segments`、`segment_set_digest`：快照时的段文件集合及摘要。
- `snapshot_point`：段名、字节偏移、逻辑时间戳、状态摘要。
- `logical_timestamp` 与基键 `state`、`state_digest`。
- `graph_version`、`graph_digest` 和完整依赖图。
- 每个视图的缓存、版本、有效性、构建版本/摘要。
- 完整失效轨迹和下一步序号。

打开会话快照时：

1. 严格检查 `snapshot_version`、快照点版本、状态摘要、段集合摘要、图摘要和图版本。
2. 快照引用的每个段文件都必须存在；缺失返回 `E_SNAPSHOT`。
3. 比较快照段集合摘要与当前磁盘段集合摘要，稳定列出新增/删除段、图版本差、逻辑时间戳差和视图版本差。
4. 无论摘要是否相同，均以快照状态整体替换当前内存状态，不做增量合并。

## 7. 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 操作成功 |
| `E_LOG` | 非法记录格式；或最终段尾部不完整记录被截断修复并写入恢复报告 |
| `E_CORRUPT` | 完整记录校验和不匹配，或非最终段出现不完整记录；恢复立即停止 |
| `E_SNAPSHOT` | 快照版本/摘要错误、引用段缺失、偏移非法或逻辑时间戳不连续 |
| `E_DEPEND` | 依赖图未知视图、重复视图或成环 |
| `E_VIEW` | 视图重建前后版本/摘要一致性校验失败 |
| `E_IO` | 本地文件系统错误 |
| `E_INPUT` | 命令行或库调用参数错误 |

异常注入与启动参数：

| 参数 | 对输入的作用 | 预期业务码 |
| --- | --- | --- |
| `--inject-tail-truncate SEGMENT:BYTES` | 物理缩短段文件尾部字节 | 恢复报告 `E_LOG` |
| `--inject-corrupt-checksum SEGMENT:RECORD_INDEX` | 翻转指定记录校验和字段一位 | `E_CORRUPT` |
| `--inject-dependency-cycle` | 在加载图前注入视图依赖环 | `E_DEPEND` |
| `--inject-missing-snapshot-segment SEGMENT` | 模拟快照引用段缺失 | `E_SNAPSHOT` |

注入只修改磁盘输入文件或加载参数，不修改引擎源码。`--log FILE` 可指定 JSONL 日志路径；默认写入 `<data-dir>/diagnostics.jsonl`。每条异常日志包含 `timestamp_ms`、`business_code`、`segment`、`offset` 和 `message`。

## 8. 命令行

启动参数：

```text
logstore [--data-dir DIR] [--graph FILE] [--session FILE] [--log FILE]
         [--max-segment-bytes N] [--auto-recover]
         [--inject-tail-truncate SEGMENT:BYTES]
         [--inject-corrupt-checksum SEGMENT:RECORD_INDEX]
         [--inject-dependency-cycle]
         [--inject-missing-snapshot-segment SEGMENT]
```

stdin 命令：

```text
recover | report | snapshot | snapshot-save PATH | session-save PATH
put KEY VALUE | delete KEY | expire KEY
session-open PATH | graph-load PATH | view-declare FILE
view-rebuild ID|all | validate | views | trace | segments | state KEY
diagnose-snapshot PATH | help | quit
```

示例：

```bash
./target/debug/logstore \
  --data-dir demo-output/scenario1/data \
  --session samples/basic/session.json \
  --auto-recover
```

报告字段顺序固定，地图/集合内容使用有序结构输出。恢复报告包含业务码、快照位置、段集合摘要、每个段摘要、重放数量、截断信息、视图版本/有效性和失效轨迹。

## 9. 库 API 示例

```rust
use log_structured_store::graph::{Dependency, GraphDocument, ViewDefinition, ViewKind};
use log_structured_store::{Store, StoreConfig};
use std::path::Path;

fn demo() -> log_structured_store::Result<()> {
    let mut store = Store::open(
        Path::new("data"),
        StoreConfig { max_segment_bytes: 4096 },
    )?;
    store.install_graph_document(GraphDocument {
        graph_version: 1,
        views: vec![ViewDefinition {
            id: "user_count".into(),
            kind: ViewKind::PrefixCount { prefix: "user:".into() },
            dependencies: vec![Dependency::Prefix { prefix: "user:".into() }],
        }],
    })?;

    store.put("user:cn:alice", "Alice")?;
    store.rebuild_all_views()?;
    store.create_snapshot_point()?;
    store.save_session_snapshot(Path::new("session.json"))?;

    let mut reopened = Store::open(
        Path::new("data"),
        StoreConfig { max_segment_bytes: 4096 },
    )?;
    reopened.open_session_snapshot(Path::new("session.json"), None)?;
    let report = reopened.recover()?;
    println!("{report}");
    Ok(())
}
```

主要 API：

- `Store::open`、`Store::load_graph_file`、`Store::install_graph_document`、`Store::declare_view`
- `put`、`delete`、`expire`、`append_record`
- `create_snapshot_point`、`save_session_snapshot`、`open_session_snapshot`
- `recover`、`rebuild_view`、`rebuild_all_views`、`validate_views`
- `SessionSnapshot::load`、`SegmentSetSummary`、`RecoveryReport`、`SessionOpenReport`

## 10. 确定性样本

`samples/basic/` 包含：

- `data/000001.log`：逻辑时间戳 1、2 的 `user:` 记录，共 88 字节。
- `data/000002.log`：逻辑时间戳 3 的 `order:` 记录，共 44 字节。
- `session.json`：快照点位于 `000001.log:88`，逻辑时间戳 2，并包含基状态、图和视图缓存。
- `graph.json`：前缀计数、二级索引、视图到视图依赖。

样本可用 `logstore generate-samples samples` 重新生成；输出字节和 FNV-1a 摘要确定。

## 11. 验证

```bash
cargo test --offline
bash scripts/demo.sh
bash scripts/anomalies.sh
```

当前验证结果、样本哈希、三个演示场景和四类异常结果记录在 `docs/VERIFICATION.md`。

## 12. 设计边界

- 这是教学/排障演示引擎，不是并发数据库；未实现多进程写入锁、压缩、崩溃一致性 fsync 协议或加密。
- 段集合摘要、状态摘要和内容摘要使用平台稳定的 64 位 FNV-1a 十六进制值，目的是确定性演示，不是密码学哈希。
- 运行时只使用本地文件；样本与异常均可由本地脚本确定性重建。
