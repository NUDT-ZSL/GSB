# 采样诊断工作台技术说明

## 1. 环境

* Python 3.11.18（通过 `uv python install 3.11` 用户态安装，无需 root/容器）
* NumPy 2.4.6，虚拟环境位于仓库根目录 `.venv`
* 仅使用标准库 + NumPy；无数据库、无外部网络服务、无真实模型

复现环境：

```bash
uv venv --python 3.11 .venv
uv pip install "numpy>=2.0"
```

## 2. 模块结构

| 模块 | 职责 |
| --- | --- |
| `errors.py` | 业务错误码常量与 `BenchError` 结构化异常（码/请求/阶段/字段/详情） |
| `models.py` | 请求、采样参数、词表、step 记录、采样结果、账本事件的数据模型与 JSON 序列化 |
| `dataset.py` | 主种子驱动的确定性数据集生成（请求 JSONL、词表、每请求 logits `.npy`） |
| `validation.py` | 重复 id、采样参数、长度、种子、词表版本的校验链路，单请求失败不阻断其他请求 |
| `sampler.py` | 温度缩放、top-k、softmax、top-p、逆 CDF 抽取的确定性逐 step 采样核心 |
| `budget.py` | 步数/缓存/槽位三预算账本，事件带单调序号，可由明细重算 |
| `admission.py` | 波次打包准入、三预算裁决、策略画像与确定性取舍仲裁 |
| `scheduler.py` | 进程池分块、确定性派发/归并、单进程重算比对、强制终止注入 |
| `snapshot.py` | 版本号 + SHA-256 校验和的原子整体替换快照 |
| `checkpoint.py` | 请求边界检查点、原子提交与恢复报告 |
| `engine.py` | 串联校验、准入、调度、快照、检查点的编排引擎 |
| `selfcheck.py` | 跨模块一致性自检 |
| `demo.py` / `cli.py` | 预算超限与崩溃恢复演示、交互式 REPL |

## 3. 确定性设计

* **唯一随机源**：每个请求使用 `numpy.random.RandomState(request.seed)`，
  逐 step 调用 `random_sample()` 得到显式均匀数并记录在 `StepRecord`。
  数据集本身由主种子 `RandomState(master_seed)` 生成。无系统时间、无 os.urandom。
* **稳定排序**：top-k/top-p 一律用 `np.lexsort` 做「分数降序、并列按 id 升序」，
  消除字典/排序的非确定性。
* **固定精度**：logits 使用 `float64`，概率记录为 Python float，序列化为规范化 JSON
  （`sort_keys=True`，固定分隔符），校验和基于规范化字节串。
* **确定性调度**：批次按请求 id 升序切块，`ProcessPoolExecutor.map` 保序派发与归并，
  分块边界只依赖 `batch_size` 与 id 排序。

## 4. 三预算与波次准入

准入把批量解码建模为波次（wave）：

* 请求按显式优先级排序：`(priority 升序, 目标长度升序, 缓存规模升序, id 字典序)`；
* 每波并发数受 `slot_budget` 限制，波内同时驻留缓存受 `cache_budget` 限制；
* 采样步数为全局累计，受 `step_budget` 限制；
* 波次数量受 `wave_budget`（完成时间预算）限制，波次耗尽时按受限维度拒绝。

错误归因维度固定顺序为 steps → cache → slots，对应
`E_STEP_BUDGET` / `E_CACHE_BUDGET` / `E_SLOT_BUDGET`。

## 5. 取舍仲裁

三个策略画像 `balanced` / `conservative` / `aggressive` 显式表达：

* 槽位增大缩短完成时间但放大缓存峰值（aggressive vs conservative）；
* `length_scale` 调整目标长度，长度上升增加步数预算；
* `top_p_cap` 收紧核采样，`detect_top_p_regrets` 记录被淘汰的基准 token。

裁决字典（全序、无随机）：准入数最多 → 缓存峰值最小 → 总步数最小 →
槽位峰值最小 → 策略声明顺序。同一观测下结果逐字节一致。

## 6. 快照与检查点

* 快照三类：`samples` / `ledger` / `decisions`，各含单调版本号与 SHA-256；
  写入走同目录临时文件 + `os.replace` 原子整体替换。旧版覆盖新版或跳号返回
  `E_SNAPSHOT_ROLLBACK`，JSON 损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`。
* 检查点仅在批次（请求）边界提交，记录已提交 id、结果、账本事件、下一请求 id。
  崩溃时未提交的进程内采样状态随进程消失，重启后从下一请求重建，已提交预算
  从检查点账本事件恢复，不重算、不重复计入。

## 7. 错误码映射

| 错误码 | 触发条件 |
| --- | --- |
| `E_DUP_REQUEST` | 重复请求标识 |
| `E_SAMPLING_INVALID` | 温度/top-k/top-p 越界或非数值 |
| `E_LENGTH_INVALID` | 目标生成长度非正 |
| `E_SEED_INVALID` | 种子不可解析或超 int64 |
| `E_VOCAB_VERSION_MISSING` | 词表缺少 vocab_version |
| `E_STEP_BUDGET` / `E_CACHE_BUDGET` / `E_SLOT_BUDGET` | 对应预算超限 |
| `E_BATCH_MISMATCH` | 进程池结果与单进程重算首个偏差 step 不一致 |
| `E_SNAPSHOT_ROLLBACK` / `E_SNAPSHOT_CORRUPT` | 快照回退/损坏 |
| `E_INCONSISTENT` | 自检发现首个偏差位置 |

## 8. 常用命令

```bash
# 生成确定性数据集
.venv/bin/python -m sampling_bench.cli generate --data-dir ./work/data --num-requests 12
# 预算超限 + 强制终止恢复演示，输出诊断报告 JSON
.venv/bin/python -m sampling_bench.cli demo --base-dir ./work/demo
# 交互式工作台
.venv/bin/python -m sampling_bench.cli repl --data-dir ./work/data --work-dir ./work/repl
# 测试
.venv/bin/python -m unittest discover -s tests -v
```
