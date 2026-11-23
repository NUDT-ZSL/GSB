# RepairFlow Studio

面向图像修复与风格迁移的本地确定性应用。核心不是一次性全图处理，而是：

- 以区域为单位建立五节点 DAG；
- 每个区域、每个节点都把产物写入内容寻址磁盘缓存；
- 掩码扩大/缩小/合并或风格参数变化时，只使受影响节点失效；
- 增量拼接结果与一次全量重算逐字节一致；
- 通过版本化快照、非阻塞并发提交、资源上限和跨模块自检守住一致性边界。

应用不访问网络、不读取真实图像素材、不使用容器或 GPU。底图、掩码和配置均由固定种子脚本生成。

## Windows 快速启动

在 PowerShell 中执行：

```powershell
cd <项目目录>
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -c "import numpy, tkinter; print(numpy.__version__, tkinter.TkVersion)"
```

生成固定种子样例：

```powershell
python samples\generate_samples.py
```

运行离线验收演示：

```powershell
python -m repairflow.cli check --data samples\data --workdir artifacts\runtime
```

启动桌面应用：

```powershell
python -m repairflow.app --data samples\data --cache .repairflow_cache\gui
```

或者：

```powershell
python -m repairflow.cli gui --data samples\data --workdir artifacts\runtime
```

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 固定种子样例

`samples/generate_samples.py` 使用 `numpy.random.PCG64(20261124)` 在本地生成：

- `samples/data/base.npy`：`240x320x3` 的 `uint8` RGB 底图和结构纹理；
- `samples/data/masks/region_00N.npy`：四个不重叠的封闭区域掩码；
- `samples/data/style.json`：风格强度、颜色矩阵、边缘保留系数、迭代步数、精度档、融合羽化、资源上限；
- `samples/data/manifest.json`：底图内容哈希、掩码校验和、配置校验和与文件 SHA-256；
- `samples/data/base.ppm` 与 `mask_overlay.ppm`：无需额外图像库即可查看的预览。

样例生成不读取系统时间、进程级随机状态或网络。重复执行脚本会得到相同数组、相同 manifest 哈希。

## 五节点计算图

配置文件里的 `graph` 明确定义节点和边。实现只接受经过 DAG 校验的五节点串联拓扑：

| 节点 | 语义 | 主要输入 | 产物 |
| --- | --- | --- | --- |
| `mask_parse` | 掩码区域解析 | 区域掩码、区域 bbox | 区域掩码裁剪 |
| `boundary_context` | 边界上下文采样 | 底图、上下文半径 | 上下文 RGB 裁剪 |
| `content_completion` | 区域内容确定性补全 | 上下文、区域掩码、迭代步数、精度档 | 补全后的上下文 RGB |
| `style_transfer` | 风格迁移着色 | 补全结果、风格强度、颜色矩阵、边缘系数 | 风格化 RGB |
| `blend_output` | 边界融合与全图输出 | 风格化 RGB、内收羽化半径 | `RGB + alpha` 区域产物 |

算法全部为本地 NumPy 计算。补全使用确定性的最近边界颜色播种与固定次数扩散；风格化使用颜色矩阵、强度混合和边缘感知平滑；融合使用掩码内收的整数距离羽化。

## 内容寻址缓存

缓存目录结构：

```text
<cache>/
  objects/<product_hash>.npy
  records/<cache_key>.json
```

每个节点的缓存键是规范化 JSON 的 SHA-256，包含：

- `base_hash`：底图内容哈希；
- `region_mask_digest`：区域掩码内容摘要；
- `context_radius`：边界上下文采样半径；
- `style_param_version`：该节点实际消费的风格参数投影摘要；
- `upstream_artifact_hashes`：全部上游节点的产物哈希；
- `node_params`：节点算法版本、迭代步数、精度档、颜色矩阵、羽化半径等；
- `graph_signature`、区域编号与缓存 schema 版本。

`style_param_version` 使用节点依赖投影，而不是把整个风格字典混入所有节点。这样：

- 只改 `style_strength` 时，`mask_parse`、`boundary_context`、`content_completion` 的键不变；
- `style_transfer` 和 `blend_output` 的键变化；
- 既满足“键包含风格参数版本”，又不会误伤上游复用。

所有数组产物同时记录内容哈希。验收报告和命令行查询入口会展示键组成与产物哈希：

```powershell
python -m repairflow.cli inspect --data samples\data --workdir artifacts\inspect --region 1 --json
```

Python API：

```python
engine.inspect_region(region_id)
```

返回每个节点的 `key_components`、`artifact_hash`、落盘文件状态和最近一次命中/重算状态。

## 定向失效

失效由编辑差异推导，再由缓存键实际命中结果确认。报告同时给出语义失效范围与实际重算/复用范围。

| 编辑 | 失效节点范围 |
| --- | --- |
| 扩大某区域 | 该区域五节点；其他区域不失效 |
| 缩小某区域 | 该区域五节点；其他区域不失效 |
| 合并区域 | 目标区域五节点；被合并源区域从快照中移除；其他区域不失效 |
| 只改风格强度/颜色矩阵/边缘系数 | 所有区域的 `style_transfer` 与 `blend_output` |
| 修改迭代步数/精度档 | 所有区域的 `content_completion`、`style_transfer`、`blend_output` |
| 修改融合羽化 | 所有区域的 `blend_output` |
| 修改上下文半径 | 所有区域五节点 |
| 修改底图或图拓扑 | 全部失效 |

每次提交报告包含：

- `edits`：触发变更的编辑记录；
- `invalidated_regions`：语义失效区域；
- `recomputed_regions` / `reused_regions`：实际重算与复用区域；
- `recomputed_nodes` / `reused_nodes`：逐区域、逐节点状态；
- `cache_hits` / `cache_misses`；
- `degradation`、`execution`、`snapshots`、`blend_check` 与 `self_check`。

其中 `invalidated_regions` 是本次编辑的语义失效范围；`affected_regions` 是当前整图参与集合覆盖自检的全部活动区域，因此恒等于 `recomputed_regions ∪ reused_regions`。

## 融合一致性

每个区域融合产物是 `HxWx4` 数组，前三通道为风格化 RGB，第四通道为显式羽化权重：

- 权重只在掩码内非零，掩码外严格为 `0`；
- 每个像素 `region_weight + (1 - region_weight) = 1`；
- 区域互不重叠，因此全图总区域权重不超过 `1`；
- 区域逐块拼接路径与一次全图融合路径分别计算并逐像素比较；
- 差异会抛出 `E_BLEND_MISMATCH`，携带首个差异行列坐标、通道和所属区域。

正常样例的融合偏差谱全部为 `0`。桌面应用会按区域展示融合带像素数、差异像素数、最大通道差和 `0/1/2-3/4-7/8-15/16-31/32-63/64+` 偏差直方图。

## 版本化快照

掩码、风格参数和整图结果分别维护独立快照：

- 每个快照有单调递增版本号和内容 SHA-256；
- 内容不变时版本不变，内容变化时版本 `+1`；
- 提交以完整对象替换旧快照，不做字段级修补；
- 旧版本尝试覆盖新版本返回 `E_SNAPSHOT_ROLLBACK`；
- 期望校验和与实际内容不符、同一版本对应不同内容、快照被篡改，返回 `E_SNAPSHOT_CORRUPT`。

引擎入口：

```python
engine.snapshot_status()
engine.verify_snapshots()
```

## 并发与资源上限

- 单图提交使用非阻塞 stage 锁。另一个提交正在执行时，后到者立即返回 `E_STAGE_BUSY`，不等待。
- `limits.max_regions` 控制单图区域数。
- `limits.max_resident_pixels` 按各区域上下文面积 × `resident_node_copies` 估算驻留像素数。
- `limits.max_parallel_regions` 控制并行重算区域数；超过上限时自动降级为串行，报告类别 `parallel_region_limit_serial`，并明确 `omitted_regions: []`。
- 区域数和驻留像素超限返回 `E_RESOURCE_LIMIT`；降级只影响并行度，不遗漏任何受影响区域。

## 结构化业务错误

关键校验与失败码：

| 场景 | 业务码 |
| --- | --- |
| 重复区域编号 | `E_DUPLICATE_REGION` |
| 掩码为空 | `E_EMPTY_MASK` |
| 区域越出画布 / 掩码形状不符 | `E_REGION_OUT_OF_BOUNDS` |
| 区域重叠 | `E_MASK_OVERLAP` |
| 风格强度越界 | `E_STYLE_STRENGTH_RANGE` |
| 精度档未知 | `E_UNKNOWN_PRECISION` |
| 迭代步数非正 | `E_INVALID_ITERATIONS` |
| 上下文半径非正 | `E_INVALID_CONTEXT_RADIUS` |
| 并发提交冲突 | `E_STAGE_BUSY` |
| 区域数/驻留像素超限 | `E_RESOURCE_LIMIT` |
| 节点执行失败 | `E_REGION_NODE_FAILURE`（含 `region_id`、`node`） |
| 融合差异 | `E_BLEND_MISMATCH`（含坐标、区域） |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` |
| 快照损坏/校验和不符 | `E_SNAPSHOT_CORRUPT` |
| 自检集合不一致 | `E_INCONSISTENT` |

所有错误通过 `BusinessError.to_dict()` 输出 `code`、`message` 和机器可读 `details`。区域级异常不会被静默丢弃，也不会用未修复底图像素冒充结果。

## 桌面应用

`python -m repairflow.app` 启动 Tkinter 专业工作台，包含：

- 全图输出与区域轮廓/编号；
- 计算图拓扑和当前参数；
- 区域 × 节点的缓存键、产物哈希、落盘状态、命中或重算状态；
- 编辑记录、失效范围、重算/复用清单和降级信息；
- 融合带偏差谱；
- 三快照版本、校验和与最近业务错误码；
- 扩大、缩小、合并、参数提交、编辑演示、全量重算对比和一致性自检按钮。

“演示：编辑+全量对比”会先扩大选中区域并调整风格强度，再提交一次增量，随后自动执行全量重算对比。

## 实际验证结果

本次交付在本地离线环境执行了样例生成、18 项单元测试和 5 步验收演示。

```text
$ python -m unittest discover -s tests -v
Ran 18 tests in 2.511s
OK
```

验收演示实际结果：

```text
initial          recomputed=[1, 2, 3, 4] reused=[]          byte_equal=True artifacts_equal=True
grow_region_1    recomputed=[1]          reused=[2, 3, 4]   byte_equal=True artifacts_equal=True
shrink_region_2  recomputed=[2]          reused=[1, 3, 4]   byte_equal=True artifacts_equal=True
style_strength   recomputed=[1, 2, 3, 4] reused=[]          byte_equal=True artifacts_equal=True
merge_3_4        recomputed=[3]          reused=[1, 2]      byte_equal=True artifacts_equal=True
all_passed: True
```

其中风格强度步骤的实际节点清单为：

```text
region 1: recomputed=[style_transfer, blend_output]
region 2: recomputed=[style_transfer, blend_output]
region 3: recomputed=[style_transfer, blend_output]
region 4: recomputed=[style_transfer, blend_output]
```

验收产物：

- `artifacts/runtime/acceptance_report.json`：完整机器可读结果、缓存键查询、快照与防护检查；
- `artifacts/runtime/reports/`：每一步完整增量报告与全量对比；
- `artifacts/runtime/images/`：每一步修复结果 PPM。

本仓库当前执行环境为 Linux 无头容器，自带 Python 3.10 与 NumPy，但未安装 Tkinter，因此核心、CLI、缓存、快照和测试均做了实际运行验证，GUI 未做窗口级启动验证。代码仅使用 Python 3.11/3.12 兼容语法与 Windows 官方 Python 自带 Tkinter，不依赖 Linux 图形组件。
