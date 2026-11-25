# Audio QC First Slice

本地确定性的 16 kHz 单声道音频质检首个可运行组件。它不访问网络或真实音频，使用固定种子生成 PCM 与 v1/v2 量化门限，并完成分块包络、门限迁移对齐、漂移裁决、内容缓存、四阶段快照恢复、并发提交控制和追回放。

## 快速运行

```bash
python3 -m pip install -r requirements.txt

# 生成固定种子样本到 samples/
python3 scripts/generate_samples.py

# 一键执行硬退出、恢复、一次性结果比对、迁移和回放验收
python3 -m audio_qc.cli acceptance --workdir artifacts/acceptance

# 单独运行串行/整段自检与双代门限迁移检查
python3 -m audio_qc.cli verify --sample-dir samples \
  --report artifacts/acceptance/recovered-report.json

# 完整测试
python3 -m pytest -q

# 或执行上述全部步骤
bash scripts/validate.sh
```

也可以直接使用根入口：

```bash
python3 main.py acceptance --workdir artifacts/acceptance
```

## 手动强制终止恢复

使用全新状态目录，让进程在计算并持久化前 3 个分块后以 `os._exit(42)` 硬退出：

```bash
python3 -m audio_qc.cli run \
  --sample-dir samples --threshold v1 \
  --state-dir /tmp/audio-qc-crash-demo \
  --crash-after 3
echo "hard exit code: $?"   # 42

python3 -m audio_qc.cli run \
  --sample-dir samples --threshold v1 \
  --state-dir /tmp/audio-qc-crash-demo
```

恢复摘要中 `envelope_reused_cache_count` 为 `3`，对应分块 `[0, 1, 2]`，其余 `1196` 块从阶段起点继续计算；四阶段快照全部提交后再次运行会直接加载已提交报表。

## 确定性样本

固定种子为 `20261126`，样本参数：

- 采样率：`16000 Hz`
- 声道/格式：单声道、signed-int16 little-endian
- 时长/样本数：12 秒、`192000` samples
- 帧长/帧移：`320` samples（20 ms）/ `160` samples（10 ms）
- 完整帧数：`1199`
- PCM SHA-256：`aeaedb386def4fa9842051c6657c7a7bb9e4bde414eeabf7ca691cd8eb27ee79`

PCM 包含起始静音、正常稳态语音、增益跳变、间歇静音、频谱子带偏移和恢复段。生成文件：

- `samples/sample_16k_mono_s16.pcm`
- `samples/sample_metadata.json`
- `samples/thresholds_v1.json`：扁平表
- `samples/thresholds_v2.json`：分片嵌套 JSON

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `QC_OK` | 流水线成功且无漂移 |
| `QC_DRIFT_DETECTED` | 流水线成功并检出漂移，漂移明细在 `events` |
| `QC_VERIFY_OK` | 自检、迁移一致性或回放成功 |
| `DRIFT_ENERGY` | 能量/RMS 相对门限目标超差 |
| `DRIFT_SUBBAND` | 某个小波子带比例或 RMS 上限超差 |
| `DRIFT_DISTRIBUTION` | 子带分布距离超过容差 |
| `NO_DRIFT` | 当前分块未漂移 |
| `E_VERSION_UNSUPPORTED` | 门限主版本不是 1 或 2 |
| `E_FORMAT_CORRUPT` | 字段缺失、类型错误或数值不可解析 |
| `E_TIMELINE_OVERLAP` | 门限时间窗在样本时间轴上重叠 |
| `E_STAGE_BUSY` | 已有门限/裁决提交持锁，后到者立即得到此码 |
| `E_MEMORY_BUDGET` | 驻留统计量或内存预算超限，已降级串行流式处理 |
| `E_RESOURCE_LIMIT` | 并行分块或缓存条目预算超限，已降级串行处理 |
| `E_INCONSISTENT` | 串行/整段、float64/定点或迁移判定不一致 |
| `E_HASH_MISMATCH` | 快照、缓存或回放记录校验和不匹配 |

错误以 `QCError(code, message, details)` 抛出；CLI 捕获后输出包含 `code` 的 JSON 并返回非零退出码。预算降级时主流程可继续完成，报表和返回值同时提供顶层 `budget_code` 与完整 `budget_plan`。

## 代码地图

- `audio_qc/dsp.py:43`：精确样本帧边界、短时能量、三级 Haar 小波子带统计。
- `audio_qc/samples.py:273`：固定种子 PCM、v1 扁平表和 v2 分片配置生成器。
- `audio_qc/thresholds.py:293`：版本/字段集分派、解析校验、统一门限曲线和双向迁移。
- `audio_qc/detect.py:233`：时间轴对齐、float64/`int64 × 1e6` 双档裁决、事件哈希与回放。
- `audio_qc/storage.py:67`：非阻塞提交锁、原子 JSON、内容寻址分块缓存和快照存储。
- `audio_qc/resources.py:52`：驻留块、内存字节、缓存条目和并行块预算。
- `audio_qc/pipeline.py:227`：四阶段状态机、缓存键、替换计划和恢复流程。
- `audio_qc/verify.py:16`：分块串行与整段一次性路径比对，输出首个偏差位置。
- `audio_qc/acceptance.py:37`：真实子进程硬退出与恢复等价性验收。
- `tests/test_core.py`、`tests/test_pipeline.py`：22 个自动化测试。

## 实际验证结果

最近一次本地环境为 Python `3.10.12`、NumPy `1.26.4`：

- `python3 -m pytest --junitxml=/tmp/audio-qc-junit.xml -q`：JUnit 记录 `22` 项测试、`0` 失败/错误，耗时约 `15.02s`。
- `python3 -m audio_qc.cli acceptance --workdir artifacts/acceptance`：`ok=true`、`code=QC_VERIFY_OK`。
- 硬退出子进程退出码：`42`；退出后不存在 `current.json`，但有 `3` 个原子缓存分块。
- 恢复时复用分块 `[0, 1, 2]`，重算 `1196` 块；恢复结果与整段一次性处理的事件流完全相等。
- 共处理 `1199` 个包络块，检出 `503` 个漂移事件：`202 DRIFT_ENERGY`、`301 DRIFT_SUBBAND`。
- 包络哈希：`c514675fd75ab881dd47aeed0f06956bb500d1511dbcaa1da217b8a5bde84986`。
- 漂移事件数组哈希：`597dc57a8ca72872973a0cf9ffd17b792765e10969fb8d4bf88e3884eaeb637d`。
- v1→v2 与 v2→v1 各比较 `1199` 个对齐块的能量门限判定，全部一致。
- 报表内 `503` 条事件均可从 `replay_input` 和 `precomputed_hash` 重放出相同类别与样本区间。

完整机器可读摘要位于 `artifacts/acceptance/acceptance_summary.json`，设计细节见 `docs/TECHNICAL.md`。
