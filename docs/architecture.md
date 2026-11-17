# 架构与状态机说明

## 1. 运行拓扑

```text
JSONL client
    |
    v
127.0.0.1:24310 GatewayTCPServer
    |
    v
GatewayEngine (RLock, deterministic route/batch/ledger)
    |-------------------> 127.0.0.1:24311 NumPy backend be-alpha
    |-------------------> 127.0.0.1:24312 NumPy backend be-beta
    |
    v
SessionStore (SQLite: current view + append-only history)
```

网关一次连接读取完整 NDJSON 请求序列后路由与分批。批次串行执行，因此相同请求序列、规则版本和指纹集合产生相同批次编号、输出摘要、整数耗时和账本顺序。

## 2. 加载与版本建模

`load_declarations()` 在进入任何 SQLite 状态变更前完成全部校验：

- 后端 ID、模型 ID、回环地址/端口对唯一；
- 端口 `1..65535`，且 host 为 `127.0.0.1` 或 `::1`；
- `max_concurrency`、`parameter_length`、`batch_size` 为受区间约束的正整数；
- `batch_size <= target.max_concurrency`；
- 参数文件相对声明目录，不能目录穿越；
- 参数为定长有限 binary64；
- 规则 ID 唯一，条件字段在 `feature_schema` 中，目标后端存在；
- 规则版本由 feature schema 与排序后的规范化规则哈希得出。

任一条失败立即抛出带 JSON 路径的业务错误，不写入半加载状态。

## 3. 指纹状态转移

```text
backend content/config unchanged + same DB state
    -> preserve backend-local load_sequence and fingerprint

backend content/config changed
    -> load_sequence = old + 1
    -> new canonical vector/config digest/content signature
    -> new fingerprint

explicit reload, even with same content
    -> load_sequence = old + 1
    -> new fingerprint
```

初始批量加载所有后端均使用序号 `0`，并按 backend ID 稳定物化，所以声明数组顺序和并发加载不会改变初始指纹集合。

## 4. 请求状态机

```text
parse/validate
    -> route by (priority, rule_id)
    -> deterministic grouping and chunking
    -> batch record IN_FLIGHT + pending ledger entries
    -> backend TCP call
        |
        +-- fingerprint unchanged + success
        |      -> COMPLETED, pending entries committed/effective
        |      -> supersede any prior effective entry for same request
        |
        +-- fingerprint changed before commit
        |      -> VOID, discard output, pending entries voided
        |      -> if policy allows and retry budget remains:
        |           create redispatch batch under new fingerprint
        |
        +-- backend unreachable/protocol error
               -> ERROR/E_UPSTREAM, no effective result
               -> review all effective bindings by fingerprint/load sequence
```

`ledger_current` 的部分唯一索引 `(request_id) WHERE effective=1` 是“一个请求至多一条有效归属”的最终数据库保证。

## 5. 演进事务

指纹集合差异分为 `added`、`removed`、`changed`。演进步骤：

1. 物化新后端状态；
2. 按当前指纹逐条复核有效归属并标记失效；
3. 识别仍在途批次并作废；
4. 计算受影响规则和统计键；
5. 从现行有效归属重算统计；
6. 将旧统计版本置为只读并插入新版本；
7. 在单个事务中删除/重建 `backend_current`、`ledger_current`、`batch_current` 和 `current_state`。

统计差异函数把规则命中、后端命中、指纹命中、缓存项和全局项扁平化。未受影响的非全局键如果发生变化，被视为状态机错误 `E_STATE`，而不是静默修补。

## 6. 快照恢复

快照是当前视图的完整不可变副本，包含状态摘要。匹配时通过 `replace_current_view()` 做整表替换；冲突时不由快照补丁修复当前状态，而是整体建立当前声明的最新空视图。

恢复后重放的期望来自快照；每个请求取最新一次尝试的绑定指纹、规则、后端、作废标志、输出摘要和错误码。完整旧尝试仍保留在快照状态/批次历史中。首个差异产生 `E_REPLAY` 并写入逻辑序号日志。

## 7. 确定性边界

- NumPy 运算只使用乘、加、求和和固定索引缩放，不用随机数、wall-clock 或线程聚合。
- 输出摘要包含后端指纹和 binary64 位串，因此指纹变化必然反映到摘要。
- canonical JSON、稳定排序和逻辑序号用于 SQLite、报告和日志。
- 执行器串行批次；后端服务可并发接受连接，但当前诊断视图不依赖并发完成顺序。
