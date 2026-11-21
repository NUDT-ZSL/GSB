# 技术说明：潜空间分块去噪/解码数值诊断工作台

## 1. 确定性设计

- 所有随机性来自 `prompt_seed`：按阶段标签派生子种子
  `seed = (prompt_seed * A + sha256(prompt)[:8] + stage_tag * B) mod 2^63`，
  再由 `numpy.random.default_rng` 生成条件向量（阶段 1）、初始噪声
  （阶段 2）、条件投影矩阵（阶段 3 内部）。无系统时间、无字典序依赖
  （JSON 一律 `sort_keys`）、无未声明外部状态。
- 精度档只在阶段边界 cast 中间结果，算子一律在 float64 上求值，
  因此切换 dtype_profile 不改变 task 集合与随机数消费顺序
  （测试 `test_profiles_share_inputs_and_rng` 直接断言三档参考路径
  逐元素一致）。

## 2. 分块/整块一致性

- 去噪算子为纯逐元素运算 `x - (noise_scale/(s+1)) * tanh(x*0.5 + bias)`，
  分块执行与整块执行在浮点语义上完全等价。
- 边界归一化统计量（均值/标准差）按**整块口径**在拼接后的完整潜帧
  上计算，再逐块应用；因此 float64 下分块拼接潜帧与整块参考**逐元素
  bit 级一致**（`elementwise_equal=True`，边界 max_abs/max_rel 偏差
  均为 0.0）。
- 若不一致（例如统计量被改成按块口径），`compare_chunked_vs_whole`
  抛 `E_CHUNK_MISMATCH` 并给出首个越界 block 坐标（有单测覆盖）。

## 3. dtype_profile 执行层

| 档位 | 阶段边界 cast | 定点量化级 |
|---|---|---|
| float64 | 恒等 | 恒等 |
| float32 | float32 舍入 | float32 舍入 |
| fixed | 恒等（float64 计算） | `round(x*scale)` 截断 int32 后 `/scale` |

定点档的 `quant_scale` 与 `rounding_mode`（half_even/half_up/floor/
trunc）由清单显式给出；非正 scale 或未知舍入模式在 decode 阶段触发
`E_INVALID_QUANT_SCALE` / `E_UNKNOWN_ROUNDING_MODE`。

## 4. 误差谱与归因

以同一输入的 float64 整块路径为参考真值，对 cond_embed /
chunk_denoise / boundary_norm / quantize / decode 五级输出：
max/mean 绝对偏差、max 相对偏差、p50/p90/p99/p100 分位数、
误差放大比（本级 max_abs / 上一级 max_abs）。

主导来源评分（全部中间量写入报告，可复算）：

- `chunk_boundary`：归一化后边界元素相对内部元素的超额偏差
  `max(0, bnd_max - interior_max)`；
- `normalization`：归一化级相对去噪级的增量误差（统计量放大）；
- `quantization`：量化级相对归一化级的增量误差。

`dominant = argmax(scores)`，全零为 `none`。

## 5. 门限稳定性

- `strict`：裸比较；`tolerance_band`：门限 ±band 内的值吸附到门限后
  判定，带边界用 `math.nextafter` 外扩一个 ulp，保证门限附近元素在
  重复运行与多精度档下不被舍入翻转。
- 次正规数保护：`|v| < float64.tiny` 按 0 处理。
- top-k：按容差带吸附后的键降序、索引升序决胜，并列确定。
- 门限策略指纹随报告 `threshold_policy_trace` 留痕。

## 6. 快照语义

每类快照一个 JSON 文件 `{name, version, checksum, payload}`，
`checksum = sha256(canonical_json(payload))`。

- `update`：版本 = 已存版本 + 1（初始 1），临时文件 + `os.replace`
  原子**整体替换**，不做增量修补；
- `save` 版本 ≤ 已存版本 → `E_SNAPSHOT_ROLLBACK`，旧版本不得覆盖；
- 加载校验失败（不可解析或校验和不符）→ `E_SNAPSHOT_CORRUPT`。

## 7. 资源上限与降级

- 潜帧驻留内存估算 = C*H*W*8*2（活动 + 参考两份 float64），超限
  `E_RESOURCE_LIMIT`；
- 分块数 gh*gw 超限 → 在整除约束下确定性搜索最大可行网格，降级并
  标记 `BLOCK_GRID_REDUCED`（记录原始网格）；
- 任务数超并行上限 → 顺序分批，标记 `PARALLEL_BATCHED`。

## 8. 实际验证结果（本机，Python 3.10.12 / NumPy 1.26.4 / Pillow 11.0.0）

- `python -m unittest discover -s tests`：**20 项全部通过**，覆盖
  逐字节复现、分块=整块 bit 级一致、三档精度仅精度差异、五个注入点
  归因、快照单调版本/回退拒绝/损坏检测、资源降级、门限与 top-k
  稳定性、量化归因可复算。
- `python run_demo.py`（含注入 + 含降级，重复两次）：
  - 含注入演示 report_hash 稳定为 `23d3659d32326fee...`，
    含降级演示稳定为 `64127dc98bf873b1...`（重复运行逐字节一致）；
  - 11 个任务：6 个正常完成，5 个注入任务分别归因到
    cond_embed/`E_BAD_PROMPT_SEED`、noise_construct/`E_BAD_LATENT_SHAPE`、
    chunk_denoise/`E_BLOCK_GRID_INDIVISIBLE`、decode/`E_INVALID_QUANT_SCALE`、
    decode/`E_UNKNOWN_ROUNDING_MODE`；
  - float64 任务五级误差全 0、归因 `none`；float32 任务归一化级
    放大比 ≈2.3、主导来源 `normalization`；定点任务量化级
    max_abs ≈ 1/(2*quant_scale)、主导来源 `quantization`；
  - 降级演示中 4 个任务 `BLOCK_GRID_REDUCED`（如 [8,8]→[4,1]），
    整体 `PARALLEL_BATCHED`；
  - 连续两次 `main.py run`：快照版本 config/quant/error_spectrum
    由 v1 单调递增至 v2，整体替换生效。
