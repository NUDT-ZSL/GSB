# RepairGraph：确定性图像修复与精度诊断

本项目从零实现一个本地可交互的数值精度诊断应用，用于验证图像修复与风格迁移在“整幅一次处理”和“区域分块处理”两条路径下的中间特征、误差传播、缓存失效、快照并发与崩溃恢复行为。

项目不依赖真实图像、外部服务、Docker 或网络。样本、底图、掩码、风格参数和区域几何均由固定种子脚本在本地确定性生成。

## 快速启动

环境：Linux / WSL Ubuntu，Python 3.10+。

```bash
python3 -m pip install -r requirements.txt
python3 generate_sample.py --out data/sample
python3 -m image_diag serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。首页自动执行一次 float64 双路径对照，可切换 float64、float32、定点三档，并运行增量、风格修订、快照回退、并发占用和终止恢复场景。

常用命令：

```bash
# 单次双路径与误差传播
python3 -m image_diag run --precision float32 --path block

# 增量、风格修订、回退、并发的确定性演示
python3 -m image_diag demo --scenario all --fresh --workspace /tmp/repairgraph-demo

# 完整验收（退出码 0 表示全部通过）
python3 -m image_diag verify

# 自动化测试
python3 -m unittest discover -s tests -v
```

## 固定种子样本

`generate_sample.py` 生成 112×112 RGB float64 底图、三个互不重叠的封闭区域掩码、颜色迁移矩阵、风格强度、边缘保留系数、迭代步数、量化和门限配置。

- 区域：`R1` 椭圆、`R2` 超椭圆、`R3` 凸多边形。
- 掩码标签：1、2、3，背景为 0。
- 随机源：自实现的 splitmix64 内容哈希流，不使用系统时间或未声明随机源。
- `data/sample/sample.json` 同时记录底图与掩码摘要，加载时进行校验。

实测样本内容摘要（数组摘要不是 `.npy` 文件容器哈希）：

| 对象 | 内容 SHA-256 |
| --- | --- |
| 底图数组 | `6067b36b692cab37d7fe821ad2644884f02a67ce3972c6e4cef6b9362a709ce0` |
| 掩码数组 | `b426742fa3598f7d0c0430b026c57d20d5147be3c2dd1cb6acf1dfbb68bb79b2` |
| 风格参数 JSON | `f574f62b7ccfef3360cc2973edf9b84491951ed364932e13dd849e78f4716e20` |

## 五阶段计算图

每个节点都记录输入摘要、输出摘要和确定性中间特征。

1. **掩码区域解析**：验证标签集合、区域封闭性、面积、边界像素数和紧包围盒。
2. **边界上下文采样**：在固定半径内以高斯距离权重采样区域外像素，逐像素归一化；整幅和分块共用同一采样语义。
3. **区域内容确定性补全**：使用边界上下文、固定哈希纹理和多轮四点邻域更新；纹理坐标绑定全图位置，不随块偏移。
4. **风格迁移着色**：按区域计算归一化均值/标准差，应用 3×3 颜色矩阵、风格强度、边缘保留系数和稳定的门限判定。
5. **边界融合与输出**：按闭合区域距离生成羽化权重，以稳定边界残差门限控制融合增益，输出 float 特征与确定性 PNG。

同一输入、参数和精度档下，阶段摘要、最终 float64 数组和 PNG 字节均可重复。

## 双路径一致性

- 整幅路径在完整画布上批量执行。
- 分块路径按区域切片、带边界 padding 独立执行，再按区域掩码拼回全图。
- 两条路径共享控制面：区域集合、边界采样权重、风格归一化统计量、门限判定、羽化图和候选分类完全相同。
- 拼接后先比较最终数组，再比较每一阶段的输出摘要。任一差异触发 `E_BLOCK_MISMATCH`，响应包含首个偏差像素、所属区域、各通道偏差和最大偏差量级。

实测三档精度下分块与整幅均逐像素一致，最终 SHA-256：

| 精度 | 最终 SHA-256 | 最终最大绝对误差（相对 float64） |
| --- | --- | --- |
| float64 | `278eb4d7b027e64dfbf9e2eafeeddb390ffc51d2cbc3c95882114a34d1a61f37` | `0` |
| float32 | `1d716f2b56e23ef0c49391d743b106b6a4cc9acccbe53f6c9c6b650c37390b4b` | `3.6608378068885557e-06` |
| fixed, scale=4096, half-even | `a4c81162371abd9a04cf0f0e6ee867a33d5df047aa94fec0d64b5bbe2bb4ac1f` | `0.04319874363735346` |

## 精度执行层

`image_diag/precision.py` 提供统一的 `Executor` 算子面：

- `float64`：参考真值。
- `float32`：每次算子输出舍入到 IEEE-754 binary32。
- `fixed`：显式使用风格参数中的 `fixed_scale` 和 `fixed_rounding`（`half_even`、`half_up`、`trunc`），每次算术结果回到定点网格。

门限分类在独立的 float64 控制面上完成，因此切换精度不会改变区域集合、区域顺序或候选判定。严格策略使用 `>=`；容差带策略使用 `>= threshold - tolerance`，并对次正规数执行零化保护。

## 误差传播与归因

以同一输入上的 float64 整幅路径为参考，依次比较：

1. `boundary_sampling`
2. `block_completion`
3. `style_coloring`
4. `fixed_quantization`
5. `feather_fusion`

每级输出绝对均值、最大绝对误差、P95 绝对误差、相对均值/最大值、RMS、误差放大比和误差区间，并按最大绝对误差定位主导来源。最终误差超过 `error_tolerance` 时，响应携带 `E_TOLERANCE_EXCEEDED` 及完整级联明细。

## 区域内容寻址缓存

缓存布局：

```text
data/runtime/cache/
  entries/<cache-key>.json       # key -> 内容对象
  objects/<content-sha256>.npy   # 内容寻址产物
```

缓存键按阶段纳入实际依赖：底图摘要、区域掩码摘要、边界半径/σ、迭代数、风格版本与参数摘要、区域归一化统计量、门限判定摘要、羽化参数和上游产物摘要。

缓存键同时绑定精度档、定点缩放与舍入模式，避免 float64、float32、定点产物互相污染。

- 掩码区域编辑：只重算被编辑区域及其下游 5 个节点，其他区域复用。
- 风格参数修订：解析、边界采样、内容补全 9 个节点复用，风格着色与融合 6 个节点重算。
- 增量结果与关闭缓存后的全量重算逐像素一致。

实测正常增量：`R2` 重算 5 个节点，`R1`/`R3` 复用 10 个节点。

实测风格修订：上游复用 9 个节点，下游重算 6 个节点。

## 快照、并发与恢复

SQLite 文件位于 `data/runtime/state.db`。

- 掩码、风格、整图结果三类快照都携带单调递增版本号和内容校验和。
- 每次提交写入完整快照并原子切换 head，不增量修补旧快照。
- 使用旧版本号提交返回 `E_SNAPSHOT_ROLLBACK`，head 保持不变。
- 版本或校验和不匹配分别返回 `E_SNAPSHOT_CONFLICT` / `E_SNAPSHOT_CHECKSUM`。
- 区域增量阶段使用跨进程 `flock` 非阻塞租约，后到者立即得到 `E_STAGE_BUSY`。
- 每个区域阶段提交后写入检查点；`worker-run --force-recompute --fail-after N` 在写入第 N 个检查点后调用 `os._exit(42)` 强制终止。
- 重启后从检查点恢复，已提交产物直接从内容缓存加载，不重算。

实测恢复流程写入 6 个检查点后进程退出码为 42，重启后 6 个节点以 `resumed_checkpoint` 复用，恢复结果与全量重算一致。

## Web/API

- `GET /api/state`：样本摘要、快照版本、缓存、检查点、业务错误日志。
- `POST /api/run`：`precision`、`focus_path`、`policy`、`threshold_tolerance`、`error_tolerance`。
- `POST /api/demo/incremental`：正常区域增量。
- `POST /api/demo/style`：风格参数修订与依赖失效。
- `POST /api/demo/rollback`：旧快照回退拒绝。
- `POST /api/demo/busy`：跨进程并发阶段占用。
- `POST /api/demo/recovery`：强制终止后的检查点恢复。
- `POST /api/reset`：恢复固定种子样本。

仪表盘展示底图、掩码、整幅结果、分块结果、块边界偏差热图、逐级误差谱、区域边界谱、缓存事件、快照版本、五阶段哈希账本和业务错误码。

## 代码结构

```text
generate_sample.py          固定种子样本生成器
image_diag/
  models.py                 样本与风格参数模型、校验
  deterministic.py          内容哈希、确定性噪声、门限保护、PNG
  precision.py              float64 / float32 / fixed 共用算子面
  graph.py                  五阶段 DAG、双路径、误差传播
  cache.py                  内容寻址缓存与检查点钩子
  state.py                  SQLite 快照、检查点、跨进程阶段锁
  service.py                运行编排、增量/回退/恢复演示
  web.py                    本地 HTTP 服务
  static/                   离线仪表盘
  cli.py                    命令行入口
tests/                      自动化验收测试
artifacts/verification.json 最近一次完整验收结果
```

## 实测结果

最近一次执行：

- `python3 -m unittest discover -s tests -v`：14 项测试全部通过。
- `python3 -m image_diag verify`：退出码 0，全部检查通过。
- 三档精度分块/整幅位级一致，五阶段摘要一致。
- 同一 float32 输入重复运行：阶段摘要与最终摘要相同。
- 增量缓存：仅 `R2` 重算，`R1`/`R3` 复用，增量等于全量。
- 风格修订：上游复用、下游重算，修订结果等于全量。
- `E_SNAPSHOT_ROLLBACK`：回退被拒绝且 head 未改变。
- `E_STAGE_BUSY`：跨进程后到提交者立即失败。
- 强制终止恢复：退出码 42，6 个检查点恢复，结果等于全量。

详细机器可读结果见 `artifacts/verification.json`。
