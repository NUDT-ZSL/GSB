# 实际检查结果（check_demo.py 自动生成）

- 运行环境：Python 3.14.7，numpy 2.5.2，soundfile 0.12.1，平台 win32
- 首次运行（locked_tone.wav 被独占锁定）：ok 3 条 / skipped 9 条
- 解锁后重跑：ok 4 条 / skipped 8 条

## 关键实测数值

- sine_tone 接缝得分：0.000000（要求 < 0.2）
- abrupt_step 选中接缝索引：1，得分：1.085474（要求 > 0.8）
- abrupt_step 回绕跳变：淡化前 0.7277 → 淡化后 0.000000（满量程比）
- 静音条目 seam_score：1.0

## 逐项判定（28/28 通过）

- PASS **R5 报告顺序与清单一致**
- PASS **R5 每条恰含 file/status/seam_index/seam_score/reason 五字段**
- PASS **R1 sine_tone 正常导出 status=ok**
- PASS **R1 stereo_tone 被跳过且原因含声道** — 格式不支持，跳过该条目：声道数为 2，不符合单声道
- PASS **R1 低采样率条目原因含采样率** — 格式不支持，跳过该条目：采样率为 32000 Hz，不符合 44100 Hz
- PASS **R1 浮点位深条目原因含位深** — 格式不支持，跳过该条目：位深/子类型为 FLOAT，不符合 16 位 PCM
- PASS **R2 连续正弦接缝得分 < 0.2** — 实测得分 = 0.000000
- PASS **R2 突变接缝选中得分 > 0.8** — 实测得分 = 1.085474
- PASS **R3 sine_tone.wav 选中索引等于独立复算最低分索引** — 报告索引=0，复算各候选得分=[0.0, 0.0, 0.0]
- PASS **R3 abrupt_step.wav 选中索引等于独立复算最低分索引** — 报告索引=1，复算各候选得分=[1.414091, 1.085474, 1.406556]
- PASS **R4 sine_tone.wav 时长偏差<=1ms** — 实际 44000 采样 / 预期 44000 采样，偏差 0.0000 ms
- PASS **R4 sine_tone.wav 回绕接缝无爆音(边界跳变<2%满量程)** — 边界跳变 0.000000 满量程
- PASS **R4 sine_tone.wav 输出为 44100Hz/16bit/单声道**
- PASS **R4 abrupt_step.wav 时长偏差<=1ms** — 实际 44000 采样 / 预期 44000 采样，偏差 0.0000 ms
- PASS **R4 abrupt_step.wav 回绕接缝无爆音(边界跳变<2%满量程)** — 边界跳变 0.000000 满量程
- PASS **R4 abrupt_step.wav 输出为 44100Hz/16bit/单声道**
- PASS **R4 交叉淡化显著降低接缝跳变** — 淡化前 0.7277 -> 淡化后 0.000000（满量程比）
- PASS **R6 missing_tone 跳过且原因含缺失**
- PASS **R6 同批 sine_tone 导出文件存在**
- PASS **R7 short_tone 跳过且原因含长度不足** — 长度不足：预期循环长度 44100 采样，实际音频仅 22050 采样
- PASS **R8 同名条目仅首个 ok、次条跳过且原因含重复**
- PASS **R9 不存在的输出目录被自动创建且文件位于其中**
- PASS **R10 控制台逐条输出文件名与 OK/SKIPPED 标识** — OK 3 条 / SKIPPED 9 条
- PASS **R11 静音条目 skipped、seam_score=1.0、原因含静音且无导出文件** — 报告：{'file': 'silence.wav', 'status': 'skipped', 'seam_index': None, 'seam_score': 1.0, 'reason': '静音（全零音频），跳过且不产出导出文件'}
- PASS **R12 锁定条目首次运行跳过且原因含不可读** — 文件暂时不可读（无法读取音频头）：Error opening 'D:\\demo_GSB\\demo\\projects\\project69\\loop_audio\\samples\\locked_tone.wav': Format not recognised.
- PASS **R12 首次运行其余条目正常 ok（共 3 条 ok）**
- PASS **R12 解锁后重跑，locked_tone 成功导出 status=ok**
- PASS **R12 重跑后原有成功条目仍保持 ok** — 首次成功文件：['sine_tone.wav', 'abrupt_step.wav', 'dup_tone.wav']
