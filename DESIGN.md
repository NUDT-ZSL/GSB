# 图文证据联合判定管线 — 技术说明

面向本地确定性样本的图像区域视觉证据与结构化文本断言协同推理管线。全部计算在本地完成，
不访问任何外部目标，不使用容器。

## 1. 环境

- Python 3.11（实测 3.11.0rc1）
- NumPy 2.4.6、Pillow 13.0.0（通过 `get-pip.py` 引导 pip 后安装）
- 仅使用标准库 + NumPy + Pillow，无其它第三方依赖

## 2. 目录结构

```
pipeline/
  errors.py       业务错误码与带码异常
  hashing.py      规范化 JSON（sort_keys、紧凑分隔符）与 SHA-256
  atomicio.py     临时文件 + fsync + os.replace 原子写
  config.py       融合配置 dataclass、校验、配置哈希（剔除 concurrency）
  samples.py      固定种子 PNG/断言生成、加载与 PNG 头/尺寸/JSON 校验
  vision.py       确定性分块统计、L2 归一化、量化档位离散化
  textrel.py      断言特征向量、关系表解析、关联/冲突/补充对齐与悬空检测
  fusion.py       减最大值稳定 softmax、冲突折减、补充叠加、贡献分解
  injection.py    独立注入配置与五类故障注入器
  snapshot.py     轮次快照、游标、哈希校验、恢复
  report.py       稳定排序键与确定性报告装配
  engine.py       线程池并发、逐轮编排、信号语义、报告落盘
  cli.py          generate / run / hashes 三个子命令
configs/          融合配置（含串行版与量化/温度变体）、关系表、注入配置
tests/            16 个单元与集成测试
run_validation.sh 一键验收脚本
```

## 3. 确定性设计（逐字节一致的来源）

1. **无 RNG 进入推理路径**：样本图像与断言由固定种子 `numpy.random.default_rng`
   生成；文本特征向量由断言 id/属性的 SHA-256 派生种子生成，不消耗任何全局随机状态。
2. **规范化序列化**：所有 JSON 经 `json.dumps(sort_keys=True,
   separators=(",",":"))` 后再哈希或落盘，字典顺序与平台无关。
3. **浮点确定**：视觉统计仅含切片、均值、差分、arctan2 与 L2 归一化；softmax 先减最大值
   再 exp，求和顺序固定（单线程内 NumPy 按固定轴归约）。线程之间不共享可变缓冲区。
4. **排序键**：`task_sort_key = [sample_name, round_index]`，失败清单、注入命中、贡献分解
   全部经确定性 key 排序后提交，线程调度顺序不影响输出。
5. **并发度不参与判定**：`concurrency` 仅控制线程池大小，被排除在配置哈希与报告主体之外；
   运行时元数据（工作目录、并发度）写入独立的 `*.run-metadata.json` sidecar，主报告在
   并发 1 与并发 N 下逐字节相同。
6. **原子持久化**：快照、失败清单、报告均写入同目录 `.tmp-*` 临时文件，flush+fsync 后
   `os.replace` 原子替换，并 fsync 目录项。任何时刻只存在“旧完整文件”或“新完整文件”。

## 4. 视觉特征与量化

- 网格按 `grid_rows x grid_cols` 整数切片分块（边界由配置唯一确定）。
- 每块统计 6 个基元：灰度均值、梯度强度、4 档主方向直方图（幅值加权）。
- 基元按 `feature_dim / 6` 确定性平铺后 L2 归一化。
- 量化：逐向量 min-max 归一化到 [0,1]，再 `round(x*(L-1))/(L-1)` 投到 L 档均匀网格。
  L 改变离散网格即改变结果；同图同档多次运行逐元素一致（测试断言）。

## 5. 融合推理

每条有效关系产生一个证据项：

- 相似度 `s = clip(dot(visual[region], text[assertion]), -1, 1)`
- 基础证据：关联/补充 `0.5 + 0.5s`，冲突 `0.5 - 0.5s`
- logit = 基础证据 / 逐轮温度（温度按 `T*(1+0.1*round)` 确定性演进）
- 稳定 softmax 权重 × 有效权重（冲突乘 `conflict_discount`，补充加
  `supplement_boost`），归一化后得到贡献分解
- 判定得分 = Σ(贡献 × 基础证据)，置信度 = 最大贡献；低于阈值给 REJECTED，
  跑满轮次给 ACCEPTED，并记录触发原因

## 6. 快照与恢复

快照含：版本号、轮序号、配置/关系/注入哈希、样本游标、各样本得分摘要、完整轮次记录、
失败样本集合、注入命中、失败清单、静态悬空关系清单。恢复时先校验三个哈希，不符抛
`E_CONFIG_MISMATCH`；一致则从 `round+1` 继续，静态悬空关系不再重复生成，最终报告与
不中断运行逐字节一致（含快照版本号）。

## 7. 故障注入与业务码

| 故障 | 业务码 | 阶段 |
| --- | --- | --- |
| 图像截断 | E_IMAGE_TRUNCATED | visual_feature |
| 断言字段缺失 | E_ASSERTION_MISSING | relation_align |
| 区域越界 | E_REGION_OUT_OF_RANGE | relation_align |
| 非有限特征 | E_NON_FINITE_FEATURE | fusion |
| 关系悬空（注入/静态） | E_RELATION_DANGLING | relation_align |
| 样本文件缺失/PNG 或 JSON 不合法 | E_SAMPLE_INVALID | load |
| 快照哈希不符 | E_CONFIG_MISMATCH | snapshot |

注入按 `(sample, round)` 精确命中，注入项编号写入 `injection_hits` 与失败清单。
样本一旦在某轮失败即进入 `failed_samples`，后续轮次不再产出结论，其它样本不受影响。
静态悬空关系（关系表自带）与注入悬空分开记录：前者进入 `static_dangling_relations`，
绝不参与对齐。

## 8. 退出语义

- `SIGTERM`/`SIGINT`：完成当前轮快照与失败清单写盘后以退出码 0 退出，不生成最终报告。
- 强制终止（SIGKILL/掉电）：仅已完成 `os.replace` 的快照可用于恢复，不承诺退出报告。
- 测试通过 `PIPELINE_INTERRUPT_AFTER_ROUND` 环境变量提供确定性的中断恢复点。

## 9. 实测验证结果（见 artifacts/validation/SHA256SUMS）

- 16 个单元/集成测试全部通过。
- 并发 1 与并发 3 的主报告 SHA-256 完全相同：
  `ba063ba5485b02d66c5997f1fade020289df17b91c7f691a84eef20c739b3e80`
- 中断恢复报告与不中断报告 SHA-256 完全相同：
  `7519a794328af580b21f029df598ca0a0413dc0056acc9d92ce492d819068203`
- 连续两次执行 `run_validation.sh`，全部报告哈希稳定不变。
- 量化 4 档时特征仅取 `{0, 1/3, 2/3, 1}`，与 16 档存在可观测差异；8 个样本得分轨迹
  全部变化，其中 sample-007 由 REJECTED 变为 ACCEPTED，证明档位/温度精确影响结论。

## 10. 常用命令

```bash
python3 -m pipeline.cli generate --input-dir data/samples \
    --config configs/fusion_config.json --samples 8 --manifest artifacts/manifest.json
python3 -m pipeline.cli run --input-dir data/samples \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections.json --work-dir work/run --report artifacts/report.json
python3 -m unittest discover -s tests
./run_validation.sh
```
