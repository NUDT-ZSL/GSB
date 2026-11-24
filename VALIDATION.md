# 实际验证记录

验证日期：2026-11-25。

## 环境

主验证环境：

```text
Python:  3.12.13
平台:    Linux 6.18.33.2-microsoft-standard-WSL2 x86_64
SQLite:  3.53.1
numpy:   1.26.4
pytest:  8.2.2
```

兼容性复跑环境：系统 Python 3.10.12 / pytest 8.2.0 / SQLite 3.37.2，同样
23 项全部通过。主环境使用被 `.gitignore` 忽略的本地 `.venv` 创建，依赖与题面
精确版本一致。

题面要求 Windows 官方 Python 环境，但本次只有 WSL2 可用于执行验证。代码使用
3.10 起可用的语法，不依赖 3.11 专属 API；Windows 文件锁分支通过 `msvcrt`
实现，但没有 Windows 实机执行证据。依赖版本记录在 `requirements-dev.txt`。

## 自动测试

执行：

```bash
python -m pyflakes memguard tests
.venv/bin/python -m pytest -q
```

结果：

```text
.......................                                                  [100%]
23 passed
```

覆盖项：

- 合法访问、自身越界、祖先包含、跨越祖先、端点相接、零长度
- 祖先环、父区域缺失、自身/祖先生命周期
- trace/region 唯一性、arena_size、非法类型、64 位区间溢出
- 非法 JSON 后续行继续处理、重复序号首次真值、缺口区间
- 串行/并行等价、低预算、低队列上限、真实耗时超时
- 真值标记不一致检测、检测不依赖 `expected_*` 字段
- SQLite 批事务回滚、布局变化重建、账本篡改重放检测
- 强制终止后恢复、半写临时快照、损坏最终快照修复
- 文件锁冲突 `E_LOCK`、输入文件哈希不变

## 组合验证

额外执行 4 个固定种子，每个种子覆盖：

- workers/budget/pending: `(1, 10^8, 64)`、`(4, 10^8, 64)`、
  `(7, 1, 64)`、`(3, 2000, 1)`
- `arena_timeout_ms=1e-12`
- 在批次 1、2、3 后分别强制退出，再使用相同命令恢复

结果：所有档位的访问结论、区域证据、输入拒绝清单、账本行完全一致；所有恢复
运行的 `truth.ok=true`，恢复账本与无中断参考账本一致。输出：

```text
seed 1 ok
seed 2 ok
seed 17 ok
seed 999 ok
PROPERTY_OK
```

## 端到端样本

固定种子样本包含 2 个 arena、15 条唯一有效访问、1 条重复访问、序号缺口
14–19、2 个祖先图错误、5 类越界、1 条非法 JSON 和 5 条非法字段记录。

一次实际运行结果：

```text
batch partial {'error': 2, 'ok': 4, 'out_of_bounds': 9, 'total': 15}
truth {'business_code': 'OK', 'checked': True, 'ledger_mismatches': [],
       'matched': 16, 'mismatched': 0, 'mismatches': [], 'missing': [], 'ok': True}
```

其中 `partial` 来自样本刻意包含的重复序号、缺口和语义错误；检测本身完成，
并行/串行结果一致。临时文件、快照替换、SQLite 恢复和文件锁均在上述测试中
实际执行。
