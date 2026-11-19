# 配置互操作与恢复裁决管线 — 技术说明与验证报告

## 概述

本项目在本地文件系统上从零实现了一个平台故障恢复的配置互操作与恢复
裁决管线：加载并校验 v1（单文件 JSON）与 v2（分片 JSON + SQLite 元数据
库）两代恢复配置，合并为统一恢复计划，在可注入逻辑时钟下推演切换与
回切裁决，逐动作以事务方式持久化阶段状态与内容摘要，并支持进程被强
制终止后的纯持久化状态恢复。不依赖 Docker、真实集群或外部网络。

## 环境

- Python 3.11.15（工作区 `.venv`，PEP 668 环境下以 venv 隔离）
- numpy 2.4.6（回切流量阶梯校验与推进计算）
- pytest 9.2.0
- 运行前清理：`run_demo.py` 与样本生成器均先删除残留目录、临时文件
  与锁文件；SQLite、分片目录、锁文件全部位于工作区内

## 结构

| 模块 | 职责 |
| --- | --- |
| `recovery_pipeline/errors.py` | 全部业务错误码与诊断字段 |
| `recovery_pipeline/clock.py` | 可注入逻辑时钟，回拨容差判定 |
| `recovery_pipeline/v1_loader.py` | v1 解析校验（纯函数，不落盘） |
| `recovery_pipeline/v2_loader.py` | v2 分片哈希、元数据库代数单调、依赖闭合校验 |
| `recovery_pipeline/merge.py` | 跨版本合并裁决与稳定排序 |
| `recovery_pipeline/migrate.py` | v1→v2 时间/语义迁移与不变量校验 |
| `recovery_pipeline/store.py` | SQLite 持久化（计划版本指针、阶段、水位、审计） |
| `recovery_pipeline/executor.py` | 切换/回切裁决执行链与崩溃安全提交协议 |
| `recovery_pipeline/recovery.py` | 强杀后的恢复裁决 |
| `recovery_pipeline/packages.py` | 配置包版本识别、整体替换、回退拒绝、旧版清理 |
| `recovery_pipeline/locks.py` | 跨进程文件锁（msvcrt/fcntl） |
| `recovery_pipeline/http_target.py` | 回环 HTTP 测试目标（成功/超时/版本不符/不健康） |
| `samples/gen_samples.py` | 固定数据确定性样本生成 |
| `run_demo.py` | 端到端演示（退出码即结论） |
| `tests/` | 41 个 pytest 用例 |

## 业务要求映射

1. **解析与校验**：v1 校验服务名唯一（`E_V1_DUPLICATE_SERVICE`）、回退
   秒数非负整数（`E_V1_INVALID_ROLLBACK_SECONDS`）、步骤逻辑时钟非降
   序（`E_V1_NON_MONOTONIC_STEPS`）、扁平映射一致
   （`E_V1_MAPPING_INCONSISTENT`）；v2 校验分片 SHA-256
   （`E_V2_SHARD_HASH_MISMATCH`）、元数据库恢复代数单调
   （`E_V2_GENERATION_REGRESSION`）、生效时刻非负毫秒整数
   （`E_V2_INVALID_EFFECTIVE_MS`）、上游依赖两端闭合
   （`E_V2_DANGLING_DEPENDENCY`）。加载器均为纯函数，非法条目不落任何
   持久化状态（`test_invalid_config_writes_nothing`）。
2. **合并裁决**：同服务冲突按（恢复代数 → 声明逻辑时钟 → 来源格式优
   先级 v2>v1）裁决；每条动作保留来源版本、原始时间表示
   （`tick:N`/`ms:N`）与原始相对回退秒数；动作按（生效时刻、优先级、
   声明序号）稳定排序。
3. **语义迁移**：v1 相对回退秒数在注入时钟下换算为绝对回退毫秒，记
   录换算标度（`1s=1000ms`）与舍入方向（`EXACT`）；v1 动作迁移为 v2
   统一语义（默认代数 1、优先级 P3）并记录迁移映射；
   `migration_invariant_holds` 验证迁移前后应回退状态不变。
4. **裁决执行链**：按依赖拓扑序逐服务裁决；上游未就绪时下游 `WAIT`
   并记录 `upstream_not_ready:<列表>` 触发来源；回切按流量阶梯
   （10/25/50/100）推进，每步校验目标健康与持久化阶段，失败 `HOLD`
   在上一稳定阶梯；每服务输出决定链与首个触发原因。
5. **逻辑时钟**：全部生效判定只读注入时钟；同一时钟序列重放得到逐位
   一致的持久化视图（`test_deterministic_replay_same_clock`）；回拨超
   容差返回 `E_CLOCK_ROLLBACK_EXCEEDED` 且计划状态不前进。
6. **强杀恢复**：阶段、动作水位、流量阶梯、迁移决策、时钟水位在
   SQLite 事务中原子写入；提交协议为 intent(pending) → 下发 →
   watermark+committed 两段事务；恢复路径仅凭持久化状态区分已提交与
   半提交动作，半提交动作经幂等目标重放补齐，已提交阶梯不重复计入；
   恢复后持久化视图与未中断运行逐字节一致（含子进程 `os._exit(137)`
   强杀场景）。
7. **配置包管理**：比较清单版本号、恢复代数下限、分片哈希与内容摘要，
   判定 `FULL_REPLACE`/`INCREMENTAL`/`NOOP`；整体替换先写新版本计划行
   再原子翻转 `is_current` 指针；版本回退抛
   `E_PACKAGE_VERSION_REGRESSION` 并写审计；旧版本仅无已提交水位引用
   时可清理。
8. **并发与异常链**：文件锁 + `BEGIN IMMEDIATE` + 水位主键保证同一服
   务同一动作版本只提交一次，重复提交幂等（`already_committed`），读
   方只见完整已提交阶段；连接拒绝、超时、回显版本不符、摘要不匹配、
   依赖成环分别映射 `E_TARGET_CONN_REFUSED`、`E_TARGET_TIMEOUT`、
   `E_TARGET_VERSION_ECHO_MISMATCH`、`E_DIGEST_MISMATCH`、
   `E_DEPENDENCY_CYCLE`，均带可诊断字段。

## 实际验证结果

- `pytest tests/`：**41 passed**（覆盖上述 8 条要求，含子进程强杀恢复、
  6 线程并发提交同一动作仅 1 次成功、目标端 apply 计数恰好为动作数）。
- `python run_demo.py`：**RESULT: PASS**（退出码 0）。输出显示：
  svc-beta 由 v2（代数 2）裁决胜出、svc-alpha 保留 v1 来源并完成迁移
  （rollback_abs_ms=1030000）、回切四阶梯全部推进至 `failed_back`；
  模拟强杀后恢复报告仅重放 `alpha-switch`（半提交）与 `alpha-start`
  （缺失），恢复视图与未中断运行完全一致。

## 复现

```bash
python3.11 -m venv .venv && .venv/bin/pip install numpy pytest
.venv/bin/python -m pytest tests/ -q
.venv/bin/python run_demo.py
```
