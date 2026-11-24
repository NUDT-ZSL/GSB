# 离线互操作验证工作台

本交付不访问网络、不使用 Docker。所有时间裁决只读取样本中的 `samples/clock.json#observed_at_ms`，代码中不调用系统当前时间。

## Windows 快速开始

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install pytest
python generate_samples.py --output samples --seed 20261125
python -m pytest -q
.\scripts\demo.ps1
```

CLI 也可用根目录包装器 `python workbench.py ...`；等价模块入口为 `python -m auth_interop.cli ...`。

也可以逐条执行：

```powershell
python -m auth_interop.cli import --sample-dir samples --state-dir state --report artifacts/reports/import-report.json
python -m auth_interop.cli redeem --state-dir state --clock samples/clock.json --attempts samples/redemption_attempts_success.json --report artifacts/reports/redeem-success.json
python -m auth_interop.cli redeem --state-dir state --clock samples/clock.json --attempts samples/redemption_attempts.json --report artifacts/reports/redeem-report.json
python -m auth_interop.cli revoke --state-dir state --clock samples/clock.json --requests samples/revocation_requests.json --report artifacts/reports/revoke-report.json
python -m auth_interop.cli checkpoint --state-dir state
python -m auth_interop.cli view --state-dir state --revocations samples/revocation_requests.json
```

## 模块与需求映射

- `auth_interop/samplegen.py`：固定种子 `20261125` 生成 v1 单文件 JSON、v2 分片 JSON、v2 SQLite 元数据库、观测时钟、兑换/撤销输入和批量样本。
- `auth_interop/parser.py`：两代格式规范化；v1 将 `issued_at_offset_seconds + expires_in_seconds` 转为绝对毫秒，v2 直接读取毫秒时间戳；输出含 `source_version`、`path`、`record_no`、`record_index` 和稳定码的拒绝记录。
- `auth_interop/merge.py`：以 `code:{client_id}:{code}`、`session:{id}`、`access_token:{id}`、`refresh_token:{id}` 为实体键；按 `(generation, as_of_ms, source_rank, path, record_no)` 选择权威来源；墓碑为最新时覆盖旧活动记录；客户端、作用域集合或过期时刻不一致时输出 `E_STATE_CONFLICT`。
- `auth_interop/pkce.py`：实现 S256 与 `plain` 校验，S256 使用 `BASE64URL(SHA256(verifier))` 并去除 `=` 填充。
- `auth_interop/adjudicate.py`：授权码兑换、重用、第二次兑换重放回溯、客户端/重定向匹配、PKCE 不匹配和降级判定。
- `auth_interop/graph.py`：构造 `会话 -> 授权码 -> 访问令牌 -> 刷新令牌 -> 轮换后继` 依赖边，计算排序无关的传递闭包并列出环路径。
- `auth_interop/checkpoint.py`：SQLite `checkpoint/checkpoint_history/applied_batches` 加 `snapshots/checkpoint-<watermark>.json`；结构版本或合并口径变化时整体重建。
- `auth_interop/locking.py`：Windows 使用 `msvcrt.locking(..., LK_NBLCK, 1)`，非 Windows 开发/CI 使用 `fcntl.flock` 回退；锁冲突返回 `E_LOCK`。
- `auth_interop/io_utils.py`：报告和快照均先写隐藏 `.tmp` 文件、回读解析校验，再 `os.replace` 原子替换；恢复时清理原子写临时文件和未提交的高水位孤儿快照。
- `auth_interop/cli.py`：`import`、`redeem`、`revoke`、`checkpoint` 四类主命令，外加 `view` 本地诊断视图和 `generate-samples` 便利命令。

## 退出码

- `0`：命令范围内全部成功。
- `1`：样本或裁决存在部分/预期业务失败，例如 `E_CODE_EXPIRED`、`E_CODE_REPLAY`、`E_STATE_CONFLICT`、`E_TOKEN_CYCLE`、`E_LOCK`。
- `2`：命令行参数、必填文件或检查点输入不可用。argparse 参数错误也使用退出码 `2`。

主样本故意包含拒绝、冲突、过期、重放、PKCE 降级和环，因此完整诊断命令返回 `1` 是预期结果；`redemption_attempts_success.json` 对应全成功路径，退出码为 `0`。

## 关键稳定业务码

解析类包括 `E_INVALID_JSON`、`E_INVALID_RECORD`、`E_DUPLICATE_CODE`、`E_DUPLICATE_CLIENT`、`E_DUPLICATE_SESSION`、`E_NON_MONOTONIC_TIMESTAMP`、`E_METADATA_MISMATCH`。

裁决类包括 `E_STATE_CONFLICT`、`E_CODE_EXPIRED`、`E_CODE_NOT_ACTIVE`、`E_CODE_REUSED`、`E_CODE_REPLAY`、`E_CLIENT_MISMATCH`、`E_PKCE_MISMATCH`、`E_PKCE_DOWNGRADE`、`E_ENTITY_TOMBSTONED`。

检查点/图类包括 `E_TOKEN_CYCLE`、`E_LOCK`、`E_CHECKPOINT_CORRUPT`、`E_VERSION_REBUILD_REQUIRED`。

## 崩溃与并发边界

- SQLite 中的单例检查点行是提交标记；事务提交后才发布最终快照。
- 若 SQLite 已提交而快照缺失，`recover()` 用 SQLite 中的 `state_json` 修复快照。
- 若快照已写到高水位但 SQLite 未提交，恢复只读取 SQLite 最后水位，并删除高于该水位的孤儿 `checkpoint-*.json`。
- 强杀留下的 `.*.tmp` / `*.tmp` 会在下次 `recover()` 时清理；正式 JSON 路径只会是旧完整文件或新完整文件。
- 写检查点前获取 `state/checkpoint.lock` 的非阻塞排他锁；冲突立即得到 `E_LOCK`，不会覆盖已提交检查点。

## 确定性样本场景

生成的主样本包含：正常 v1/S256/plain 兑换、过期、已使用、两次重放、客户端不匹配、重定向不匹配、PKCE 错误、缺少 verifier、v1 无 PKCE 被 v2 PKCE 替换、状态冲突、墓碑、正常撤销链和刷新令牌环。`samples/batch/` 包含两个干净样本和一个坏 JSON 样本，用于验证单样本失败不中断同批其他样本。
