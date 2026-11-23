# 实际验证记录

运行环境：Python 3.11.18、NumPy 2.4.6（`.venv`），工作目录本地可读写，
使用 `ProcessPoolExecutor` 本地进程池与 `os.replace` 原子替换，无容器、无外部服务。

## 测试套件

命令：`.venv/bin/python -m unittest discover -s tests -v`

结果：`Ran 32 tests ... OK`，全部通过。覆盖：

* 采样确定性、top-k/top-p 截断、逆 CDF、EOS 完成、参数覆盖（`test_sampler.py`）
* 五类输入校验错误码与单请求隔离（`test_validation.py`、`test_engine_validation.py`）
* 三预算拒绝、优先级排序、账本归零、策略仲裁确定性（`test_budget_admission.py`）
* 快照单调版本、回退拒绝、跳号拒绝、校验和损坏与 JSON 损坏（`test_snapshot.py`）
* 全链路自检、`E_BATCH_MISMATCH` 首个偏差 step、崩溃恢复逐字节一致、
  `E_INCONSISTENT`、快照版本递增（`test_engine.py`）

## 确定性核对

* 同种子重新生成数据集：全部文件 SHA-256 指纹一致；
* 不同进程池工作进程数（1/2/3）与独立目录多次运行：采样结果、调度计划、
  账本事件的规范化 JSON 逐字节一致；
* 强制终止（提交 4 个请求后中断）再恢复：恢复结果与不间断运行
  `byte_identical_to_uninterrupted = true`，已提交集与重执行集 `sets_disjoint = true`。

## 预算错误码观测

演示配置 `step_budget=22, cache_budget=18, slot_budget=2, wave_budget=2`，
在同一数据集上跨策略观测到全部三类预算错误：

```
observed_rejection_codes_across_profiles =
  ["E_CACHE_BUDGET", "E_SLOT_BUDGET", "E_STEP_BUDGET"]
```

## 输入校验观测

注入非法目标长度、不可解析种子、越界 top-p 后：

```
error_codes = ["E_LENGTH_INVALID", "E_SAMPLING_INVALID", "E_SEED_INVALID"]
```

非法请求被隔离，其余请求正常采样；词表版本缺失由 `E_VOCAB_VERSION_MISSING` 覆盖（单测）。

## 快照语义

同一工作目录重复运行，快照负载逐字节一致、版本号单调递增（1 → 2 → …）；
旧版本覆盖或跳号返回 `E_SNAPSHOT_ROLLBACK`，校验和不符/JSON 损坏返回
`E_SNAPSHOT_CORRUPT`，均带快照名与版本号。

完整机器可读结果见 `artifacts/diagnostic_report.json`。
