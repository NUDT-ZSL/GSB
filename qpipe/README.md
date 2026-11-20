# qpipe — 本地 NumPy 量化 / 校准 / 可恢复推理演示

一个纯 Python（3.10+）+ NumPy 的本地专业应用，覆盖**模型包校验 → 逐层权重量化 →
离线校准统计 → 逐层确定性推理 → 摘要输出**全链路，并演示：

- 同配置下量化参数、反量化权重、激活校验和、预测向量**逐字节可复现**；
- 进程被强制终止（`os._exit(137)`，子进程）后，从最近一次**已提交**进度点恢复，
  结果与不间断完整运行逐字节一致；
- 某层权重以更高 `weight_revision` 到达时，仅该层及其下游层失效重算，上游缓存复用，
  旧版本缓存被**整体替换**，旧版本提交被拒绝（`E_SNAPSHOT_ROLLBACK`）；
- 非阻塞并发提交控制、算力/内存/缓存条目双预算与流式降级；
- 由输入/参数触发的四阶段异常注入与链路归因；
- 跨模块一致性自检与可交互控制台。

不依赖 GPU、容器、网络、真实账号或任何付费资源。

## 目录结构

```
qpipe/
├── qpipe/                  # 核心包
│   ├── errors.py           # E_* 业务码与 PipelineError
│   ├── deterministic.py    # 规范化序列化、SHA-256、计时、内存估算
│   ├── models.py           # LayerSpec / ModelPackage / 预算 / 故障上下文
│   ├── samplegen.py        # 固定种子的清单、权重、校准张量生成
│   ├── quantize.py         # 缩放/舍入/反量化与误差统计
│   ├── calibrate.py        # 离线校准统计
│   ├── infer.py            # 逐层前向（ReLU + 末端 softmax）
│   ├── storage.py          # 原子写、非阻塞文件锁、进度点与缓存
│   ├── pipeline.py         # 四阶段编排、恢复、失效、预算、归因
│   ├── consistency.py      # 跨模块一致性自检
│   └── cli.py              # 命令行与交互式控制台
├── tests/                  # 29 个 pytest 用例
├── scripts/
│   ├── run_demo.py         # 端到端验收运行器（输出 acceptance_report.json）
│   ├── run_demo.bat        # Windows 一键演示
│   └── faults_example.json # 故障注入配置样例（仅由参数读取）
├── pyproject.toml
└── requirements.txt
```

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# 1) 生成确定性样本
python -m qpipe.cli --data-dir data/sample --work-dir data/work generate
# 2) 跑完整四阶段
python -m qpipe.cli --data-dir data/sample --work-dir data/work run
# 3) 查看当前进度点
python -m qpipe.cli --data-dir data/sample --work-dir data/work status
# 4) 一致性自检
python -m qpipe.cli --data-dir data/sample --work-dir data/work check
# 5) 交互式控制台
python -m qpipe.cli --data-dir data/sample --work-dir data/work interactive
```

一键端到端验收（含强制终止/恢复、版本失效、损坏、并发、故障注入、自检）：

```bash
python scripts/run_demo.py          # Linux/macOS
scripts\run_demo.bat                # Windows
```

## 关键演示命令

```bash
# 强制终止 + 恢复：子进程在 infer 阶段第 1 层后硬退出（137），随后恢复并比对
python -m qpipe.cli --data-dir data/sample --work-dir data/kill kill-demo \
    --kill-after-layer 1 --kill-stage infer

# 权重版本变化：仅第 2 层及其下游（2,3,4）重算，0/1 层复用
python -m qpipe.cli --data-dir data/sample --work-dir data/rev revision-demo \
    --change-layer 2

# 参数触发的异常注入（不修改源码）
python -m qpipe.cli --data-dir data/sample --work-dir data/fault \
    --faults scripts/faults_example.json run
```

## 设计要点

### 确定性

- 所有随机数来自显式 `np.random.default_rng(seed 派生整数)`，不使用全局 RNG；
- JSON 一律 `sort_keys=True` 规范化后再哈希，不依赖字典遍历顺序；
- 数组先转连续小端，再对 `shape/dtype + 原始字节` 计算 SHA-256；
- 量化采用确定性 `floor(x / scale + 0.5)`（半向上舍入）+ clip 到 dtype 范围；
- 时间仅用于 `perf_counter` 计时观测，绝不参与任何张量或校验和。

### 原子进度点与恢复

- 每个阶段**全部层**处理完才在非阻塞锁内提交进度点；
- 写入采用临时文件 + `os.replace` 原子改名，HEAD 指针最后原子切换，部分完成的
  阶段不存在进度点，因此不会被当作已完成；
- 进度点带单调递增 `progress_version`、已完成层集合、重算/复用集合与内容校验和；
- 重启读取 HEAD，已提交阶段整段复用缓存，未提交阶段从该阶段起点重建，
  进行中的中间层结果一律丢弃。

### 版本失效与整体替换

- 受影响集合 = 变更层 ∪ 全部下游层（`layer_index >= 变更层`）；
- 替换前先删除受影响层在四个阶段的旧缓存条目与数组（整体替换，非增量修补）；
- HEAD 记录每层 `weight_revision`，任何更旧修订提交触发 `E_SNAPSHOT_ROLLBACK`；
- 校准统计更新使全部层失效（全链路 fan-out），并带独立 `calibration_revision`。

### 预算与降级

- 可配置 `max_layers_per_stage`、`max_resident_dequant_bytes`、
  `max_cache_entries`、`stream_dequant_bytes`；
- 超硬上限返回 `E_RESOURCE_LIMIT`；超过流式阈值则逐层流式处理并在结果中标记
  `streaming_*` 降级类别——降级只改变调度/驻留策略，不改变已提交的反量化权重与激活；
- 内存按层形状与量化档**估算**，计时为本地循环实测，二者仅作预算观测，
  不构成真实资源上限或隔离。

### 异常注入与归因

- 注入点仅来自 `--faults` JSON（阶段、层号、kind、code、message），源码零改动；
- 每条记录带阶段名、业务码、层号；上游产物缺失时下游采用零权重/零激活降级，
  并在 `degradations` 中携带触发它的上游业务码。

## 业务错误码

| 代码 | 含义 |
| --- | --- |
| `E_PACKAGE_FORMAT` | 清单缺字段/JSON 非法/未知 quant_dtype/权重文件缺失 |
| `E_SHAPE_MISMATCH` | 层形状与权重长度不符，或校准/推理维度不符 |
| `E_SCALE_INVALID` | `scale_hint` 非正或非有限 |
| `E_ZP_OUT_OF_RANGE` | `zero_point` 超出 quant_dtype 可表示范围 |
| `E_SNAPSHOT_ROLLBACK` | 更旧修订试图覆盖更新的已提交快照 |
| `E_CACHE_CORRUPT` | 进度点/缓存条目损坏或校验和不符（含层号与版本） |
| `E_STAGE_BUSY` | 并发提交冲突，后到者立即失败不阻塞 |
| `E_RESOURCE_LIMIT` | 层数/驻留内存/缓存条目硬上限被突破 |
| `E_INCONSISTENT` | 跨模块一致性自检失败（带类别与层号） |
| `E_FAULT` / `E_UNATTACHED` | 注入故障 / 未被任何阶段归因的异常 |

## 验证结果

`pytest` 与 `scripts/run_demo.py` 的实际运行结果记录在
[docs/VALIDATION.md](docs/VALIDATION.md)。
