# 确定性增量手写数字 OCR

这是一个不依赖网络、容器或外部字体资源的本地领域组件。组件在 28×28 灰度画布上按显式笔画规则和固定种子生成 0–9 手写风格样本，完成投影特征提取、原型距离计算、拒识/置信度判决和指标汇总，并对特征、距离和判决执行内容寻址缓存、定向增量校准和版本追溯。

核心约束已经固化在实现中：

- 相同 `image_id`、种子和配置产生逐字节相同的特征与判决。
- 门限变化只使判决及指标失效；单类原型变化只使该类距离及判决失效；归一化变化使全部下游失效。
- 增量复用结果与按引用全量重算的结果逐字节一致。
- 每个判决记录特征版本、原型集合版本、门限版本、距离版本和完整节点路径。
- 旧判决在当前依赖版本变化后返回 `E_LINEAGE_STALE`，也可从历史快照和缓存重放为原结果。
- 配置与指标快照按完整载荷整体替换，支持校验和检查、回退拒绝和损坏检测。

## 快速开始

环境要求：Python 3.9+、NumPy 1.24+。本机实测为 Python 3.10.12、NumPy 1.26.4。

```bash
python -m pip install "numpy>=1.24"
python -m unittest discover -s tests -v
python -m digit_ocr --parallel-workers 2
```

最后一条命令会：

1. 从固定种子生成平衡的 30 张数字图像；
2. 使用 spawn 进程池并行计算节点；
3. 输出初始批量识别、门限校准、单类原型替换、归一化变更报告；
4. 验证失效追溯、历史重放、资源降级、严格资源错误和快照保护；
5. 将 JSON 报告写入 `artifacts/demo_report.json`，同时打印到标准输出。

Windows 下调用并行入口时，应用脚本必须保留 `if __name__ == "__main__":` 保护；组件显式使用 `multiprocessing.get_context("spawn")`。

最小集成示例见 `examples/demo.py`：

```python
from pathlib import Path

from digit_ocr import (
    DigitOCRService,
    ResourcePolicy,
    build_default_config,
    generate_samples,
)

samples = generate_samples(seed=20261123, per_class=3)
service = DigitOCRService(
    build_default_config(seed=20261123),
    Path("artifacts/my_cache"),
    ResourcePolicy(max_feature_items=64, max_parallel_nodes=2),
)
result = service.recognize_batch(samples, parallel_workers=2)
print(result.decisions[0]["cache_key"], result.metrics_artifact["accuracy"])
print(service.self_check(result))
```

## 公开入口

`DigitOCRService` 提供：

- `recognize(sample, parallel_workers=...)`：单图识别。
- `recognize_batch(samples, parallel_workers=..., update_metrics=...)`：批量识别。
- `calibrate(samples, new_config, parallel_workers=..., verify_full_recompute=True)`：增量校准。
- `replay_decision(decision, allow_archived=False)`：严格重放或历史快照重放。
- `self_check(result=None)`：跨模块一致性自检。

所有失败均抛出 `BusinessError`，其 `to_dict()` 可直接写入业务报告。

`save_config(config, path)` / `load_config(path)` 用于配置的规范 JSON 落盘与整体加载；配置内的归一化、原型集合、门限和距离度量均保留独立版本号与内容校验和。

## 确定性规则

### 图像生成

- 每类由显式七段笔画集合 `A`–`G` 拼出；固定种子只驱动端点抖动、笔画粗细、轻微斜率和确定性灰度颗粒。
- 噪声与形状参数由 `numpy.random.SeedSequence` 的显式整数熵派生，不读取系统时间、OS 熵或网络。
- 样本 ID 为固定宽度 `img-NNNN`，批量处理始终按 `image_id` 升序。
- 每张图附带真值 `label` 和确定性 `uncertain` 标记；真值不参与识别链路。

### 归一化与特征

归一化顺序为：校验 28×28 uint8 → 按配置裁剪输入范围 → 仿射缩放到输出范围。

特征共 32 维，顺序固定：

1. 8 个行投影均值；
2. 8 个列投影均值；
3. 4×4 区域积分均值。

对长度为 `size` 的第 `b` 个分箱，边界显式定义为：

```text
start = floor(b * size / bins)
stop  = floor((b + 1) * size / bins)
```

像素坐标为零基、行优先；分箱为左闭右开。特征产物同时记录行/列/区域分箱范围、像素索引规则和归一化校验和。

### 判决

- 最近原型按 `(distance, class_id)` 排序，距离相同时类别编号小者胜出。
- `best_distance <= reject_threshold` 接受，否则拒识。
- 接受样本按距离落入 `high`、`medium`、`low`；拒识样本为 `none`。
- 拒识且 `high`、接受且 `none` 均返回 `E_CONFIDENCE_CONFLICT`，错误中携带 `image_id`。

## 缓存与定向失效

每个节点条目以规范 JSON 落盘：

```text
artifacts/.../nodes/{node}/{cache_key}.json
```

条目包含 `key_parts`、`cache_key`、`artifact_sha256` 和带自身 `content_hash` 的完整产物。读取时独立重算键、产物哈希和节点种类；任一不符返回 `E_CACHE_CORRUPT`。

缓存键按节点依赖闭包组成：

| 节点 | 关键键组成 |
| --- | --- |
| feature | image_id、seed、图像内容哈希、归一化版本/校验和、投影维度、上游图像哈希 |
| distance | 图像哈希、特征产物哈希、归一化版本、对应类原型成员版本/哈希、距离度量版本/校验和 |
| decision | 图像哈希、特征哈希、全部距离产物哈希、完整原型集合版本/哈希、度量版本、门限版本/校验和 |
| metrics | 全部判决产物哈希、真值、不确定标记、图像哈希、指标规则版本 |

这是有意采用的节点级依赖闭包：距离节点不纳入门限，未受影响类原型不进入其他类距离键，因此既可独立核对，又能实现定向失效。

失效传播矩阵：

| 变更 | 重算节点 | 复用节点 |
| --- | --- | --- |
| 仅拒识门限 | 全部 decision、metrics | 全部 feature、distance |
| 替换类 `c` 原型 | 全部 `distance:c`、全部 decision、metrics | 全部 feature、其他类 distance |
| 归一化参数 | 全部 feature、distance、decision、metrics | 无上游节点 |

`calibrate()` 默认按引用重新执行全量确定性计算，并逐图比较规范字节；不一致时返回 `E_INCREMENTAL_MISMATCH` 和首个偏差 `image_id`。

## 判决追溯

每个判决包含：

- `feature_version` 和 `feature_cache_key`；
- 归一化版本/校验和；
- 原型集合版本/校验和及成员哈希；
- 门限版本/校验和；
- 距离度量版本/校验和；
- 每类距离产物哈希；
- feature、10 个 distance、decision 的完整节点路径。

`replay_decision()` 默认要求依赖版本全部匹配当前快照，并从特征与原型独立重算距离和判决。版本过旧时返回 `E_LINEAGE_STALE`，`details.stale_versions` 同时给出记录版本和当前版本。

传入 `allow_archived=True` 时，组件从版本化快照目录加载记录对应的归一化、原型、门限和度量，再从缓存特征独立重算；只有类别、拒识标记、置信度和距离全部一致才返回成功。

## 快照与资源

- 快照名：`normalization`、`prototypes`、`thresholds`、`distance_metric`、`metrics`。
- 每个版本写入完整 envelope；低版本覆盖返回 `E_SNAPSHOT_ROLLBACK`。
- 同版本不同校验和、文件篡改、声明校验和错误返回 `E_SNAPSHOT_CORRUPT`，并携带快照名。
- `ResourcePolicy.max_feature_items` 和 `max_feature_bytes` 限制特征驻留；`error` 策略超限返回 `E_RESOURCE_LIMIT`，`evict` 策略使用有界 LRU 并标记 `feature_memory` 降级。
- `max_parallel_nodes` 限制并行节点数；请求超限时降级为串行，标记 `parallelism`，但仍处理全部受影响节点。
- 并行计算固定使用 spawn；父进程按缓存键排序任务并统一落盘，合并结果与串行结果一致。

## 自检

`self_check()` 校验：

- feature、distance、decision、sample 的 `image_id` 集合一致；
- 预测类别属于受控标签 `0..9`；
- 距离可由特征与原型独立重算；
- 判决可由距离与门限独立重算；
- 拒识/置信度组合合法；
- 重算集合与复用集合无交集，并覆盖全部受影响节点及当前节点；
- 指标可由判决和标签独立重算；
- 所有磁盘缓存条目的键、产物哈希和内容哈希完整。

任一项失败返回 `E_INCONSISTENT` 和具体 `category`/`image_id`。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 成功 |
| `E_CONFIDENCE_CONFLICT` | 拒识/置信度组合非法 |
| `E_INCREMENTAL_MISMATCH` | 增量与全量判决不一致 |
| `E_LINEAGE_STALE` | 判决依赖版本已过期 |
| `E_RESOURCE_LIMIT` | 特征驻留或并行上限超限 |
| `E_SNAPSHOT_ROLLBACK` | 尝试用旧快照覆盖新快照 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符 |
| `E_INCONSISTENT` | 跨模块一致性自检失败 |
| `E_CACHE_CORRUPT` | 缓存条目索引、键或哈希损坏 |
| `E_INVALID_INPUT` | 输入/配置不合法 |

## 本次实际验证

2026-11-23 在本工作区执行：

```text
python -m unittest discover -s tests -v
```

结果：22 项测试全部通过，耗时约 16 秒，包括 spawn 并行与串行判决逐字节一致、缓存复用、四类增量传播、增量/全量一致性、过期追溯、历史重放、快照保护、资源上限和自检故障注入。

默认 CLI 实测（种子 `20261123`，每类 3 张，共 30 张）：

- 初始：29/30 正确，29 接受、1 拒识，准确率 `0.966666...`；spawn 并行已使用。
- 初始节点：361 个重算、0 个复用。
- 拒绝门限 `0.30 → 0.25`：重算 `decision=30`、`metrics=1`，复用 `feature=30`、`distance=300`，增量/全量摘要一致。
- 替换类 4 原型：重算 `distance=30`（只属于类 4）、`decision=30`、`metrics=1`，特征与其他 270 个距离复用，增量/全量摘要一致。
- 归一化输入上限 `255 → 254`：重算 `feature=30`、`distance=300`、`decision=30`、`metrics=1`，复用 0，增量/全量摘要一致。
- 门限校准后严格重放旧判决返回 `E_LINEAGE_STALE`；历史重放返回相同类别、拒识标记和置信度。
- 资源降级场景同时标记 `parallelism` 和 `feature_memory`，全部判决与基线逐字节一致，自检通过；严格特征驻留返回 `E_RESOURCE_LIMIT`。
- 快照回退返回 `E_SNAPSHOT_ROLLBACK`，错误校验和返回 `E_SNAPSHOT_CORRUPT`；缓存全量校验通过。

完整机器可读结果见 `artifacts/demo_report.json`，其中包含每图预测、10 类距离、判决缓存键、节点清单、重算/复用分类、业务码和降级标记。

更细的设计说明见 `docs/design.md`。
