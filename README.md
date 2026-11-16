# 可集成语音端点检测（VAD）领域组件

本地、确定性、无网络服务依赖的 VAD 组件与完整示例。输入为固定种子合成的
16kHz/单声道/16bit PCM WAV，输出语音段列表与可复现 JSON 诊断报告。

## 1. 环境与安装

- Python 3.11（实测 `Python 3.11.0rc1`），依赖仅 NumPy 与 soundfile（均本地计算，无服务调用）。
- 系统 Python 无 pip 时的用户级引导方式（无需 sudo）：

```bash
curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
python3.11 /tmp/get-pip.py --user
python3.11 -m pip install --user -r requirements.txt
```

不使用 Docker / 容器；运行前组件会自动创建输出目录，要求工作区可写。

## 2. 目录结构

```
configs/vad_config.json          # 帧长/帧移、门限、迟滞、最短段、噪声窗、并发数等
configs/injection_manifest.json  # 按样本名+帧序号的异常注入清单
scripts/generate_samples.py      # 固定种子确定性 WAV 生成器（含 3 个故意不合规文件）
src/vad/
  errors.py        # 固定业务码
  util.py          # SHA-256 哈希、规范化 JSON、临时文件+os.replace 原子写
  config.py        # 配置加载与 config_hash
  audio.py         # 容器校验、读取、确定性分帧（补零标志、整数毫秒时间戳）
  features.py      # 对数能量/过零率/频谱平坦度（float64）、自适应噪声底
  state_machine.py # 四状态双门限迟滞状态机（显式判据、固定优先级）
  postprocess.py   # 端点合并、最短段丢弃、短间隔合并、首尾裁除
  injection.py     # 真实输入扰动（pop/clip/mute/nonfinite）与 manifest_hash
  snapshot.py      # 阶段快照、版本与哈希校验
  pipeline.py      # 单样本端到端流水线（域错误进失败清单，不崩整个运行）
  runner.py        # 扫描、串行/线程池并发比对、快照游标、信号处理、原子报告
  cli.py           # 命令行入口与退出码
tests/test_vad.py  # 13 个 unittest 用例，覆盖 8 项业务要求
```

## 3. 一键运行

```bash
bash scripts/run_demo.sh
# 或分步：
PYTHONPATH=src python3.11 scripts/generate_samples.py
PYTHONPATH=src python3.11 -m vad.cli --force-restart
PYTHONPATH=src python3.11 -m unittest tests.test_vad -v
```

CLI 参数：`--input/--output/--config/--manifest/--force-restart/--graceful-after N`
（`--graceful-after` 为测试钩子，处理 N 个样本后走与真实信号完全相同的优雅退出路径）。

## 4. 关键设计与业务码映射

| 业务码 | 触发位置 | 含义 |
| --- | --- | --- |
| `E_AUDIO_FORMAT` | `audio.load_wav` / pipeline 兜底 | 容器、采样率、声道、位深不合规，进入失败清单 |
| `E_NONFINITE_SAMPLE` | 读取/注入后校验 | 出现 NaN/Inf，样本失败但运行继续 |
| `E_SEG_TOO_SHORT` | `postprocess` | 短于最短语音段的区间丢弃，写入可追溯 audit |
| `E_GAP_MERGED` | `postprocess` | 短于最短静音的间隔并入相邻语音段 |
| `E_CONFIG_MISMATCH` | `snapshot.validate_snapshot` | 快照版本号或配置/清单哈希不一致，拒绝恢复（退出码 3） |
| `E_SNAPSHOT_CORRUPT` | `snapshot.read_snapshot` | 快照文件损坏 |
| `E_INJECTION_FRAME` | `injection` | 注入帧越界 |

- **分帧**：帧号从 0 开始；`timestamp_ms = frame_index * frame_hop_ms`，纯整数运算；
  末帧补零并置 `zero_padded=true`。
- **特征**：`log(sqrt(mean(x^2)))` 对数能量、过零率、`GM/AM` 频谱平坦度（去直流、Hann 窗），
  全部 float64，按 `feature_decimals` 四舍五入落盘；数字静音使用固定哨兵值，无随机源，
  平稳底噪两次运行逐位一致（测试已断言）。
- **状态机**：`SILENCE / SUSPECT / SPEECH / TRAILING`，低门限=噪声底+迟滞，高门限与释放门限
  来自配置；每帧输出 `previous_state/state/reason`。边界相等按固定优先级 P1 高门限 →
  P2 低门限+低平坦度 → P3 低门限+低过零率；转移只依赖当前状态与当前帧特征。
- **并发一致性**：线程池大小取 `max_workers`，任务带文件名排序键；结果按提交索引归位后，
  再与单线程字典序结果做逐字段相等比较，摘要写入报告。
- **快照/恢复**：每完成一个样本即把阶段、版本号、config_hash、manifest_hash、游标与部分摘要
  经临时文件 + `os.replace` 原子落盘；恢复先校验版本与两个哈希，通过后仅处理游标外样本，
  最终结果与不中断运行完全一致。
- **报告**：`data/output/diagnostic_report.json` 原子写盘，含语音段、帧状态轨迹摘要、噪声底、
  注入命中、并发比对、快照版本、两个哈希与失败清单。
- **退出语义**：SIGINT/SIGTERM 时完成当前样本、落盘快照与中断报告后以退出码 0 退出；
  强制终止（SIGKILL 等）后仅依据已持久化快照恢复。

## 5. 实测验证结果（本机）

- 8 个 WAV：4 个成功、4 个失败（3 个 `E_AUDIO_FORMAT` + 1 个 `E_NONFINITE_SAMPLE`）。
- 分段与合成真值吻合：`sample_b` 2 段、`sample_c` 2 段（含 1 次 `E_GAP_MERGED`）、
  `sample_d` 1 段（含短段丢弃与间隔合并）、纯底噪 `sample_a` 0 段。
- 4 个注入项全部命中并记录帧号；非有限注入在读取阶段被拒，运行未崩溃。
- 并发与串行 `field_level_equal=true`，样本/帧/段/失败清单顺序完全一致。
- 优雅中断（3 个样本后）退出码 0，恢复后报告与不中断运行逐字段一致。
- 篡改配置后恢复被拒：`E_CONFIG_MISMATCH`，退出码 3。
- 两次全新运行的报告 SHA-256 完全相同（字节级可复现）。
- `python3.11 -m unittest tests.test_vad`：13/13 通过。
