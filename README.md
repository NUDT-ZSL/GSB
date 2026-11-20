# Local Lease Coordinator

一个完全本地、可重复、无第三方运行时依赖的租约选举与故障转移引擎。应用使用注入的逻辑时钟推进租约，不读取系统墙钟；租约、选举结论、精度敏感记录和时钟水位以 SQLite 事务持久化，并通过文件锁与唯一约束保证多进程幂等。

## 环境

- Python 3.11+，仅在标准库上运行。
- SQLite 3.35+，启用 WAL、`BEGIN IMMEDIATE` 和 `synchronous=FULL`。
- Windows、Linux、macOS 均可；Windows 文件锁通过 `msvcrt`，其余平台通过 `fcntl`。

本机已用 `uv python install 3.11` 安装 Python 3.11.15。检查环境：

```bash
python3.11 scripts/check_env.py
```

## 快速开始

固定样本位于 `workspace/`：

- `nodes.json`：候选节点清单。
- `election_config.json`：安全余量、法定人数、租约上限、定点标度、时钟与精度容差。
- `clock_steps.json`：固定逻辑时钟步进表。

```bash
python3.11 -m leasecoord load --workspace workspace --reset --clock 0
python3.11 -m leasecoord elect --workspace workspace --clock 0
python3.11 -m leasecoord renew --workspace workspace --node alpha --session-epoch 1 --clock 100
python3.11 -m leasecoord status --workspace workspace --clock 1000
python3.11 -m leasecoord verify --workspace workspace
python3.11 -m leasecoord resume --workspace workspace
```

所有命令输出稳定排序的 JSON。成功返回码为 `0`；业务失败返回 `2`，格式为：

```json
{
  "ok": false,
  "error": {
    "code": "E_BAD_LEASE",
    "message": "...",
    "details": {}
  }
}
```

## 固定演示

```bash
python3.11 scripts/demo.py --workspace workspace --workers 8 --compact
```

演示会：

1. 加载样本并在逻辑时钟 `0` 授予三个租约，`bravo`（优先级 30）成为主节点。
2. 持续续约 `alpha` 与 `charlie`，`bravo` 租约在 `1000ms` 到期。
3. 在 `1000ms` 故障转移，`charlie` 与 `alpha` 同优先级、同租约代数，安全余量分别为 `850.0002ms` 和 `850ms`，差值 `0.0002ms` 小于 `0.001ms` 容差，因此标记精度敏感排序。
4. 在 `1849.9999ms` 只读视图中，两节点均落入安全余量边界带，但持久化水位仍为 `1000ms`。
5. 运行 8 进程并发授予与幂等续约检查。
6. 分别强制终止在 `after_prepare`、`after_final_insert`、`after_commit`，再执行恢复。

两次干净运行输出应逐字节相同：

```bash
python3.11 scripts/demo.py --workspace workspace --compact > /tmp/demo-a.json
python3.11 scripts/demo.py --workspace workspace --compact > /tmp/demo-b.json
cmp /tmp/demo-a.json /tmp/demo-b.json
```

`scripts/record_verification.py` 会重新运行测试、演示和两个干净轮次，并写入 `artifacts/verification.json`。

## CLI

| 命令 | 作用 |
| --- | --- |
| `load` | 校验候选与配置；非法输入不创建数据库，也不写任何状态。`--reset` 建立新的确定性运行时。 |
| `elect` | 首次调用执行原子租约授予；后续调用在主节点失效时执行故障转移。同钟同状态重复调用幂等。 |
| `renew` | 校验节点、会话代数、租约代数、数据版本后原子续约。同请求重复提交返回缓存结果。 |
| `resume` | 仅依据持久化状态清理半提交记录、回收过期租约并重建选举。 |
| `status` | 在指定视图时钟输出稳定节点排序、剩余租约、定点残差、边界与精度敏感标记。只读。 |
| `verify` | 重算时钟、租约、任期与选举结论，检查半提交、租约上限和精度记录。 |

`elect`、`renew` 支持测试用故障注入：

```bash
python3.11 -m leasecoord elect --workspace workspace --clock 0 --crash-at after_prepare
```

进程以退出码 `86` 被强制终止。随后必须运行 `resume` 清理。

## 持久化与错误码

运行时文件位于 `workspace/coord.db`、WAL/SHM 文件和 `.coord.lock`。SQLite 只向读取方暴露已提交快照；写入路径使用文件锁和 `BEGIN IMMEDIATE` 串行化。

主要错误码：

- `E_BAD_LEASE`、`E_BAD_HEARTBEAT`：租约时长、心跳周期约束失败。
- `E_BAD_QUORUM`、`E_BAD_MAX_LEASES`：法定人数或租约上限非法。
- `E_BAD_SAFETY_MARGIN`、`E_BAD_SCALE`、`E_BAD_ROUNDING`：定点或安全参数非法。
- `E_STALE_SESSION`、`E_SESSION_MISMATCH`、`E_STALE_LEASE`、`E_STALE_DATA_VERSION`：续约鉴权失败。
- `E_LEASE_EXPIRED`、`E_NOT_LEASE_HOLDER`：无有效持有租约。
- `E_CLOCK_REWIND`、`E_CLOCK_JUMP`：逻辑时钟回拨或单步跳跃超限。
- `E_RECOVERY_REQUIRED`：存在半提交记录，需要先执行 `resume`。
- `E_VERIFY_FAILED`：持久化状态与重算的选举结论不一致。

## 测试

```bash
python3.11 -m unittest discover -v
```

测试覆盖非法输入零写入、会话代数单调、定点残差与误差上界、故障转移与精度敏感、边界视图、续约鉴权、时钟错误、三类强制终止、6/8 进程并发幂等，以及两轮演示逐字节一致。

更完整的设计与证明见 [TECHNICAL.md](TECHNICAL.md)。
