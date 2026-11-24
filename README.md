# OSL Forensics Prototype

独立、确定性的 OSL（ Object Segment Log ）解析与鉴证账本原型。项目不依赖网络、账号、Docker 或外部服务；样本由固定种子和 numpy 确定性生成，SQLite 使用标准库 `sqlite3`。

## 快速开始

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m osl_forensics.cli generate --out samples --plan anomaly_injections.json
python -m osl_forensics.cli verify-injections --root samples\injections --report .osl-work\verify.json
python -m osl_forensics.cli process --root samples\injections --db .osl-work\ledger.sqlite --workdir .osl-work --report .osl-work\batch-report.json --concurrency 4 --queue 30
python -m osl_forensics.cli recover --workdir .osl-work --db .osl-work\ledger.sqlite
pytest -q
```

bash/WSL：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m osl_forensics.cli generate
pytest -q
```

## 一键验收路径

```bash
python -m osl_forensics.cli generate --out samples --plan anomaly_injections.json
python -m osl_forensics.cli verify-injections --root samples/injections
pytest -q
```

生成器会写出：

- `anomaly_injections.json`：可注入异常参数清单。
- `samples/injections/<case>/sample.osl`：固定种子样本。
- `samples/injections/<case>/sync_marker.json`：同步标记，包含期望业务码、出错偏移和复现该标记所需的预算/策略覆盖项。
- `samples/revision_fixture.json`：修订批次与传递失效闭包样例。

验证端只从 `sync_marker.json` 读取期望业务码和偏移，不读取生成器内部断言。每个 OSL 头保留区还写入 8 字节二进制同步字 `OSLSYNC1`（偏移 26）。

## 工程结构

- `osl_forensics/parser.py`：严格头部/记录解析、区间冲突、摘要校验、嵌套、深度、环和配额。
- `osl_forensics/writer.py`：确定性 OSL 构造器与偏移追踪。
- `osl_forensics/generator.py`：固定种子样本和 22 个注入标记。
- `osl_forensics/verifier.py`：同步标记核对。
- `osl_forensics/ledger.py`：SQLite 主键状态、状态代、水位、累计哈希和可重放事件链。
- `osl_forensics/revisions.py`：修订摘要比对、依赖图和传递失效闭包。
- `osl_forensics/engine.py`：并发解析、墙钟超时和确定性队列准入。
- `osl_forensics/storage.py`：跨平台文件锁、临时文件回读校验、原子替换和快照版本识别。
- `osl_forensics/checkpoint.py`：单调检查点、完整检查点选择和快照恢复。
- `tests/`：结构、完整性、嵌套、账本、修订、并发、预算、锁和强杀恢复测试。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_HEADER` | 魔数、版本、头部自洽、UTF-8、类型/压缩白名单等头部或记录头错误 |
| `E_BOUNDS` | 越界、截断或 `offset + length` 溢出 uint64 |
| `E_DUP_NAME` | 同层段名重复；首次出现为权威真值 |
| `E_SELFREF` | 段载荷正向引用段表区间 |
| `E_OVERLAP` | 两个非空载荷区间部分交叉或完全包含 |
| `E_CHECK_ALGO` | 未知算法、算法与声明位宽不匹配、分派算法不一致 |
| `E_CHECKSUM` | SHA-256 或 HMAC-SHA256 与实际载荷不符 |
| `E_DEPTH` | 嵌套 OSL 超过最大递归深度 4 |
| `E_CYCLE` | 引用边回到祖先段 |
| `E_QUOTA` | 递归展开累计字节超过配额 |
| `E_TIMEOUT` | `time.perf_counter()` 实测墙钟预算耗尽 |
| `E_QUEUE_FULL` | 超过可配置待处理队列上限，已确定性拒绝 |
| `E_LOCK` | 工作目录文件锁冲突或等待超时 |

## 二进制格式说明

题面的头部算术存在 2 字节冲突：

```text
4(magic) + 2(version) + 4(count) + 8(table_offset) + 8(total_length) + 16(reserved) = 42
```

但题面同时称固定头为 40 字节。原型提供两种显式布局：

- `OSL40_LAYOUT`：默认，`header_size=40`，按 `<4sHIQQ` 紧凑字段计算后保留区为 14 字节。
- `LITERAL_RESERVED16_LAYOUT`：严格保留 16 字节，`header_size=42`，由标记 `policy_overrides.header_layout="literal42"` 复现。

为使“声明摘要位宽不匹配”可从二进制自身判定，默认记录在 `checksum_algorithm:u8` 后增加小端 `digest_length:u16`，随后是摘要字节。记录顺序为：

```text
name_length:u16 | name:utf8 | segment_type:u8 | compression:u8 |
payload_offset:u64 | declared_length:u64 | checksum_algorithm:u8 |
digest_length:u16 | digest_bytes
```

分派策略为：

- `compression=0x00`：普通载荷，不做密码学校验。
- `compression=0x01`：SHA-256，算法编号 1，摘要 32 字节。
- `compression=0x02`：HMAC-SHA256，算法编号 2，摘要 32 字节；本地固定开发密钥在 `policy.py`。
- `compression=0x10`：载荷本身是完整 OSL，递归解析。

所有段仍额外计算原始内容 SHA-256，供修订批次比较，避免 `compression=0` 没有声明摘要时无法做内容差异判定。

## 规范键与引用边

根段规范键为 `/<UTF-8 段名>`；嵌套段追加路径，例如 `/outer/inner`。`~` 和 `/` 使用 JSON Pointer 风格转义。

`segment_type=3` 定义为引用段，其 UTF-8 载荷每行一个规范依赖目标，形成 `当前段 -> 被依赖段` 边。嵌套 `0x10` 段还会产生到直接子段的隐式依赖边。修订内容变化后，沿反向边计算传递重算闭包。

## 账本与恢复

SQLite 表 `segment_state` 以 `(file_id, segment_key)` 为主键，保存：

- `status`、`error_code`、`error_offset`
- `state_generation`
- `processed_watermark`
- `cumulative_digest`
- 实际/声明摘要、内容摘要和规范化详情

每文件的账本推进在一个事务内完成。`E_HEADER`、`E_BOUNDS`、`E_TIMEOUT`、`E_QUOTA` 等前置/预算致命错误不会调用写入。类型白名单或算法白名单形成 `policy_fingerprint`；指纹变化时会 DROP 并重建状态、事件和运行表，避免旧口径与新口径混合。

`ledger_event` 保存全局哈希链；`Ledger.verify_replay()` 重算事件哈希并核对当前状态。检查点保存完整账本 JSON 快照的 SHA-256 和事件尖。恢复时若 SQLite 已领先于最后完整检查点，则用快照整体恢复到该检查点。

## 并发一致性

`BudgetConfig` 暴露 `concurrency`、`timeout_seconds`、`max_queue` 和 `max_expanded_bytes`。解析在线程池中进行，SQLite 推进在收集结果后按 `(file_id, revision_id)` 稳定排序串行提交。

队列拒绝不依赖瞬时到达速度：同一批次按稳定身份排序，超过 `max_queue` 的尾部作业得到 `E_QUEUE_FULL`。因此任意提交顺序、任意并发度下，段状态、事件哈希、失效闭包和拒绝记录均与串行模式一致。墙钟超时使用 `time.perf_counter()`，并在文档、段和 64 KiB 摘要块边界协作式检查截止时间。

## 验证结果

实际命令与结果记录在 `VALIDATION.md`。
