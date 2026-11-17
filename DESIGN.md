# 视频预测与运动时序渲染诊断工作台 — 技术说明

## 1. 设计目标与边界

工作台在纯 CPU、无网络、仅依赖 NumPy + Pillow 的条件下，模拟文生视频（text）与
图生视频（image）双路径的确定性潜变量去噪管线，覆盖多分辨率档位、去噪步预算、
首帧响应预算、增量复用、原子快照与并发一致性诊断。所有“模型计算”均为可审计的
确定性数值模拟：随机量由 `(seed, clip_id, tier, step, 用途)` 经 sha256 派生到
numpy `PCG64`，同一输入在任意机器、任意并发度、任意重复运行下逐元素一致。

## 2. 目录与模块

- `vbench/hashing.py`：稳定哈希、内容指纹、`DeterministicRNG`（PCG64 封装）。
- `vbench/config.py`：配置加载与校验，非法时抛 `ConfigError`（业务码
  `E_CONFIG_INVALID`），含档位单调性、表长度、种子/引导强度取值范围。
- `vbench/samples.py`：两份 JSONL（轨迹、语义标签）与各档初始图 PNG 校验，
  单片段失败产出 `E_TIMESERIES_INVALID` 且不阻断其他片段。
- `vbench/embedding.py`：受限词表上的哈希词袋嵌入，L2 归一化，OOV 确定投影。
- `vbench/latents.py`：文生（提示词偏置+确定性噪声）与图生（8x 池化“编码器”+
  确定性抖动）初始化，产物带 sha256 指纹与依赖清单。
- `vbench/denoise.py`：共享去噪步、图生替代步、最近邻上采样升级、8x RGB 解码。
- `vbench/scheduler.py`：预置策略表（逐帧串行/帧维批量/跨片段合并）、候选评分、
  档位检查点复用、全局去噪步预算与首帧预算评估。
- `vbench/snapshot.py`：临时文件 + `os.replace` 原子持久化，清单版本号、配置
  哈希、档位集合指纹与失效/恢复判定。
- `vbench/pipeline.py`：阶段编排（init→denoise→upgrade→decode）、线程池、
  稳定排序键、共享/分歧段校验、增量缓存复用、信号边界处理。
- `vbench/report.py`：诊断 JSON 报告（帧数、最大通道差、共享判定、预算、决策、
  快照版本、失败清单与失败码汇总）。
- `vbench/samplegen.py`：固定种子样本与 9 类异常注入用例生成。
- `vbench/cli.py` / `run_workbench.py`：命令行入口。
- `acceptance_check.py`：18 项端到端验收；`tests/`：49 个单元/集成测试。

## 3. 双路径共享段模型

设去噪步数为 S、图生替代步数为 R。

- 步 `0..R-1`：文生走共享去噪；图生走携带图像先验的替代步，两路径潜变量不同
  （分歧段）。
- 步 `R-1` 结束：图生潜变量锚定到文生同一步潜变量（共享锚点）。
- 步 `R..S-1`：两路径从同一锚点、同一噪声实现、同一引导表出发执行逐元素相同的
  共享计算，中间潜变量严格相等（共享段）。
- 校验器对共享段任何非零差报 `E_PATH_DIVERGENCE`；分歧段（锚点之前的替代步）
  若意外相同也报该码。注入用例 `fault_path_divergence` 人为污染共享步以验证检测。

## 4. 档位升级与去噪步复用

档位 t（t>0）在检查点 `c_t = min(S-1, R+t)` 复用上一档已验证潜变量：

1. 对低档位第 `c_t` 步潜变量做真实空间最近邻上采样（潜变量空间 2x/4x…，对应
   像素 16x/64x…），作为高档位种子；
2. 仅计算 `c_t+1..S-1` 的后续去噪步，已复用步不重新计算；
3. 高档位终帧由上采样后继续去噪再解码得到，与“直接缩放低档位终帧”的最大通道差
   为 255（验收实测），杜绝以缩放终帧冒充升级。

## 5. 预算与策略选择

- 单帧单步代价 `cost(t) = 基准代价 * 像素数(t)/像素数(0)`，分辨率越近代价越高。
- 三种批量模式按峰值内存（模式权重 × 帧潜变量大小）与内存上限判定可行性，再以
  吞吐收益、算力冗余与内存压力综合评分，选择可行最优模式并记录全部候选分数。
- 全局去噪步预算按“新增步 × 帧数 × 片段数”逐档扣减；不足档位产出
  `E_DENOISE_BUDGET_PENDING`，保留清单与可继续的中间状态，不静默丢弃。
- 首帧预览仅评估最低档（初始化+全步去噪+解码）耗时，超出
  `first_frame_budget_ms` 时返回 `E_PREVIEW_BUDGET_PENDING` 中间状态而非阻塞。

## 6. 增量一致性

每个片段的缓存键由 `配置哈希 + 片段 ID + 提示词嵌入指纹 + 初始图指纹 + 潜变量
补丁指纹` 组成。命中时从已完整替换的快照直接恢复，不产生新阶段替换（清单版本
不增长）；未命中或配置/档位集合变化时整体失效重建。编辑序列（提示词、种子、
步数、引导强度、帧数、初始图、档位收窄/扩张、局部潜变量补丁）后的增量结果，与
空白工作区按相同最终输入的一次性全量结果逐像素、逐潜变量一致（测试与验收覆盖）。

## 7. 快照、终止与恢复

- 所有产物先写同目录 `.tmp-*` 临时文件，`fsync` 后 `os.replace` 原子切换。
- 清单记录全局版本号、配置哈希、档位集合指纹、每片段状态与步序游标、缓存键。
- 读取时以版本号 + 配置哈希判定命中；档位指纹不匹配即识别为档位集合变化并清空。
- SIGTERM/SIGINT 仅设置事件标志，管线在片段任务边界完成当前阶段产物与清单替换后
  以退出码 0 退出；强制终止（kill -9）后只依据完整替换的产物恢复，损坏/半截文件
  触发回退重算，且不承诺生成退出报告。

## 8. 并发与排序

线程池最大并发数取自配置。去噪随机流始终按完整帧形状生成后按索引切片，去噪函数
始终作用于完整数组，因此三种批量模式与不同 worker 数的浮点运算逐元素一致。每个
任务携带 `(clip, tier, path, step, frame)` 五元稳定排序键，结果、潜变量清单、
复用清单与失败清单均按键排序，与单线程顺序执行一致。单片段/单阶段异常被隔离为
`E_STAGE_FAILED` 并继续其余任务。

## 9. 业务错误码

| 代码 | 触发场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 缺字段、表长度不符、引导强度/种子非法、档位非单调 |
| `E_TIMESERIES_INVALID` | ID 重复/缺失、轨迹与标签帧数不一致、初始图缺失或尺寸不符 |
| `E_PATH_DIVERGENCE` | 共享段潜变量不一致或分歧段意外相同 |
| `E_DENOISE_BUDGET_PENDING` | 全局去噪步预算不足以渲染某档位 |
| `E_PREVIEW_BUDGET_PENDING` | 首帧预算内无法交付最低档预览 |
| `E_STAGE_FAILED` | 渲染阶段异常（隔离，不阻断其他片段） |
| `E_SNAPSHOT_INVALID` | 快照产物缺失/尺寸不符（保留码，恢复时回退重算） |

## 10. 复现实验命令

```bash
pip install -r requirements.txt
python3 run_workbench.py generate --sample-dir samples/valid
python3 run_workbench.py run --config samples/valid/config.json \
    --sample-dir samples/valid --output-dir artifacts/run
python3 run_workbench.py preview --config samples/valid/config.json \
    --sample-dir samples/valid --output-dir artifacts/preview
python3 -m unittest discover -t .          # 49 个测试
python3 acceptance_check.py                # 18 项端到端验收
```
