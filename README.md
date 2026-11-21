# latent_diag：潜空间分块去噪/解码数值诊断工作台

面向文生图/图生图场景的**本地、确定性**诊断工作台，核心对象是潜空间
分块去噪与解码过程中的数值精度与误差传播。无需 GPU、模型文件与网络，
仅依赖 Python 3.10+、NumPy、Pillow。

## 功能总览

- **确定性四级生成流水**：文本条件嵌入 → 初始噪声构造 → 按 block_grid
  分块逐步去噪 + 边界归一化 → 潜帧解码为 RGB。同一 task_id 与配置下
  逐字节可复现（随机性全部来自 prompt_seed 显式派生子种子）。
- **分块/整块对照**：归一化统计量按整块口径计算，float64 下分块拼接
  潜帧与整块参考逐元素一致；输出边界元素最大绝对/相对偏差，不一致
  返回 `E_CHUNK_MISMATCH` 并指出首个越界 block 坐标。
- **可切换 dtype_profile**：float64 / float32 / 定点缩放整数三档共用
  同一算子语义；定点档 quant_scale 与 rounding_mode 显式配置。
- **逐级误差谱与归因**：以 float64 整块路径为参考真值，输出条件嵌入、
  分块去噪、边界归一化、定点量化、解码各级的绝对/相对偏差、分位数、
  误差放大比，并定位主导来源（分块边界 / 量化舍入 / 归一化）。
- **门限稳定性保护**：严格比较与容差带比较两种策略 + 次正规数保护，
  top-k 候选选取确定不翻转；门限策略在报告中留痕。
- **错误注入与归因**：异常参数文件按 task_id 覆盖字段触发四阶段注入，
  产出带阶段名/业务码/task_id 的结构化记录，未归因异常单独汇总。
- **版本化快照**：配置/量化参数/误差谱统计各携带单调递增版本号与
  sha256 校验和，整体原子替换；回退返回 `E_SNAPSHOT_ROLLBACK`，
  损坏返回 `E_SNAPSHOT_CORRUPT`。
- **资源上限与降级**：潜帧内存估算、单次分块数、并行任务数上限；
  超限返回 `E_RESOURCE_LIMIT` 或降级到更小分块并标记降级类别。

## 快速开始

```bash
pip install numpy pillow          # 唯一依赖

python main.py generate-samples --clear-snapshots   # 生成确定性样本
python main.py run --anomalies                      # 运行诊断（含注入）
python main.py workbench                            # 渲染最近报告

python run_demo.py                # 一键演示：含注入 + 含降级（可重复）
python -m unittest discover -s tests -v             # 20 项验收测试
```

Windows 本地命令行同样适用（所有路径均为相对路径，无平台特定调用）。

## 目录布局

```
latent_diag/        # 工作台包
  pipeline.py       # 四级确定流水 + 分块/整块对照
  profiles.py       # dtype_profile 执行层（float64/float32/定点）
  spectrum.py       # 逐级误差谱与主导来源归因
  thresholds.py     # 门限策略、容差带、次正规数保护、稳定 top-k
  snapshots.py      # 版本化快照与整体替换
  resources.py      # 资源上限与降级
  injection.py      # 异常参数注入与未归因异常汇总
  runner.py         # 运行编排与报告生成
  workbench.py      # 诊断工作台文本视图
main.py             # CLI 入口
run_demo.py         # 一键演示（含注入 + 含降级）
scripts/generate_samples.py   # 确定性样本生成
tests/              # 验收测试（unittest，20 项）
samples/  snapshots/  cache/  reports/   # 运行期目录
```

## 任务清单格式（JSONL）

每行一个任务，字段：`task_id, prompt, prompt_seed, latent_shape [C,H,W],
block_grid [gh,gw], sampler_steps, noise_scale, decode_mode
(tanh|sigmoid|linear), quant_scale, rounding_mode
(half_even|half_up|floor|trunc), dtype_profile (float64|float32|fixed),
ref_mode (whole)`。

异常参数文件（JSON）按 task_id 给出字段覆盖，例如：
`[{"task_id": "task-inj-grid", "overrides": {"block_grid": [3, 3]}}]`。

详细设计与验证结果见 `docs/TECH_NOTES.md`。
