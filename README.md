# MVST — 多语言音视频母版确定性封装工具

纯 Python 标准库实现（Python ≥ 3.11，无第三方依赖、无网络），把分离的
25fps 恒定帧率视频轨、多条单声道 WAV、多条 SRT 字幕整合为带头部索引的
自定义二进制母版容器 `.mvst`，并产出确定性 JSON 校验报告。

## 命令

```powershell
python -m mvst validate --manifest manifest.json --root samples
python -m mvst package  --manifest manifest.json --root samples --out master.mvst --force
python -m mvst info master.mvst
python -m mvst selftest
```

可选 `--jobs N`（实际并发度取 `N` 与 CPU 核数的较小值）。

## 退出码契约

| 码 | 含义 |
|----|------|
| 0  | 成功 |
| 2  | 输入/业务错误，stderr 输出单行 JSON（`error_code` 以 `E_` 开头） |
| 3  | 内部错误（同样为单行 JSON，不泄露 Traceback） |
| 130 | Ctrl+C / CTRL_BREAK 中断，临时文件被清理，既有目标不被破坏 |

错误码：`E_WAV_FORMAT` `E_MANIFEST` `E_VIDEO_MISSING` `E_VIDEO_INVALID`
`E_SUB_FORMAT` `E_SUB_OVERLAP` `E_LANG_DUP` `E_LANG_PARTIAL` `E_LANG_MISSING`
`E_RESAMPLE` `E_CLIP_RISK` `E_TARGET_EXISTS` `E_MASTER_INVALID` `E_INTERNAL`。

## 关键设计

- **时间基统一（`mvst/timebase.py`）**：帧号、毫秒、采样点全部用
  `fractions.Fraction` 精确换算，无浮点。音频起始采样
  `round(offset_ms*48000/1000)`，字幕毫秒到帧 `round(ms*25/1000)`，
  半值采用远离零规则。音频对齐、字幕吸附、容器索引共用同一数值来源。
- **WAV（`mvst/wavfile.py`）**：手工遍历 RIFF 块，容忍 fmt/data 乱序、
  奇数块 1 字节填充与多余块；对每个声明块长度做越界校验；仅接受
  16-bit PCM 单声道。
- **重采样（`mvst/resampler.py`）**：加 Hann 窗 sinc 重采样
  （默认半窗 32、截止比 0.90，可覆盖）；增益在浮点域应用，再以饱和
  （非环绕）量化到 16-bit，维护裁剪计数、峰值与 6 位小数 RMS。每次调用
  独立缓冲，便于多线程并行。
- **字幕（`mvst/subtitles.py`）**：容忍 BOM、LF/CRLF；毫秒吸附到最近帧；
  重叠时把后一条起始推到前一条结束帧+1 并保留自身帧跨度，链式连续排布；
  推后越过下一条起始帧则丢弃并记 `E_SUB_OVERLAP`；单条时间非法记
  `E_SUB_FORMAT` 但不中断整批。
- **容器（`mvst/container.py`）**：魔数 `MVST` + 固定头 + 每轨索引项 +
  按 视频/音频/字幕 顺序的数据块。`info` 只读头部索引、绝不扫描数据块，
  因此篡改数据块不影响 `info`。封装先写同目录 `.tmp` 再 `os.replace`
  原子替换，中断时清理临时文件。
- **确定性（`mvst/pipeline.py`）**：固定顶层键序；同输入两次运行除
  `generated_at` 外完全一致；环境变量 `MVST_SHUFFLE=1` 打乱内部任务
  提交顺序，输出仍一致；`MVST_FAIL_TRACK=2` 注入指定轨重采样失败；
  `MVST_SLOW_WRITE=1` 为中断测试制造写入窗口。

## 容器布局

```
header  30B  <4sHIIIIII> 魔数/版本/母版采样率/视频帧数/音轨数/字幕轨数/总轨数/标志
index   26B × 总轨数  <BBHHIiiiI> 类型/轨号/语言码/offset_ms/速率/字节偏移/首单元/末单元/数据长度
blocks  每块 12B 块头 <4sBBHI> + 载荷，按 8 字节对齐
        顺序：视频块、音频块…、字幕块…
```

语言码：1=en 2=es 3=fr 4=ja（视频轨为 0）。音频块载荷为小端 16-bit PCM；
字幕块为 `(序号,起始帧,结束帧,UTF-8 文本)` 的定长头+变长文本序列。

## 测试与独立校验

```powershell
python -m mvst selftest                 # 内置 94 个确定性断言，覆盖 12 项需求
python tools/verify_container.py master.mvst --tamper-check
```

`tools/verify_container.py` 不导入本项目代码，独立解析头部、校验偏移边界，
并按索引逐点检查音频样本范围与无环绕符号翻转。

## 目录

```
mvst/            包源码（cli/pipeline/container/resampler/subtitles/...）
tools/           独立容器校验脚本
app.py           端到端确定性演示驱动
demo.run.json    录屏交付描述
samples/         运行 app.py 后生成的确定性样本与母版
```
