# GlyphTrace OCR Data Lab

本地、离线、可复现的 OCR 文字检测与识别领域数据编辑/操作应用。系统从固定种子生成灰度页面样本，以确定性的四阶段流水执行识别，并提供注入、归因降级、阶段快照、强制终止恢复、模板升级和跨模块自检。

不使用 Docker、外部字体、网络资源或未声明的随机源。运行时依赖仅为 Python 3.10+、NumPy 和 Pillow。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 -m ocrlab init --force
python3 -m ocrlab run --reset
python3 -m ocrlab serve --port 8765
# 或：python3 app.py --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。页面加载后可以：

- 查看六张确定性生成的灰度页面、真值行框、逐行识别串和置信度。
- 编辑行框坐标与真值字符串；保存后旧快照整体失效。
- 手动运行“注入演示”“降级演示”“恢复演示”。
- 查看阶段状态、快照版本、字符模板候选、注入记录、降级链、自检和业务码。
- 将模板库整体升级到 `glyph-v2`，观察仅受影响页面重算识别/聚合。

其他命令：

```bash
python3 -m ocrlab demo injection
python3 -m ocrlab demo downgrade
python3 -m ocrlab demo recovery
python3 -m ocrlab upgrade --version glyph-v2
python3 -m unittest discover -v
python3 -m ocrlab verify --output artifacts/verification.json
```

## 目录

```text
ocrlab/
  config.py         版本化识别参数与校验和
  templates.py      内置 5×7 笔画掩码、高级/低分辨率模板、glyph-v1/v2
  samples.py        固定种子页面生成、PNG 渲染、JSONL 清单与编辑保存
  injection.py      参数注入规格、记录、降级记录
  recognition.py    行区域特征、字符切分、候选匹配、参考对照
  stages.py         预处理 / 检测 / 识别 / 聚合及阶段内降级策略
  snapshots.py      原子快照、校验和、版本单调性和非阻塞提交锁
  pipeline.py       编排、恢复、报告、自检、模板选择性升级
  service.py        UI/CLI 共用服务、持久化业务错误和报告
  server.py         标准库 HTTP 服务
  cli.py            命令行入口
  verify.py         验收验证与机器可读结果
static/             无外部依赖的本地交互界面
tests/             20 个自动化测试
data/               六页固定样本与 PNG
artifacts/          verification.json 等实测产物
```

## 页面样本

`data/pages.jsonl` 每行是一页，核心字段如下：

```json
{
  "page_id": "page-0001",
  "image_seed": 20261123,
  "image_shape": [288, 360],
  "line_count": 4,
  "charset_version": "latin-v1",
  "policy": "standard",
  "lines": [
    {
      "line_no": 0,
      "text": "EPG769O7A98H8B6HGWRELGVMP0ZYZADKR2",
      "truth_box": [24, 20, 313, 34]
    }
  ]
}
```

- 画布为固定尺寸灰度图。背景和墨迹强度由页面种子经固定 LCG/整数公式生成。
- 字形由内置 5×7 笔画掩码确定性栅格化，不依赖系统字体或外部图像。
- `truth_box` 使用 `[x0, y0, x1, y1]`，右/下边界开区间。
- 编辑页面时会按新行框和真值字符串重新绘制，并原子替换清单与 PNG。

## 四阶段流水

1. **像素预处理与二值化**：校验像素范围；输出裁剪后的安全灰度、打包二值图、墨迹计数和内容摘要。越界注入会记录 `E_PIXEL_OUT_OF_RANGE`，以夹取后的图像执行显式降级。
2. **行检测与框排序**：按行投影找连通文本带，按列投影求行框。排序主键为 `(-score, y0, x0)`，即分数相同时严格按 `y` 升序、再按 `x` 升序。检测失败回退真值框布局。
3. **行区域特征与模板匹配识别**：每行只计算一次 `mean/std` 归一化统计量。字符按列切分，深色归一化特征缩放到模板尺寸，与字符模板做余弦式匹配。分块路径和参考路径共用行框、字符切分框和归一化统计量。
4. **页面指标聚合**：汇总行数、字符数、平均置信度、低置信行、候选比较次数、坐标偏差和状态。门限非正时回退默认聚合门限并标记降级。

最终报告按规范 JSON（键排序、紧凑分隔、UTF-8）编码。相同 `page_id`、种子、模板版本、配置和注入序列会生成逐字节相同的报告与注入日志。

### 确定性约束

- 不读取系统时间，不将时间戳写入任何流水产物。
- 字典输出使用排序键；集合在进入产物前排序。
- 样本和版面使用声明过的固定种子与确定性整数 PRNG。
- 浮点分数统一按固定精度输出；阶段签名和内容摘要使用 SHA-256。
- 行检测排序的同分裁决固定为 `y` 后 `x`。

## 分块与高精度参考

主识别路径按 `recognition_chunk_lines` 分批，批次内使用 `float32` 特征。参考路径一次处理整页，使用 `float64` 特征与精确模板匹配。两条路径：

- 共用同一组检测行框。
- 共用同一字符切分框。
- 共用同一行级 `mean/std` 归一化统计量。
- 不会在批次边界增加平滑、补边或截断。

报告中的 `comparison` 包含：

- `character_differences`：逐行、逐字符的差异原因、双方字符、分数差和坐标差。
- `line_differences`：行级差异。
- `max_coordinate_deviation` 和 `max_score_delta`。
- `first_difference`：首个差异的行号与字符位置。

超出配置容差时产生 `E_TOLERANCE_EXCEEDED`，记录首个差异位置，并显式降级到高精度参考结果，标记 `high_precision_reference`。

## 参数注入

注入完全由请求参数或 JSON 文件触发，不修改源码。注入文件格式：

```json
[
  {
    "stage": "preprocess",
    "code": "E_PIXEL_OUT_OF_RANGE",
    "page_id": "page-0001",
    "line_no": null,
    "params": {"value": 300, "x": 5, "y": 6}
  },
  {
    "stage": "detect",
    "code": "E_INVALID_LINE_BOX",
    "page_id": "page-0002",
    "line_no": 1,
    "params": {"box": [-8, -4, 99999, 12]}
  },
  {
    "stage": "recognize",
    "code": "E_TEMPLATE_SHAPE_MISMATCH",
    "page_id": "page-0003",
    "line_no": 2,
    "params": {"shape": [3, 3]}
  },
  {
    "stage": "aggregate",
    "code": "E_INVALID_THRESHOLD",
    "page_id": "page-0004",
    "params": {"threshold": 0}
  }
]
```

运行：

```bash
python3 -m ocrlab run --injections injection-plan.json
```

每条注入日志都包含稳定的 `injection_id`、`sequence`、`stage`、`code`、`page_id`、`line_no`、参数和应用详情。同一序列重跑会得到逐字节相同的 `injection_log.jsonl`。

### 降级策略

| 触发阶段 | 失败 | 降级类别 | 触发码 |
|---|---|---|---|
| 预处理 | 越界像素 | `clamped_binarization` + 下游 `truth_box_layout` | `E_PIXEL_OUT_OF_RANGE` |
| 行检测 | 非法行框或检测为空 | `truth_box_layout` | `E_INVALID_LINE_BOX` / `E_NO_LINES_DETECTED` |
| 字符切分 | 无可切分字符 | `truth_text_after_segmentation` | `E_CHAR_SEGMENTATION_FAILED` |
| 模板匹配 | 模板维度不符 / 未知字符集 | `low_resolution_templates` | `E_TEMPLATE_SHAPE_MISMATCH` / `E_UNKNOWN_CHARSET_VERSION` |
| 资源限制 | 比较次数或特征内存超限 | `streaming_line_recognition` 或 `truth_text_resource_fallback` | `E_RESOURCE_LIMIT` |
| 参考对照 | 超容差 | `high_precision_reference` | `E_TOLERANCE_EXCEEDED` |
| 指标聚合 | 门限非正 | `default_aggregation_threshold` | `E_INVALID_THRESHOLD` |
| 未预期异常 | 非声明异常 | `unattributed_failure` | `E_UNATTRIBUTED` |

降级记录总带有 `category` 和触发它的上游 `trigger_code`。未归因异常显式记录为 `attributed: false`，自检会以 `E_INCONSISTENT` 失败并指出阶段与页面，不会用未标记的空结果掩盖失败。

## 快照与恢复

每个阶段完成全部页面后才提交一个 JSON 快照：

```json
{
  "stage": 3,
  "stage_name": "recognize",
  "version": 7,
  "signature": "...",
  "config_version": "rec-v1",
  "config_checksum": "...",
  "template_version": "glyph-v1",
  "template_checksum": "...",
  "dataset_checksum": "...",
  "completed_pages": ["page-0001", "..."],
  "content_checksum": "...",
  "content": {"pages": {}}
}
```

- 写入采用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。
- 提交守卫使用线程锁加 `flock(LOCK_EX | LOCK_NB)`；冲突立即返回 `E_STAGE_BUSY`。
- 快照版本全局单调递增，旧版本提交返回 `E_SNAPSHOT_ROLLBACK`。
- 读取时重算内容摘要；不一致返回 `E_SNAPSHOT_CORRUPT`，详情含阶段名和版本号。
- 只有完整页面集合能提交；阶段签名变化会整体失效该阶段及下游。
- 恢复从最后一个已提交阶段继续：已提交页面直接复用，未提交的中间行框/匹配表不落地，从该阶段起点重建。

`demo recovery` 首先记录不间断基线，然后重置并在第 2 阶段提交后模拟终止。恢复从阶段 3 开始，最终报告与基线逐字节一致。

## 版本替换与模板升级

- `PipelineConfig` 携带 `version` 和 `config_checksum`。
- `TemplateLibrary` 携带 `version` 和 `template_checksum`。
- 阶段快照携带上述身份及 `dataset_checksum`。
- 配置版本或摘要变化会使相关阶段签名变化，旧快照被整体替换，不做增量修补。
- `glyph-v2` 改变字符 `7` 和 `Q` 的笔画模板。升级时先比较两个模板库，计算受影响的页面集合，仅重新执行这些页面的识别与聚合；不含变化字形的页面复用旧阶段页结果。
- 模板版本回退返回 `E_SNAPSHOT_ROLLBACK`，旧版本不能覆盖新快照。

## 资源上限与输入校验

关键配置：

```text
max_lines_per_page
max_candidate_comparisons
max_feature_memory_bytes
resource_policy = error | stream
recognition_chunk_lines
comparison_score_tolerance
comparison_coordinate_tolerance
aggregate_confidence_threshold
```

输入校验行为：

| 情况 | 业务码 | 字段 |
|---|---|---|
| 重复页面 | `E_DUPLICATE_PAGE_ID` | `page_id` |
| 画布尺寸不符 | `E_IMAGE_SHAPE_MISMATCH` | `image_shape` |
| 未知字符集 | `E_UNKNOWN_CHARSET_VERSION` | `charset_version` |
| 行框越界/退化 | `E_INVALID_LINE_BOX` | `truth_box` / `boxes` |
| 行数超限 | `E_RESOURCE_LIMIT` | `line_count` |
| 比较/内存超限 | `E_RESOURCE_LIMIT` 或流式降级 | `resource_limits` |

## HTTP API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/state` | 页面、报告、快照、注入、降级、自检、恢复、最近错误 |
| GET | `/api/report` | 最近完整报告 |
| GET | `/api/injection-log` | 最近注入日志 |
| GET | `/api/images/{page_id}.png` | 本地页面图像 |
| POST | `/api/run` | 运行四阶段，可带 `injections` 和 `reset` |
| POST | `/api/demo/injection` | 四阶段注入演示 |
| POST | `/api/demo/downgrade` | 行检测失败回退真值框演示 |
| POST | `/api/demo/recovery` | 强制终止与恢复对照演示 |
| POST | `/api/reset` | 失效快照并重跑基线 |
| POST | `/api/template-upgrade` | 整体升级模板库 |
| PUT | `/api/pages/{page_id}` | 编辑行框与真值字符串 |
| POST | `/api/config` | 整体替换版本化识别配置 |

## 一致性自检

每次成功运行都会检查：

- 四个阶段的 `page_id` 集合完全一致。
- 识别串长度等于字符明细数量。
- 每个字符都有可核对的候选字符、分数、模板级别和模板尺寸。
- 所有异常已归因；不存在未归因异常。
- 模板升级时，重算页面集合与复用页面集合无交集，且并集覆盖受影响集合。

任一项失败返回 `E_INCONSISTENT`，详情指出阶段和页面。UI 的 Recovery 与 Self-check 面板展示实测结果。

## 实际验证结果

测试命令：

```text
python3 -m unittest discover -v
Ran 20 tests in 19.610s
OK
```

覆盖：报告与阶段快照逐字节复现、四阶段页面集合、行框同分排序、分块/float64 参考、四阶段注入及日志复现、未知字符集低分辨率回退、降级链、跨进程提交冲突、快照回滚/损坏/版本、不完整阶段拒绝、恢复逐字节一致、行数/比较/内存上限、输入校验、选择性模板升级、未归因异常、配置版本替换、编辑失效以及 HTTP/API 冒烟。

验收验证：

```text
python3 -m ocrlab verify --output artifacts/verification.json
ok = true
page_count = 6
line_count = 30
report_bytes = 111842
report_checksum = 60ec5dcba8e1e41993e250e9d26c9e027e68b1da4849226a390602f38511886b
max_coordinate_deviation = 0.0
max_score_delta = 1.32e-07
injection_log_checksum = 43218025177a6b4e52de4b9e66d73660ebd19d194ba7363803a6c4e658920913
recovery.byte_identical = true
recovery.resumed_from_stage = 3
template_upgrade.affected_pages = page-0001 ... page-0005
template_upgrade.reused_pages = ["page-0006"]
```

机器可读明细见 `artifacts/verification.json`，技术验证摘要见 `VERIFICATION.md`。

## 说明

这是面向本地领域数据与流程验证的 OCR 仿真应用，不包含真实字体渲染或通用自然场景 OCR 模型。其重点是业务流水契约：确定性、版本化、异常注入、显式归因降级、原子快照和可恢复性。所有页面、模板和异常样本均由本地脚本生成，不访问外部目标。

