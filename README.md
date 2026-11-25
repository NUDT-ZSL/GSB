# LogChain：确定性日志合并、回放、账本与崩溃恢复验证台

本项目在空工作区中交付一个可本地复现的数据库日志/崩溃恢复技术验证模块。它解析 v1 JSON 与
v2 分片 JSON + SQLite 元数据库，合并统一日志链，在可注入逻辑时钟下回放 JSONL WAL，维护行版本、
墓碑、定点提交时刻账本、精度敏感裁决、原子快照指针以及 SIGKILL 后的恢复一致性。

> 说明：初始工作树和固定基线提交中只有题面 `README.md`，没有题述“随题交付”的种子脚本。
> 因此这里交付了固定种子生成器 `scripts/generate_samples.py`；连续两次生成的全部样本
> （包括 `meta.sqlite`）字节一致。

## 1. 环境准备

要求 Linux/WSL Ubuntu、Python 3.11。

```bash
bash scripts/setup_venv.sh
source .venv/bin/activate
```

也可以手动执行：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python scripts/generate_samples.py
```

`sqlite3` 是 Python 标准库；`requirements.txt` 安装 `numpy`。核心账本为避免浮点误差使用
`fractions.Fraction`，不依赖 numpy 做裁决。

## 2. 六个 CLI 子命令

所有命令均支持 `--root PATH` 与 `--clock-ms INTEGER`。`--clock-ms` 是注入逻辑时钟，不读取墙钟。

```bash
python -m logchain load   --root .
python -m logchain merge  --root .
python -m logchain replay --root .
python -m logchain resume --root .
python -m logchain status --root .
python -m logchain verify --root .
```

输出均为 JSON；`status` 使用 `sort_keys=True` 的稳定 JSON。

快照包版本裁决：

```bash
python -m logchain load --root . --package /path/to/v2-package
```

返回的 `decision` 为 `full_replace`、`incremental_append` 或 `noop`；旧版本返回业务错误码
`SNAPSHOT_VERSION_ROLLBACK` 和退出码 `3`。

崩溃注入演示：

```bash
python -m logchain merge  --root /tmp/demo --crash-at crash-merge-staging
python -m logchain replay --root /tmp/demo --crash-at crash-replay-g1-mid
python -m logchain replay --root /tmp/demo --crash-at crash-checkpoint-g2-before-commit
python -m logchain resume --root /tmp/demo
python -m logchain verify --root /tmp/demo
```

崩溃 ID 来自 `samples/crashes/crash_plan.json`。进程收到真实 `SIGKILL`，不是普通异常回滚。

## 3. 一键测试

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

测试覆盖：

- 固定种子全文件字节确定性；
- v1/v2 典型错误码与诊断字段；
- 合并产物重复生成逐字节一致；
- WAL 增量结果与空库全量重放逐行逐值一致；
- 合并、回放、检查点三类 SIGKILL 后恢复；
- 多进程并发回放和快照包提交；
- 快照包 no-op 与版本回退拒绝。

## 4. 生成的样本

```text
seed_manifest.json
samples/snapshots/v1_snapshot.json
samples/v2/manifest.json
samples/v2/shards/*.json
samples/v2/meta.sqlite
samples/wal/manifest.json
samples/wal/generation=*/*.jsonl
samples/wal/orphans/*
samples/crashes/crash_plan.json
```

运行态位于 `runtime/logchain.sqlite`，文件锁位于 `runtime/locks/`，孤儿/半写文件校验后移动到
`runtime/quarantine/`。该目录已加入 `.gitignore`。

## 5. 关键结果

当前固定样本的正常最终状态：

- `accounts`：6 个物理行，其中 5 个有效行、1 个墓碑；
- `orders`：3 个物理行，其中 2 个有效行、1 个墓碑；
- g1/g2 提交时刻差为 `1ms`，容差为 `2ms`，因此 `precision_sensitive=true`；
- 边界裁决为 `advance_atomically_with_pinned_canonical_order`；
- `verify.full_replay_equivalence=true`。

实测摘要和错误码见 `TECHNICAL_NOTES.md`，JSON 示例见 `artifacts/example-*.json`。
