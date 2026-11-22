# 技术设计

## 1. 分层

- `multipart_store/manifest.py`：纯解析和校验。解析完成前不打开 SQLite，因此格式非法时不会产生数据库、锁文件或临时目录。
- `multipart_store/db.py`：SQLite schema、`BEGIN IMMEDIATE` 事务、跨进程文件锁。
- `multipart_store/storage.py`：临时文件写入、fsync、`os.replace` 原子改名、流式摘要和故障注入。
- `multipart_store/service.py`：领域状态机、覆盖版本、轮次调度、完成仲裁、恢复与 GC。
- `multipart_store/cli.py`：七个命令与稳定 JSON 输出。

## 2. 持久化模型

SQLite 保存所有裁决事实：

- `buckets`、`objects`：桶/对象声明和唯一完成者。
- `sessions`：阶段、可见集合版本、可见集合摘要、会话逻辑时钟水位、裁决及裁决版本。
- `visible_parts`：当前可见分片映射，读取方只使用该表。
- `part_versions`：每次成功上传的版本事实，包含覆盖前版本/摘要、事件来源与文件复用标记。
- `overwrite_records`：覆盖来源、前后摘要、是否同内容幂等覆盖。
- `events`：清单事件和直接上传的幂等键；`disposition` 明确区分 `abandoned_budget`、`not_started`、`started_ok`、`started_failed`、`abandoned_manual`、`late_rejected`。
- `late_uploads`：最终裁决后到达的分片及其拒绝原因。
- `completion_attempts`：两阶段完成意图和半提交状态。
- `aborts`、`recovery_log`、`gc_log`：中止触发来源、恢复动作与孤儿删除依据。

会话阶段为：

```text
pending_upload -> uploading -> pending_completion -> completed
       |              |                |
       +--------------+----------------+-> aborted
```

阶段只由持久化行转换。服务对象不缓存会话状态，所以新进程重启后不会根据内存猜测。

## 3. 分片提交协议

1. 进程取得 `.store.lock` 排他文件锁。
2. 校验事件幂等键、会话阶段、对象完成意图、逻辑时钟和期望版本。
3. 先把上传事件置为 `started`，再写独立 staging 临时文件、`fsync`、`os.replace` 到最终分片路径。
4. 在单个 `BEGIN IMMEDIATE` 中插入 `part_versions`、更新 `visible_parts`、推进 `visible_version`、更新阶段和可见集合摘要、写覆盖记录、把事件置为 `succeeded`。

若进程在步骤 3 与 4 之间终止，磁盘上只留下无元数据引用的文件；`status` 会列出它，`resume --cleanup` 会记录摘要与删除依据后回收。

同内容重复上传会创建新的逻辑版本和覆盖记录，但不写入第二个物理文件；`part_versions.reused_existing_file` 为 1。完全相同事件 ID 和负载重复提交则直接返回原结果，版本不再次推进。

## 4. 时钟与预算

- 事件时间只取 `logical_time_ms`，不使用 wall clock 做业务判定。
- 每个会话保存 `clock_watermark_ms`；全局 `meta.clock_watermark_ms` 保存所有会话最大值。
- 新事件时间小于“会话水位 - 容差”时返回 `E_CLOCK_REGRESSION`，分片、可见版本和会话阶段不变。
- 每轮工作量单位为 `round_time_budget_ms // clock_step_ms`。选定的事件按 `event_id` 顺序取得预算；未取得预算的事件保持 pending，并记录 `abandoned_budget`。
- 已标记 started 的事件不再接受预算超时重标记；失败只可能来自启动后的真实业务/存储错误。

同一轮内，不同会话分组在线程池中并行。单个会话内按 `event_id` 提交，以保证同分片覆盖顺序和最终可见集合可复现；`max_concurrency` 同时作为该会话并发工作的上限约束。

## 5. 完成仲裁

完成分为两个持久化阶段：

1. `prepared`：在 SQLite 事务中为对象取得 `completion_claim_session_id`，写入绑定当前 `visible_version` 的完成意图。
2. `validated` / `materialized` / `committed`：
   - 校验所有声明分片可见；
   - 逐片重新计算摘要并与事件校验和、已存内容摘要和大小比较；
   - 按 `part_number` 升序流式拼接，计算对象声明算法对应的整体摘要；
   - 原子落盘最终对象；
   - 在单个事务中将会话置为 completed、对象写入唯一完成者、其他同对象会话置为 aborted 并写入触发来源。

任一失败都返回独立业务码，并在 `completion_attempts.failure_json` 保存 `first_bad_part`、`expected_*` 和 `actual_*`。失败会释放对象完成 claim，允许修复分片后重试。

`completion_after_prepare`、`completion_after_validation`、`completion_after_materialize` 任意点被强制终止后，`resume` 从持久化 attempt 重放未完成部分。重放不会重新执行已成功分片事件，因此不会重复计数。

## 6. 多进程一致性

- 所有写命令和读取快照都先取得跨进程排他文件锁。Unix 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`。
- SQLite 使用 WAL、`synchronous=FULL`、`busy_timeout=30000` 和显式 `BEGIN IMMEDIATE`。
- 期望版本在事务中再次核对，因此同一会话版本最多只有一个提交者。
- 同一对象的有效完成意图有唯一部分索引；最终只允许一个 `completed_session_id`。
- 同事件 ID 的重复提交由 `events` 主键和负载比对保证幂等。

## 7. 孤儿与读取可见性

物理分片引用只来自非 aborted 会话的当前 `visible_parts`。以下文件会被判定为孤儿：

- aborted 会话的可见分片，原因为 `aborted_session_unreferenced`；
- 已被覆盖且不再可见的历史文件，原因为 `superseded_version_unreferenced`；
- 无任何元数据引用的崩溃残留，原因为 `unreferenced_part_file`；
- staging 临时文件，原因为 `staging_debris`。

`status` 只报告，不修改状态。`resume --cleanup` 在文件锁内删除分片孤儿，并将路径、大小、SHA-256、原因和逻辑时钟写入 `gc_log`。最终对象不在分片目录，不会被 GC 误删。

## 8. 业务错误码

主要错误码：

```text
E_INVALID_JSON / E_SCHEMA_INVALID
E_BUCKET_NAME_INVALID / E_OBJECT_KEY_INVALID
E_DUPLICATE_SESSION_ID / E_INVALID_LIMIT
E_DECLARED_PARTS_INVALID / E_EVENT_ORDER_INVALID
E_DUPLICATE_EVENT_ID
E_PART_NUMBER_INVALID / E_LOGICAL_TIME_INVALID
E_CHECKSUM_FORMAT_INVALID / E_PART_TOO_LARGE
E_CLOCK_REGRESSION / E_VERSION_CONFLICT / E_EVENT_CONFLICT
E_PART_MISSING / E_PART_FILE_MISSING
E_CHECKSUM_MISMATCH / E_DIGEST_MISMATCH
E_SESSION_ABORTED / E_SESSION_COMPLETED / E_OBJECT_ALREADY_COMPLETED
E_COMPLETION_PENDING / E_STORE_CONFLICT / E_STORE_NOT_LOADED
```

CLI 对领域错误返回退出码 1，并在 stdout 输出稳定 JSON；`verify` 在发现不一致时返回退出码 1，但输出完整检查报告。
