# ckptlab：确定性分片检查点与诊断工作台

从零实现的本地检查点管理组件。它在确定性样本上覆盖：

- 按层名哈希分片的并行落盘，以及并发上限与注入逻辑时间预算；
- 内容寻址分片复用、增量补丁、全量/增量清单同一性；
- 清单顺序拼接与逐层独立重建的双路径恢复裁决；
- SQLite 原子账本、文件锁、多进程幂等提交；
- `os._exit` 强杀后的半提交识别、未完成分片重放与临时文件清理；
- `save / restore / resume / status / verify` CLI 和仅回环监听的 Flask 工作台。

不使用 Docker、网络、账号、外部数据集或真实加速器。

## 快速开始

目标运行时为 Python 3.11+、NumPy、Flask、pytest。Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m pytest
```

Linux/macOS/WSL：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest
```

所有状态都在本机 SQLite 与目录中，网络不是依赖项。

## 样本与命令

主样本：

- `samples/training_plan.json`：版本 1，全量保存；
- `samples/training_plan_v2.json`：版本 2，仅一个层发生变化；
- `samples/shard_policy.json`：4 分片、每分片 2 MiB 上限、SHA-256 分配；
- `samples/crash_injection.json`：关闭的崩溃注入模板；
- `samples/crash_after_two_shards.json`：提交 2 个分片后强杀；
- `samples/crash_during_manifest_write.json`：清单临时文件写到一半时强杀。

```bash
python -m ckptlab.cli save \
  --plan samples/training_plan.json \
  --shards samples/shard_policy.json \
  --db runtime/ledger.sqlite3 \
  --store runtime/store

python -m ckptlab.cli save \
  --plan samples/training_plan_v2.json \
  --shards samples/shard_policy.json \
  --db runtime/ledger.sqlite3 \
  --store runtime/store

python -m ckptlab.cli status \
  --db runtime/ledger.sqlite3 --store runtime/store

python -m ckptlab.cli verify \
  --db runtime/ledger.sqlite3 --store runtime/store

python -m ckptlab.cli restore \
  --db runtime/ledger.sqlite3 --store runtime/store \
  --output-dir runtime/restored

python -m ckptlab.cli serve \
  --db runtime/ledger.sqlite3 --store runtime/store \
  --host 127.0.0.1 --port 8766
```

`status` 始终输出 `sort_keys=True` 的稳定 JSON；`save/resume` 未形成完整检查点时退出码为 `3`，业务错误退出码为 `1`，错误 JSON 中包含稳定业务码。

## 强杀恢复

```bash
# 预期进程退出码 91，指针仍为空
python -m ckptlab.cli save \
  --plan samples/training_plan.json \
  --shards samples/shard_policy.json \
  --crash-plan samples/crash_after_two_shards.json \
  --db runtime/crash.sqlite3 --store runtime/crash-store

# 已提交分片命中，缺失分片重放
python -m ckptlab.cli resume \
  --plan samples/training_plan.json \
  --shards samples/shard_policy.json \
  --db runtime/crash.sqlite3 --store runtime/crash-store
```

恢复后的清单摘要与未中断运行逐字段一致。`resume` 也支持不传 `--plan/--shards`，此时使用未完成构建已持久化的内容计划。

## 诊断工作台

Flask 仅通过 CLI 接受回环地址；`0.0.0.0` 等地址会在启动前被拒绝。工作台提供：

- `/api/status`：指针、水位、分片命中、放弃原因、恢复判定依据；
- `/api/verify`：清单/分片摘要及双路径恢复校验；
- `/api/restore`：只读恢复裁决；
- `/api/health`：健康检查。

当前验收环境实测 `127.0.0.1:8765` 已被占用，`127.0.0.1:8766` 可绑定，因此默认演示端口为 `8766`。

## 实际验证

- `python -m pytest -q`：`27 passed`。
- `python scripts/run_acceptance.py --output results/verification.json`：`12/12` 端到端检查通过。
- v1 清单摘要：`bb3281a1e511651950d1c2f8c51e9a9e7c90000f34056b8dd5815bdea4be3304`
- v2 增量与独立全量清单逐字节一致：`e80899cb35457b414c85500e0779a6fccf0fbd7859ced04345b27df37d33e058`
- v2 增量命中 3 个分片、实际写入 1 个分片。
- 强杀后恢复只重放 1 个缺失分片，得到与未中断 v1 相同的清单。
- 双进程同版本提交：一个正常提交、一个幂等返回；账本只有 1 个构建和 4 条分片提交。

详细结果见 `results/verification.json` 与 `results/verification.md`，一致性协议见 `docs/DESIGN.md`。

## 环境说明

本次代码在 WSL2/Ubuntu 上的 Python `3.10.12`、NumPy `1.26.4`、Flask `3.0.3`、pytest `8.3.3` 中实际运行。项目声明并要求 Python `>=3.11`；当前容器未安装 3.11，因此“Windows + Python 3.11”没有在本机实际执行。Windows 文件锁分支使用 `msvcrt.locking`，POSIX 分支使用 `flock`，两者在代码中隔离实现。建议在 Windows 上按上面的 PowerShell 命令做一次本地冒烟测试。
