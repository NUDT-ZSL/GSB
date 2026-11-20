# 运行报告（本地确定性验证）

验证时间：2026-11-21（Asia/Shanghai）

## 环境

- 平台：WSL2 Linux（代码仅使用跨平台标准库与 NumPy，Windows 原生 Python 3.10+ 同样可运行）。
- Python 3.10.12，NumPy 1.26.4，pytest 8.3.3，全部使用本地已安装版本，未联网安装依赖。
- 无 Docker/容器，无外部网络目标访问。

## 测试套件

命令：`PYTHONPATH=src python -m pytest -q`

结果：**38 passed**。覆盖范围：

- 任务校验与四类参数异常码（`test_validation.py`）。
- 显式块边界、Welford 合并、块大小无关性、转置语义轴、分块 vs 参考一致（`test_chunking.py`）。
- 容差裁决、越界位置/偏差量、布局损坏检出（`test_adjudication.py`）。
- 批量裁决、逐字节重跑、查询/批量一致、延迟预算、偏差检出、估算记录（`test_engine.py`）。
- 缓存键构成、后缀失效/复用不变量、资源上限、策略依据、快照版本/损坏/自检（`test_cache_policy_snapshot.py`）。
- CLI 生成/批量/查询/自检/校验子命令与退出码（`test_cli.py`）。

## 有效样本执行结果

| case_id | dtype | 处理块数 | 最大绝对偏差 | 越界数 | 实测耗时 |
| --- | --- | --- | --- | --- | --- |
| case_mix_small_f64 | float64 | 15 | 0.0 | 0 | ~1.45 ms |
| case_three_axis_f32 | float32 | 8 | 1.86e-07 | 0 | ~1.04 ms |
| case_repeat_activation_f64 | float64 | 8 | 0.0 | 0 | ~0.88 ms |

- float64 分块与整段参考逐元素完全相等（最大绝对偏差 0）。
- float32 偏差来自 dtype 舍入，处于 float32 容差档（atol 1e-6 / rtol 1e-5）内，无越界。
- 精度档与容差口径在每个结果的 `tolerance` 字段显式记录。

## 异常码验证矩阵

| 异常样本 | 期望业务码 | 实测业务码 | 退出码 |
| --- | --- | --- | --- |
| bad_unknown_op | E_OP_UNKNOWN | E_OP_UNKNOWN | 10 |
| bad_axis_order | E_AXIS_INVALID | E_AXIS_INVALID | 11 |
| bad_eps | E_PARAM_INVALID | E_PARAM_INVALID | 12 |
| bad_dtype | E_DTYPE_UNSUPPORTED | E_DTYPE_UNSUPPORTED | 13 |
| bad_block_budget | E_LATENCY_BUDGET | E_LATENCY_BUDGET | 15 |
| bad_resource_limit | E_RESOURCE_LIMIT | E_RESOURCE_LIMIT | 17 |

另有快照篡改实验返回 `E_SNAPSHOT_CORRUPT`（退出码 18），由 `test_snapshot_roundtrip_and_corruption_detection` 与 CLI 手工验证共同覆盖。

## 确定性验证

- 连续两次 `batch` 写出的快照文件经 `cmp` 比较**逐字节相同**。
- 观测延迟属于运行期观测，不进入持久化快照，仅在 stdout 摘要/查询响应中返回。
- 序列化统一排序键、固定分隔符；输入仅由固定种子 `np.random.default_rng` 生成。

## 缓存与失效联动

- 批量快照包含 13 个内容寻址缓存条目（等于全部案例算子阶段数之和 5+4+4）。
- block_size 变更实验：复用 0、失效 5（全部阶段键改变）。
- 末位激活种类变更实验：复用 4、失效 1（仅后缀重算）。
- 复用/失效集合无交集，且并集等于受影响集合，自检强制校验通过。

## 快照与自检

- 快照版本从 1 单调递增，携带 64 位十六进制 SHA-256 校验和，落盘为临时文件原子替换。
- `selfcheck` 重算全部缓存键与阶段哈希、校验集合不变量并重放整条链，结果 `E_OK`。

## 复现步骤

```bash
PYTHONPATH=src python -m opchain.cli generate --samples-dir samples
PYTHONPATH=src python -m opchain.cli batch --input samples/tasks.valid.jsonl --output results/batch_snapshot.json
PYTHONPATH=src python -m opchain.cli query --input samples/tasks.valid.jsonl --snapshot results/batch_snapshot.json --case-id case_three_axis_f32
PYTHONPATH=src python -m opchain.cli selfcheck --input samples/tasks.valid.jsonl --snapshot results/batch_snapshot.json
PYTHONPATH=src python -m opchain.cli validate --input samples/tasks.anomaly.jsonl
PYTHONPATH=src python -m pytest -q
```
