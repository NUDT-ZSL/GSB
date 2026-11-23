# 平台故障恢复配置增量补丁与依赖失效裁决引擎 — 技术说明

## 结构

```
recovery_engine/
  errors.py       业务错误码（E_BAD_CONFIG / E_CYCLE / E_HASH_MISMATCH /
                  E_GENERATION_CONFLICT / E_CLOCK_REGRESS / E_CONN_REFUSED /
                  E_BUDGET_EXCEEDED / E_VERSION_ROLLBACK / E_TOMBSTONED /
                  E_UPSTREAM_NOT_READY），BizError.details 保留可诊断字段
  clock.py        注入式逻辑时钟：只依赖注入毫秒序列，回拨超容差抛
                  E_CLOCK_REGRESS 且内部状态不变；绝不读取系统墙钟
  models.py       统一服务/动作/计划模型，stable_json 稳定序列化
  v1_loader.py    v1 单文件 JSON 解析校验（服务名唯一、回退秒数非负整数、
                  步骤非降序、扁平映射与动作声明一致）
  v2_loader.py    v2 分片 + SQLite 元库校验（sha256、恢复代数单调、
                  生效时刻整数、上游存在、无自环、无环）；只读打开元库
  merge.py        跨版本合并：先比逻辑时钟，相同按格式优先级 v2>v1；
                  裁决依据 rationale 稳定排序输出
  migrate.py      v1 相对回退秒数 → 绝对回退时刻（标度 1000、舍入 exact），
                  决策持久化，同一时钟重复判定复用持久化结果
  store.py        SQLite 持久化：BEGIN IMMEDIATE 原子事务、WAL、
                  committed_actions 主键幂等、pending_actions 半提交标记、
                  计划版本表 + 指针、fcntl/msvcrt 文件锁
  executor.py     确定性模拟执行器/健康源（故障注入点）
  adjudicator.py  切换/回切裁决链：拓扑序、上游等待、水位提交、
                  流量阶梯逐級校验、失败停留上一稳定阶梯
  patch.py        补丁应用与依赖失效闭包（种子 + 下游传递闭包）
  packages.py     配置包分类（full_replace/incremental/noop）与整体替换
  engine.py       编排层：build_plan / failover / failback / recover
samples/          确定性本地样本（scripts/make_samples.py 生成，逐位可复现）
scripts/crash_child.py  崩溃注入子进程（SIGKILL 强杀模拟）
tests/            57 个 pytest 用例
```

## 关键设计

- **校验失败零写入**：两代配置全部校验在迁移持久化之前完成；v2 元库以
  `mode=ro` 打开，非法输入不会产生任何持久化副作用（test_v2_loader 验证）。
- **裁决确定性**：合并依据、结论、提交记录均经 `sort_keys` 稳定序列化；
  同一注入时钟序列重放得到逐位一致结果（test_clock::test_replay_determinism）。
- **时间迁移幂等**：迁移决策以 `(service, clock_ms)` 为主键持久化，
  `INSERT OR IGNORE`；重复判定直接复用，回切窗口不被重复换算。
- **半提交识别**：动作执行前先写 `pending_actions`（事务 A），执行后在
  事务 B 中写 `committed_actions` 并清除 pending；崩溃残留 pending 即半提交，
  恢复路径清除后按水位重放。
- **失效闭包**：沿“服务→依赖上游→恢复动作→计划执行结论”反向（下游）传播，
  只删除闭包内结论，闭包外结论逐字保留（test_patch 验证 byte-identical）。
- **并发一致性**：`BEGIN IMMEDIATE` 串行化写入，主键约束保证同一
  `(service, action_id, version)` 只提交一次；计划整体替换经文件锁 +
  版本表 + 指针原子切换，版本回退抛 E_VERSION_ROLLBACK。

## 验证结果（2026-11-24，Python 3.10.12 + pytest 8.3.3）

- `python3 -m pytest tests/ -q`：57 passed，连续 3 轮全部通过（含多进程并发用例）。
- 覆盖：v1/v2 校验 16 例、合并与迁移 10 例、切换/回切裁决 9 例、
  逻辑时钟 4 例、补丁闭包 8 例、崩溃恢复 2 例、配置包 6 例、并发 2 例。
- 强制终止恢复：子进程提交 2 个动作后在第 3 个动作提交前被 SIGKILL，
  恢复路径正确识别半提交（api-stop-v2）、仅重放水位之后动作，
  最终 9 个动作各提交一次。
