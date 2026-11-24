# 技术说明

## 1. 模块划分

| 模块 | 职责 |
| --- | --- |
| `volumetric_engine.validation` | JSON 解析、数据文件加载、有限性/范围校验、目录路径解析 |
| `volumetric_engine.geometry` | 光线-AABB 求交、事件链、重叠归属、阴影光学厚度 |
| `volumetric_engine.renderer` | 相机射线、定点/float64 双路径积分、逐采样记录、像素合成 |
| `volumetric_engine.scheduler` | 按索引提交、线程池、内存预算、取消边界 |
| `volumetric_engine.persistence` | 版本隔离块存储、块摘要、恢复/补渲 |
| `volumetric_engine.stability` | 步长与阈值重渲、逐像素偏差和趋势分类 |
| `volumetric_engine.output` | PNG/NPY/JSONL.GZ、输出清单、临时文件提交 |
| `volumetric_engine.pipeline` | 端到端调度、恢复、稳定性、输出和完成标记 |

## 2. 统一世界坐标与介质求交

每台相机使用：

- `right = normalize(cross(up, forward))`
- `true_up = normalize(cross(forward, right))`
- 像素射线方向：`normalize(forward + x*tan(fov/2)*aspect*right + y*tan(fov/2)*true_up)`

对每个介质盒使用 slab 法求进入/离开参数 `t_enter/t_exit`，并裁剪到 `[0, +inf)`。所有盒边界成为事件点，相邻事件形成初等区间。每个初等区间取中点，查询覆盖该点的全部盒，再按 `(ior, -declaration_index)` 取最大优先级：

1. 折射率更高者拥有重叠区。
2. 折射率相同时，声明顺序更早者拥有。

因此同一世界位置不会因分块、像素位置或相机极小变化而由端点 epsilon 决定归属。相邻且同归属的初等区间会合并，非介质空隙被移除。

阴影光线使用同一归属规则，但采用事件扫描而不是显式构造整条链，以减少逐采样开销。测试验证扫描结果与链式积分一致。

## 3. 光学模型

记有效消光为 `sigma_t`，有效散射系数为 `sigma_s`。采样段长度为 `ds`，段光学厚度为：

```text
tau_step = sigma_t * ds
T_mid = T * exp(-tau_step/2)
```

点光源贡献：

```text
local = sum_i sigma_s * I_i * visibility_i / (4*pi*d_i^2)
radiance += T_mid * local * ds
T *= exp(-tau_step)
```

`visibility_i` 为从采样点到光源的介质透射率。当前版本使用各向同性相位函数。

当 `T < transmittance_cutoff` 或总步数达到 `max_steps` 时终止对应路径。终止判定是严格小于。

最终颜色使用 `1-exp(-exposure*radiance)` 映射到 0–255。深度为像素所有子样本首个介质入射距离的平均值；未命中为 `-1.0`。

## 4. 定点路径与 float64 诊断

定点标度 `S` 为正整数，初值 `T_fixed=S`。每段使用：

```text
q = round(exp(-tau_step) * S)        # 非负 half-up
T_fixed = (T_fixed*q + S//2)//S      # 整数 half-up 舍入
```

中点量也量化为 `q_mid=round(exp(-tau_step/2)*S)`。由于因子和乘积都发生舍入，量化路径可能比 float64 更早穿过截断阈值。

同时维护一个“量化因子精确乘积”`fixed_product_t`，只用于衡量定点路径自身的乘法舍入偏差：

```text
fixed_rounding_difference = abs(T_fixed/S - fixed_product_t)
```

像素最终裁定 `fixed_verdict` 只依据该定点内部偏差与容差；图像和深度也只使用定点路径。float64 路径的结果不参与最终颜色或最终裁定。

逐采样轨迹列：

| 列 | 含义 |
| --- | --- |
| `x,y,sample,step` | 像素、子样本、全局步序号 |
| `medium_id,medium_index` | 介质归属与声明索引 |
| `segment_start,sample_t,segment_length` | 世界射线参数位置与段长 |
| `segment_optical_depth` | 当前段光学厚度 |
| `accumulated_optical_depth` | 到采样中点的累计光学厚度 |
| `float_transmittance` | float64 透射率 |
| `fixed_transmittance` | 定点透射率 |
| `abs_difference` | 两者绝对差 |
| `fixed_rounding_difference` | 定点路径自身舍入偏差 |
| `fixed_active` | 该步定点路径是否仍在步进 |

首次 `abs_difference > transmittance_tolerance` 的记录会复制到 `error_report.json` 的 `first_exceed`。

## 5. 稳定性评估

基准配置为 `scale=1.0`。对 `jitter.json` 中每个步长/阈值缩放：

- 步长：`step = base_step * scale`
- 阈值：`cutoff = base_cutoff * scale`

每次保持相同相机、介质、光源与抖动序列重渲，逐像素比较灰度：

- `max_abs_pixel_deviation`
- `mse` / `rmse`
- `changed_pixels`
- `threshold_early_terminated_pixels`

趋势分类：

- 步长按从大到小排列，若 RMSE 单调不增：`converges_as_step_size_decreases`。
- 阈值按从小到大排列，若 RMSE 单调不减：`diverges_as_cutoff_increases`。

“提前终止像素”定义为：任一样本的定点路径在某步低于阈值，而 float64 路径在同一步仍未低于阈值，或 float64 尚未终止。

## 6. 分块并发与内存预算

分块覆盖普通像素网格，每个像素包含 `samples_per_pixel` 个子样本。每个在途块保守记为：

```text
tile_width * tile_height * samples_per_pixel * 1024 bytes
```

调度器严格按块索引顺序提交：

1. 若下一个块加上当前在途预算不超过上限，提交。
2. 否则等待至少一个块完成，不跳过该块。
3. 线程数上限为 `max_workers`。
4. 子样本数量、光线步长、最大步数从不因预算降低。

每块的结果、轨迹和像素误差均为块私有。最终按块索引拼装并再次按 `(x,y,sample,step)` 排序轨迹，因此任意并发度结果一致。

## 7. 版本、持久化与恢复

渲染版本标识摘要包含：

- 算法语义版本
- 介质中心、尺寸、消光、反照率、折射率、声明顺序
- 光源位置与强度
- 相机位置、朝向、上方向、垂直视场角、宽高比
- 分辨率、超采样、步长、最大步数、截断阈值、定点标度、容差、曝光、分块规格
- 子样本抖动偏移

块文件路径为 `state/blocks/<version>/block_<index>.json`。版本变化时新版本使用新目录，旧块保留，不直接覆盖。

每个块写入前：

- 若同路径已有不同版本块，拒绝覆盖。
- 计算除 `digest` 外全部内容的规范化 SHA-256。
- 写同目录临时文件，fsync 后 `os.replace`。

完成后的强制校验会重新读取每个块，验证：

- 格式与版本
- 内容摘要
- 声明像素数
- 颜色/深度/诊断数组尺寸
- 轨迹列与行结构
- 块几何与当前分块布局

缺失或异常块会记录 `E_DIGEST`，补渲一次；仍失败则不写输出。

## 8. 中断与原子输出

`SIGINT`/`SIGTERM` 的处理器只设置线程安全事件，不做 I/O。调度器在块边界停止提交新块，等待已运行块完成并持久化，然后写中断标记：

- `SIGINT` → 130
- `SIGTERM` → 143

未完成块不会产生“完成”文件。被强杀后遗留的临时文件不会被恢复逻辑读取。

输出提交顺序：

1. 生成 PNG、NPY、轨迹 GZ、报告 JSON 的完整字节。
2. 在同目录写临时文件并 fsync。
3. 读取已有输出清单、PNG 尺寸与摘要版本。
4. 同版本且全部文件摘要/尺寸有效：完全跳过替换。
5. 版本不同，或同版本但有文件损坏：按序原子替换，清单最后提交。

多文件系统无法提供单个跨文件原子事务，因此 `manifest.json` 是提交完成点；此前所有文件都已完整写入临时文件，读者只有在清单有效时才应认为整组输出完成。

## 9. 已知边界

- 首版为灰度，彩色介质按 Rec.709 权重合成。
- 点光源为理想各向同性点光，不含面光源/环境光。
- 折射率只参与重叠优先级，不改变射线方向；首版不做折射偏折。
- 深度定义为首个介质入射距离，不代表盒内散射事件深度。
- 块文件包含完整逐采样轨迹，状态体积随像素、超采样和步数增长。
