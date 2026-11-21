当前是空白工作区，没有现存项目、代码或数据附件。需要从零构建一个面向视觉目标检测候选匹配后处理的本地诊断工作台，并把结果以可互操作的配置文件与图像标注导出形式对外呈现。业务对象是本地确定性生成的样本：每张图由固定种子生成固定尺寸 RGB 像素数组，候选框集合（含 x1、y1、x2、y2、score、class_id）与真值框集合（含 label、difficulty、遮挡标记）由同一随机源派生，样本清单为 JSONL。系统须把候选匹配建模为有向无环计算图：样本摄入与校验→按类别分组→IoU 矩阵与贪心指派→TP/FP/FN 统计→按类别与难度聚合。匹配结果还需要以三套并存的书写形式导出并互相读取：v1 为无字段名的竖线分列行式文本，v2 为带字段名的行式文本，v3 为结构化 JSON；同时绑定一份类别词表版本（类别编号到类别名的映射，含已停用类别）。核心业务问题是三套书写形式在类别词表版本演进下的互操作：任一格式读取后必须还原出完全相同的匹配结果，v1 表达能力不足时须显式裁决而非静默丢弃，且诊断工作台需能复现任一历史裁决。

任务环境与准备：
确认工作目录为空并可直接创建文件，使用 Windows 本地文件系统。
安装 Python 3.11 与 NumPy 1.26（本地 wheel 安装即可），无需网络与 GPU。
准备本地确定性样本生成脚本：按固定种子生成 RGB 像素数组、候选框与真值框，并输出样本清单 JSONL 与三套格式的匹配结果书写样例。
准备类别词表样例两个版本（含已停用类别），以及一份用于注入格式损坏、停用类别与资源超限的输入清单。
准备运行命令与演示入口的调用说明，确保全部计算只依赖本地样本与固定种子。

业务要求：
1. 实现确定性候选匹配计算图：按 class_id 分组，用可配置 IoU 阈值做贪心指派（score 降序、同分按坐标字典序打破平局），输出每个候选匹配的真值框索引或未匹配标记；同一输入、同一类别词表版本与同一配置下必须逐字节产出相同指派、TP/FP/FN 与聚合统计，禁止依赖字典遍历顺序或系统时间。
2. 实现 v1、v2、v3 三种匹配结果书写形式的读取器，按格式头或字段集分派解析路径；未知格式版本返回 E_FORMAT_VERSION_UNSUPPORTED，格式头截断、竖线字段数不符、必填字段缺失或数值不可解析返回 E_FORMAT_CORRUPT 并指出首个出错行号与字段名。
3. 实现三套书写形式的双向迁移器：v1 升级到 v2/v3 时补全类别名、难度标记与置信度区间默认值并逐项记录补全来源；v3 降级到 v1 时对 v1 无法表达的遮挡标记与置信度区间按显式声明的丢弃规则处理并输出丢弃明细；迁移不得改变已能表达字段的取值。
4. 实现跨格式裁决：同一匹配结果以 v1、v2、v3 三种形式书写时，读取与迁移后必须产出相同候选指派、相同 TP/FP/FN 集合与相同聚合数值；当 v1 表达能力不足导致语义不可完全还原时返回 E_SEMANTIC_DRIFT 并输出不可还原字段清单，禁止以静默截断冒充等价。
5. 实现类别词表版本演进处理：类别词表携带单调递增版本号与内容校验和以及已停用类别集合；加载时检测版本号或校验和变化必须整体替换旧快照而非增量修补，旧版本不得覆盖新版本且回退尝试返回 E_SNAPSHOT_ROLLBACK；当已有指派命中已停用类别时返回 E_LABEL_RETIRED 或以显式回退类别替代并在结果中标记回退来源。
6. 实现匹配侧缓存与增量失效联动：缓存键须纳入图像内容哈希、候选与真值框摘要、IoU 阈值、类别词表版本与上游节点哈希；键相同必须复用快照，键不同必须重算；修改阈值或词表版本时计算受影响图像集合，复用未受影响的 IoU 与指派结果，并输出重算与复用集合，两个集合须无交集且并集覆盖全部受影响图像。
7. 实现快照版本化与并发控制：匹配结果、统计量与缓存条目各携带单调递增版本号与内容校验和，检测到版本号或校验和变化时整体替换旧快照，损坏或校验和不符返回 E_CACHE_CORRUPT；同一时刻只允许一个提交生效，并发冲突时后到者立即获得 E_STAGE_BUSY 而不阻塞等待；并为图像数、IoU 比较次数与驻留候选内存设可配置上限，超限返回 E_RESOURCE_LIMIT 或降级并标记降级类别。
8. 提供本地诊断工作台并实现跨模块一致性自检：展示计算图拓扑、各节点命中或重算状态、失效传播覆盖的图像、逐图匹配明细、按类别与难度聚合统计、当前格式与词表版本视图及最近业务错误码，支持切换 v1、v2、v3 视图并触发一次含跨格式裁决与一次含降级模式演示；自检校验任一候选不得同时计入 TP 与 FP、任一真值框不得同时计入 TP 与 FN、顶点无环、缓存键与产物哈希互相可核对，不一致返回 E_INCONSISTENT 并指出类别。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
用本地样本验证三格式读取迁移、候选匹配统计一致及注入异常归因，附运行报告。

---

# 交付说明（detwb 工作台）

## 环境

- Python 3.10+（开发机为 3.10.12；代码兼容 3.11）与 NumPy 1.26.x，无网络/GPU/容器依赖。
- 仅标准库 + NumPy；测试使用标准库 `unittest`。

## 目录结构

- `detwb/`：核心包
  - `samples.py` 确定性样本生成（固定种子 → RGB 数组/候选框/真值框 → JSONL 清单）
  - `vocab.py` 类别词表快照（版本号、校验和、停用类别、回退拒绝）
  - `matching.py` IoU 矩阵、贪心指派、TP/FP/FN、按类别与难度聚合
  - `dag.py` 计算图（ingest→group→iou→assign→stats→aggregate）与节点级缓存流水线
  - `formats.py` v1/v2/v3 读写器（格式头分派、损坏归因）
  - `migrate.py` 双向迁移器（补全来源记录 / 丢弃明细）
  - `adjudicate.py` 跨格式裁决、报告持久化与历史裁决复现
  - `cache.py` 缓存键与增量失效联动（重算/复用集合）
  - `snapshots.py` 快照版本化、非阻塞提交互斥、资源上限与降级
  - `selfcheck.py` 跨模块一致性自检
  - `workbench.py` 诊断工作台 CLI
- `scripts/gen_all.py`：样本/词表/注入清单生成入口
- `tests/`：39 个单元测试（`python3 -m unittest discover -s tests`）
- `reports/run_report.md`：实际验证结果
- `data/`：生成的样本、词表、注入用例与运行产物

## 运行命令

```bash
python3 scripts/gen_all.py data                 # 生成确定性样本、词表两版本、注入清单
python3 -m detwb.workbench --data data run      # 运行计算图并导出 v1/v2/v3
python3 -m detwb.workbench --data data run --threshold 0.7        # 改阈值 → 增量失效
python3 -m detwb.workbench --data data run --vocab 2 --fallback-class 0  # 词表演进+显式回退
python3 -m detwb.workbench --data data show topology|nodes|invalidation|stats|errors
python3 -m detwb.workbench --data data show matches --sample s0001
python3 -m detwb.workbench --data data view --format v1|v2|v3 --lines 5
python3 -m detwb.workbench --data data adjudicate          # 跨格式裁决（持久化报告）
python3 -m detwb.workbench --data data replay <裁决ID>      # 复现历史裁决
python3 -m detwb.workbench --data data inject              # 注入异常归因演示
python3 -m detwb.workbench --data data degrade-demo        # 资源超限与降级演示
python3 -m detwb.workbench --data data vocab-demo          # 词表演进/回退/停用演示
python3 -m detwb.workbench --data data selfcheck           # 一致性自检
python3 -m unittest discover -s tests                      # 全部测试
```

## 错误码

`E_FORMAT_VERSION_UNSUPPORTED` / `E_FORMAT_CORRUPT`（含首个出错行号与字段名）/ `E_SEMANTIC_DRIFT`（含不可还原字段清单）/ `E_SNAPSHOT_ROLLBACK` / `E_LABEL_RETIRED` / `E_CACHE_CORRUPT` / `E_STAGE_BUSY` / `E_RESOURCE_LIMIT`（含超限类别）/ `E_INCONSISTENT`（含不一致类别）。
