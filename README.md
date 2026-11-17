# SLPGuard

SLPGuard 是一个纯 Python 3.11+、离线、确定性的本地协议状态机脆弱性验证组件。它面向自研 `SLP1` 二进制会话记录，提供严格解析、状态机判定、异常注入、并发批处理、检查点恢复、JSON 报告与命令行入口。实现只使用标准库；测试额外使用 pytest。

本仓库不访问网络、不启动容器，也不向任何外部目标发送报文。

## 快速开始

Linux/macOS/WSL：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --find-links ./wheels "pytest>=7"
python scripts/generate_samples.py
python -m pytest
python examples/run_demo.py
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheels "pytest>=7"
python scripts\generate_samples.py
python -m pytest
python examples\run_demo.py
```

若测试机已能访问本地 wheel 目录，可将 `wheels` 替换为实际的离线 wheel 路径。运行批处理前，程序会清理 `reports` 与 `checkpoints` 中匹配 `*.tmp` 的残留文件；也可用 `--no-clean-temp` 关闭。

## 目录

```text
slpguard/                 可集成组件
  protocol.py             二进制编解码、边界校验、资源计量
  analyzer.py             状态机、跨会话判定、真值观测
  injector.py             确定性异常注入
  batch.py                并发批处理、报告聚合
  checkpoint.py           文件锁、版本化检查点
  cli.py                  命令行入口
samples/                  确定性样本与 manifest
reports/                  JSON 报告输出
checkpoints/              检查点输出
scripts/generate_samples.py
examples/run_demo.py
tests/                    41 项 pytest 测试
```

## SLP1 二进制格式

所有整数为小端序。

### 文件头（12 字节）

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 4 | 魔数 `b"SLP1"` |
| 4 | 1 | 版本，仅允许 1 或 2 |
| 5 | 1 | 文件级扩展位，当前必须为 0 |
| 6 | 2 | 报文数量 `message_count` |
| 8 | 4 | 声明总长，等于整个文件字节数 |

### 报文头（28 字节）+ 可选载荷

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 4 | 同步标记 `b"MSG1"` |
| 4 | 4 | 全局唯一 `sync_id`，非 0 |
| 8 | 4 | `session_id`，非 0 |
| 12 | 4 | 会话内 `sequence`，非 0 |
| 16 | 1 | 阶段码：HELLO=1、AUTH=2、NEGOTIATE=3、DATA=4、CLOSE=5 |
| 17 | 1 | 方向：C2S=0、S2C=1 |
| 18 | 1 | 扩展位，见下文 |
| 19 | 1 | 保留，必须为 0 |
| 20 | 4 | 期望业务码位图 `truth_mask` |
| 24 | 4 | 载荷长度 |
| 28 | N | 载荷 |

`truth_mask` 的 bit N 对应稳定业务错误码 ID N。例如 bit 8 表示 `E_AFTER_CLOSE`。位图只用于样本内真值记录；解析和状态机判定本身不依赖它。

每个报文的 `sync_id` 与 `truth_mask` 都必须由样本显式携带。验证函数 `verify_truth()` 只读取已解析记录中的这两个字段，不接受注入参数或注入器中间对象。

### 扩展位

| 位 | 名称 | 合法性 |
|---:|---|---|
| 0x01 | `RENEGOTIATE` | 仅 NEGOTIATE |
| 0x02 | `HALF_CLOSE` | 仅 CLOSE |

未知位、保留字节非 0、方向非法、扩展位与阶段不匹配均返回 `E_EXT`，不会静默忽略。

## 稳定业务码

业务码名称和整数 ID 是磁盘格式的一部分，不可重编号。

| ID | 名称 | 含义 |
|---:|---|---|
| 0 | `E_OK` | 无期望违规 |
| 1 | `E_MAGIC` | 文件魔数错误 |
| 2 | `E_VERSION` | 版本不在白名单 |
| 3 | `E_FORMAT` | 记录标记、计数、保留真值位等格式错误 |
| 4 | `E_BOUNDS` | 长度/偏移越界 |
| 5 | `E_PHASE` | 未知阶段码 |
| 6 | `E_EXT` | 未知或非法扩展组合 |
| 7 | `E_TRANSITION` | 非法阶段迁移、重复序号/阶段 |
| 8 | `E_AFTER_CLOSE` | CLOSE 或半关闭后继续发送 |
| 9 | `E_INCOMPLETE` | 缺少必需阶段 |
| 10 | `E_MIXUP` | 跨会话序号复用、方向矛盾、重协商声明缺失 |
| 11 | `E_STORM` | 重协商次数超过会话上限 |
| 12 | `E_STEPS` | 单会话步数超限 |
| 13 | `E_TIMEOUT` | 单文件真实墙钟耗时超限 |
| 14 | `E_LOCK` | 检查点文件锁冲突 |
| 15 | `E_IO` | 文件 I/O 失败 |
| 16 | `E_ARG` | 参数错误 |
| 17 | `E_CHECKPOINT` | 检查点/完整性封装错误 |
| 18 | `E_TRUTH` | 保留的真值业务码 |

解析错误包含 `offset`；能定位到报文时还包含 `session_id`、`sequence` 和 `sync_ids`。载荷或记录头越界时，`offset` 指向相关长度字段或记录起点。

## 状态机语义

基础阶段链：

```text
HELLO -> AUTH -> NEGOTIATE -> DATA -> CLOSE
```

合法边界迁移：

- 每个合法会话必须包含全部五个阶段；缺失任一阶段返回 `E_INCOMPLETE`。
- 首报文必须是 HELLO；AUTH 前出现 DATA、阶段跳跃、阶段回退均返回 `E_TRANSITION`。
- 同一 `sequence` 在同一会话重复返回 `E_TRANSITION`。
- 第一个 CLOSE 之后的任意报文返回 `E_AFTER_CLOSE`，不再重复产生状态迁移错误。
- DATA 之后允许 `NEGOTIATE(RENEGOTIATE)` 再进入 DATA，但该 NEGOTIATE 必须声明 `RENEGOTIATE` 且此前存在基础 NEGOTIATE。
- 会话内方向标记必须一致；不一致返回 `E_MIXUP`。
- 同一 `sequence` 跨会话出现返回 `E_MIXUP`，冲突序号写入 `sequences`。
- 重协商次数按会话累计；超过 `max_renegotiations` 时对超限记录返回 `E_STORM`。上限按会话独立应用。

判定前按 `(session_id, sequence, sync_id, offset)` 归一化。最终会话按 `session_id` 排序，会话内违规按 `(最小序号, 业务码 ID, sync_ids)` 排序，因此物理报文顺序和线程完成顺序不影响结论。

## 资源隔离

- `max_steps_per_session`：解析每个完整报文后累计该会话步数；达到上限时在下一报文前返回 `E_STEPS`。不同会话分别计数。
- `max_file_seconds`：使用 `time.perf_counter()` 测量真实墙钟时间，从单文件读取/解析开始计时，不使用 sleep 或固定常量伪造成超时。
- 超限只终止当前文件，返回 `status="limited"`、`termination`、`processed_messages`、`declared_message_count` 与已计数步骤；同批其他文件继续执行。

## Python 接口

```python
from pathlib import Path
from slpguard import (
    BatchConfig, InjectionSpec, JudgeConfig, ResourceLimits,
    analyze_path, inject, judge, parse_file, run_batch,
)

# 解析
parsed = parse_file("samples/valid_slp.slp")

# 判定
sessions = judge(parsed, JudgeConfig(max_renegotiations=3))

# 单文件检测（含资源限制和真值观测）
result = analyze_path(
    "samples/half_close_violation.slp",
    limits=ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0),
    judge_config=JudgeConfig(max_renegotiations=3),
)

# 注入
injected = inject(
    Path("samples/valid_slp.slp").read_bytes(),
    InjectionSpec(target_session=1, half_close_append=True),
    "examples/injected.slp",
)

# 批处理
report = run_batch(
    "samples",
    config=BatchConfig(workers=4),
    workdir=".",
)
```

四类可集成接口为：

- 解析：`parse_bytes`、`parse_file`
- 判定：`judge`、`analyze_bytes`、`analyze_path`、`verify_truth`
- 注入：`InjectionSpec`、`inject`
- 批处理：`BatchConfig`、`run_batch`、`normalized_results`

## 注入参数

`InjectionSpec` 字段：

| 字段 | 语义 |
|---|---|
| `target_session` | 重协商/半关闭等操作的目标会话；省略时选最小会话 ID |
| `stage_jump_sequence` | 删除该序号的报文以强制阶段跳跃；下一报文标记 `E_TRANSITION`，会话末报文标记 `E_INCOMPLETE` |
| `physical_swap_positions` | 交换两个物理报文位置；语义真值不变 |
| `mixin_source_session` | 跨会话混入源会话 |
| `mixin_target_session` | 跨会话混入目标会话 |
| `mixin_sequence` | 从源会话复制的序号，目标副本沿用该序号 |
| `renegotiation_count` | 会话内目标重协商总数；只增加缺少的循环 |
| `max_renegotiations` | 注入时用于标记 `E_STORM` 的上限 |
| `half_close_append` | 给目标 CLOSE 加 `HALF_CLOSE` 并追加 DATA，标记 `E_AFTER_CLOSE` |

注入器只负责生成可解析的畸形样本并写入显式真值。验证端只根据样本内的 `sync_id + truth_mask` 与规范化判定的交集/差集计算 `matched`、`mismatched`、`unexpected`。若检测结果与样本真值不一致，文件状态为 `verification_failed`。

## 批处理、检查点与报告

`run_batch()` 使用 `ThreadPoolExecutor` 并行处理文件，但由协调线程按固定顺序完成检查点写入；结果最终按相对路径排序。检查点中的已完成文件键由“相对路径 + 内容 SHA-256”决定，避免文件名相同但内容变化时误复用。

检查点：

- 默认路径：`checkpoints/checkpoint.json`
- 保存已完成文件结果和聚合结论
- 使用 `flock`（POSIX）或 `msvcrt.locking`（Windows）非阻塞文件锁；冲突返回 `E_LOCK`
- 以下任一变化会整体重建：检查点结构版本、阶段码白名单版本、分析配置指纹
- 临时文件写入、fsync、内容复核后再 `os.replace`

报告默认路径：`reports/slp-report.json`。报告本身也是带 SHA-256 完整性封装的 JSON，核心结构如下：

```json
{
  "envelope": "slp-integrity-v1",
  "kind": "slp-report",
  "sha256": "...",
  "payload": {
    "batch": {
      "files": 21,
      "success": 14,
      "limited": 0,
      "failed": 7,
      "schedule_summary": {
        "files": 21,
        "success": 14,
        "limited": 0,
        "failed": 7
      },
      "truth_matched_events": 19,
      "truth_mismatched_events": 0
    },
    "rejections": [],
    "files": []
  }
}
```

`success` 表示文件完成了分析（即使检测出业务违规）；`limited` 表示 `E_STEPS`/`E_TIMEOUT`；`failed` 表示解析/I/O 失败或真值观测不一致。CLI 会另外检查业务违规决定退出码。

## CLI

```bash
python -m slpguard samples/valid_slp.slp \
  --report reports/valid.json --no-checkpoint

python -m slpguard samples \
  --workers 8 --max-steps 1000 --max-seconds 15 \
  --max-renegotiations 3 --report reports/batch.json
```

主要选项：`--workers`、`--max-steps`、`--max-seconds`、`--max-renegotiations`、`--workdir`、`--report`、`--no-resume`、`--no-checkpoint`、`--no-clean-temp`、`--quiet`。

退出码：

- `0`：全部文件完成分析，无业务违规、无真值不一致、无超限/解析失败/锁错误
- `1`：部分或全部文件存在业务违规、真值不一致、超限、解析/I/O/锁错误
- `2`：命令行参数错误或输入路径不存在

JSON 报告同时写到 `--report`，默认还会输出到 stdout；`--quiet` 只抑制 stdout。

## 测试与实测结果

测试覆盖解析边界、物理乱序归一化、重复/阶段跳跃/AUTH 前 DATA、跨会话混入、方向矛盾、重协商缺失与风暴、半关闭后发送、步数/墙钟超限、注入真值、检查点版本重建、锁冲突、子进程强制终止恢复、并发一致性和 CLI 退出码。

本次实际执行环境：Linux/WSL 风格工作区，Python 3.12.13，pytest 9.2.0。

```text
$ python -m pytest
41 passed in 1.61s

$ python examples/run_demo.py
concurrency_consistent: true
injection_codes: ["E_AFTER_CLOSE"]
injection_truth_consistent: true

$ python -m slpguard samples --workers 8 --no-checkpoint \
    --report reports/slp-report.json
exit code: 1
batch: files=21, success=14, limited=0, failed=7
truth: matched=19, mismatched=0, unexpected=0
```

原生 Windows 的 `msvcrt.locking` 分支已实现，但本仓库的实际测试记录来自当前 Linux/WSL 环境；Windows 上建议先运行完整 pytest，再执行示例。

确定性样本共 21 个，`samples/manifest.json` 记录用途和预期业务码。真值本身始终在二进制报文的 `sync_id + truth_mask` 内，manifest 只用于人工说明。
