# KLP 密钥生命周期审计引擎

面向本地 KLP（Key Lifecycle Protocol）二进制会话记录的离线审计组件。项目不访问网络、账号或外部服务，提供严格解析、顺序无关状态机、派生链一致性校验、确定性规模降级、真实资源预算、检查点恢复、原子 JSON 报告、命令行入口、样本生成器和 pytest 测试集。

## 交付内容

```text
klp_audit/
  parser.py          严格二进制解析、CRC、记录级拒绝
  normalization.py   按会话/阶段序号稳定排序、幂等去重、冲突检测
  state_machine.py   协商→派生→轮换→封存→销毁状态机
  derivation.py      派生森林重建、盐/长度/迭代规则及父子路径
  adaptive.py        基于观测规模的全量/分块采样策略
  checkpoint.py      版本化、完整性校验、水位恢复
  atomic_io.py       跨平台文件锁、临时文件、原子替换
  engine.py          并发/预算调度、逐会话结论、报告组装
  generator.py       确定性合法/畸形样本与真值清单
  cli.py             `generate` / `audit` 命令及退出码
tests/               35 项 pytest 验收测试
examples/
  integration_demo.py 可集成 API 的完整运行示例
docs/
  architecture.md    状态机、采样、检查点与并发设计说明
```

## 环境准备（Windows 10+）

执行阶段完全离线。联网仅在准备阶段安装依赖。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

运行时仅使用 Python 标准库；NumPy 与 pytest 是准备阶段声明的开发/验收依赖。

文件系统能力：

- POSIX 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`，均为非阻塞独占锁；冲突返回 `E_LOCK`。
- 检查点和报告先写入同目录临时文件，`flush + fsync` 后通过 `os.replace` 整体替换。
- `tests/test_checkpoint.py` 与 `tests/test_report_cli.py` 会实际验证锁冲突、原文件不变和临时文件清理。

## 生成样本

```powershell
python -m klp_audit generate --output-dir artifacts\generated
```

该命令创建：

```text
artifacts/generated/
  samples/       合法与畸形 .klp 文件
  truth/         逐文件真值 JSON 与 manifest.json
  reports/       报告输出目录
  checkpoints/   检查点输出目录
```

真值清单为每个发送端事件显式记录 `sync_marker`、阶段、算法、阶段序号和参数向量。预算/超时样本还在 `expected.config_overrides` 中声明触发预期所需的策略覆盖。测试通过同步标记关联审计报告事件，只比较规范化阶段、算法、序号及参数，不依赖生成器内部对象。

样本矩阵：

| 文件 | 预期 | 覆盖点 |
| --- | --- | --- |
| `legal.klp` | PASS | 多会话、多层派生、记录乱序到达 |
| `idempotent.klp` | PASS | 完全相同序号和内容幂等去重 |
| `state_errors.klp` | FAIL | 非法跳转、缺失阶段、序号冲突 |
| `chain_errors.klp` | FAIL | 盐复用、长度升级、迭代回退 |
| `parse_errors.klp` | FAIL | CRC、阶段、算法、参数、长度错误后继续处理 |
| `scale_small.klp` | PASS | 阈值以下全量校验 |
| `scale_large.klp` | PASS / DEGRADED_PASS | 阈值以上确定性分块采样 |
| `budget.klp` | FAIL | 单会话派生物上限，其他会话继续 |
| `timeout.klp` | FAIL | 真实计时超时 |
| `checkpoint.klp` | PASS | 强制终止后水位恢复 |
| `lock.klp` | PASS | 用于 CLI 锁冲突测试 |

## 审计命令

```powershell
python -m klp_audit audit artifacts\generated\samples\legal.klp `
  --workspace artifacts\generated `
  --report artifacts\generated\reports\legal.json `
  --checkpoint artifacts\generated\checkpoints\legal.checkpoint.json `
  --concurrency 4 `
  --timeout 30 `
  --max-derived 50000 `
  --scale-bytes 1048576 `
  --scale-derived 10000 `
  --sample-ratio 0.25 `
  --chunk-size 8
```

关键参数：

- `--timeout`：单会话真实墙钟上限，`0` 表示立即超时测试，`none` 表示关闭；`time.perf_counter()` 直接判定，不使用人为休眠。
- `--max-derived`：单会话派生物计算上限，超出返回 `E_BUDGET`，不再执行该会话派生链。
- `--scale-bytes` / `--scale-derived`：全量模式阈值。等于阈值仍为全量，严格超过任一阈值才降级。
- `--sample-ratio` / `--chunk-size`：降级时按稳定哈希选择完整分块；实际覆盖比例写入报告。
- `--checkpoint`：省略时关闭检查点；提供时按稳定会话水位持久化。
- `--stdout`：同时把完整 JSON 报告输出到 stdout。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 文件级结论为 `PASS`，无拒绝记录 |
| `1` | `FAIL`、`DEGRADED_PASS`、解析拒绝、预算/超时/锁冲突等业务失败 |
| `2` | CLI 参数、配置、输入路径或读取错误 |

所有业务错误码同时写入 JSON 报告和 stderr（全局锁导致报告无法写入时，stderr 保留 `E_LOCK`）。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_FORMAT` | 文件/记录框架、长度、索引或 UTF-8 格式错误 |
| `E_CRC` | 记录 CRC32 不匹配 |
| `E_STAGE` | 阶段标识不在白名单 |
| `E_ALGORITHM` | 算法标识不在白名单 |
| `E_PARAM` | 参数类型、范围或派生父子关系非法 |
| `E_TRANSITION` | 非法阶段迁移 |
| `E_INCOMPLETE` | 缺失必需生命周期阶段 |
| `E_SEQ_CONFLICT` | 同会话同阶段序号内容冲突 |
| `E_SALT_REUSE` | 同一派生链盐重复 |
| `E_LENGTH_ESCALATION` | 子密钥输出长度超过父密钥 |
| `E_ITER_REGRESSION` | 子节点迭代次数低于父节点 |
| `E_TIMEOUT` | 单会话真实耗时超限 |
| `E_BUDGET` | 单会话派生物计算上限超限 |
| `E_LOCK` | 工作目录锁冲突 |
| `E_INPUT` | 输入文件不可读 |
| `E_IO` | 非预期会话级失败（隔离为该会话） |

## KLP 二进制格式

所有整数为大端序。文件由头部、固定长度目录和记录载荷组成。

### 文件头（20 字节）

```text
magic            4s     "KLPB"
version          u16    1
flags            u16    必须为 0
header_size      u32    20 + 20 * record_count
max_iterations   u32    正整数，所有事件迭代次数上限
record_count     u32    目录项数量
```

### 目录项（每项 20 字节）

```text
payload_offset   u64    绝对文件偏移
payload_length   u32
payload_crc32    u32    payload 的 CRC32
reserved         u32
```

目录使解析器可在单条载荷损坏、CRC 错误或长度异常时跳过该条并继续处理后续记录。

### 记录载荷

```text
record_index     u32    必须等于目录位置
sync_marker      u64    样本真值同步标记
session_id_len   u16    1..128
stage_id         u8     1=negotiate, 2=derive, 3=rotate, 4=seal, 5=destroy
algorithm_id     u8     1=HMAC-SHA256, 2=HKDF-SHA256, 3=PBKDF2-SHA256
stage_seq        u32    同一会话内规范化后严格递增
iterations       u32    1..header.max_iterations
salt_len         u32    实际盐字节数
output_len       u32    输出字节数，必须为正
level            u32    派生层级，1..64
session_id       UTF-8  session_id_len 字节
salt             原始字节 salt_len 字节
```

载荷总长度必须严格等于 `36 + session_id_len + salt_len`。

## 核心判定语义

- 同一会话先按 `(stage_seq, record_index)` 稳定排序；完全相同的事件幂等去重，同序号不同内容产生 `E_SEQ_CONFLICT`。
- 状态机允许连续多个 `derive` 和连续多个 `rotate`，其余阶段只能单步向前；任何跳级、回退、销毁后迁移都产生 `E_TRANSITION`，结束未到 `destroy` 产生 `E_INCOMPLETE`。
- 派生森林以 `level=1` 为链根；`level>1` 节点的父节点是同会话、上一层、序号小于自身且序号最大的节点。
- 盐按派生链分组比较；不同链使用相同盐合法。违规报告包含违规节点、父节点、链标识、父子路径和两个节点的完整链路径。
- 全量模式检查所有派生节点；降级模式按节点稳定排序后切块，以块内容的 SHA-256 排名选择固定数量块。相同输入和配置的模式、覆盖块、覆盖比例完全一致。
- 采样未覆盖节点不报告为 `pass`；文件结论最多为 `DEGRADED_PASS`，退出码为 `1`。
- 超时/预算会话固定为 `fail`，派生检查标记 `timeout`/`not_run`，绝不把未完成校验计入通过。

## 检查点

检查点包含：

- `checkpoint_format_version`
- `report_schema_version`
- `algorithm_policy_version`
- 策略指纹、源文件 SHA-256/大小
- 已处理会话水位、聚合状态、逐会话结论
- 对除 `integrity` 外全部内容的 SHA-256 完整性摘要

恢复时先校验版本、算法策略、配置指纹、源文件和完整性，再要求已处理会话是当前稳定排序列表的连续前缀。任一版本或策略变化都整体重建，不混合旧结论。并发度不进入策略指纹，因此调整并发度不会使可复用结论失效。

## 测试与验证

```powershell
python -m pytest -q
```

当前实际验证结果（2026-11-18，WSL/Linux 本地环境，Python 3.11.15，NumPy 2.4.6，pytest 9.2.0）：

```text
..................................                                       [100%]
35 passed in 1.45s
```

测试覆盖：

- 真值清单按同步标记核对规范化参数；
- 畸形记录逐条拒绝且后续合法记录继续通过；
- 状态机顺序无关、幂等去重、冲突、非法跳转、缺失阶段；
- 三类派生链错误及父子路径；
- 规模阈值两侧、降级覆盖比例、重复运行与并发度不变性；
- 真实超时、计算预算隔离、未完成校验不算通过；
- 锁冲突 `E_LOCK`、原子报告稳定性；
- 强制终止后从水位恢复、版本/完整性变化整体重建；
- CLI 的 `0/1/2` 退出码及报告/stderr 业务码。

当前执行容器不是 Windows，因此 Windows 分支（`msvcrt.locking`、NTFS 上 `os.replace`）已实现但未在本机 Windows 文件系统实跑；Linux/POSIX 分支已实际验证。建议在目标 Windows 10 机器上先运行同一 pytest 集作为部署冒烟测试。

## 可集成 API

```python
from klp_audit import AuditConfig, AuditEngine

config = AuditConfig(concurrency=4, session_timeout_seconds=30.0)
report = AuditEngine(
    config,
    checkpoint_path="checkpoints/session.json",
    workspace=".",
).audit_file("samples/legal.klp")

assert report["file"]["conclusion"] == "PASS"
```

完整示例见 `examples/integration_demo.py`，内部设计见 `docs/architecture.md`。

