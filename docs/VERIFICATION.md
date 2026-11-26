# 实际验证结果

验证时间：2026-11-27（Asia/Shanghai）。环境：Linux/WSL，Python 3.10.12，
NumPy 2.0.0，SQLite3 标准库。下列耗时为本机一次运行的真实观测，每次会有
毫秒级波动；裁决结论与哈希在相同输入/配置下完全可复现。

## 1. 自动化测试

命令：`python -m pytest tests/ -q`

结果：**36 passed**（约 4.4s）。测试分布：

- `tests/test_1_shape_and_determinism.py`：`E_CLIP_SHAPE`、关键帧索引关系、跨实例逐字节一致、换种子改变像素。
- `tests/test_2_blocks_query_timing.py`：块覆盖、估算因子、块间部分查询、实测计时记录。
- `tests/test_3_consistency.py`：8 个 clip 分块=整段、`E_BLOCK_MISMATCH` 首帧/像素坐标定位、shape 不符检测。
- `tests/test_4_tradeoff.py`：三类样本裁决、计划哈希复现、优先级排列改变结论、风险单调性。
- `tests/test_5_invalidation.py`：定向噪声 `[5]/块[1]/段[4,5]`、提示词全 clip 重算、跨 clip 复用、二次 exact hit、修订跨进程持久化。
- `tests/test_6_cache_snapshot.py`：内容对象与缓存键字段、版本与 `E_SNAPSHOT_ROLLBACK`、过期 writer、前进式恢复、校验和篡改、二次全量无重算。
- `tests/test_7_resources_concurrency.py`：线程冲突 `E_STAGE_BUSY`、6 查询并发峰值、内存 `E_RESOURCE_LIMIT`、槽位 reject/串行降级。
- `tests/test_8_selfcheck.py`：自检全过、注入漂移返回 `E_INCONSISTENT`、未生成 clip 跳过。

## 2. 端到端验收

命令：`python scripts/run_acceptance.py .`

结果：**acceptance PASS: 22/22**（约 1.4s），完整机器可读报告：
`records/acceptance_report.json`，控制台留档：`records/acceptance_run.txt`。

22 项断言全部通过：形状约束、字节确定性、部分查询、计时记录、全 clip 一致性、
偏差定位、裁决哈希复现、优先级矩阵复现、定向失效集合、跨 clip 复用、提示词修订、
未改动 clip exact hit、快照回滚码、过期 writer、校验和、`E_STAGE_BUSY`、
查询并发、内存上限、串行降级、并行 reject、篡改检测、跨模块自检。

自检命令 `python -m kfdiff selfcheck`：**ok=True，32/32 checks passed**
（8 clip × 4 类检查）。

## 3. 首块实测 vs 估算（验收报告中的一次运行，预算 18 ms）

| clip | 选定 g | strength | 状态 | 首块实测 ms | 估算 ms | 预算内 |
| --- | ---: | ---: | --- | ---: | ---: | :-: |
| clip_0001 | 3 | 1.00 | feasible | 13.19 | 9.56 | yes |
| clip_0002 | 3 | 1.00 | feasible | 12.96 | 13.99 | yes |
| clip_0003 | 3 | 1.00 | feasible | 10.26 | 9.01 | yes |
| clip_0004 | 3 | 0.75 | approximated | 12.27 | 17.30 | yes |
| clip_0005 | 2 | 1.00 | feasible | 7.82 | 6.44 | yes |
| clip_0006 | 2 | 1.00 | feasible | 7.22 | 6.87 | yes |
| clip_0007 | 2 | 1.00 | feasible | 8.12 | 9.43 | yes |
| clip_0008 | 4 | 1.00 | feasible | 14.11 | 15.75 | yes |

裁决场景（默认优先级 `block_grid > first_frame_budget > diffusion_strength`）：

- clip_0003 请求 g=1：10 边界 / 20 耦合态，risk=0.85 > 0.30 → 放大到 g=3。
- clip_0002 请求 g=5：全强度首块估算约 22.2 ms > 18 ms → 缩小到 g=3。
- clip_0004 最大画幅 32×32、12 步：g=3 全强度约 20.6 ms 超预算 → 强度降到 0.75。

优先级敏感性（验收对 clip_0004 用更紧预算 12 ms 跑全部 6 种排列，每种两次
`plan_hash` 相同）：预算优先时会选择更小 g/更低强度，步数优先时保留
strength=1.0 并在 `residual_violations` 记录未满足的预算——证明“按声明优先级裁决”。

## 4. 修订失效传播实测

先 `generate --all`，再 `revise evt_rev2_targeted_noise`（clip_0003，g=3）：

```
recomputed_kf=[5]  reused_kf=[0,1,2,3,4,6,7,8,9,10]   # 其余为 upstream_reuse
blocks recompute=[1]  reuse=[0,2,3]
segments recompute=[4,5]  reuse=8 段
other clips: clip_0001/2/4/5/6/7/8 artifacts_intact=true, cache_state=reused
```

`evt_rev2_prompt_embedding`（clip_0004）：9/9 关键帧重算，其他 clip 全部复用。
同一修订再生成一次：`recomputed=[]`，全部 `exact_hit`。

## 5. 并发与跨进程互斥实测

- 进程内：持 `stage` 锁时另一线程提交立即得到 `E_STAGE_BUSY`；8 查询屏障观测到峰值并发。
- 跨进程：一个独立 Python 进程持有 stage 4 s，另一进程执行生成：
  `contender code: E_STAGE_BUSY after ms 0.6`（非阻塞、不等待）。

## 6. Web 应用人工/接口验证

```bash
python -m kfdiff serve --port 8931
curl -s http://127.0.0.1:8931/api/state | python -m json.tool
# 浏览器打开 http://127.0.0.1:8931/
```

已验证：`GET /` 200（约 20 KB 单文件仪表盘）、`/api/state` 返回 8 clips、
`/api/query` 9/9、`/api/frame` 返回 `BM` BMP（16×16×3 → 822 字节）、
`POST /api/selfcheck` → ok 32/32、`POST /api/generate`、
`POST /api/rollback` → HTTP 409 `E_SNAPSHOT_ROLLBACK`。
关键帧联系表见 `records/contact_sheet.png`（仅用于人工查看，由 Pillow 辅助拼图，
Pillow 不是运行依赖；应用自身用标准库编码 BMP）。

## 7. 复现方式

```bash
pip install -r requirements.txt          # 仅 numpy；环境通常已具备
python scripts/seed_data.py .             # 固定种子输入
python -m pytest tests/ -q               # 36 passed
python scripts/run_acceptance.py .       # 22/22，写 records/acceptance_report.json
python -m kfdiff serve --port 8931       # 交互应用
```

说明：真实墙钟耗时随机器负载波动，属正常观测；可复现的是确定性像素字节、
帧索引集合、`plan_hash`、失效集合与错误码。若希望裁决更宽松/严格，调整
`data/config.json` 的 `first_frame_budget_ms`、`consistency_risk_limit`、
`latency_model` 系数或 `priority_order` 即可，无需改代码。
