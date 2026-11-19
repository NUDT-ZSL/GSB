# local-shardrouter

一个只依赖 Python 标准库的本地分库分表路由、查询改写、跨分片执行与恢复组件。固定样本位于 `samples/`，持久化状态使用 SQLite，测试使用 pytest。

## 环境

- Python 3.10+
- pytest（离线可用）
- 不需要 Docker、外部数据库或网络

当前验证环境：Python 3.12.13、SQLite 3.53.1、pytest 9.2.0。

## 快速开始

```bash
python -m shardroute load --sample samples/sample.json --state run/state.db --clock 1000
python -m shardroute route --state run/state.db --template orders_range --clock 1100
python -m shardroute query --state run/state.db --template orders_sum_count --clock 1100
python -m shardroute apply --sample samples/sample.json --state run/state.db --version r2 --clock 2000
python -m shardroute query --state run/state.db --template orders_eq --clock 2100
python -m shardroute status --state run/state.db
python -m shardroute verify --state run/state.db
```

CLI 支持 `load`、`route`、`query`、`apply`、`resume`、`status`、`verify` 七个子命令。所有输出均为 `sort_keys=True` 的稳定 JSON；业务错误输出 `{"ok": false, "error": ...}` 并返回非零退出码。

主要参数：

- `--state`：状态 SQLite 文件，默认 `run/state.db`。
- `--clock`：可注入逻辑时钟毫秒；也支持环境变量 `SHARDROUTE_CLOCK_MS`。
- `--template`：查询模板 ID。
- `--param key=value` / `--params-json`：覆盖模板默认参数。
- `--injections`：本地异常注入清单。
- `--crash-at after_route|after_rewrite|after_execute|after_merge`：强制退出测试钩子，退出码 75。

## 固定样本

`samples/sample.json` 包含两个逻辑表：

- `orders`：按 `customer_id` 取模分片。`r1` 为 4 分片，`r2` 为 2 分片。
- `events`：按 `event_no` 范围分片，边界 `[0,1000,2000,3000,4000)`，包含 SQLite 生成列 `bucket`。

规则版本：

- `r1`：初始版本，逻辑时钟 1000。
- `r2`：取模 2 分片，逻辑时钟 2000，使用独立物理表 `orders_v2_00/01`。
- `r3-rollback`：带 `rollback_target=r1` 的回滚包，逻辑时钟 3000，应用时必须返回 `E_RULE_ROLLBACK`。

物理主库与备库均已在 `samples/shards/` 中预生成。可重复执行 `python tools/build_samples.py` 重新生成，SQLite 文件字节稳定。

查询模板覆盖等值、范围、IN、无分片键全路由、跨分片 `SUM`/`COUNT` 聚合。`samples/injections.json` 覆盖路由、改写、执行、合并四个阶段。

## 验证

```bash
python -m pytest
bash scripts/verify_twice.sh
```

`scripts/verify_twice.sh` 会执行完整测试，并分别运行三组固定工作负载两次：

- `normal`：路由、查询、聚合、规则切换与幂等查询。
- `injection`：四个阶段异常及未受影响查询。
- `recovery`：崩溃、规则更新、原版本绑定恢复。

两次运行的 30 条命令及全部标准输出逐字一致。结果保存在：

- `artifacts/verification-run-1.json`
- `artifacts/verification-run-2.json`
- `artifacts/verification-summary.json`

本次实际结果：`44 passed`；两次输出 SHA-256 均为 `sha256:2e13bf492940811863627604e5df06b4498a9a9cd993cd9f07c9c861b9a42b6f`。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_RULE` | 规则版本、逻辑表、分片键、范围边界或摘要非法 |
| `E_BAD_MAPPING` | 物理映射缺失、文件不可打开、表/列缺失或数据落片错误 |
| `E_BAD_TEMPLATE` | 查询模板结构非法 |
| `E_RULE_CONFLICT` | 同版本不同内容 |
| `E_RULE_ROLLBACK` | 尝试应用回滚包或重新激活旧版本 |
| `E_CLOCK_BEFORE_EFFECTIVE` | 逻辑时钟早于规则生效时间 |
| `E_RULE_VERSION_CONFLICT` | 路由阶段注入的版本冲突 |
| `E_SHARD_KEY_OUT_OF_RANGE` | 分片键越界或改写阶段注入 |
| `E_PHYSICAL_TABLE_MISSING` | 执行阶段物理表缺失或注入 |
| `E_RESULT_DIGEST_MISMATCH` | 合并阶段分片摘要不一致 |
| `E_PARTIAL_SHARD_FAILURE` | 部分分片失败，成功分片结果已提交 |
| `E_ALL_SHARDS_FAILED` | 全部分片失败且失败码不唯一 |
| `E_QUERY_IN_PROGRESS` | 同查询等待超时或崩溃后尚未恢复 |

完整设计、事务边界与恢复算法见 [TECHNICAL.md](TECHNICAL.md)。
