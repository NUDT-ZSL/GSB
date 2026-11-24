# 实际验证记录

验证环境（本次执行容器）：

* OS：Linux（容器内），Python 3.10.12，numpy 1.26.4
* 代码语法保持 3.10/3.11 兼容；目标 Windows 环境为 Python 3.11+
* 不使用 Docker、不访问网络/外部素材，样本由固定种子 20261125 本地生成

## 1. 复现步骤

```bash
pip install -r requirements.txt
python scripts/generate_sample.py --root data --seed 20261125
python -m soundfield.main --scene data/scene.json --state-dir state --output-dir out --clean
python -m unittest discover -s tests -v
```

## 2. 单元测试结果

`python -m unittest tests.test_validation tests.test_fixedpoint tests.test_images`

* 23 个用例全部通过，覆盖：
  * JSON 解析错误 `E_JSON_PARSE`、尺寸 `E_DIMENSION`、吸声 `E_ABSORPTION`、
    坐标 `E_COORDINATE`、重合 `E_DUPLICATE_POINT`、采样率 `E_SAMPLE_RATE`、
    布局 `E_LAYOUT`、阶数 `E_ORDER`、频段 `E_BAND`；
  * Q24 量化往返、定点乘法标度、反射链逐步舍入的顺序依赖、Q24 卷积标度、
    int16 饱和、非有限值拒绝；
  * 镜像枚举确定性、格点去重、字典序、阶数边界、反射几何、吸声链历史。

## 3. 集成测试结果

`python -m unittest tests.test_pipeline_integration`（6 个用例，全部通过，约 19 秒）

`python -m unittest tests.test_edge_cases`（3 个用例，全部通过，约 1 秒）：
零阶仅直达声、内存预算违规 `E_BUDGET`、干信号采样率不匹配 `E_WAV`。

| 用例 | 验收点 | 结果 |
| --- | --- | --- |
| `test_full_run_outputs_and_report` | 90 分块全部完成、6 声道 48kHz WAV、状态/报告齐全、业务码 E_OK | 通过 |
| `test_resume_produces_identical_samples` | 第 5 块边界中断 -> 写 interrupted.json、无伪完成块 -> 续算输出与一次跑完逐采样相同 | 通过 |
| `test_version_mismatch_forces_rebuild` | 改吸声系数后版本号变化、状态整体重做、无新旧 IR 混用 | 通过 |
| `test_concurrency_and_partition_independence` | max_workers=2 与 =1 的最终 WAV 逐采样一致 | 通过 |
| `test_output_not_replaced_when_version_unchanged` | 版本未变时 WAV 文件不被替换（mtime 不变） | 通过 |
| `test_fixed_float_paths_both_present` | 定点 WAV 与 float64 参考 WAV 同形、接近但不全等 | 通过 |

## 4. 样本运行的关键数值（固定种子可复现）

* 镜像声源数：30（直达 3 + 一阶 18 + 二阶 9，已对重合格点去重）
* 分块数：90（5 频段 × 3 阶 × 6 扬声器），IR 长度 4096 采样点
* 输出：6 声道、48kHz、16bit PCM、8191 帧
* 首个采样点级越限：频段 0、采样点 121、扬声器 0、阶数 0、镜像 0，
  聚合残差约 7.29e-8（容差 2e-8），放大路径判定为 `absorption_chain`
* 首个单镜像能量越限：镜像 0 / 阶数 0，能量差约 1.87e-7（容差 1e-12）
* 最终混音定点 vs float64 最大绝对差：约 2.05e-5（Q24 累积极限内，
  远小于信号幅度 O(1)）
* 可忽略能量裁定：样本内 900 个“镜像×频段×扬声器”组合均高于定点阈值，
  无裁定翻转；裁定逻辑的单测覆盖定点/浮点阈值差异路径

## 5. 中断与强制终止差异验证

* 可处理终止：通过测试钩子在分块边界置位中断标志，调度器完成/丢弃当前
  块后停止，落盘块集合与 `interrupted.json` 完全一致，退出码 3；续算只
  补缺失块，最终输出与不中断一次跑完逐采样相等。
* 强制终止：进程被强杀时没有信号回调机会；因所有块都经
  “同目录临时文件 -> os.replace -> 元数据双写”提交，残留临时文件不会被
  识别为完成块，下次启动按缺失块补算。

## 6. 已知边界

* 为让残差溯源在样本上真实触发，采样点容差取与 Q24 半 LSB 同量级的
  2e-8；若放宽到 1e-6，样本将落在容差内，报告状态变为
  `within_tolerance`，链路行为不变。
* 定点卷积采用 int64 分块累加 + Python 大整数块间求总（块和保证不溢出），
  样本规模下端到端约 2–3 秒；更大规模可在保持标度不变的前提下继续分块向量化。
