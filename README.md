# 流式 24bit PCM 多轨混音引擎

本地录音棚母带引擎:把同一首曲目的多条 24bit 小端裸 PCM 单声道/立体声分轨,
按精确有理数时间轴拼接混音为一条峰值归一化的立体声母带。
纯 Python 3.11 标准库实现,无第三方依赖,不使用 `wave` 模块,全程不引入浮点。

## 文件

| 文件 | 说明 |
|---|---|
| `engine.py` | 引擎本体(5 个子命令) |
| `testgen.py` | 确定性测试样本生成器(不依赖任何外部音频文件) |
| `run_acceptance.py` | 12 项验收的自动验证(参照值用独立 Fraction/整数实现重算) |
| `sample_timeline.json` | 内置确定性时间轴(含 1/3 拍、5/7 拍事件,分段分数 BPM) |

## 子命令

```
python engine.py timeline  --timeline T.json --sample-rate 48000
python engine.py positions --manifest M.json --timeline T.json
python engine.py mix       --manifest M.json --timeline T.json --output out.pcm
                           [--block-samples N] [--window-samples N]
                           [--trace-window] [--trace-peak-window]
                           [--fail-after-output-bytes N]
                           [--crash-after-output-samples N] [--sidecar S.json]
python engine.py copy      --input a.pcm --output b.pcm --channels 2
                           [--block-samples N] [--window-samples N]
                           [--trace-window] [--trace-peak-window]
python engine.py validate  --master out.pcm --sidecar out.pcm.sidecar.json
                           [--crash-after-blocks N]
```

## 设计要点

### 1. 有理数时间轴(要求 1)
时间轴 JSON 由若干片段组成,每段 `{start, bpm}`(均为分数)。事件位置 q(四分音符)
换算为采样点:`sum over segments of (段长拍数 * 60 / bpm * sr)`,全程 `Fraction`
精确累计,最后**向下取整**一次。采样率限 44100/48000/96000。

### 2. 24bit 小端流式读写(要求 2、8)
三字节小端有符号定点,值域 -8388608..8388607,越界饱和(`clamp24`)。
小端主机上用 `array('i')` 重解释做 C 速度编解码,另有纯 Python 回退路径。
输入按子块读取:子块帧数 = `min(block, window // (2*channels))`,
任一时刻内存中持有的输入样本数由 `WindowLedger` 精确记账(确定性统计,
不依赖平台内存测量),已处理子块立即释放,绝不保留整轨。
`--trace-window` 每块输出读入范围与持有量,`--trace-peak-window` 输出峰值。

### 3. 定点混音(要求 3)
每轨贡献 = `round_half_away(sample * gain * coeff)`,其中
mono: `coeffL=(1-pan)/2, coeffR=(1+pan)/2`;stereo: 左右声道分别按同两系数缩放,
不互相混合。各轨贡献以整数累加,最终饱和到 24bit。
所有舍入为**半值远离零**(`(2n+d)//(2d)` 纯整数运算)。
每个输出样本的计算互相独立,因此结果与分块边界无关(块 1/17/4096 SHA256 相同)。

### 4. 间断输入(要求 4)
区间按 `(起始采样点, 声明顺序)` 排序后依次填充块缓冲,后者覆盖前者;
未被任何区间覆盖的位置保持静音。排序只涉及区间元数据(不读样本),
覆盖语义在流式逐块处理中完成,不依赖整体加载。

### 5. 原子提交(要求 5)
阶段 2 完整写入同目录临时文件 `<out>.tmp2`,flush+fsync 后 `os.replace` 原子替换目标。
提交前目标(哪怕已存在)保持不变。受控 I/O 失败(`--fail-after-output-bytes`
或 `OSError`)清理本轮临时文件与恢复日志,目标不动,stderr 输出
`E_COMMIT_FAILED` 单行 JSON,退出码 1。`--crash-after-output-samples` 用
`os._exit(3)` 模拟无法执行清理代码的强制终止,临时文件与日志按设计保留供恢复。

### 6. 崩溃恢复(要求 6)
每块处理后:先 flush+fsync 临时文件,再原子更新(临时文件+`os.replace`)恢复日志
`<out>.recover.json`。日志含:清单/时间轴/参数摘要 digest、处理阶段(1=混音求峰/
2=归一化写入,两阶段明确区分)、总帧数、已写输出采样数与字节偏移、峰值、
各输入轨已读位置。重启同一命令时校验 digest 与各偏移合法性,把临时文件截到
检查点偏移后续写;日志缺失/损坏/偏移非法(如负数)时作为 `E_LOG_CORRUPT`
**告警**记入最终报告,清理临时文件从头重算,成功退出码 0。完成后删除日志。
崩溃注入仅在全新运行(启动时无日志)时武装,保证"重启同一命令"必能完成。

### 7. 错误协议(要求 7)
不可恢复业务错误:stderr 单行 JSON `{"code","message"}`,退出码 1;
使用错误(未知子命令、非法参数):`E_USAGE`,退出码 2;成功:0。
已实现 code:`E_TIMELINE`(如 BPM=0)、`E_SAMPLE_RANGE`(清单显式整型样本值越界;
原始三字节输入的所有位模式均为合法 24bit 数,不触发此错)、`E_MISSING_INPUT`、
`E_LOG_CORRUPT`(仅作可恢复告警,不作为错误级 JSON)、`E_COMMIT_FAILED`、
`E_EVENT_MISMATCH`、`E_USAGE`。

### 8. 分数采样偏移(要求 9)
区间绝对位置 = 分段 BPM 累计(Fraction) + 分数采样偏移,**最后仅舍入一次**
(半值远离零)。偏移默认相对全局时间轴(`"offset_mode":"abs"`,缺省);
增量偏移必须由清单显式声明(`"offset_mode":"delta"`),按声明顺序在
Fraction 域累计,100 段每段 1/7 的末区间位置与直接累加 `100/7` 完全相等。
`positions` 子命令输出每区间的精确 Fraction 位置与舍入后采样点。

### 9. 峰值归一化(要求 10)
两阶段:阶段 1 流式混音写入 `<out>.tmp1` 并求全局峰值(饱和后样本的最大绝对值);
阶段 2 以 `Fraction` 增益回读 `.tmp1` 二次写入 `.tmp2` 后原子提交。
增益规则:`0 < peak < 8388607` 时 `gain = 8388607/peak`,否则 `gain = 1`
(负满幅 -8388608 的绝对值为 8388608,已达/超过满幅的饱和信号保持原样,
这保证削波测试的正峰恰为 8388607)。应用时逐样本 `round_half_away` 并饱和;
全零输入增益为 1,不除零,全零区间保持全零。
基准比较与 sidecar 每块 SHA256 均针对归一化后的最终母带。

### 10. validate(要求 11)
按 sidecar(格式/声道/块大小/每块 SHA256)逐块重算母带哈希,报告不一致块索引;
存在不一致退出码 1,全部一致退出码 0。每块写断点日志,
`--crash-after-blocks` 强制终止后重启同一命令从断点续算,最终报告与未中断一致。

### 11. 事件一致性(要求 12)
混音前(创建任何临时文件之前)检查:时间轴中引用轨道/区间的事件必须存在,
且按采样点排序后,区间覆盖 `[本事件位置, 下一事件位置)`;否则 `E_EVENT_MISMATCH`,
退出码 1,目标目录不产生任何新文件。

## 清单格式

```json
{
  "sample_rate": 48000,
  "tracks": [
    {"id": "vox", "file": "vox.pcm", "channels": 1,
     "gain": "3/4", "pan": "-1/2",
     "intervals": [
       {"id": "a", "start": "1/3", "offset": "1/7", "offset_mode": "abs",
        "length": 24000, "file_offset": 0},
       {"id": "b", "start": "8", "length": 1000, "constant": 500}
     ]}
  ]
}
```

- 分数可写 `"num/den"`、整数或 `[num, den]`;`start` 单位为四分音符。
- `offset`: 分数采样点偏移;`offset_mode` 缺省 `abs`(相对全局时间轴),
  `delta` 为增量偏移(须显式声明)。
- `length`: 区间帧数;`file_offset`: 文件内起始帧(缺省按声明顺序累计)。
- `constant`: 确定性常量区间(测试用),显式整型样本值,越界报 `E_SAMPLE_RANGE`。

## 验证结果(本机实测)

`python run_acceptance.py` — **23/23 通过**,覆盖任务书 12 项要求:

- T1 时间轴定位 48000/44100 与 Fraction 真值误差 0(含 1/3、5/7 拍事件)
- T2 极值三字节模式逐字节一致(`ff ff 7f`/`00 00 80`/`00 00 00`);
  ~50MB 与 ~25MB 输入峰值持有量均为 65536(=窗口),不随长度增长
- T3 混音与独立 Fraction 参照逐字节一致;块 1/17/4096 SHA256 相同;
  双满幅叠加输出全部落在 [-8388608,8388607],正峰恰 8388607
- T4 重叠 100 采样点后开始者覆盖(=1000)、间隙 200 采样点全 0、
  声明顺序颠倒 SHA256 不变;同起点时声明后者覆盖前者
- T5 占位目标被原子替换;注入失败后目标不变、无临时残留、
  退出码 1、stderr 为含 `E_COMMIT_FAILED` 的单行 JSON
- T6 `--crash-after-output-samples 5000` 后日志含合法偏移/采样数;
  重启同一命令输出与未中断 SHA256 一致且日志删除;
  日志偏移改负后重算成功(退出码 0,报告含 `E_LOG_CORRUPT` 告警)
- T7 `E_TIMELINE`/`E_SAMPLE_RANGE`/`E_MISSING_INPUT`/`E_COMMIT_FAILED`
  退出码 1,`E_USAGE` 退出码 2,均为单行 JSON
- T8 `window=512` 处理 100000 帧:持有量≤512、已处理范围不重现、
  与无窗口运行 SHA256 一致
- T9 偏移 1/3、2/5(及 3/5)位置与 Fraction 四舍五入真值一致;
  100 段 delta 1/7 末区间位置 == `100/7`,误差 0
- T10 峰值≤4194303 归一化后正峰恰 8388607;全零输入不除零仍全零;
  全零区间保持全零
- T11 改块后 validate 报告 `[3]` 且退出码 1;`--crash-after-blocks`
  断点续算最终报告与未中断一致
- T12 事件超出区间时长报 `E_EVENT_MISMATCH`(退出码 1,无任何新文件);
  修正后同命令成功

复现:`python testgen.py && python run_acceptance.py`
