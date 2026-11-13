# 播客单集合成器（Python 标准库）

按 JSON 清单把多段 **16 位 PCM、单声道、44100 Hz** 的 WAV 片段合成为单集音频，
同时输出包含成功片段、问题清单与静音区间的 UTF-8 JSON 报告。

仅依赖 Python 3.10+ 标准库（`json` / `wave` / `array` / `decimal` / `argparse`），
无网络、无第三方包、无容器。

## 文件

| 文件 | 说明 |
| --- | --- |
| `podcast_assembler.py` | 主程序（CLI + 可导入的函数） |
| `generate_samples.py` | 确定性生成演示 WAV 与清单 |
| `verify_demo.py` | 独立复核演示输出（用 `wave` 重读并逐采样比对） |
| `run_demo.py` | 一键执行「生成 → 合成 → 复核」全流程 |
| `test_podcast_assembler.py` | 验收测试（`unittest`，31 项） |
| `demo/` | 生成物：片段、清单、输出 WAV 与报告 |

## 启动方式

```powershell
# 在项目根目录执行
python --version          # 需要 3.10 或更高
python -m unittest -v test_podcast_assembler.py   # 31 项验收测试
python run_demo.py                               # 一键演示 + 复核
```

演示全部通过时最后一行是 `DEMO RESULT: PASS`，退出码 0。

也可以直接按用户方式手工运行：

```powershell
python generate_samples.py
python podcast_assembler.py demo/mixed_manifest.json `
    -o demo/out/mixed_episode.wav -r demo/out/mixed_report.json
python verify_demo.py
```

## 清单格式

```json
{
  "segments": [
    {"path": "segments/clip_a.wav", "start": 0.0},
    {"path": "segments/clip_b.wav", "start": 4.0,
     "fade_in_ms": 100, "fade_out_ms": 100}
  ]
}
```

- `segments` 必须是非空数组，否则整体终止：退出码 2，stderr 含 `invalid manifest`。
- `path`：非空字符串，相对路径按**清单文件所在目录**解析（也可写绝对路径）。
- `start`：非负秒数（JSON 小数以 `Decimal` 解析，避免二进制浮点漂移）。
  负数、缺失、布尔值、非数字 → `invalid manifest`，退出码 2。
- `fade_in_ms` / `fade_out_ms`：可选，非负毫秒；`null` 视作未启用。
  格式非法写为该片段的问题，不影响其他片段。

## 处理规则

| 场景 | 行为 |
| --- | --- |
| 文件不存在 | 问题 `missing`，跳过该片段 |
| 不是 16 位 PCM / 单声道 / 44100 Hz / 有压缩 / 空文件 / 截断 | 问题 `invalid_format`，跳过 |
| 时间区间与已接受片段重叠 | 问题 `overlap`，保留清单中先出现者，跳过后来者 |
| 同一路径 + 同一起始秒重复 | 问题 `duplicate`，仅保留首个 |
| 任一侧淡入/淡出 > 片段时长一半 | 问题 `fade_too_long`，跳过 |
| 全部片段都无效 | 仍退出 0，写出 0 帧 WAV + 全部问题 |

- 起始秒换算：`round(start * 44100)`（0.5 远离零方向），0.001 s = 44 采样。
  同一路径同一 `start` 的比较按数值，因此 `4` 与 `4.0` 视为重复。
- 区间为**左闭右开** `[start, start + 帧数)`：上一段结束正好等于下一段开始不算重叠。
  插值位置按 `round(start*44100)`，相邻片段因此不会互相污染。
- 淡入第 `i` 个采样增益 `i/N`（首采样为 0），淡出尾部第 `k` 个增益
  `(M-k-1)/M`（末采样为 0）；线性、对称四舍五入。渐变区之外的采样**原样复制**。
- 输出 WAV 为 16 位 PCM 单声道 44100 Hz，总长 = 最晚结束采样，空隙补零。
  成功片段按起始采样升序写入，与报告顺序一致（清单乱序不影响结果）。
- 静音区间：首片段之前、相邻成功片段之间、末片段之后；精度 0.001 s，升序。
- 报告问题只写文件，stderr 只给摘要（`N issue(s)`），退出码 0。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 合成完成（可能有片段级问题，见报告） |
| 1 | 输出 WAV / 报告写入失败 |
| 2 | 清单本身无效（整体终止，不产生输出） |

## 实际验证结果

`python run_demo.py` 的实测输出（Python 3.10.20，Windows 11）：

```
[OK] generate deterministic samples (exit 0, expected 0)
[OK] assemble clean_manifest.json (exit 0, expected 0)
       stdout: wrote 3 segment(s) to ...\demo\out\clean_episode.wav (5.000 s)
[OK] assemble mixed_manifest.json (exit 0, expected 0)
       stdout: wrote 3 segment(s) to ...\demo\out\mixed_episode.wav (5.900 s)
       stderr: podcast: 5 issues - see ...\demo\out\mixed_report.json
[OK] assemble precision_manifest.json (exit 0, expected 0)
       stdout: wrote 1 segment(s) to ...\demo\out\precision_episode.wav (0.401 s)
[OK] reject invalid_manifest.json (exit 2, expected 2)
       stderr: error: invalid manifest: segment 0: 'start' must be a non-negative number of seconds
```

`verify_demo.py`（独立重读输出）全部 PASS，要点：

- 干净清单（清单顺序 4s/0s/2s 乱序）：输出 5.0 s、报告顺序 0/2/4 s，
  逐采样等于三段源文件，静音区间 `[(1,2),(3,4)]`。
- 混合清单：5 类问题依次为
  `duplicate / missing / invalid_format / fade_too_long / overlap`（退出码 0），
  输出 5.9 s；1–4 s 全零静音；带 100 ms 淡入淡出的片段首尾为 0 且渐变区外逐采样一致；
  中文名片段在 5.5 s 处逐采样一致；报告 UTF-8 中文可读。
- 精度清单：`start=0.001` → 44 采样，前 44 采样全零，`out[44] == source[0]`。
- 2 s 起 0.5 s 片段：总帧数 110250（2.5 s），空隙为 0。

`python -m unittest test_podcast_assembler.py`：`Ran 31 tests ... OK`，
覆盖上述全部验收项，另含空清单、负 `start`、布尔 `start`、BOM/CRLF 清单、
`fade: null`、全片段无效、输出不可写等边界。

## 设计说明（关键取舍）

1. **清单级 vs 片段级错误分离**：`ManifestError` 在读取阶段抛出，CLI 立即以 2 退出；
   片段问题在 `assemble()` 中以 `Issue(code, description)` 收集，永不改变退出码。
2. **冲突求解按清单顺序**：一次遍历中先登记 `(规范化绝对路径, start Decimal)` 以便翻重，
   再在已接受集合中查区间重叠；因此"先出现者优先"对 duplicate 与 overlap 都成立。
   接受列表最后按起始采样排序，实现输出与报告同序。
3. **十进制时间**：清单读取 `parse_float=Decimal`，采样换算用精确十进制四舍五入；
   杜绝 `0.1 + 0.2` 一类误差导致的边界抖动（例如 0.001 s 恰好 44.1 → 44）。
4. **采样保真**：只在淡入淡出索引上写增益，其余采样区间通过
   `output[a:b] = samples` 原样拷贝；空白缓冲由 `array('h', bytes(n*2))` 生成为 0。
5. **`wave` 写出后即验证**：报告中的起止秒由**采样位置**反向换算并量化到 0.001 s，
   而不是直接复述清单值，便于和输出 WAV 对账。

## 业务操作样例

1. 剪辑师把 `segments/` 下的录音按 `demo/mixed_manifest.json` 编排，其中一条重复、
   一条文件没拿到、一条是立体声导出、一条 100 ms 片段要求 60 ms 淡入淡出、
   一条与其他条重叠——运行后得到 `mixed_episode.wav` 与 `mixed_report.json`，
   stderr 只提示 `podcast: 5 issues`，退出码 0，节目照常可发。
2. 需要精确对轴的旁白：清单写 `"start": 0.001`，工具放在第 44 个采样上（0.001 s），
   前 44 个采样为静音，方便与视频音轨对拍。
3. 片头片尾淡化：`"fade_in_ms": 100, "fade_out_ms": 100`，首末采样归零，
   中间原样保留；淡入淡出超过片段一半时整段报告为 `fade too long` 并跳过。
4. 中文素材：清单与文件名均可含中文（如 `片段素材/开场白.wav`），
   报告以 UTF-8（`ensure_ascii=False`）写出，`json.load` 打开无乱码。
