# 本地确定性多模态协同推理管线

面向本地确定性样本的图文配对证据联合判定管线。仅依赖 Python 3.11 标准库与
NumPy；PNG 编解码由 `mm_pipeline/png_codec.py` 用标准库实现，不访问任何在线资源，
不使用容器。

## 1. 目录结构

```
mm_pipeline/
  png_codec.py     纯标准库 PNG 编解码（8bit 灰度/RGB/RGBA，filter 0-4）
  config.py        协同推理配置、证据关系表加载与规范化 SHA-256 哈希
  sample.py        确定性样本生成、加载校验、实体/属性/断言解析
  features.py      视觉分块特征、量化、区域 bbox 与文本断言特征
  alignment.py     关联/冲突/补充三类关系对齐与悬空关系处理
  fusion.py        softmax 加权融合、冲突折减、补充叠加与逐轮判定
  persistence.py   同卷临时文件 + os.replace 原子写、快照与版本校验
  engine.py        轮次调度、线程池并发、快照恢复、信号处理、报告
  cli.py           命令行入口（generate / run）
tests/             15 个单元/端到端/信号/强制终止测试
run_tests.sh       一键测试脚本
requirements.txt   numpy>=1.24
```

## 2. 快速开始

```bash
python3.11 -m pip install --user -r requirements.txt
# 生成确定性数据集（含 5 个有效场景 + 3 个非法样本）
python3.11 -m mm_pipeline.cli generate --input-dir artifacts/input
# 完整推理（自动在每轮写快照，结束写原子报告）
python3.11 -m mm_pipeline.cli run \
  --input-dir artifacts/input \
  --snapshot-dir artifacts/snapshots \
  --report artifacts/report.json
# 模拟可处理退出：完成第 1 轮快照后以退出码 0 退出；再次执行同命令即恢复
python3.11 -m mm_pipeline.cli run ... --exit-after-round 1
```

## 3. 确定性样本与场景

固定随机种子 `20261117`，图像为 64x64 RGB，按 4x4 网格绘制几何图元并叠加有界噪声：

| 样本 | 场景 | 期望行为 |
| --- | --- | --- |
| `sample_fast` | 强匹配、1 条补充 | 第 1 轮即过置信度阈值，少轮收敛 |
| `sample_slow` | 匹配、3 条补充 | 补充证据逐轮激活，第 3 轮收敛 |
| `sample_conflict` | 含 conflict 关系 | 冲突折减，跑满最大轮次，结论 mismatch |
| `sample_dangling` | 引用不存在区域/断言 | 记录 E_RELATION_DANGLING 并跳过该关系 |
| `sample_mismatch` | 图文属性不兼容 | 证据为负，结论 mismatch |
| `sample_bad_*` | 缺文件/坏 PNG 头/坏 JSON | E_SAMPLE_INVALID 入失败清单且不影响其他样本 |

## 4. 业务码（逐字）

- `E_SAMPLE_INVALID`：样本文件缺失、PNG 容器头或 JSON 结构不合法。
- `E_RELATION_DANGLING`：关系引用了不存在的区域或断言，该关系跳过。
- `E_CONFIG_MISMATCH`：恢复时配置哈希或关系表哈希与快照不一致，拒绝恢复。
- 阶段异常码：`E_FEATURE_FAILED` / `E_ALIGNMENT_FAILED` / `E_FUSION_FAILED`。

## 5. 关键设计

### 视觉特征与量化
按 `grid_rows x grid_cols` 确定性分块，对每块计算区域均值、梯度强度与 4 档主
方向直方图，拼成 `feature_dim` 维向量，再按 `quantization_levels` 做
`floor(v*L)/L` 离散化；量化档位变化必然改变离散结果，特征计算无任何随机调用，
同图同配置两次运行逐元素一致，并输出每块 `[x0,y0,x1,y1]` bbox。

### 文本特征与关系对齐
每条断言基于属性/取值经 SHA-256 派生确定性单位向量；关系按来源顺序对齐为
associate/conflict/supplement，悬空关系只记录一次（跨轮去重），对齐结果保留
`relation_id` 与 `source_order`。

### 逐轮融合
每轮对视觉/文本证据按 `fusion_temperature` 做 softmax，再与
`text_evidence_weight` 归一化混合；冲突关系按 `conflict_penalty` 折减置信度，
补充关系按轮次激活并以权重叠加；输出判定得分、归一化置信度与逐条证据贡献分解。
置信度低于 `confidence_threshold` 进入下一轮，达到 `max_evidence_rounds` 强制
给结论，杜绝无界循环。

### 快照与恢复一致性
每轮结束用同目录临时文件 + `os.replace` 原子替换 `snapshot.json`，含轮序号、
`config_hash`、`relations_hash`、已处理游标、各样本得分/证据摘要与 `version`。
重启读取最新快照，哈希不一致即抛 `E_CONFIG_MISMATCH`；一致则从该轮游标继续，
恢复后报告与不中断运行逐字段一致（测试 `test_resume_matches_uninterrupted_field_by_field`）。

### 并发与串行一致性
线程池并发度取自配置；样本按名称排序、轮次按序推进，每个任务带
`sample_name#round000000` 稳定排序键，结果回收后统一按样本名重排，因此结论、
证据分解顺序、失败清单顺序、快照内样本顺序与并发度无关（测试
`test_threadpool_matches_single_thread_output`）。

### 异常链路与可处理退出
单样本在特征/对齐/融合阶段抛异常时记录样本名、轮序号、阶段名、异常类型到失败
清单，该样本标记 failed 且后续轮次不再产出结论，其余样本继续。收到 SIGINT/
SIGTERM（或 `--exit-after-round`）时完成当前轮快照与进度记录后以退出码 0 退出；
进程被强制终止后只依据已完整替换的快照恢复，启动时清理同卷残留 `*.tmp`
（测试 `test_force_kill.py`）。

### 报告
报告经临时文件原子替换写盘，含每样本联合结论、最终置信度、各轮得分轨迹、证据
贡献分解、bbox、快照版本号、配置/关系表哈希、失败清单、悬空警告与各阶段耗时。

## 6. 实测验证结果

执行 `./run_tests.sh`：**15 个测试全部通过（OK）**，覆盖 PNG 回环、特征确定性
与量化档位敏感性、非法样本容错、悬空关系、少轮/多轮收敛、并发-串行一致、
快照恢复逐字段一致、配置/关系表变更拒绝、异常链路、SIGTERM 退出码 0、
强制终止后恢复。

端到端验收（`artifacts/accept/acceptance_summary.json`）：

- 不中断运行 4 轮；第 2 轮中断后恢复，`resume_field_identical = true`。
- 篡改融合温度后重启，`config_tamper_rejected = true`（E_CONFIG_MISMATCH）。
- 收敛轮次：fast=1、slow=3；conflict/mismatch 跑满 4 轮且结论 mismatch。
- 3 个非法样本均以 E_SAMPLE_INVALID 入失败清单；2 条悬空关系被识别跳过。

## 7. Windows 落地说明

代码仅使用跨平台标准库与 NumPy，路径全部基于 `pathlib`，原子替换使用
`os.replace`（Windows 上为原子 MoveFile 语义），临时文件与目标文件同目录同卷。
在 Windows PowerShell 中将 `python3.11` 换为 `py -3.11` 或 `python`，
`./run_tests.sh` 可改为 `python -m unittest discover -s tests -v`。
