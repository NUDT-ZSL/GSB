# 本地确定性 OCR 检测与识别交互工作台

面向**本地程序生成的确定性合成版面图**的文字检测、字符切分与模板匹配识别工作台。
仅依赖 `Pillow` 与 `NumPy`，不使用任何 OCR/深度学习框架、外部模型或网络服务。
所有计算均为确定性纯函数（无随机源参与检测识别），支持参数热调整、基于内容指纹的
分阶段增量失效、交互延迟预算响应、原子快照与崩溃恢复，以及增量与全量结果的逐字段一致性。

## 1. 环境与安装

要求 Python 3.10+（已在 Python 3.10 / 3.11 验证）。

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

> 本机环境系统 Python 未提供 `ensurepip/pip`，因此使用 `--without-pip` 建虚拟环境后，
> 以 `get-pip.py` 引导安装 pip，再安装 `Pillow`、`NumPy`。运行期第三方依赖只有这两个；
> `pytest` 仅为开发测试依赖，不进入工作台运行时。

固定依赖版本见 `requirements.txt`：

```
numpy==2.2.6
pillow==12.3.0
```

## 2. 目录结构

```
ocr_workbench/
  errors.py       业务错误码（E_IMAGE_DECODE / E_IMAGE_SHAPE / E_TRUTH_OUT_OF_RANGE 等）
  font.py         内置 5x7 点阵字模（43 字符），生成与识别共用同一字模
  config.py       检测/识别配置加载、校验与参数指纹
  samplegen.py    固定种子的确定性版面图 + 真值 JSON 生成器
  loader.py       PNG 容器头/尺寸/模式校验、灰度解码、真值校验
  detector.py     滑窗特征、连通域、投影聚合成文字行（确定性）
  recognizer.py   水平投影字符切分 + 模板匹配识别
  pipeline.py     四阶段缓存流水线与精确失效判定
  snapshot.py     临时文件 + 原子替换的阶段快照与恢复
  engine.py       后台线程池、预算响应、版本化增量重算、可处理退出
  report.py       JSON 诊断报告原子写盘
  runner.py       CLI：generate / full / incremental / verify
configs/
  detection.json     检测配置
  recognition.json   识别配置
  commands.json      交互命令流（仅参数注入，不改源码）
samples/             生成的 PNG 版面图与配套真值 JSON
tests/               36 个 pytest 用例
scripts/             样本生成与强制终止恢复演示
reports/             诊断报告、指标、崩溃恢复结果
workdir/             快照与进度持久化目录
```

## 3. 数据与配置

### 3.1 确定性样本

`scripts/generate_samples.py` 以固定种子（默认 `20261117`）在 480x320 浅色背景上绘制
4 条横排文字条带与 2 个独立字符块，加入确定性盐椒噪声，并为每张图写出：

- `layout_xxx.png`：8 位灰度（mode `L`）PNG；
- `layout_xxx.json`：版面真值，含每个文字行/块与每个字符的整数像素边界框、字符与空格标记。

重新生成：

```bash
python scripts/generate_samples.py --count 6
```

### 3.2 检测配置 `configs/detection.json`

滑窗尺度与步长、二值化阈值、密度/边缘阈值、行合并投影间距、连通条件（4/8）、
最小字符高度、候选框得分阈值、线程池并发数、交互延迟预算（毫秒）、快照提交间隔。

### 3.3 识别配置 `configs/recognition.json`

模板集合与字模版本、匹配度量（`coverage` / `xor` / `mse`）、置信度阈值、
最小字符宽度、固定字距、空格间隙、窄片段合并开关、同分决断顺序。

### 3.4 交互命令流 `configs/commands.json`

命令只通过参数注入触发重算，不修改任何源码。支持：

- `{"op":"set_param","stage":"detection|recognition","param":...,"value":...}`
- `{"op":"snapshot"}`：请求当前版本摘要/落盘
- `{"op":"exit"}`：可处理退出（完成当前阶段快照后以退出码 0 退出）

## 4. 流水线与业务要求映射

### 4.1 加载层（要求 1）

`loader.py` 扫描目录内全部 `*.png`（按文件名排序），先校验 8 字节 PNG 容器签名，
再用 Pillow 解码并校验像素模式为 `L`、尺寸与配置一致，输出 uint8 灰度数组。
真值 JSON 校验所有行/字符框为整数、位于图像范围内，并校验同行字符框纵向重叠。
失败分别产出业务码并记入失败清单，不影响其余图像：

- 容器头错误/无法解码 → `E_IMAGE_DECODE`
- 模式或尺寸不符 → `E_IMAGE_SHAPE`
- 真值越界/同行不重叠 → `E_TRUTH_OUT_OF_RANGE`

### 4.2 确定性检测（要求 2）

`detector.py`：二值化得到笔画掩码 → 多尺度滑窗计算笔画密度与边缘强度，
与阈值比较得到覆盖证据 → 连通域标注（4/8 连通，union-find）得到原子字块 →
按垂直投影条带与水平间隙聚合为文字行 → 行/列投影裁切去噪，输出整数、边界裁剪的
行框、内部分块计数与归一化得分。链路无随机源，同图同配置任意运行逐字段一致。

### 4.3 字符切分与模板识别（要求 3）

`recognizer.py`：利用内置等宽字模的固定字距做水平投影网格切分，笔画段锚定到字距网格、
空槽还原为空格单元；过窄片段就地扩宽到最小字宽而**不是丢弃**。每个字符块与全部内置
模板按配置度量打分，取最高分；同分按配置决断顺序（`charset`/`reverse`）固定裁决，
输出整数框、识别字符、得分、次优模板得分，低于置信度阈值标记 `?`。

### 4.4 内容指纹与精确失效（要求 4）

每个阶段缓存键由图像内容指纹、该阶段真正读取的参数指纹与字模版本号派生（`pipeline.py`）：

| 编辑参数 | 失效阶段 | 直接复用阶段 |
| --- | --- | --- |
| 二值化/滑窗/行合并等检测参数 | 检测→聚合→切分→识别 | 无 |
| 匹配度量、置信度阈值 | 识别 | 检测、聚合、切分 |
| 最小字宽/字距/空格间隙等切分参数 | 切分、识别 | 检测、聚合 |

`StageCache` 为多版本缓存（按 `(图像, 阶段, 缓存键)` 保存），参数改回旧值时可直接复用
历史产物。每次运行的复用（命中）与重算（失效）清单写入诊断报告 `cache.hits` /
`cache.invalidations`。

### 4.5 交互延迟预算语义（要求 5）

`engine.py` 后台线程池执行，命令到达后在 `interact_budget_ms` 内立即返回最近一次
**已完成**版本摘要（已完成行数、逐行文本、受影响行清单、进度、响应耗时与是否超预算）。
最新参数尚未算完时返回 `E_INTERACT_BUDGET_PENDING`，绝不阻塞后续命令；预算耗尽不被描述为
线程终止/超时。`exit` 走状态持久化路径：完成当前阶段快照与进度后退出码 0。

### 4.6 增量与全量一致性（要求 6）

`runner.py verify` 回放任意编辑序列得到最终增量结果，再以**相同最终图像与最终配置**
从零做一次全量执行，作为真值来源，按同一全局版本号对齐，逐字段比对逐行文本、
逐字符边界框与得分、行/字符顺序与缓存键。比对函数对识别产物（不含执行路径相关的
复用计数）做严格相等校验。

### 4.7 原子快照与恢复（要求 7）

`snapshot.py` 每个快照先写同目录临时文件并 `fsync`，再 `os.replace` 原子整体替换
（POSIX 与 Windows 均为原子语义）。快照含阶段名、配置哈希、字模版本、已完成图像列表、
产物摘要与全局版本号；版本变化是整体替换而非逐文件覆盖。强制终止（SIGKILL）后重启
只依据已完整替换的快照恢复并报告恢复点版本号，不承诺终止中的行结果或退出报告
（见 `scripts/crash_recovery_demo.py`）。

### 4.8 并发一致性与诊断报告（要求 8）

线程池最大并发数取自配置；每个任务带由文件名、行序号、阶段名派生的稳定排序键
`stable_task_key`，汇总前按文件名顺序重排，因此图像/行/字符/复用/失败清单顺序与
单线程按文件名执行完全一致，与调度顺序、并发度无关。每次完整运行以原子替换写
JSON 诊断报告，含逐行文本、逐字符框与得分、缓存命中/失效清单、各命令响应耗时与
超预算标记、快照版本号、业务码与失败清单。

## 5. 使用方式

```bash
# 1) 生成确定性样本
python -m ocr_workbench.runner generate --samples samples --count 6

# 2) 从零全量执行 + 诊断报告
python -m ocr_workbench.runner full --report reports/full.json

# 3) 回放交互命令流的增量执行 + 诊断报告
python -m ocr_workbench.runner incremental --report reports/incremental.json

# 4) 增量 vs 全量逐字段一致性验证（报告内 verification.equal=true）
python -m ocr_workbench.runner verify --report reports/diagnostic.json

# 5) 强制终止后重启恢复演示
python scripts/crash_recovery_demo.py
```

## 6. 测试

```bash
python -m pytest tests/ -q
```

测试覆盖：加载三类业务码与同伴容错、检测行数/IoU/整数裁剪/确定性、识别切分不丢片段与
次优分、四阶段精确失效与参数回退复用、预算内 pending 非阻塞、并发顺序稳定、
快照原子替换/损坏检测/重启恢复、以及增量-全量一致性。

## 7. 验证结果（本机实测）

- 依赖：`Pillow 12.3.0`、`NumPy 2.2.6`，无网络/站点/账号/模型依赖，全部数据本地生成。
- 测试：`36 passed`。
- 检测：6 张图 36/36 行，平均 IoU `0.9731`（指标见 `reports/metrics.json`）。
- 识别：非空格字符最佳模板准确率约 `0.90`，空格召回约 `0.84`；
  少量漂移来自行尾空格与边缘稀疏标点的投影裁切固有特性，不影响确定性与一致性保证。
- 一致性：`verify` 输出 `VERIFY OK`，增量与从零全量结果逐字段一致。
- 预算：预算设为 0 时所有命令即时返回 `E_INTERACT_BUDGET_PENDING`，后台完成后结果仍与全量一致。
- 恢复：SIGKILL 后重启仅依据完整快照恢复，恢复版本与已完成图像列表正确
  （见 `reports/crash_recovery.json`）。

## 8. 设计边界

- 工作台针对固定分辨率、内置点阵字模的确定性合成图，不追求自然场景 OCR 鲁棒性。
- 空格与标点切分依赖等宽字距假设；生成器与识别器共用同一字模以保证闭环。
- 为保证逐字段可复现，所有浮点得分在写出前统一 `round`，排序均使用显式稳定键。
