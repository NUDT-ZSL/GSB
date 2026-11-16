# 多模态图生文互操作与版本兼容诊断工作台 — 技术说明

## 概览

本工作台面向本地确定性样本，完成：图像区域特征抽取与确定性分类、描述生成、
v1/v2 双版本序列化与反序列化、格式互转与信息保留、跨版本渲染一致性校验、
版本化快照整体替换与恢复、并发/串行结果一致性验证、异常注入隔离与可复现诊断报告。

运行环境：Python 3.11+，依赖 numpy 与 Pillow（`python3 -m pip install numpy pillow`）。
不使用 Docker、不访问网络模型或 GPU。

## 目录结构

```
run.py                  CLI：gen-samples / run / verify
workbench/
  samples.py            固定种子样本生成（PNG、关系表、配置、v1/v2 交换文件、注入配置）
  features.py           PNG 容器头与尺寸校验、颜色连通域切分、区域特征计算
  classify.py           加权打分确定性分类（同分按类别名字典序）、千分比置信度裁剪
  v1io.py / v2io.py     v1 固定宽度文本与 v2 JSON 的解析/序列化
  model.py              统一中间表示（整数像素 bbox + 千分比置信度 + 来源版本与原始精度）
  convert.py            v1<->v2 双向转换、降级损失清单、往返还原校验
  render.py             覆盖图渲染、逐像素差异、E_BOUNDARY_SENSITIVE 边界诊断
  snapshot.py           manifest 版本化快照，临时目录整体原子替换
  inject.py             独立注入配置应用与期望业务码映射
  pipeline.py           并发编排、稳定排序键、异常隔离、原子报告写盘
tests/test_workbench.py 13 个 unittest 用例
```

## 关键设计

- **统一中间表示（IR）**：`RegionDesc` 以整数像素 bbox 与 0..1000 千分比置信度为标量，
  同时保留 `source_version`、`orig_bbox`（双精度浮点）、`orig_confidence`，
  供降级残差审计与边界敏感诊断引用。
- **v1 格式**：48 字符定宽行（ID 8 + 类别 12 + 坐标 4×5 + 置信度 4，数字零填充）。
- **v2 格式**：JSON 数组，首元素为命名颜色表，其余为实例（浮点 bbox、0..1 置信度、
  关系引用列表、颜色名）。
- **升级 v1→v2**：整数转浮点、生成默认命名颜色条目，整数来源标记写入旁路元数据
  （`upgrade_sidecar.json` 的 `integer_origin`）。
- **降级 v2→v1**：坐标四舍五入（半值远离零）、置信度重算千分比；逐区域舍入残差、
  丢失的关系引用与颜色名汇总进降级损失清单（含 `lossless` 标记，可审计）。
- **边界敏感判定**：浮点坐标小数部分恰为 0.5，或双精度置信度落在
  阈值 ± boundary_epsilon 内，记 `E_BOUNDARY_SENSITIVE` 并输出双精度参考值与两版本判定。
- **快照**：`manifest.json` 记录全局版本号、配置哈希、关系表哈希、各区域描述版本与
  覆盖图版本；写入先落 `snapshot.tmp/` 再 `os.replace` 整体替换（旧目录备份为
  `.old` 后清理）；启动仅凭完整替换的 manifest 判定命中，版本/哈希不符整体重建。
  启动时探测工作目录与临时目录可写性并写入报告 `environment` 字段。
- **并发一致性**：线程池大小取配置 `concurrency`；每个区域任务携带
  （图像文件名， 区域序号）稳定排序键，所有输出列表落盘前按排序键重排，
  与单线程逐字段一致。
- **信号处理**：CLI 安装 SIGINT/SIGTERM 处理器置位停止事件；流水线完成当前区域、
  取消未完成任务、以 `status=interrupted` 落盘进度后以退出码 0 结束。
- **报告**：`report.json` 以临时文件 + `os.replace` 原子写盘，含渲染差异摘要、
  降级损失清单、边界敏感区域、快照版本、失败清单、注入命中与各阶段耗时。

## 业务码

| 代码 | 含义 |
|---|---|
| `E_REGION_INVALID` | 零面积区域 / 边界框越界 |
| `E_PARSE_INVALID` | v1 缺字段行 / 非法 JSON / 字段类型错误（记行号或数组索引） |
| `E_CONFIDENCE_RANGE` | 置信度超出 [0,1] |
| `E_RELATION_DANGLING` | 关系引用悬空 |
| `E_BOUNDARY_SENSITIVE` | 阈值附近置信度 / 半像素舍入边界 |
| `E_IMAGE_INVALID` | PNG 容器头或尺寸校验失败 |

## 使用

```bash
python3 run.py gen-samples --data data      # 生成确定性样本
python3 run.py run --data data --out out    # 完整诊断运行（并发）
python3 run.py run --data data --out out --serial
python3 run.py verify --data data           # 串行 vs 并发逐字段一致性
python3 -m unittest discover -s tests       # 测试套件
```

## 实际验证结果（种子 20261117，3 张 256×256 样本）

- 区域抽取/分类：21 个区域（每图 7 个），跨运行逐字段一致。
- 往返转换：`v1→v2→v1` 与 `v2→v1→v2` 关键字段均精确还原（`round_trip` 全 true）。
- 渲染一致性：v1/v2 覆盖图逐像素最大通道差 255、差异像素 98
  （来源于 INST0006 半像素坐标 196.5/150.5/236.5/196.5 的舍入位移）。
- 边界敏感：INST0006（`half_pixel_rounding`，四轴残差恰为 -0.5）、
  INST0007（`confidence_at_threshold`，双精度参考值 0.5 等于阈值 0.5）。
- 降级损失清单：INST0006 坐标残差 [-0.5]×4；INST0001 丢失关系引用 [R2, R3] 等，
  均带 `lossless` 审计标记。
- 异常注入：INJ-001..INJ-005 全部命中，业务码分别为
  E_PARSE_INVALID、E_PARSE_INVALID、E_REGION_INVALID、E_CONFIDENCE_RANGE、
  E_RELATION_DANGLING。
- 快照：二次运行 manifest 命中（`hit: true`）；全局版本号 +1 后整体重建。
- 并发一致性：`verify` 对 9 组输出键比较串行与并发结果，`mismatches: []`。
- 信号退出：注入停止事件后以退出码 0 结束，manifest 记 `interrupted` 与进度。
- 测试：`python3 -m unittest discover -s tests` — 13 个用例全部通过。
