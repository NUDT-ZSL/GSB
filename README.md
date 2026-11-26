# 量化核函数自动调优引擎

可运行实现位于 `quant_tuner/`，测试位于 `tests/`，强制 `SIGKILL` 恢复验收位于 `scripts/run_acceptance.py`。

```bash
python3 -m pip install -r requirements.txt
python3 -m quant_tuner.cli generate-samples --output samples
python3 -m unittest discover -s tests -v
./scripts/acceptance.sh
```

常用 CLI：

```bash
python3 -m quant_tuner.cli run --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json --config samples/engine_config.json \
  --output runs/demo
python3 -m quant_tuner.cli run --resume --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json --config samples/engine_config.json \
  --output runs/demo
python3 -m quant_tuner.cli verify --tasks samples/tuning_tasks.jsonl \
  --platform samples/platform_stats.json --config samples/engine_config.json \
  --output runs/demo
```

技术设计见 `docs/TECHNICAL_NOTES.md`，实际验证结果见 `docs/VALIDATION.md`。

---

工作目录尚未创建任何文件，需要先落地一个可运行的量化核函数自动调优引擎。业务对象是算子调优搜索空间与平台定量统计表：调优任务清单为 JSONL，字段含 tuning_id、op_kind（matmul 或 conv2d）、workload_shape、quant_config（scale 与 zero_point）、候选调度列表、target_latency_us、priority、logical_clock_ms；平台统计表给出各层缓存、线程占用与吞吐的代价值。系统需在并行搜索过程中对每个评估点做定点量化反量化、候选配置预测与实测比较，同时处理两代调优配置（v1 单文件 JSON，记录相对生效秒与扁平参数；v2 分片 JSON 加 SQLite 元数据库，记录绝对毫秒、调优代数与摘要）和强制终止后的恢复。核心复杂点在于并行预算与精度误差传播互锁，以及中断恢复必须丢弃未提交评估点并保持调度账本一致。

任务环境与准备：
Linux（WSL Ubuntu）环境，已安装 Python 3.10+，推荐 NumPy 1.24+，使用本地 SQLite 模块。
无需外部网络；准备固定种子脚本来生成本地输入 JSON 与 JSONL 样本。
创建空工作目录，安装依赖：pip install numpy==1.24.4 或兼容版本。

业务要求：
1. 实现 v1 与 v2 调优配置的读取与统一内部表示，v1 为单文件 JSON 记录相对生效秒与扁平参数，v2 为分片 JSON 加 SQLite 元数据库记录绝对毫秒、调优代数与内容摘要；未知版本返回 E_VERSION_UNSUPPORTED，字段缺失或时间戳倒退返回 E_FORMAT_CORRUPT，并把两代配置互迁到同一量化参数与调度搜索空间。
2. 实现确定性的定点量化与反量化模拟层，按 quant_config 中的 scale 与 zero_point 对 workload 权重和激活做量化反量化，量化公式与舍入模式显式给出，同一 tuning_id、workload_shape 与配置下必须逐字节产出一致的量化误差指标。
3. 实现并行调度计划器，按可配置 worker 数、并发预算和逻辑时钟预算把候选调度分派到本地进程池，分派顺序与打分归并顺序确定性可复现，并基于平台统计表估算每个候选的乘加预算、驻留内存与预期延迟。
4. 实现并行预算与精度取舍的显式裁决：当调高并行度缩短总体搜索耗时但放大驻留内存和核间干扰，或提高定点量化精度但增加反量化计算预算时，按配置声明的优先级顺序在进度、内存、精度之间裁决，拒绝候选时返回 E_BUDGET_EXCEEDED 或 E_PRECISION_TRADE。
5. 实现基于实际循环计时的延迟观测与结果账本，每个已提交调优点必须记录实际耗时、量化误差和调度配置，账本以 versioned snapshot 落盘，内容校验和由确定性哈希生成，检测到版本或校验和变化时整体替换旧快照而非增量修补。
6. 实现两代调优配置互迁后的量化统计量失效传播，当 v2 配置代数提升、某 workload 的 quant_config 或候选调度版本修订时，只对受影响的调优点和统计量做重算，复用未受影响的上游量化摘要，并输出重算与复用集合。
7. 实现强制终止后的检查点恢复，并行评估器在调度批次边界提交带单调递增版本号的检查点，重启后读取最近检查点从下一调度继续，已提交调优点不得重算，未提交的中间量化与调度账本必须丢弃，不得生成包含未提交评估点的退出报告。
8. 实现并发提交控制与跨模块一致性自检，同一时刻只允许一个账本快照提交生效，并发冲突时后到者立即返回 E_STAGE_BUSY 而不阻塞；自检校验账本中的调优点与调度批次一致、量化误差可独立复算、重算与复用集合无交集且并集覆盖全部受影响集合，不一致返回 E_INCONSISTENT。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
运行本地确定性样本完成调优搜索与一次强制终止恢复，验证账本、误差传播和恢复一致性。
