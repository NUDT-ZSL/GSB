# spkvc-repro

面向多说话人语音转换的本地确定性算法与可复现实验环境。项目只依赖 Python 3.10+ 与 NumPy，不访问网络、不使用容器，也不需要真实账号或外部数据。

核心验证目标：

- 固定种子生成 16kHz 单声道 PCM、参考音色片段、JSONL 清单与 20ms 逐帧真值。
- 五阶段转换链路：帧级特征 → 音色嵌入 → 发音单元对齐 → 转换参数映射 → 逐帧波形合成。
- 四类内容寻址缓存：帧特征、音色嵌入、转换参数、合成 PCM。
- 参考音色 `ref_revision` 演进时只失效必要后缀；映射参数版本变化时只重算参数与 PCM。
- 五阶段快照版本化、原子提交、强制终止恢复、回滚/损坏防护与非阻塞并发提交锁。
- 输入校验、资源上限、降级标记与跨模块一致性自检。
- 真值只用于帧对齐校验与独立误差观测，不进入音色嵌入或对齐决策。

## 快速开始

```bash
python --version
python -m pip install -r requirements.txt

python -m spkvc generate --out data --force
python -m spkvc run --data data --state-dir .spkvc --out .spkvc/outputs
python -m spkvc selfcheck --data data --state-dir .spkvc
python -m spkvc evaluate --data data --state-dir .spkvc
python -m spkvc cache --data data --state-dir .spkvc --utterance-id utt-001
```

增量示例：

```bash
# 仅映射参数版本变化
python -m spkvc incremental --data data --state-dir .spkvc \
  --mapping-version map-v2 --mapping-param f0_strength=0.55

# 仅某参考音色到达更高 revision
python -m spkvc incremental --data data --state-dir .spkvc \
  --ref-revision ref-spk-02=2
```

强制终止实验：

```bash
python -m spkvc run --data data --state-dir .spkvc-crash \
  --crash-after-stage 1
python -m spkvc run --data data --state-dir .spkvc-crash

python -m spkvc run --data data --state-dir .spkvc-mid \
  --crash-mid-stage 2 --crash-after-utterances 3
python -m spkvc run --data data --state-dir .spkvc-mid
```

完整复现实验并输出验证报告：

```bash
python scripts/run_experiment.py
python -m unittest discover -v
```

## 目录

```text
spkvc/
  dataset.py     固定种子样本、参考片段、真值与清单生成
  dsp.py         五个 DSP 阶段的确定性实现
  cache.py       四类节点内容寻址缓存与显式键构造
  snapshots.py   阶段快照、原子写入、校验和与非阻塞锁
  pipeline.py    状态机、恢复、增量失效、资源控制
  selfcheck.py   跨模块一致性自检
  evaluation.py  真值误差观测
  cli.py         命令行入口
tests/           32 项单元/集成测试
scripts/run_experiment.py
docs/TECHNICAL.md
reports/verification.json
```

## 实测结果

结果由 `python scripts/run_experiment.py` 在 Python 3.10.12 / NumPy 1.26.4 上生成，完整数据见 `reports/verification.json`。

| 验证项 | 结果 |
|---|---|
| 不间断完整运行与同状态重跑 PCM 一致 | 通过 |
| 阶段提交后 `os._exit(86)`，从下一阶段恢复 | 通过，`resumed_from_stage=2` |
| 阶段中途 `os._exit(87)`，丢弃未提交状态并从阶段起点恢复 | 通过，`resumed_from_stage=2` |
| 映射版本增量只重算 `params` 与 `synthesis` | 通过，16 个受影响节点全部重算，其余 24 个节点复用 |
| 参考 revision 增量只影响 `ref-spk-02` 的 `utt-001/utt-008` | 通过，特征全部复用 |
| 增量提交中途强杀后旧五阶段快照保持完整 | 通过，`version_counter` 仍为 5 |
| 恢复/增量后 PCM 与对应不间断运行逐字节一致 | 通过 |
| 缓存键组成、产物哈希、四类节点查询 | 通过 |
| 五阶段集合一致、重算/复用互斥覆盖、PCM 独立复算 | 通过 |
| 真值独立误差观测帧数一致 | 通过，298 帧；F0 MAE 39.06Hz |
| 测试套件 | 32/32 通过 |

基线快照版本为 `features=1, timbre=2, alignment=3, params=4, synthesis=5`；一次增量整体替换后依次为 `6..10`。

## 关键语义

- 帧数公式：`frame_count = 1 + (sample_count - frame_samples) // hop_samples`；不足一帧返回 0。
- 默认配置为 16kHz、`frame_ms=20`、`hop_ms=20`，与 20ms 真值逐帧对齐。
- 音色嵌入为 7 维统计向量并显式做逐参考片段 L2 归一化。
- 对齐只使用目标 `ref_id` 的参考片段派生单元，不读取真值；同分按 `speaker_id`、`phoneme_id` 升序。
- 特征缓存键刻意不含 `ref_revision`：特征只依赖源 PCM 与帧配置，因此参考演进时完整特征前缀可复用。
- 下游缓存键包含 `ref_revision`、参考 PCM 哈希、帧长/帧移、映射版本/配置哈希、上游产物哈希。
- 每个阶段只在全部 utterance 完成后提交；快照 payload 先落盘，索引 bundle 再以临时文件 + `os.replace` 整体替换。
- 增量提交使用一次 bundle 整体替换；中途终止不会暴露混合阶段状态。
- 并发提交使用非阻塞文件锁，后到者立即返回 `E_STAGE_BUSY`。

更详细的算法公式、缓存键表、错误码和恢复流程见 `docs/TECHNICAL.md`。

