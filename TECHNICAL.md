# 技术说明

## 1. 架构

```text
model_manifest.jsonl
calibration_samples.jsonl
        |
        v
DiagnosticEngine
  |-- manifest / weights    确定性清单与固定种子权重
  |-- quant                 分组缩放、舍入、反量化
  |-- calibration           校准统计与预算
  |-- inference             串联激活与预测
  |-- diagnostics           四级误差分解与容差
  |-- selfcheck             跨模块一致性
  |-- SnapshotStore         原子阶段快照
        |
        +-- CLI
        +-- 本地 Web 工作台
        +-- 自动验收演示
```

模块边界：

- `quant_diag/manifest.py`：JSONL 解析、序列校验、链形状校验、清单摘要。
- `quant_diag/weights.py`：固定种子权重生成、权重校验和、截断注入。
- `quant_diag/quant.py`：dtype 范围、分组量化、精确/存储 scale、反量化。
- `quant_diag/calibration.py`：校准统计、比较次数预算。
- `quant_diag/inference.py`：单层推理与独立复算。
- `quant_diag/diagnostics.py`：四链路误差谱、区间、放大比、容差。
- `quant_diag/snapshots.py`：非阻塞锁、原子文件替换、版本与校验和守卫。
- `quant_diag/engine.py`：四阶段编排、恢复、修订失效、预算与降级。
- `quant_diag/selfcheck.py`：层集合、量化往返、依赖集合、独立预测复算。
- `quant_diag/demo.py`：验收场景编排。
- `quant_diag/web.py` 与 `static/`：仅标准库 HTTP 服务和本地前端。

## 2. 确定性与规范序列化

所有持久化对象使用：

```python
json.dumps(
    value,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

浮点数组通过 NumPy `float64` 转 Python `float`，使用 Python 可往返的最短十进制表示。数组摘要额外包含：

- dtype
- shape
- C order
- 原始小端字节

不依赖：

- 系统时间
- 随机未播种状态
- 集合遍历顺序
- 当前工作目录
- 网络

同机、同 Python/NumPy 版本、同清单与配置下，快照和结果摘要逐字节一致。

## 3. 量化公式

对每个展平分组：

```text
positive_span = qmax - zero_point
negative_span = zero_point - qmin
q_abs = max(positive_span, negative_span, 1)

base_scale = max(max(abs(group)), 1e-12) / q_abs
scale_exact = base_scale * scale_hint
scale_stored = float64(float16(scale_exact))

code = clip(round(group / scale_exact) + zero_point, qmin, qmax)
dequant_stored = (code - zero_point) * scale_stored
dequant_exact  = (code - zero_point) * scale_exact
```

`dequant_stored` 是实际提交并用于推理的反量化权重。`dequant_exact` 只用于把量化舍入和 scale 存储误差分开。

## 4. 推理

```text
acc = W_dequant @ x        # float32 或 float64
h   = tanh(acc)            # 输出转回 float64 存储
```

每层记录：

- 输入摘要
- 反量化权重摘要
- 量化快照校验和
- 激活向量
- 激活摘要

诊断复算会再次只使用快照中已提交的 `dequant_weights`，从初始输入重建全链，确保预测不是单独缓存出来的无来源结果。

## 5. 误差来源定义

设：

- `A_ref(l)`：原始 float64 权重链路第 `l` 层激活。
- `A_round(l)`：整数码 + 精确 scale 的 float64 链路。
- `A_scale(l)`：整数码 + 存储 scale 的 float64 链路。
- `A_op(l)`：存储 scale 权重 + 实际累加精度的链路。

`A_scale` 就是“已提交反量化权重的 float64 推理”快照内参考；`A_ref` 是原始生成权重的 float64 外部量化参考。

| 来源 | 计算 |
|---|---|
| 量化舍入 | `A_round - A_ref` |
| 分组缩放 | `A_scale - A_round` |
| 激活累加 | `A_op - A_scale` |
| 逐层串联 | `A_op - A_ref` |

最终输出另给出 `final_activation_accumulation_error = A_op_last - A_scale_last`，用于单独描述激活累加误差。

每项输出：

```text
abs_mean
abs_max
abs_p05 / abs_p50 / abs_p95
relative = ||delta||_2 / max(||reference||_2, 1e-300)
error_range = [min(abs(delta)), max(abs(delta))]
error_interval_90 = [abs_p05, abs_p95]
amplification_ratio = layer_abs_mean / previous_layer_abs_mean
```

主导来源取三种局部误差均值最大者。平局按固定顺序打破，不依赖集合顺序。

容差检查采用逐层串联误差：

```text
abs_mean > tolerance_abs OR relative > tolerance_rel
```

违规时返回 `E_TOLERANCE_EXCEEDED`，`details.violations` 包含所有越界层。

## 6. 快照协议

目录：

```text
runtime/
  current.json
  commit.lock
  inflight.json
  events.jsonl
  snapshots/
    v000000000001.json
    v000000000002.json
    ...
```

`current.json` 保存：

```json
{"version": 4, "stage": "layer_inference", "content_checksum": "..."}
```

快照内容 CHECKSUM 排除 `content_checksum` 自身。读取时重新规范化并比对。

提交约束：

1. 层集合必须等于清单全部层，否则 `E_SNAPSHOT_PARTIAL`。
2. 版本必须为当前版本 + 1。
3. 旧版本提交返回 `E_SNAPSHOT_ROLLBACK`。
4. 父校验和不匹配返回 `E_SNAPSHOT_ROLLBACK`。
5. 锁冲突立即返回 `E_STAGE_BUSY`。
6. 快照文件和当前指针均通过临时文件 + `fsync` + `os.replace`。

锁实现：

- Windows：`msvcrt.locking(..., LK_NBLCK, 1)`
- Linux/macOS：`fcntl.flock(..., LOCK_EX | LOCK_NB)`
- 进程内另有非阻塞 `threading.Lock`

进程退出时 OS 自动释放文件锁，无永久陈旧锁问题。

## 7. 恢复语义

阶段是提交单元，层是执行单元。若进程在阶段中间终止：

- 最近提交快照仍指向该阶段之前的完整视图。
- `inflight.json` 只用于诊断，不参与恢复。
- 恢复删除 in-flight 记录。
- 未提交阶段从其第一层完整重建。

这比允许半阶段恢复更严格，保证任何可见快照中的层集合完整且可核对。

## 8. 修订失效

比较新清单与最近一个含量化产物的完整快照：

- `weight_revision` 降低：`E_REVISION_ROLLBACK`。
- 权重相关字段变化但修订未增加：`E_REVISION_ROLLBACK`。
- 层集合或链结构变化：`E_INVALID_MANIFEST`。
- 最早变化层为 `i`：受影响集合 `[i, n)`。

量化阶段：

- 前缀 `[0, i)` 直接复制已提交量化层。
- 后缀 `[i, n)` 重新生成权重、量化并提交。

校准阶段：

- 权重修订变化和校准样本摘要变化的并集触发后缀重算。

推理阶段：

- 仅当当前量化层校验和、输入摘要、配置摘要一致时复用某层激活。
- 从首个受影响层起重建后缀。

每次提交都写完整新快照。旧版本文件保持不可变。

## 9. 预算与降级

估算：

```text
per_layer_peak  = elements * 24 bytes
total_peak      = sum(elements) * 24 bytes
```

24 bytes/元素覆盖整数码、float64 反量化权重和工作副本。

- `per_layer_peak > max_resident_weight_mb`：硬失败。
- `total_peak > max_resident_weight_mb >= per_layer_peak`：降级。
- 降级组大小取 `degrade_group_size`，并在阶段快照记录 `grouped_quantization`。

校准比较次数按实际读取的标量元素数累计。

## 10. 自检类别

- `missing_stage`
- `stage_layer_set_mismatch`
- `four_stage_layer_set_mismatch`
- `missing_quant_layer`
- `quant_dequant_checksum_mismatch`
- `quant_code_range_mismatch`
- `quant_group_coverage_mismatch`
- `reuse_rebuild_overlap`
- `affected_rebuild_mismatch`
- `dependency_coverage_mismatch`
- `final_prediction_activation_mismatch`
- `independent_replay_mismatch`
- `snapshot_reference_replay_mismatch`

所有失败抛出 `E_INCONSISTENT`，`details.category` 给出类别。

## 11. Web API

- `GET /api/health`
- `GET /api/overview`
- `POST /api/action`

动作：

```json
{"action": "run"}
{"action": "inject", "kind": "weight_truncate", "layer_id": "layer_1"}
{"action": "recover"}
{"action": "update"}
```

服务仅绑定 `127.0.0.1`，无外部资源，无认证，适合本机诊断。

## 12. 已知边界

- “逐字节一致”以同一 Python/NumPy/CPU 环境为前提；跨架构 `tanh` 和矩阵乘法可能产生末位差异。
- 快照保存完整阶段产物，适合本题的小样本；生产规模应改为分块二进制索引。
- 校准统计当前用于链路失效、诊断与预算，不参与量化 scale 搜索。
- Web 工作台是本地单用户工具，不提供远程部署安全边界。
