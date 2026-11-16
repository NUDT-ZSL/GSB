# 多模态证据协同推理组件（mmef）技术说明

## 组件结构

- `mmef/fixedpoint.py` — 确定性定点运算：两级量化（量化档位 -> 定点小数位宽）、
  定点 exp（截断 Taylor，记录截断位置）、减最大值稳定 softmax。全部整数运算。
- `mmef/loader.py` — 样本加载层：PNG 容器头/尺寸校验、JSON 结构校验、
  非法样本进入失败清单（`E_SAMPLE_INVALID`），其余样本继续。
- `mmef/fusion.py` — 视觉特征（区域均值+梯度强度）、文本断言按关系表映射到
  视觉区域、加权融合（冲突折减/补充叠加）、逐轮收敛、双精度（W 与 W+1 位宽）
  误差追踪、阈值边界诊断。
- `mmef/runner.py` — 线程池并发（每任务携带 `样本名#轮次` 稳定排序键）、
  轮次快照（临时文件 + `os.replace` 原子替换）、恢复判定、信号处理、报告输出。
- `mmef/snapshot.py` — 原子写盘与配置/关系表内容哈希。
- `mmef/samplegen.py` — 固定种子样本生成（几何图元+噪声 PNG、断言 JSON、
  融合配置、关系表、4 个非法样本、1 个构造的精度敏感边界样本）。
- `mmef/cli.py` — `gen` / `run` 子命令。

## 数值与误差模型

- 定点值 = 整数 × 2^-W；W 取自配置 `frac_bits`，高一档为 W+1。
- 量化残差：档位残差与定点残差分别记录，绝对值超过半单位（0.5×2^-W）
  计入 `error_sources`。
- 误差追踪三环节（均以高精度档单位为量纲）：特征量化 `quantization`、
  softmax 指数与归一化 `softmax`、加权求和 `weighted_sum`；
  `first_exceed_stage` 为首个超过 `error_tolerance` 的环节。
- 精度敏感样本：两档位最终判定类别（argmax）翻转，单独列入
  `sensitive.precision`，不静默归并。
- 边界敏感样本：置信度落在阈值邻域（`boundary_margin`）且两档位的阈值
  比较结果不一致（注意阈值本身也按各自位宽量化，二者并非精确移位关系），
  列入 `sensitive.boundary` 并给出证据贡献主项 `dominant_evidence`。

## 并发与一致性

每轮内各样本任务提交线程池（`concurrency` 取自配置），结果按
`样本名#轮次` 排序键合并，与线程完成顺序无关；测试断言并发=1 与并发=4
的样本结论、贡献分解、敏感清单、失败清单逐字段一致。

## 快照与恢复

每轮结束写 `snapshot.json`（临时文件 + 原子替换），含轮序号、配置哈希、
关系表哈希、定点位宽、已处理样本游标、各样本得分摘要、版本号。重启时：
哈希不符输出 `E_CONFIG_MISMATCH`（退出码 2）；版本变化整体替换（忽略旧
快照重新开始）；损坏/截断快照视为不存在。收到 SIGTERM/SIGINT 时完成当前
轮快照后以退出码 0 退出，不生成退出报告。

## 实际验证结果（本机 Linux, Python 3.11, numpy 2.4.6, Pillow 13.0.0）

- 默认数据集（9 合法 + 4 非法样本，3 轮，frac_bits=12）：
  - 4 个非法样本全部进入失败清单，业务码 `E_SAMPLE_INVALID`
    （缺图像 / JSON 结构不合法 / 尺寸不符 / PNG 头非法），其余样本正常产出结论。
  - 构造样本 `sample_009` 判定类别在两档位间翻转（refuted vs supported），
    被标记为精度敏感样本并单独列出。
  - 3 个样本的误差首次超容差环节标注为 `weighted_sum`。
  - 两次完整运行 `report.json` 字节级一致；并发 1 与并发 4 逐字段一致。
- 低精度实验（quant_levels=6, frac_bits=6）：3 个样本置信度落在阈值邻域
  且两档位判定不一致，被正确分类为边界敏感样本并给出证据贡献主项。
- 配置不符：修改 `config.json` 后重跑输出 `E_CONFIG_MISMATCH`，退出码 2。
- 强制终止恢复：运行中发 SIGTERM，进程以 0 退出且仅有快照无报告；
  再次运行从最近完整快照恢复，最终报告与未中断运行逐字段一致。
- `pytest tests/`：12 项测试全部通过。

## 复现

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m mmef.cli gen --out data
.venv/bin/python -m mmef.cli run --data data --out out
.venv/bin/python -m pytest tests/ -q
```
