# OCR 本地可视化诊断工作台 — 技术说明

## 1. 环境与无网络保证

- 运行解释器：Python 3.11.18（标准库 `tkinter` 9.0 可用）。
- 唯一第三方依赖：`numpy>=1.26`（验证环境实际安装 2.4.6），安装在项目本地
  虚拟环境 `.venv` 中，不污染系统解释器。
- 所有页面像素、字形笔画、随机数均由固定种子在代码内确定性生成；不读取
  任何外部图像、字体或 OCR 模型，不发起任何网络请求。

复现环境：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # 或 uv pip install
```

## 2. 模块结构

| 模块 | 职责 |
| --- | --- |
| `errors.py` | 业务错误码与带 `page_id`/`stage`/`details` 的结构化异常 |
| `deterministic.py` | 规范化 JSON、SHA-256 内容哈希、确定性 RNG、定点量化 |
| `config.py` | 检测阈值、去重阈值、资源上限、图节点与有向边、配置哈希 |
| `templates.py` | 确定性笔画字形库、单调版本号、整库内容校验和 |
| `samplegen.py` | 固定种子页面渲染、真值框/真值串、JSONL 清单读写 |
| `graph.py` | DAG 拓扑无环校验（Kahn）、受影响后缀传播 |
| `pipeline.py` | 六个节点的纯函数计算（无 IO、无时间/字典序依赖） |
| `cache.py` | 内容寻址缓存、复合键校验、原子提交、非阻塞并发锁、容量上限 |
| `snapshot.py` | 模板快照整体替换、回退拦截、损坏/校验和检测 |
| `resource.py` | 行数/候选数/条目/驻留内存上限与流式降级类别 |
| `engine.py` | 缓存编排、键明细、命中/重算跟踪、分块与整页一致性 |
| `incremental.py` | 四类变更的失效传播与复用/重算节点清单 |
| `consistency.py` | 拓扑、键↔哈希互核、集合互斥覆盖、指标独立重算 |
| `workbench.py` | 门面：数据集、缓存、快照、增量场景、错误日志 |
| `demo.py` | 可重复端到端演示并落盘 `demo_report.json` |
| `ui.py` | Tkinter 工作台；无显示环境时回退为无头文本摘要 |

## 3. 确定性保证（要求 1）

- 规范化序列化使用 `json.dumps(sort_keys=True, separators=(",",":"))`，
  消除字典遍历顺序影响；所有哈希基于该字节串。
- 随机数统一由 `np.random.default_rng` 配合由稳定种子折叠出的 64 位种子
  生成，不使用系统时间、PID 或 OS 熵。
- 浮点特征经 `quantize` 定点量化（默认千分位）后进入缓存与识别，跨平台
  BLAS 差异不影响逐字节结果。
- 节点为纯函数：相同 `page_id`、`image_seed` 与配置下，行框、逐行识别串、
  页面指标逐字节一致（见 `test_node_outputs_byte_identical_across_runs`）。

## 4. 内容寻址缓存与复合键（要求 2）

每个条目落盘为 `cache/entries/<key前2位>/<key>.json`，包含：

- `key_details`：复合键组成明细（可导出诊断）；
- `key`：对 `{node, details}` 规范化哈希；
- `content_hash`：对 `payload` 规范化哈希；
- `payload`：节点产物。

键必含的语义输入按节点强制校验：

- 全局字段：`node`、`page_id`、`pixel_hash`、`config_hash`、`upstream_hash`；
- 检测侧节点额外含 `detect_threshold`、`line_box_summary`；
- 模板侧节点额外含 `charset_version`；识别含 `dedup_threshold`；
  聚合含 `truth_hash`。

任一字段缺失或为空立即返回 `E_CACHE_KEY_INVALID` 并在 `missing_fields`
中列出全部缺项。读取时同时重算键与产物哈希，不一致返回
`E_CACHE_CORRUPT`。

## 5. 增量失效传播（要求 3）

`IncrementalPlanner` 根据变更集计算受影响页面、行与节点后缀：

- 追加页面：仅新页面的全部节点重算，旧页面全部复用；
- 修正单行真值：仅该页面 `aggregate` 重算，识别串保持逐字节不变；
- 调整检测阈值：`preprocess`/`detect` 起的全后缀在所有页面重算；
- 模板库升级：仅 `match`/`recognize`/`aggregate` 重算，像素侧三节点复用。

每次运行输出 `recomputed_nodes` 与 `reused_nodes`，二者无交集且并集覆盖
全部图节点（由 `consistency.check_reuse_recompute_partition` 强制）。

关键设计：模板版本不进入像素侧 `config_hash`（见
`PipelineConfig.pipeline_hash`），仅作为模板侧节点的专用键字段，因此版本
升级不会错误地令预处理/检测/特征缓存失效。

## 6. 模板版本与整体替换（要求 4）

- `TemplateLibrary` 不可变，携带单调 `version` 与覆盖全部字形字节的
  `checksum`；版本升级通过整体重建生成新快照，绝不就地修补单字形。
- `TemplateSnapshotStore.replace` 原子整体替换；版本相同且校验和相同则复用
  不重写；低版本覆盖高版本返回 `E_SNAPSHOT_ROLLBACK`。
- 读取时校验结构与校验和，损坏返回 `E_CACHE_CORRUPT` 并在 `version` 字段
指明快照版本号。

## 7. 分块与整页逐字节一致性（要求 5）

- 特征归一化统计量（全局均值/标准差）对整页所有单元格一次性计算，分块
  复用同一统计量，块边界不改变任何归一化值。
- 行间去重阈值与置信度量化口径在整页与分块路径中调用同一函数
  （`_apply_dedup`、`quantize`），分块拼接后再做一次全页去重。
- 拼接结果与整页结果逐行逐字符比对，首个差异返回 `E_CHUNK_MISMATCH`，
  携带 `first_bad_line` 与 `first_bad_char`。

## 8. 资源与并发写控制（要求 6）

- 可配置上限：单页行数、候选比较次数、缓存条目总量、驻留特征内存。
- 硬上限超限返回 `E_RESOURCE_LIMIT`（含 `limit`/`observed`）；超过软驻留
  阈值时降级为逐行流式识别并标记 `streaming_line_recognition`。
- 同一缓存键的并发写由按键的锁保护；非阻塞模式下后到者立即得到
  `E_STAGE_BUSY` 而不等待，已提交条目不受破坏（原子 `os.replace`）。

## 9. 自检与异常归因（要求 7）

- 拓扑无环、复合键可由明细重建、产物哈希可重算、复用/重算集合互斥且全覆盖、
  页面指标可由逐行明细独立重算一致；任一破坏返回 `E_INCONSISTENT` 并带
  `category` 与定位字段。
- 解析失败（`E_PARSE_FAILED`）、检测空结果（`E_DETECT_EMPTY`）、模板匹配
  失败（`E_MATCH_FAILED`）均携带具体 `page_id` 与 `stage`。

## 10. 工作台与演示（要求 8）

- Tkinter 窗口展示图拓扑、节点命中/重算状态、失效覆盖页面与行、字符/行
  准确率、模板版本、最近业务错误码；点击节点查看复合键组成与内容哈希。
- 无显示环境（如 CI）自动回退为无头文本摘要，行为与数据完全一致。
- 演示脚本一键完成：冷启动 → 全量缓存命中 → 追加/真值修正/阈值变更 →
  模板整体升级 → 多尺寸分块对照 → 全错误码与自检，并写出
  `demo_report.json`。

## 11. 错误码对照

| 错误码 | 触发场景 |
| --- | --- |
| `E_CACHE_KEY_INVALID` | 复合键缺项，附 `missing_fields` |
| `E_CACHE_CORRUPT` | 条目/快照损坏或校验和不符，附版本号 |
| `E_SNAPSHOT_ROLLBACK` | 旧模板快照试图覆盖新版本 |
| `E_CHUNK_MISMATCH` | 分块与整页结果差异，附首个行/字符位置 |
| `E_RESOURCE_LIMIT` | 行数/候选/容量/内存超限 |
| `E_STAGE_BUSY` | 同键并发非阻塞提交冲突 |
| `E_INCONSISTENT` | 跨模块一致性自检失败，附类别与定位 |
| `E_PARSE_FAILED` | 清单解析/字段缺失，附行号与缺项 |
| `E_DETECT_EMPTY` | 检测不到任何文本行，附 page_id |
| `E_MATCH_FAILED` | 单元格匹配失败，附 page_id/line_index |
| `E_GRAPH_CYCLE` / `E_GRAPH_CONFIG` | 图成环或边引用未知节点 |
