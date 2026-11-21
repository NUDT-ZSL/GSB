# 实际验证记录

验证日期：2026-11-22  
工作目录：`/home/zsl/feedback/uid2d1b8ac8-default-245`

## 环境

```text
Python 3.10.12
NumPy 1.26.4
pytest 8.3.3
Linux / 32 logical CPUs
本地 spawn 多进程池
未使用 Docker、容器、网络或外部数据
```

## 复现命令

```bash
export PYTHONPATH="$PWD/src"
python3 scripts/generate_data.py --root . --seed 20261122 --samples 240
python3 -m linear_path_engine train -c configs/float64-serial.json
python3 -m linear_path_engine train -c configs/float64.json
python3 -m linear_path_engine train -c configs/float32.json
python3 -m linear_path_engine train -c configs/fixed-point.json
python3 -m linear_path_engine train -c configs/float64-l2.json
python3 -m linear_path_engine validate \
  -c configs/float64.json -r artifacts/run-float64/result.json
python3 -m pytest -q
```

最终测试输出：

```text
..........................                                               [100%]
33 passed in 8.00s
```

## 完整路径结果

数据由 `seed=20261122, samples=240` 生成。路径包含 8 档，配置递增：

```text
[0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1]
```

求解顺序为从 `0.1` 到 `0.0005`。四份运行结果均完成 8 档并全部收敛。

| 运行 | workers | 档位 | 收敛 | 比较次数 | path_checksum |
|---|---:|---:|---:|---:|---|
| float64-serial | 1 | 8 | 8 | 3,766,885 | `c2ca407cbe9db6724671f12f8bfdc4ff8760666e3dcafeb1293e59312f992da6` |
| float64 | 4 | 8 | 8 | 3,766,885 | `c2ca407cbe9db6724671f12f8bfdc4ff8760666e3dcafeb1293e59312f992da6` |
| float32 | 4 | 8 | 8 | 511,619 | `3f5f21bc993dbb443eb8e40893aea4a737d2e7c4f342c4904e31987916d3b6af` |
| fixed_point | 4 | 8 | 8 | 228,301 | `6c76132d8c41b812e507aca5bea0e21f719fb92ee05f255cbcec9c8d45b55cfb` |
| float64 + L2 | 4 | 8 | 8 | 2,613,376 | `31bc962ca7f80cca95ba64c9f59303f433215c173a4b298f33e3f57ebabab645` |

串行和 4-worker 的 float64 路径逐项相同，路径校验和相同。两份 `result.json`
文件不同，仅仅因为其配置中的 `workers` 和运行 ID 不同；训练路径本身完全一致。

float64 每档迭代数：

```text
[51, 54, 95, 103, 135, 205, 276, 376]   # solve order 0.1 -> 0.0005
```

系数 L2 范数（按强度升序）：

```text
[0.721220413, 0.720345069, 0.718603440, 0.713449839,
 0.705090641, 0.689184292, 0.647766878, 0.592527231]
```

相邻档最小范数余量 `0.0008753435377266694`，大于配置的绝对容差 `1e-10`。

## 三档精度自检

| 精度 | 目标重算最大绝对差 | 相邻范数最小余量 | 结果 |
|---|---:|---:|---|
| float64 | `2.7755575615628914e-17` | `0.0008753435377266694` | 通过 |
| float32 | `7.361122933424724e-09` | `0.0008790911763959519` | 通过 |
| fixed_point scale=4096 | `8.131020313051351e-06` | `0.0006522884842037335` | 通过 |

三档均满足：

- 路径长度一致；
- 求解顺序一致；
- 所有档位收敛；
- `stats_version` 与标准化统计量内容哈希一致；
- 目标函数可由已保存系数和标准化统计量独立重算；
- 系数范数随正则化强度单调不增。

另外，`configs/float64-l2.json` 走 L2 分支，8 档全部收敛，目标重算最大绝对差
`5.551115123125783e-17`，路径校验和为
`31bc962ca7f80cca95ba64c9f59303f433215c173a4b298f33e3f57ebabab645`。

## 恢复验证

测试使用真实子进程和 `os._exit(97)` 强制终止，不使用正常异常退出。

### 完整档位提交后终止

配置包含 3 档，在第 2 档提交后终止：

```text
return code: 97
当前快照: version=3, stage=path_solve, path length=2, phase_complete=false
```

恢复后：

- 前 2 个已提交路径项与快照内容逐项相同；
- 只求解剩余档位；
- 恢复结果与同配置从零重跑的结果文件逐字节相同；
- `comparisons_used` 与从零重跑相同，不包含被丢弃的中间迭代。

### 档内未提交迭代终止

在强度升序索引 2（`lambda=0.2`）的第 3 次迭代强制终止：

```text
return code: 97
当前快照: version=1, stage=standardization, path length=0
```

恢复后从该档起点重建，最终结果与从零重跑的结果文件逐字节相同。档内中间迭代
从未落盘。

### 降级计划恢复

使用内存预算触发：

```text
degradation_categories = ["workers_reduced", "small_block"]
chosen workers = 1
chosen block_size = 4
coverage = 完整覆盖 90/90 行
```

在第 1 档提交后终止并恢复。恢复后的 `decision_checksum` 和已提交路径项保持不变，
没有重新规划或改变档位结果。

## 快照与并发验证

| 场景 | 实际结果 |
|---|---|
| 快照 payload 被修改 | `E_SNAPSHOT_CORRUPT`, stage=`path_summary`, version=2 |
| manifest JSON 损坏 | `E_SNAPSHOT_CORRUPT`, 从快照文件恢复 stage/version 证据 |
| 尝试提交旧版本 | `E_SNAPSHOT_ROLLBACK`，当前版本保持 |
| 使用旧 checksum 提交 | `E_SNAPSHOT_ROLLBACK` |
| `snapshot-rollback` | `E_SNAPSHOT_ROLLBACK` |
| 另一进程持有提交锁 | `E_STAGE_BUSY`，实测立即返回（<0.5s 断言） |
| 新快照提交 | 整份 payload 替换，旧版本文件保留但不被 manifest 指向 |

快照版本序列实测为：

```text
v1  standardization
v2  path_solve / strength 7
v3  path_solve / strength 6
v4  path_solve / strength 5
v5  path_solve / strength 4
v6  path_solve / strength 3
v7  path_solve / strength 2
v8  path_solve / strength 1
v9  path_solve / strength 0
v10 path_validation
v11 path_summary
```

## 业务码验证

| 业务码 | 触发方式 | 归因结果 |
|---|---|---|
| `E_STAGE_BUSY` | 并发持有提交锁 | stage=`snapshot_commit` |
| `E_SNAPSHOT_CORRUPT` | 修改快照 payload / manifest | stage/version 来自 manifest 或快照文件名与 envelope |
| `E_SNAPSHOT_ROLLBACK` | 旧版本、旧 checksum、CLI 回退 | 返回当前权威版本 |
| `E_WORKER` | 注入 worker 异常 | stage=`PATH_SOLVE`, strength_index=2, strength=0.2, block_index=1 |
| `E_MONOTONICITY` | 放大最强档系数 | strength_index=2, strength=0.2 |
| `E_OBJECTIVE_MISMATCH` | 修改保存目标值 | strength_index=2, strength=0.2 |
| `E_VERSION_MISMATCH` | 修改档位 stats_version | strength_index=2, strength=0.2 |
| `E_RESOURCE_LIMIT` | 路径数、迭代上限、比较预算、内存预算 | 返回具体上限与请求值 |
| `E_CONFIG` | 非递增强度路径等 | stage=`config` |
| `E_DATA` | 重复 row_id 等 | stage=`data_load` |

## 验证产物

```text
data/table.jsonl
configs/float64-serial.json
configs/float64.json
configs/float32.json
configs/fixed-point.json
configs/float64-l2.json
artifacts/run-float64-serial/result.json
artifacts/run-float64/result.json
artifacts/run-float32/result.json
artifacts/run-fixed-point/result.json
artifacts/run-float64-l2/result.json
artifacts/snapshots-float64-serial/
artifacts/snapshots-float64/
artifacts/snapshots-float32/
artifacts/snapshots-fixed-point/
artifacts/snapshots-float64-l2/
```

