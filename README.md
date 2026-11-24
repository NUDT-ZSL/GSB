# memguard

离线、确定性的内存访问边界守卫。程序按 arena 调度 JSONL 轨迹，在 SQLite
中按批原子推进区域账本，并生成可解析的 JSON 诊断报告。实现不依赖网络、
Docker 或外部数据。

## 环境与运行

代码兼容 Python 3.10–3.12。本次主验证环境为 WSL2 中的 Python 3.12.13、
SQLite 3.53.1、numpy 1.26.4、pytest 8.2.2，同时用系统 Python 3.10.12 /
SQLite 3.37.2 复跑了测试。题面要求的 Windows 官方 Python 环境当前不可用；
Windows 路径使用 `msvcrt` 文件锁，POSIX 路径使用 `fcntl` 文件锁。

```bash
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
python generate_samples.py --out-dir sample --seed 20261125
python -m memguard run \
  --regions sample/regions.json \
  --trace sample/traces.jsonl \
  --truth-markers sample/sync_markers.json \
  --database work/ledger.sqlite \
  --snapshot work/ledger.snapshot.json \
  --report work/report.json \
  --workers 4 \
  --max-inflight-bytes 33554432 \
  --arena-timeout-ms 5000 \
  --max-pending 64 \
  --batch-size 256
```

运行测试：

```bash
python -m pytest -q
```

退出码：`0` 表示完整执行完成，`1` 表示数据部分失败、预算降级、真值不一致
或运行故障，`2` 表示参数/配置错误。正常检测到 `OOB_*` 属于成功完成的检测结论，
不会单独导致非零退出码。

## 输入契约

区域区间和访问区间均按半开区间 `[start, end)` 解释。区域声明可以是一份 JSON
数组、包含 `regions` 数组的 JSON，或 JSONL。示例：

```json
{
  "regions": [
    {
      "region_id": "region-leaf",
      "arena_id": "arena-alpha",
      "arena_size": 65536,
      "start_offset": 1024,
      "end_offset": 3072,
      "lifetime_start_ms": 0,
      "lifetime_end_ms": 100000,
      "generation": 3,
      "parent_region_id": "region-child"
    }
  ]
}
```

轨迹每行是一个 JSON 对象，必填字段与题面一致。生成器还会写入：

- `sender_interval`: 发送端原始区间 `[base_offset, base_offset + access_size]`
- `sync_marker`: 真值同步标记
- `expected_interval`: 预期合法交集，空交集为 `null`
- `expected_business_code`: 外部标记文件中的预期业务码

检测器不读取 `expected_*` 做判定。它们只在检测与账本提交完成后，由
`memguard.truth` 与同步标记文件和账本输出对照。

## 判定语义

判定优先级固定为：祖先链错误、自身生命周期、零长度、端点相接、自身区间内、
祖先区间包含、跨越祖先区间、完全在祖先外。

| 业务码 | 含义 |
| --- | --- |
| `OK` | 非零长度访问完全落在自身区域内 |
| `OOB_ZERO_LENGTH` | 零长度访问 |
| `OOB_ENDPOINT_TOUCH` | 正长度访问仅与自身区间端点相接 |
| `OOB_ANCESTOR_CONTAINED` | 越过自身区间，但完整落入某个祖先区间 |
| `OOB_CROSS_ANCESTOR` | 与祖先区间有交集但未被任一祖先完整包含 |
| `OOB_OUTSIDE_ANCESTORS` | 与自身及所有祖先区间均无交叠 |
| `E_PARENT_MISSING` | 祖先引用缺失，证据含缺失父区域 |
| `E_PARENT_CYCLE` | 祖先链成环，证据含排序后的环内 `region_id` |
| `E_LIFETIME_SELF` / `E_LIFETIME_ANCESTOR` | 访问时间不在区域或祖先生命周期内 |

证据中同时给出绝对访问区间、自身区域区间、`allowed_intersection`、
`oob_ranges`、`first_oob_offset`、`low_oob_bytes`、`high_oob_bytes` 和祖先路径。

## 并发、预算与等价性

- arena 是独立调度单元，`--workers` 控制并发度。
- 在途字节预算按实际扫描出的轨迹行字节数预留；worker 读取后再次报告实际
  加载字节，二者不一致会标记 `E_BUDGET`。
- 单 arena 耗时使用 `time.perf_counter()` 测得，超过
  `--arena-timeout-ms` 标记 `E_TIMEOUT`，但仍收集该 arena 的完整结果。
- 单个 arena 大于总预算时独占执行并标记 `E_BUDGET`，不会丢失判定。
- 待处理 arena 数量超过 `--max-pending` 时标记 `E_QUEUE_FULL`，其余 arena
  仍继续执行。
- 预算/超时/队列标记位于 `scheduler`，不混入输入拒绝清单，因此不同预算档位
  不会改变检测结论或拒绝清单。
- 最终结果按 arena、`access_seq`、来源文件和行号稳定排序。重复
  `access_seq` 取按来源文件与行号确定的首次出现，其余记录计数并拒绝；
  缺口从序号 1（或首个序号 0）起报告缺失区间。

## SQLite 账本与检查点

`region_ledger` 以 `(region_id, access_kind)` 为主键，记录累计访问数、
越界数、越界上界和已处理 `access_seq` 水位。越界上界使用精确十进制文本，
避免 SQLite 将大于 `2^63-1` 的合法 64 位值转成 REAL 而损失精度；报告输出
JSON 整数。

每批在一个 `BEGIN IMMEDIATE` 事务内完成：

1. 重新检查批次唯一性；
2. 更新账本；
3. 写入批次增量日志和该批结束后的账本摘要；
4. 写入 SQLite 完整检查点；
5. 提交。

提交后，检查点 JSON 先写同目录临时文件、复核可解析性，再核对当前快照版本并
`os.replace`。恢复时 SQLite 是权威完整检查点：重放全部批次增量，核对每批摘要、
最终账本和 SQLite 检查点；陈旧快照从 SQLite 修复，损坏快照/半写临时文件会
报告并忽略。布局键包含策略版本、区域声明（含代次）和输入轨迹摘要；布局变化
时在同一事务内整体重建账本。

数据库旁的文件锁非阻塞获取；同一账本已有进程写入时返回 `E_LOCK`，不打开或
修改账本。`MEMGUARD_TEST_KILL_AFTER_BATCHES=N` 是测试专用故障注入，在指定批次
提交后、快照替换前执行 `os._exit`，用于验证真实强制终止恢复。

## 报告

报告包含：

- `batch`: 批次结论、业务码、访问计数、水位、布局/输入摘要和与并发无关的
  `decision_digest`
- `scheduler`: 实测字节/耗时、预算降级和调度诊断
- `regions`: 每区域 read/write 方向的状态、业务码、证据和账本值
- `accesses`: 稳定排序的逐访问结论和规范化证据
- `rejections`: 含来源文件、行号和业务码的输入拒绝
- `sequence_diagnostics`: 重复与缺口
- `ledger`: 账本行和最后一个完整检查点
- `recovery`: 恢复/重放/快照修复信息
- `truth`: 同步标记与账本对账结果

## 目录

- `memguard/parser.py`: 输入校验、拒绝记录、arena 清单
- `memguard/evaluator.py`: 多级区间判定、去重和缺口
- `memguard/scheduler.py`: arena 并发与真实预算
- `memguard/ledger.py`: SQLite 账本、检查点、恢复
- `memguard/truth.py`: 标记真值对账
- `memguard/generator.py`: 固定种子样本
- `tests/`: 23 项 pytest 验证

实际命令输出和环境偏差见 `VALIDATION.md`。
