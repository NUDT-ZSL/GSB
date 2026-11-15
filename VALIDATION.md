# 实际验证记录

验证日期：2026-11-15

## 环境

| 项目 | 实际值 |
|---|---|
| 执行环境 | WSL2 Ubuntu 22.04.5 LTS（当前提供的运行环境） |
| Python | 3.10.12 |
| NumPy | 2.2.6 |
| ffmpeg | N-128909-g604ef3784d-20261114 |
| ffprobe | N-128909-g604ef3784d-20261114 |

当前容器不是原生 Windows，因此中断与路径处理的实际运行在 WSL Linux 完成。
代码未使用 POSIX 专属命令：FFmpeg 路径由 `shutil.which`/显式参数解析，
文件替换使用跨平台 `os.replace`，Windows 下可用 `Ctrl+C` 触发 `SIGINT`，
强制终止使用 `taskkill /PID <pid> /F`。原生 Windows 验收仍需按 README 在目标机复跑。

依赖检查实际通过：

```text
$ ffmpeg -version
ffmpeg version N-128909-g604ef3784d-20261114 ...
$ ffprobe -version
ffprobe version N-128909-g604ef3784d-20261114 ...
```

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

实际结果：

```text
Ran 15 tests in 18.742s
OK
```

覆盖并通过的断言：

- 负偏移返回 `INVALID_MANIFEST`，成品不创建。
- 声明 16 位但实际 24 位返回 `INPUT_MISMATCH`，成品不创建。
- 独立输出规格文件可按自身目录解析并正确合并。
- 44.1 kHz 源、100 ms 偏移、源内 250 ms 冲激映射到 48 kHz 成品第 `16800`
  个采样点，实测误差 `0`。
- 单声道复制到立体声、立体声左右独立映射、逐轨增益和叠加结果符合参考值。
- 单声道/立体声下混规则正确。
- 源时间静音在 100 ms 偏移后严格对应成品零样本区间，边界外第一个样本仍有声。
- 44.1 kHz -> 48 kHz 重采样后静音边界仍准确落在目标采样点。
- 100 ms 窗口的起止采样点、时间、总时长、窗口数和尾部静音窗口数值严格一致。
- `scale` 策略按预限幅峰值归一化，报告中的 `applied_scale` 一致。
- 并发 `1` 与 `4` 的成品逐样本完全相同。
- 快照参数变化后 `recovery.mode = restart`、复用轨道数为 `0`。
- 收到真实 `SIGINT` 后退出码为 `130`，状态为 `interrupted`，没有成品；
  重跑后以 `resume` 完成。
- 在一条轨道完成并持久化后执行强制终止，成品不存在；重跑以 `resume` 复用
  至少一条已完成轨道并成功产出。
- 超过内存预算的单个工作块返回 `RESOURCE_LIMIT`。

## 演示样本端到端结果

命令：

```bash
python3 tools/generate_samples.py --output-dir demo
PATH="$PWD/.tools:$PATH" python3 transcoder.py demo/manifest.json
```

样本包含 48 kHz 单声道 16 位、44.1 kHz 立体声 24 位、22.05 kHz 单声道
32 位三条轨道，成品为 48 kHz/双声道/16 位。

实际输出摘要：

```text
total_frames=108000
total_duration_ms=2250.0
sample_rate=48000
channels=2
bit_depth=16
window_frames=4800
window_count=23
overall_rms=0.1469980906432876
overall_peak=0.699999988079071
```

`ffprobe` 复核成品：

```text
codec_name=pcm_s16le
sample_rate=48000
channels=2
bits_per_sample=16
duration=2.250000
```

22.05 kHz 轨道在源时间 0.5 s 的冲激、1250 ms 轨道偏移，应在成品
`1.75 s = 84000` 采样点；实际最大值索引也是 `84000`，误差 `0` 个采样点。

状态文件最终阶段为 `complete`，三条轨道全部记录；再次执行会识别快照并返回
`recovered_complete`，不会再产生半成品。

## 清理

验证生成的 `demo/`、缓存和报告均被 `.gitignore` 忽略。仓库不包含 WAV 二进制附件。
