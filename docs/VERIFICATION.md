# 实际验证记录

验证日期：2026-11-17  
环境：Ubuntu 22.04.5（WSL），Rust 1.75.0，Cargo 1.75.0  
约束：构建使用 `--offline` 与本地 `vendor/`；运行只访问本地文件和 stdin/stdout。

Windows 目标编译检查：

```text
$ cargo check --offline --target x86_64-pc-windows-msvc
Finished dev [unoptimized + debuginfo] target(s)
```

当前执行容器没有 Windows/Wine 运行时，因此 Windows `.exe` 的实际交互运行未在本机执行；工程只使用跨平台 `std` 文件/终端 API，并提供 `scripts/demo.ps1` 与 `scripts/anomalies.ps1` 供 Windows PowerShell 直接运行。

## 1. 构建与自动化测试

```text
$ rustc --version
rustc 1.75.0 (82e1608df 2023-12-21)

$ cargo --version
cargo 1.75.0 (1d8b05cdd 2023-11-20)

$ cargo test --offline --all-targets
running 2 tests  (record CRC/round-trip)
test result: ok. 2 passed

running 13 tests (integration)
test result: ok. 13 passed

running 2 tests (CLI executable)
test result: ok. 2 passed

$ cargo build --offline --release
Finished release [optimized] target(s)

$ cargo clippy --offline --all-targets -- -D warnings
Finished dev [unoptimized + debuginfo] target(s)
```

集成测试覆盖：

- 快照后仅重放 1 条增量记录；间接视图 `total_count` 经 `view:order_count` 失效。
- 同进程第二次恢复重放 0 条，不重复应用。
- 最终段尾部截断保留完整前缀，报告 `E_LOG`。
- 非最终段校验和损坏返回 `E_CORRUPT`，失败时不提交部分状态。
- 长度前缀不自洽返回带路径/偏移的 `E_LOG`。
- 逻辑时间戳跳号返回 `E_SNAPSHOT`。
- 快照引用段缺失返回 `E_SNAPSHOT`。
- 会话打开整体替换内存状态而非合并。
- 依赖成环在加载时返回 `E_DEPEND`。
- 视图版本/摘要不一致返回 `E_VIEW`，消息含首个差异视图和键。
- 删除、过期触发基状态删除和视图失效。
- 依赖失效顺序为 `user_count -> total_count -> user_country_index`。
- put/delete/expire 操作码和读写往返稳定。
- CLI 启动注入参数、成功/失败退出码、`diagnostics.jsonl` 写入和报告字段自动验证。

## 2. 确定性样本哈希

```text
34e5b3152d5630fad74ed51eee95811553b73c0c6732968626dacbb75dbde5c7  samples/basic/session.json
a1613da5e706e80a510cf08f919d909ea26bb02764cc0a0e15fa78817ef01e11  samples/basic/graph.json
6264bf10f1f4f28192e0e5fb21d382f646597d31af56ce32bedbc282680ae0ee  samples/basic/data/000001.log
536fe31d2d09b3e4ace697b46b4ee89eceffbaf316dfcfd46dbe84efe8c9cf24  samples/basic/data/000002.log
```

段大小：`000001.log=88` 字节，`000002.log=44` 字节。  
快照点：`000001.log:88`，逻辑时间戳 2。  
快照段集合摘要：`c593434dbc095f33`。

## 3. 三场景演示

执行：

```bash
bash scripts/demo.sh
```

### 场景 1：首次恢复

```text
replacement=full
segment_digest_match=false
added_segment=000002.log
business_code=OK
logical_timestamp=3
segment_set_digest=db235dbb5d220c68
replayed_records=1
view=order_count kind=prefix_count version=2 valid=false digest=eb0470ef3b585bbb
view=total_count kind=prefix_count version=4 valid=false digest=9a83091fbe06bc4f
view=user_count kind=prefix_count version=3 valid=true digest=8e94cab338d29c58
view=user_country_index kind=secondary_index version=3 valid=true digest=f45d0935f426e514
trace=7 trigger=put:order:cn:100@3 affected_view=order_count relation=prefix:order: action=invalidated
trace=8 trigger=put:order:cn:100@3 affected_view=total_count relation=view:order_count action=invalidated
```

结论：只从快照点后重放逻辑时间戳 3 的记录；直接视图 `order_count` 与间接视图 `total_count` 失效，未受影响的用户视图保持有效。

### 场景 2：增量写入、新快照点与校验

写入 `user:cn:carol`（逻辑时间戳 4），创建 `000002.log:90` 新快照点，保存会话后校验：

```text
report=view-validation
business_code=OK
view=order_count version=2 digest=28187ae6d142194a previous_digest=eb0470ef3b585bbb status=ok
view=user_count version=4 digest=dfec40ba40089089 previous_digest=8e94cab338d29c58 status=ok
view=total_count version=5 digest=b66464eda3c85e65 previous_digest=9a83091fbe06bc4f status=ok
view=user_country_index version=4 digest=675fc03c6a4c0bb9 previous_digest=f45d0935f426e514 status=ok
first_different_view=none
first_different_key=none
segment_set_digest=75aa4031c68ed08e
```

结论：重建全部基于当前基状态；版本与摘要同时变化；校验通过。

### 场景 3：注入尾部截断后恢复

样本副本的 `000002.log` 被移除尾部 8 字节：

```text
injection=tail-truncate segment=000002.log removed_bytes=8 new_bytes=36
business_code=E_LOG
message=tail truncation detected and repaired
logical_timestamp=2
segment_set_digest=7090dd94bf3c6aad
replayed_records=0
truncation.segment=000002.log
truncation.offset=0
truncation.original_bytes=36
truncation.valid_bytes=0
truncation.removed_bytes=36
```

结论：恢复保留快照与完整前缀，截断不完整尾部，报告 `E_LOG` 并继续使用已持久化状态。

重复运行 `demo.sh` 两次，三个场景报告文件逐字节一致（`diff` 无输出）。

## 4. 四类异常链路

执行：

```bash
bash scripts/anomalies.sh
```

| 注入 | 控制台业务码 | CLI 退出码 | 结构化日志要点 |
| --- | --- | ---: | --- |
| 尾部截断 | `E_LOG` | 0（已修复并继续） | `segment=000002.log offset=0`，记录修复字节数 |
| 非最终段校验和损坏 | `E_CORRUPT` | 2 | `segment=.../000001.log offset=46` |
| 依赖图成环 | `E_DEPEND` | 2 | 稳定列出受影响视图，`segment=null offset=null` |
| 快照引用段缺失 | `E_SNAPSHOT` | 2 | `segment=000001.log offset=null` |

示例 JSONL（时间戳随运行变化）：

```json
{"timestamp_ms":1794853546497,"business_code":"E_LOG","segment":"000002.log","offset":36,"message":"injected tail truncation removing 8 bytes"}
{"timestamp_ms":1794853546502,"business_code":"E_LOG","segment":"000002.log","offset":0,"message":"tail truncation repaired: removed 36 bytes"}
{"timestamp_ms":1794853546519,"business_code":"E_CORRUPT","segment":"000001.log","offset":46,"message":"injected checksum corruption"}
{"timestamp_ms":1794853546528,"business_code":"E_DEPEND","segment":null,"offset":null,"message":"dependency graph contains a cycle; affected views: order_count,total_count,user_country_index"}
{"timestamp_ms":1794853546541,"business_code":"E_SNAPSHOT","segment":"000001.log","offset":null,"message":"snapshot references missing segment 000001.log"}
```

## 5. 验证产物

脚本生成（未纳入版本控制）：

- `demo-output/scenario1.txt`
- `demo-output/scenario2.txt`
- `demo-output/scenario3.txt`
- `demo-output/anomalies/*.txt`
- 每个数据目录下的 `diagnostics.jsonl` 与 `recovery-report.json`

仓库内纳入版本控制的确定性输入为 `samples/basic/`。演示和异常注入均在复制目录中运行，不改写样本。
