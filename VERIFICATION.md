# 实际验证记录

## 环境

| 项目 | 值 |
| --- | --- |
| 执行容器 | Linux/WSL |
| Python | 3.10.12（代码兼容 3.10/3.11+） |
| NumPy | 1.26.4 |
| pytest | 8.3.3 |
| SQLite | 3.37.2 |
| Docker | 未使用 |
| 外部服务/网络 | 未使用 |
| 墙钟时间 | 引擎未读取 |

目标使用环境是 Windows 10/11 + Python 3.11+。当前交付执行容器只提供 Python 3.10.12，因此实际命令在该解释器上运行；文件锁实现包含 Windows `msvcrt.locking` 分支，逻辑时钟与持久化代码不依赖 POSIX 特性。

## pytest

执行：

```bash
pytest -q
```

结果：

```text
..............                                                           [100%]
14 passed
```

覆盖内容：

- 样本生成确定性；
- 非法函数清单、层依赖环、非法调用、非法补丁不产生持久化状态变更；
- v1/v2 合并、换算比例、舍入、迁移映射和预热状态保持；
- 预热复用、函数预算放弃、全局预算放弃、冷启动成功；
- 初始化截止主动中止和显式主动取消；
- 层修改/墓碑闭包、未受影响实例判定依据不变；
- 调用和补丁幂等；
- 版本回退拒绝；
- 强制终止后的 `resume` 重放；
- 多进程同轨迹提交一致且预算不超卖；
- 双跑演示输出逐字一致。

## 双跑演示

执行：

```bash
python scripts/demo.py
```

最近一次摘要：

```json
{
  "ok": true,
  "runs_identical": true,
  "run_sha256": "577ec488746ecc5a1f12898a85e3fc6f92f97d00c5788a372c8ac5ada637da1a",
  "checks": {
    "warm_reuse": true,
    "function_budget_abandon": true,
    "global_budget_abandon": true,
    "cold_start_success": true,
    "layer_closure": true,
    "tombstone_blocks_new_call": true,
    "modified_layer_forces_cold_start": true,
    "unaffected_warm_reuse": true,
    "forced_invoke_termination": true,
    "forced_apply_termination": true,
    "resume_reapplies_patch": true,
    "in_flight_call_completed": true,
    "obsolete_instance_invalidated_after_completion": true,
    "post_resume_cold_start": true,
    "standard_verify": true,
    "crash_verify": true
  }
}
```

两次完整输出保存在：

- `artifacts/demo/run1.json`
- `artifacts/demo/run2.json`

两文件字节完全一致；SHA-256 为上面的 `run_sha256`。

## 固定样本观测

标准场景：

```text
seq 1  resize-image   REUSED_WARM
seq 2  resize-image   REUSED_WARM
seq 3  resize-image   BUDGET_NOT_STARTED  E_FUNCTION_CONCURRENCY
seq 4  classify-text  REUSED_WARM
seq 5  report-csv     BUDGET_NOT_STARTED  E_GLOBAL_BUDGET
seq 6  report-csv     COLD_START_SUCCEEDED
seq 7  resize-image   REUSED_WARM
seq 8  classify-text  REUSED_WARM
seq 9  legacy-handler REUSED_WARM
seq 10 report-csv     REUSED_WARM
```

层补丁后：

```text
affected_functions      = [legacy-handler, resize-image]
first_affected_instance = inst-legacy-handler-g000002-000002

seq 11 classify-text  REUSED_WARM
seq 12 report-csv     REUSED_WARM
seq 13 resize-image   COLD_START_SUCCEEDED  (绑定 image-libs v3)
seq 14 legacy-handler FAILED_PRE_START      E_LAYER_TOMBSTONED
seq 15 resize-image   REUSED_WARM
```

强制终止场景：

```text
invoke --failpoint init-started  exit 137，无退出报告
apply  --failpoint mid-apply     exit 137，失效传播事务回滚
resume                           重放补丁并排空调用
call seq 1                       COLD_START_SUCCEEDED
旧 report-csv 实例                INVALIDATED，绑定 base-runtime v3
call seq 2                       COLD_START_SUCCEEDED，绑定 base-runtime v4
verify                           true
```

## 未执行项

当前容器没有 Windows 10/11 与 Python 3.11 可执行文件，因此未在本机实际启动 Windows 解释器。所有业务验收在同等源码和本地 SQLite/NumPy 环境下完成；Windows 专用文件锁分支无法在当前 Linux 容器内执行，但不存在容器、云服务或平台相关业务推断。
