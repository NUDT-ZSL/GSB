# NTFS 风格权限审计模块

这是一个完全本地、确定性的权限归一、访问判定、SQLite 账本、检查点恢复与真值核对实现。原始任务书存档于 `docs/TASK_SPEC.md`，设计细节和实际验证记录见 `docs/TECHNICAL.md`。

## 环境

题目要求 Windows 10+ / Python 3.11；本仓库代码也兼容当前 Linux 验证环境的 Python 3.10+。

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install numpy==1.26.4 pytest==8.2.2
```

POSIX 使用：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install numpy==1.26.4 pytest==8.2.2
```

## 端到端运行

```bash
.venv/bin/python -m perm_audit generate --out samples/deterministic --with-invalid
.venv/bin/python -m perm_audit parse --sample samples/invalid_sample --out runs/invalid-parse.json
.venv/bin/python -m perm_audit audit --sample samples/deterministic --workdir runs/demo
.venv/bin/python -m perm_audit verify \
  --truth samples/deterministic/truth.jsonl \
  --report runs/demo/report.json \
  --out runs/demo/verification.json
.venv/bin/python -m pytest -q
```

Windows 把 `.venv/bin/python` 替换为 `.venv\Scripts\python`。

也可以使用根入口：

```bash
python main.py audit --sample samples/deterministic --workdir runs/demo
python auditctl.py verify --truth samples/deterministic/truth.jsonl --report runs/demo/report.json
python generate_samples.py
```

## 子命令

- `generate`：固定种子生成对象、ACE、受托者、请求、同步真值及非法记录样本。
- `parse`：严格解析并输出含记录序号和业务错误码的拒绝记录。
- `audit`：执行解析、归一、继承/组展开、冲突检测、SQLite 持久化、检查点和原子报告。
- `recover`：`audit` 的恢复语义别名；自动从最后完整检查点续算。
- `verify`：按 `sync_marker` 将报告与真值对照并写验证 JSON。
- `lock`：持有工作目录锁，主要用于并发 `E_LOCK` 测试。

## 中断恢复与锁

```bash
.venv/bin/python -m perm_audit audit \
  --sample samples/deterministic \
  --workdir runs/crash \
  --crash-after-objects 4

.venv/bin/python -m perm_audit recover \
  --sample samples/deterministic \
  --workdir runs/crash
```

POSIX 崩溃注入发送真实 `SIGKILL`；Windows 使用硬退出码 137。恢复会清理 `watermark > checkpoint_watermark` 的半写残留，校验 SQLite/快照/重放摘要，然后只续算后续对象。

并发写同一工作目录时，第二个进程立即得到：

```json
{"ok": false, "error": {"code": "E_LOCK"}}
```

## 当前验证结果

- Python 3.11.18 + `numpy==1.26.4` + `pytest==8.2.2`：`21 passed`。
- 确定性样本：27 个对象、15 个受托者、14 条 ACE、17 个请求。
- 判定分布：10 allow、4 deny、3 uncertain。
- 真值核对：17/17 通过；15 条掩码精确一致，3 条按误差边界保守标记 uncertain。
- 结构场景：`E_DENY_SHADOW`、`E_INHERIT_AMP`、`E_POLICY_DIVERGE`、`E_CANONICAL_ALIAS`、`E_LOCK` 均有确定性样本和测试。
- 崩溃恢复报告与从零全量报告 SHA256 逐字节一致；pytest 同时校验 SQLite 规范导出一致。
