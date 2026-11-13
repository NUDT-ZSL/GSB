# 播客精彩片段合集拼接工具(project21)

从长录音中按清单裁剪多个精彩片段,自动避开停顿边界,拼接为合集 WAV 并生成 JSON 报告。**仅使用 Python 标准库**(wave / array / json / argparse),Python 3.10+。

## 文件

| 文件 | 说明 |
|---|---|
| `podcast_concat.py` | 主程序(CLI 工具) |
| `make_samples.py` | 确定性样本生成器(无需外部素材) |
| `test_podcast_concat.py` | 验收测试套件(23 个用例,覆盖全部 15 条要求) |
| `sample_data/` | 生成的演示样本与输出(可随时删除后重新生成) |

## 本地启动方式

```powershell
# 1. 生成确定性样本(18 秒、16000Hz、16 位单声道,含 3 处停顿)
python make_samples.py

# 2. 拼接合集(输出 WAV + 同名 .report.json 报告)
python podcast_concat.py --input sample_data\sample.wav `
    --manifest sample_data\clips.json --output sample_data\highlights.wav

# 3. 运行验收测试
python test_podcast_concat.py -v
```

## 命令行参数(--help 可查)

| 参数 | 必需 | 说明 |
|---|---|---|
| `--input` | 是 | 输入 WAV(16 位单声道 PCM,8000~48000Hz,≤10MB) |
| `--manifest` | 是 | 片段清单 JSON,每项含 `name`/`start`/`end`(秒,可含小数,须 start<end) |
| `--output` | 否 | 输出 WAV,默认 `output.wav`;已存在则拒绝写入 |
| `--threshold` | 否 | 停顿阈值 dBFS,默认 -40,合法范围 -60 至 -20 |

报告固定输出为 `<输出名去扩展名>.report.json`,包含采样率、时长、停顿清单(按开始时间升序)、各片段原始/实际起止与警告列表,时间保留三位小数。退出码:0 成功,1 输入/文件错误,2 参数错误。

## 业务操作样例

```powershell
# 常规拼接(自动修剪与停顿重叠 >0.1 秒的片段,整段落入停顿的剔除)
python podcast_concat.py --input sample_data\sample.wav --manifest sample_data\clips.json --output out.wav

# 更严格的停顿判定(-50dBFS,低音量段不再记为停顿)
python podcast_concat.py --input sample_data\sample.wav --manifest sample_data\clips.json --output out2.wav --threshold -50

# 空清单:输出 0 秒合法 WAV 与空片段报告,不算错误
python podcast_concat.py --input sample_data\sample.wav --manifest sample_data\empty.json --output empty.wav
```

## 实际验证结果(本机 Python 3.10.20,Windows 11)

- **测试套件**:`python test_podcast_concat.py` → `Ran 23 tests ... OK`,退出码 0。23 个用例逐一对应 15 条验收标准(读取校验、10MB 上限、采样率边界、停顿检测精度 ±10ms、阈值 -50/-70、字段缺失与 start>=end、0.2 秒重叠修剪、三片段 3 秒拼接与边界渐变、报告字段/顺序/三位小数、空清单、32 位浮点与立体声拒绝、已存在拒绝且原文件不变、随机字节拒绝且无半成品、--help 四项与缺参报错、两次运行哈希一致、整段停顿内片段剔除、end 越界整单拒绝)。
- **端到端演示**:`sample.wav`(18 秒,停顿 2~4s / 9~10s / 12~13s)+ 5 个片段 → `highlights.wav` 10.600 秒;报告中 `middle-overlaps-pause` 因重叠 0.2 秒被修剪到 9.000,`lost-in-pause` 整段落入停顿被剔除并标注原因,`quiet-tail` 重叠 0.5 秒被修剪。
- **阈值差异**:同一输入 `--threshold -50` 时停顿从 3 处变为 2 处(12~13s 的 -44dBFS 低音量段不再计入),输出 11.100 秒;`--threshold -70` 报参数错误,退出码 2。
- **已有产物保护**:对已存在的 `highlights.wav` 重复运行 → `错误: 目标文件已存在,拒绝写入`,退出码 1,原文件不变。

## 设计要点

- **先校验后写出**:WAV 格式/大小/采样率、清单字段与越界全部校验通过后才开始处理;写出前检查输出与报告文件均不存在,任何失败都不产生半成品。
- **停顿检测**:逐样本扫描连续低于阈值(-40dBFS ≈ 幅度 328)的区段,严格超过 0.5 秒记为停顿,边界精确到样本。
- **修剪规则**:片段起点落在停顿内→起点移到停顿结束;终点落在停顿内→终点移到停顿开始;停顿完全位于片段内部→保留前段;修剪后长度 ≤0 → 剔除并在报告标注原因。
- **渐变过渡**:每个片段首尾各 10ms 线性渐强/渐弱,避免拼接处爆音,总时长不变。
- **确定性**:无时间戳、无随机数,同一输入重复运行输出字节一致(测试以 SHA-256 验证)。
