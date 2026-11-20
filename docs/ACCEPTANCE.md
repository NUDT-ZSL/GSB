# 验收记录

## 1. 执行环境

```text
OS            Linux（工作区本地文件系统）
Python        3.10.12
SQLite        3.37.2
NumPy         1.26.4
```

目标环境要求 Python 3.11/3.12；代码兼容 Python 3.10+，SQLite 仅使用标准库。当前执行机未提供 Python 3.11/3.12，因此未在本机重复 3.11/3.12 解释器测试；Windows 文件锁使用标准库 `msvcrt` 分支。

## 2. 测试结果

命令：

```bash
python -m unittest discover -s tests -t . -v
```

实际结果：

```text
Ran 21 tests in 3.2s
OK
```

覆盖矩阵：

| 类别 | 结果 |
|---|---|
| 非法层级/配额/限流/乱序事件/批量预校验 | 通过 |
| 软限进入、重复不重复告警、软限退出 | 通过 |
| 前缀硬配额拒绝与首触发约束 | 通过 |
| 删除按持久化大小归还三层账目 | 通过 |
| 令牌不足、997ms 等待、逻辑补充后放行 | 通过 |
| 时钟回拨拒绝且状态不变 | 通过 |
| 首次结果精确幂等重放 | 通过 |
| 幂等键冲突与事件序号冲突独立错误码 | 通过 |
| 故障注入后令牌、配额、裁决联合回滚 | 通过 |
| 崩溃前事务回滚与孤儿意图清理 | 通过 |
| 篡改账目后 `verify` 检出 | 通过 |
| 4 进程不同前缀并发写入 | 通过 |
| 4 进程同幂等键竞争只裁决一次 | 通过 |
| 4 进程同序号不同幂等键只有一个赢家 | 通过 |
| CLI 六个子命令 | 通过 |

## 3. 固定样本裁决结果

命令：

```bash
python -m quota_ledger load \
  --db artifacts/quickstart/ledger.db \
  --quotas samples/quotas.json \
  --limiter samples/limiter.json
python -m quota_ledger write \
  --db artifacts/quickstart/ledger.db \
  --events samples/events.json
```

关键裁决：

| 事件 | 场景 | 结果 |
|---|---|---|
| 2 | `demo` 前缀字节超过软阈值 200 | 允许，`prefix` `ENTER` |
| 3 | `demo` 桶字节超过软阈值 300 | 允许，`bucket` `ENTER` |
| 4 | 前缀投影 450 > 硬阈值 400 | `E_HARD_QUOTA`，首触发为 `prefix.bytes` |
| 5 | 写入到硬阈值 400 | 允许，未重复生成软告警 |
| 6 | 删除 `a2` | 归还 100 字节、1 对象；`bucket` `EXIT` |
| 7 | 删除 `a3` | 归还 100 字节、1 对象；`prefix` `EXIT` |
| 8 | 删除 `a5` | 归还 50 字节、1 对象 |
| 9-11 | 令牌桶连续放行 | 允许 |
| 12 | 余额 0.003 token | `E_RATE_LIMITED`，`wait_ms=997` |
| 13 | 逻辑时钟补充 1.002 token | 允许 |
| 14 | 强制终止后重试 | 允许，占用 25 字节、1 对象 |

最终账目：14 个事件，12 个允许，2 个拒绝，6 个对象，4 条软告警；全量重放一致。

## 4. 强制终止恢复

`samples/clock_manifest.json` 定义：

- 事件 14 在 `before_commit` 阶段强制退出，退出码 73。
- 孤儿意图探针在 `after_intent` 阶段强制退出，退出码 74。

实际恢复结果：

```text
before_commit 后：
  last_decided_seq = 13
  crash tenant used_bytes = 0
  resume cleaned_count = 0
  resume verify ok = true

after_intent 后：
  status pending_operations = 1
  last_decided_seq 仍为 14
  resume cleaned_count = 1
  resume verify ok = true
```

随后事件 14 正常提交，最终 `verify.ok=true`。

## 5. 双运行逐字节一致

命令：

```bash
python scripts/demo.py --output-dir artifacts/demo
```

实际输出：

```text
run_1_bytes   11705
run_2_bytes   11705
run_1_sha256  cd097111129edb907b428d8d22c7179b0f5596c514a8fe07c6d84f3043557d25
run_2_sha256  cd097111129edb907b428d8d22c7179b0f5596c514a8fe07c6d84f3043557d25
byte_identical true
```

复验命令：

```bash
sha256sum artifacts/demo/run-1.output.json artifacts/demo/run-2.output.json
```

另将 `scripts/demo.py` 连续执行两次，标准输出也逐字节一致：

```text
51a1d6175bcdbdf062e6db3de23f8871d5cd55b8a34eca0769cccad3a4585220  artifacts/acceptance-demo-stdout-a.txt
51a1d6175bcdbdf062e6db3de23f8871d5cd55b8a34eca0769cccad3a4585220  artifacts/acceptance-demo-stdout-b.txt
```

## 6. 多进程压力结果

测试 `tests.test_multiprocess` 使用 `spawn` 启动 4 个进程：

- 不同前缀各写 10 个对象，最终租户 40 字节、40 对象，四个前缀各 10/10。
- 同幂等键并发竞争，最终 `events=1`、`accepted=1`、`objects=1`，令牌只扣 1 次。
- 最终 `verify.ok=true`，守恒检查通过。

