# 验证记录

验证日期：2026-11-21  
工作目录：`/home/zsl/feedback/uid2d1b8ac8-default-144`

## 环境

```text
Python  3.11.15
NumPy   1.26.4
SQLite  3.53.1
```

依赖安装在 `.venv` 内，无容器、无外部服务。

## 样本生成

命令：

```bash
.venv/bin/python scripts/generate_samples.py --root samples
```

结果：

```json
{"defaults":5,"events":11,"manifest_sha256":"89759a1b75eda5054b6425c5e1b99b48efb14d15373f78c3e7fa68869a32dbb2","root":"/home/zsl/feedback/uid2d1b8ac8-default-144/samples","shards":3,"v1_batches":3}
```

测试同时将样本生成到两个不同临时目录并逐文件比较 SHA-256，全部一致。

## 单元与集成测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：

```text
Ran 16 tests in 8.122s
OK
```

覆盖项：

- v1 档位、间隔、观察点、重复 JSON 键。
- v2 分片哈希、元数据库字段一致性、依赖声明、回滚窗口。
- 非法输入退出码与零状态写入。
- 跨版本裁决、原始证据和 v1 迁移映射。
- 等排名档位冲突进入 `REVIEW_REQUIRED`。
- 时钟回拨超容差后视图字节不变。
- 下游最早时刻/前序稳定阻塞来源。
- 判据失败回滚和回滚超窗失败。
- 完整替换与增量追加分类。
- 版本回退拒绝与指针稳定。
- 进行中批次绑定旧包，未开始批次绑定新包。
- 强杀恢复、并发恢复幂等、两次运行字节一致。

## 双次强杀与恢复验收

命令：

```bash
.venv/bin/python -m grayroll verify --samples samples --workdir runs/verify
```

实际输出：

```json
{"checks":{"clean_equals_crash_resume":true,"clock_watermark_persisted":true,"cross_version_merge":true,"descending_rollback":true,"forced_termination_recovered":true,"parallel_resume_idempotent":true,"v1_migration":true},"ledger_sha256":"f9c5b9623ebf46b09ad4c800a6cdb64f26ee6a8ffbfc90d4dd507e62b971e0cd","runs":2,"status":"VERIFIED","status_sha256":"582e15ea2afd779c91f769b5a02efc732c460d6cbcd9dac08f9ada19a92bf7c9","two_run_byte_identical":true}
```

每次验收运行的顺序：

1. 新建状态库。
2. `load` v1/v2 包。
3. `merge` 并原子切换包指针。
4. 推进事件到批次 4 判据失败。
5. 在首个回滚 `INTENT` 提交后以退出码 `75` 强制终止。
6. 同时启动两个 `resume` 进程；文件锁串行化写命令，SQLite 唯一操作键保证幂等。
7. 完成批次 4 回滚、批次 1 人工中止回滚，并推进到逻辑时钟 `1700000012000`。
8. 导出 `status` 与账本，保存 SHA-256。

最终批次状态：

| 批次 | 来源 | 终态 | 回滚目标 |
|---|---|---|---|
| 1 | v2 | `ROLLED_BACK` | `0.5 -> 0.1 -> 0.0` |
| 2 | v2 | `STABLE` | 无 |
| 3 | v1 | `STABLE` | 无 |
| 4 | v2 | `ROLLED_BACK` | `0.15 -> 0.0` |

两次运行的状态字节哈希和账本字节哈希一致；不中断执行的状态与账本也与强杀恢复一致。

## 已知非比较对象

SQLite 物理文件页布局、`.db.lock` 文件内容和临时目录路径不属于逐字节确定性承诺。验收比较的是稳定 `status` JSON、规范化账本导出和样本生成内容。
