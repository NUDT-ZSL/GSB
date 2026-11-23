# 运行报告

## 环境

- 日期：2026-11-24
- 平台：Linux/WSL2 x86_64（当前执行环境）
- Python：3.10.12（当前容器可用版本；代码兼容 Python 3.11+）
- NumPy：1.26.4
- 进程池：`multiprocessing` / `ProcessPoolExecutor`，启动方式 `spawn`
- 外部服务/网络/容器：未使用

当前环境没有 Python 3.11 解释器，因此实际验证在 3.10.12 上完成；项目声明支持
`Python >= 3.10`，未使用 3.11 专属语法。Windows 路径兼容逻辑和 `spawn` 进程池已按标准库接口实现。

## 样本

固定参数：`seed=20261124`，72 篇文档/6 个文档批次，30 条查询/3 个查询批次，词表 48，
权重维度 64，每词 3 个非零项。

| 文件 | 行数 | SHA-256 |
|---|---:|---|
| `data/demo/documents.jsonl` | 72 | `5b354675ac68e2f8c5423f934a11a6d390ac5feb792a08a58b70d2481e59fbce` |
| `data/demo/queries.jsonl` | 30 | `88104f577d38f51b93dcb5d7c3206c181c660c7bfdb191625ce2c1f164719c8c` |
| `data/demo/term_weights.json` | - | `417c435f55c5d2925b6f7fae8b85e4aa57f43875ad66b21176e94f8a72545bdf` |

## 调度裁决

命令参数：`slots=2`、`workers=2`、`fma_budget=26000`、`memory_budget=20000`、
`chunk_size=8`。

结果：9 个观测批次中 6 个准入、2 个降级、1 个拒绝；执行 FMA 25,990/26,000；
最大并发 2；峰值驻留缓冲 17,680/20,000 字节。

| 批次 | 裁决 | 业务码 | 优先级 | 完整 FMA | 执行规模 | 共享词项 |
|---|---|---|---:|---:|---|---:|
| `query:1001` | admitted | - | 3 | 14,558 | 10 查询, top_k<=8 | 0 |
| `query:1002` | degraded | E_COMPUTE_BUDGET | 2 | 14,585 | 7/10 查询 | 8 |
| `query:1003` | rejected | E_COMPUTE_BUDGET | 1 | 14,585 | 0 | 0 |
| `doc:3` | admitted | - | 0 | 240 | 12/12 文档 | 25 |
| `doc:6` | admitted | - | 0 | 243 | 12/12 文档 | 39 |
| `doc:2` | admitted | - | 0 | 246 | 12/12 文档 | 43 |
| `doc:1` | admitted | - | 0 | 228 | 12/12 文档 | 39 |
| `doc:4` | admitted | - | 0 | 231 | 12/12 文档 | 40 |
| `doc:5` | degraded | E_COMPUTE_BUDGET | 0 | 228 | 2/12 文档 | 11 |

完整依据位于 `artifacts/demo/outputs/plan.json`。另由单元测试验证：

- `slots=0` 时全部返回 `E_SLOT_LIMIT`
- `fma_budget=1` 时返回 `E_COMPUTE_BUDGET`
- `memory_budget=1` 时返回 `E_MEMORY_BUDGET`

## 一致性与恢复

正常演示通过全部自检：

- 分区重算：6
- 文档向量重算：62
- 查询 top-k 重算：17
- 阶段集合：`ingest 8/8`、`inverted 6/6`、`projection 6/6`、`scoring 2/2`
- 重算/复用集合：无交集，并集覆盖全部受影响批次
- 同一数据分别使用 serial 与 spawn 执行，8 个业务输出哈希完全一致

强制退出测试：

1. 在投影阶段第 17 次批次边界提交后调用 `os._exit(137)`。
2. 已提交版本为 17；恢复运行复用 8 个摄入批次、6 个倒排批次、3 个投影批次。
3. 恢复运行重算 3 个投影批次和 2 个打分批次，最终版本 22。
4. 与不间断运行逐文件比较，`differing_outputs=[]`；最终检查点文件和统计量快照也逐字节相同。

八个业务输出的 SHA-256：

```text
consistency.json       30d79554bdbfe35109ba1b0a18128f5b11ea9dfb18524e2406cc2e4fbe887fee
failures.jsonl         e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
inverted_index.json    4ac12bc3fc891e9aadf79fe90d94fd1b21e5d63ba8bd34a44d8c133afeb199b0
plan.json              82211f3567286d838878fb095006b3148d7a8fb1a5c5bf90af37876afe65a3f9
results.jsonl          854d1d2fb5e8cb59423f0801d1f3d883e89638f4505456248d5141107b673ecb
statistics.json        eb72178ff4ade9a6b92a547927f8a561a6bb8a86ae41d268261a2b86d0b34d67
validation.jsonl       7dd427453a1060d28aa7745ade42bcdbc87559c0e74d35377beeb1572ff0d790
vectors.jsonl          107185b4c071af133ecbaf93dda825318f1ae0d644f46626ce0398401e7a0a86
```

机器可读比较结果：`artifacts/recovery_comparison.json`。

## 测试结果

执行：

```bash
PYTHONPATH=. python -m pytest -q
```

结果：`20 passed`。

执行：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

结果：`Ran 20 tests ... OK`。

覆盖项：

- 固定种子样本逐字节一致
- 排序摄入、未知词项、重复 ID、`doc_len` 校验
- Neumaier 补偿求和
- 分块统计与逐文档独立重算一致
- 分区统计与全局统计互相核对
- 三类预算裁决和计划字节稳定性
- 门限严格/容差策略、次正规数、top-k 平局和精度稳定性
- 快照原子替换、校验和损坏、版本回退、同版本冲突、线程与跨进程非阻塞提交锁
- spawn 进程池单批异常隔离
- spawn 流水线端到端
- 投影阶段强制退出后的增量恢复
- 单批次失败后其余批次继续

## 报告位置

- 调度计划：`artifacts/demo/outputs/plan.json`
- 正常运行报告：`artifacts/demo/run_report.json`
- 正常一致性：`artifacts/demo/outputs/consistency.json`
- 中断恢复报告：`artifacts/recovery-interrupted/recovery_report.json`
- 恢复对比：`artifacts/recovery_comparison.json`
