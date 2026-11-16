# 技术说明：需求到实现的映射与验证结果

环境：WSL2 Python 3.11.0rc1（目标 Windows 官方 Python 3.11+，Tkinter 随安装包提供）。
全部代码仅使用标准库；网络边界为 `127.0.0.1` 回环 TCP，无第三方包、无容器、无线上服务。

## 1. 确定性分片与摘要
- 实现：`oswb/chunker.py` 的 `build_manifest` 顺序读取固定偏移区间，逐片计算摘要，
  与分片序号、对象键一起登记到会话清单。
- 确定性：纯函数、无随机源；同文件同分片大小两次产出的清单逐字段相等
  （测试 `test_deterministic_manifest`）。
- 非法值：`validate_chunk_size` 对非正整数或超过文件长度的分片大小抛 `E_CHUNK`，
  切分阶段不打开任何持久化句柄（测试 `test_illegal_chunk_creates_no_files`）。
- 空文件规范化为单个 0 长度分片（`test_empty_file_single_empty_chunk`）。

## 2. 会话状态机与断点续传
- 状态：`created/uploading/verifying/committed/aborted`，迁移集中在
  `storage.Storage._transition`，非法迁移抛 `E_STATE`。
- 持久化：`sessions` + `session_chunks` 表记录会话标识、对象键、分片总数、
  已完成分片集合与每片登记摘要/长度/文件路径。
- 恢复：续传完全基于服务端持久化状态（`resume_upload` 重新 `get_session`，
  跳过 `completed` 集合）；测试 `test_resume_after_interruption_matches_one_shot`
  用全新客户端对象模拟重开工作台，验证续传对象摘要 == 一次性上传摘要 == 源文件摘要。

## 3. 分片上传、摘要校验与合并
- 上传：`upload_chunk` 校验长度（不符 `E_CHUNK`）与摘要（不符 `E_INTEGRITY`），
  原子落盘后回写 `uploaded=1`。
- 合并前：`verify_session` 对每片重新计算文件摘要并与登记摘要比较，任何不一致
  拒绝合并并把会话退回 `uploading` 保留续传（测试
  `test_chunk_truncation_is_e_chunk_and_keeps_session`）。
- 原子可见性：对象写 `*.tmp` → `fsync` → `os.replace`，随后在单事务内插入
  `object_versions` 并更新 `objects.current_version` 完成标记，保证标记与内容同时可见。

## 4. 并发上传与资源控制
- 双重配额：`uploader.Scheduler` 用全局 `BoundedSemaphore(global_concurrency)`
  与每会话 `BoundedSemaphore(session_quota)`，先取会话配额再取全局配额，
  配额耗尽即排队（测试 `test_global_and_session_limits_are_respected`、
  `test_two_sessions_share_global_cap` 用慢客户端观测实际并发峰值不超限）。
- 重复收敛：服务端会话级锁串行化写入，已 `uploaded` 的分片直接幂等返回；
  客户端对同序号在途任务去重（测试 `test_quota_bounds_and_duplicate_convergence`）。
- 观测：每次尝试记录 `start_ms/end_ms/duration_ms/attempt`，时间基准为
  `time.monotonic_ns()//1_000_000`（单调毫秒），输出按 `(index, attempt)` 稳定排序。

## 5. 元数据多版本与整体替换
- 版本：同一对象键每次提交 `version = current+1` 单调递增；新版本整行写入
  `object_versions`，`labels` 等字段整体替换，不与旧版本合并。
- 只读与差异：旧版本 `read_only=true`；`diff_versions` 稳定列出变化字段与
  分片清单的 added/removed/modified 项（测试
  `test_versions_are_monotonic_and_whole_replaced`）。

## 6. 一致性审计
- `Storage.audit` 按对象键排序，全量比较：当前版本行存在性、引用分片集合与
  磁盘 `.part` 文件集合是否完全一致、逐片摘要、对象内容摘要、版本表与对象表摘要。
- 差异返回 `E_AUDIT`，结论含 `object_key`、`first_chunk_index`、`difference_type`，
  可定位注入的首个差异分片（测试
  `test_audit_reports_first_differing_chunk_with_e_audit`）。

## 7. 快照与版本识别恢复
- 快照：`create_snapshot` 拷贝分片/对象字节与 SQLite 库，记录每对象
  `{version, content_digest, chunk_set_digest}` 指纹和当时审计结论。
- 恢复：先在暂存副本上重算指纹并与快照记录比对；随后整体清空并替换当前
  chunks/objects/db（非增量合并），再重放审计，结论不一致抛 `E_REPLAY`
  （测试 `test_snapshot_restore_reproduces_audit_and_e_replay`）。

## 8. 故障注入与结构化日志
- 三类注入均只改输入文件或参数，不修改工作台源码：
  - 分片文件截断 `inject chunk_truncation` → 合并期 `E_CHUNK`；
  - 元数据分片摘要损坏 `inject metadata_digest_corruption` → `E_INTEGRITY`；
  - 外部推进对象当前版本制造冲突 `inject version_conflict` → `E_VERSION`。
- 所有注入与状态变迁通过 `logging_utils` 写 JSONL（字段排序、含 ts/event/context）。

## 实际验证结果（本机执行）

- `python -m unittest discover -s tests`：**20/20 通过**，约 10 秒，进程干净退出。
- `python -m oswb demo`：12 项检查全部 `PASS`，总业务码 `OK`，覆盖确定性切分、
  中断续传一致性、非法分片、并发调度、三类故障码、多版本整体替换、干净审计、
  快照恢复与 `E_REPLAY`。
- 双进程手工验证（`serve` 于端口 9211 + 独立 CLI 客户端）：
  上传 4 片 → 提交 v1 → 新分片大小提交 v2 → 版本差异（chunk_size/total_chunks/labels
  变化、8 项分片清单变化）→ 审计 `ok=True` → 快照创建/恢复审计一致；
  三类注入分别观测到 `E_CHUNK(index=0)`、`E_INTEGRITY(index=2)`、
  `E_VERSION(expected=2,current=3)`。

## 已知边界与说明
- 本环境 WSL 的 Python 未安装 Tkinter，`oswb gui` 会输出友好提示；Windows 官方
  安装包默认含 Tkinter，可直接启动界面。GUI 逻辑与 CLI 共用同一客户端与工作流。
- 回环服务为线程化实现，配合每会话锁与短连接，满足本地样本规模的演示与测试；
  生产级高并发应替换为进程/协程模型与 WAL 调优，接口契约保持不变。
