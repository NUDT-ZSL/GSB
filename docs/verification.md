# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

环境：

- WSL2 / Ubuntu 22.04 兼容内核：`Linux ... microsoft-standard-WSL2 ... x86_64`
- Python `3.10.12`
- NumPy `1.26.4`
- pytest `8.2.0`

## 1. 依赖安装

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

实际结果：

- `numpy>=1.24` 已满足，使用 `numpy 1.26.4`。
- `transcript-governance-0.1.0` editable wheel 构建并安装成功。
- 安装后 `transcript-governance --help` 可正常列出 `generate` 与 `diagnose` 子命令。

## 2. 固定种子样本生成

```bash
python3 scripts/generate_samples.py samples
```

实际结果：

- 种子：`421`
- 增益：`0.72`
- PCM：16 kHz、单声道、signed-int16 little-endian
- 时长：`7750 ms`
- PCM SHA-256：`0de77382c51ffaa25d5ae7cab38cc588101d9d5b5b08873e182dbff7954749e3`
- 生成 4 个语音段和 4 个静音段，以及 v1、v2 SQLite/JSON 和异常 v1 夹具。
- 同一脚本输出到 `/tmp/tg-generated-samples` 时得到相同 PCM 哈希。

## 3. 自动化测试

命令：

```bash
python3 -m pytest -q
```

实际结果：

```text
..........................                                               [100%]
27 passed in 1.02s
```

覆盖：

- PCM 固定种子确定性、帧长/帧移、能量、过零率和分位数。
- v1/v2 正常读取及 `E_FIELD_MISSING`、`E_DUPLICATE_UTTERANCE_ID`、`E_TIME_REGRESSION`、`E_INVALID_INTERVAL`、`E_FORMAT_HEADER`、`E_METADATA_MISMATCH`。
- v1 ↔ v2 迁移、补全来源、丢弃/展平策略和毫秒边界保持。
- confidence、revision、utterance_id 字典序三级裁决。
- 重叠失效、词级时间触发、依赖图传递失效。
- 增量裁决与全量重算的逐毫秒状态一致。
- `E_OVERLAP`、`E_GAP`、毫秒点查询和稳定指纹。
- 缓存命中/未命中、依赖链重放、区间定向失效。
- 快照版本/校验和冲突、恢复提交、线程并发下 `E_STAGE_BUSY`。
- 端到端 `run_diagnostic(...)` 报告总体通过。

## 4. 完整诊断

命令：

```bash
python3 examples/run_diagnostic.py
# 或
transcript-governance diagnose --samples samples --reports reports
```

实际结果：

- `overall_ok = true`
- `25/25` 项诊断检查通过，失败 `0`。
- 裁决结论：`covered=1`、`conflict=3`、`supplement=1`。
- 报告：`reports/diagnostic-report.md` 与 `reports/diagnostic-report.json`。

关键裁决样例：

| 重叠对 | 结论 | 实际优先级/胜方 |
| --- | --- | --- |
| `UTT-001__x__UTT-001` | `covered` | 规范化文本一致 |
| `UTT-002__x__UTT-002` | `conflict` | `confidence`，v1 `0.95` 对 v2 `0.84` |
| `UTT-003__x__UTT-003` | `conflict` | `revision`，v2 revision `4` 对默认 `1` |
| `V1-LEX-040__x__V2-LEX-040` | `conflict` | `utterance_id`，`V1-LEX-040` 字典序更小 |
| `UTT-050__x__UTT-050` | `supplement` | 部分重叠，保留两侧互补区间 |

## 5. 刷新传播实测

诊断中修订 v2 `UTT-001` 的 `text` 和一个词级时间：

- 变化字段：`["text", "words"]`
- 词级触发器：`word_time@510-700`
- 失效节点：`["v1:UTT-001", "v2:UTT-001"]`
- 复用节点：其余 8 个版本化 utterance 节点
- 重算裁决对：`UTT-001__x__UTT-001`
- 复用裁决对：4 个
- 增量与全量逐毫秒状态：一致

自动化测试 `test_incremental_adjudication_matches_full_recompute_ms_by_ms` 还比较了 `[0,300)` 共 300 个毫秒点，增量状态字典与全量状态字典完全相等。

## 6. 缓存与配置定向失效实测

将 VAD 能量分位参数从 `0.32` 调整为 `0.40` 后：

- 检测到变化帧区间：`[1080,1135]`、`[3110,3165]`、`[4850,4905]`、`[7200,7325]`。
- 与转写区间相交后仅影响：`V2-LEX-040`。
- 定向删除旧区间缓存对象：1 个。
- 新配置第三轮计算：4 个键命中、1 个键未命中并重算。
- 音频帧特征、VAD 全局产物、迁移和裁决等其他缓存对象未被全量清空。
- 转写、v1→v2/v2→v1 迁移和裁决产物均验证了首次未命中、同键第二次命中。
- 点查询 `ms=500` 的缓存依赖链重放成功，链长 3：`audio-frame-features -> vad-segments -> utterance-alignment`。

## 7. 一致性与快照实测

- 5 条 v2 样例以各自 utterance 区间作为声明区域时，一致性检查通过。
- 注入 `[500,700)` 的重复覆盖后返回 `E_OVERLAP`。
- 声明 `[240,1200)` 但移除 `UTT-001` 后返回 `E_GAP`。
- `ms=500` 命中 `UTT-001`；`ms=1500` 返回 `E_GAP`。
- 快照初始版本为 1，成功提交后到版本 2，恢复提交后最终版本为 3。
- 持有 stage 锁时第二个非阻塞 stage 立即返回 `E_STAGE_BUSY`；线程测试也验证了相同行为。
- 使用旧版本号提交返回 `E_SNAPSHOT_VERSION_CONFLICT`；使用错误基线校验和返回 `E_SNAPSHOT_CHECKSUM_MISMATCH`。

## 8. 复现命令汇总

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 scripts/generate_samples.py samples
python3 -m pytest -q
python3 examples/run_diagnostic.py
```
