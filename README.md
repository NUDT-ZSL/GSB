# Meeting VAD Local

面向会议同传场景的本地语音活动区间编辑与回放应用。项目只使用固定种子、显式配置和本地 16 kHz 单声道 PCM，不依赖账号、云服务、GPU 或容器。

核心能力：

- 五个确定性 DAG 节点：分帧包络 → 活动初判 → 边界精修 → 说话人归属 → JSONL 导出。
- 内容寻址节点快照，缓存键与快照条目均可输出可读组成明细。
- 标注修正、底噪分位调整、帧移变化时的增量失效与区间局部复用。
- 每次增量结果自动和无缓存全量重算逐字节对照，不一致返回 `E_INCONSISTENT`。
- 导出清单按当时缓存键和精确快照代次回放，逐项校验起止、归属、置信区间。
- 快照代次整体提交、不可变、单调递增；回退尝试返回 `E_SNAPSHOT_ROLLBACK`。
- 本地终端交互应用，展示节点状态、重算/复用统计、回放查询和最近业务错误码。

## 环境与安装

要求 Python 3.10+、NumPy、pytest。以下命令同时适用于 Windows PowerShell 和 Linux/macOS：

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[test]"
```

Linux/macOS 将激活命令替换为 `source .venv/bin/activate`。当前项目在 Python 3.10.12、NumPy 1.26.4、pytest 8.3.3 下验证通过；实现只使用 `pathlib` 和标准库文件操作，无平台专用路径。

## 快速开始

```powershell
# 1. 生成确定性本地样本：样本清单、标注清单、PCM/WAV
python scripts\generate_samples.py

# 2. 运行测试
pytest -q

# 3. 单次分析并输出状态
python -m meeting_vad --check

# 4. 交互应用
python -m meeting_vad
```

Linux 下将反斜杠替换为 `/`。样本默认写入 `samples/`，运行期快照、回放记录和当前标注写入 `runtime/`。

交互命令：

| 命令 | 说明 |
| --- | --- |
| `status` | 节点状态、快照版本、重算统计、最近错误 |
| `analyze` | 执行缓存感知分析 |
| `edit <segment_id> <speaker_label> [revision]` | 修正标注并触发增量重算 |
| `quantile <q>` | 调整底噪分位 |
| `frame <frame_ms> <hop_ms>` | 调整帧长与帧移 |
| `replay [cache_key]` | 按缓存键回放；省略时回放最新清单 |
| `replays` | 列出本地回放记录 |
| `key <node>` | 输出节点缓存键组成明细 |
| `snapshot` | 输出当前快照清单 |
| `export` | 输出最新 JSONL |
| `last` | 输出最近业务错误 |

每次配置变化会持久化到 `runtime/config.current.json`，标注修正会持久化到 `runtime/annotations.current.jsonl`，因此交互应用重启后可恢复一致状态。

## 样本与确定性约定

- 合成配置：`config/synthesis.json`。
- 应用配置：`config/app.json`。
- 全局种子：`20260101`。
- 每个 clip 的种子：`global_seed + seed_offset`。
- 底噪随机流种子：`clip_seed + noise_seed_offset + noise.seed_salt`。
- 合成信号为多谐波语音样信号，带确定性幅度调制与首尾淡入淡出；再叠加平稳高斯底噪。
- PCM 固定为 16 kHz、单声道、16-bit little-endian。
- 样本清单包含 `clip_id/sample_rate/duration_ms/seed/frame_ms`，并附加音频路径与 SHA-256。
- 标注清单字段包含 `clip_id/segment_id/start_ms/end_ms/speaker_label/label_revision/confidence`。

重新运行 `scripts/generate_samples.py` 会得到逐字节相同的清单与 PCM/WAV。

## DAG 与增量语义

| 节点 | 主要输入 | 主要输出 |
| --- | --- | --- |
| `envelope` | PCM 分块哈希、帧长/帧移、平滑参数 | 逐帧能量、过零率、样本边界、帧数 |
| `vad` | 包络、底噪分位、双门限、最短静音 | 候选活动区间 |
| `refine` | 候选区间、搜索窗口、能量斜率 | 精修区间 |
| `speaker` | 精修区间、声学统计、受控标签、标注哈希 | 归属与置信区间 |
| `export` | 说话人区间、受控标签、导出版本 | 规范化 JSONL 清单 |

失效规则：

| 变化 | 复用节点 | 重算节点 | 受影响区间 |
| --- | --- | --- | --- |
| 标注修正 | `envelope/vad/refine` | `speaker/export` | 被修正标注覆盖的区间；其他 `speaker` 区间记录复用 |
| 底噪分位调整 | `envelope` | `vad/refine/speaker/export` | 当前全部 clip 时长 |
| 帧长或帧移变化 | 无 | 全部节点 | 当前全部 clip 时长 |

缓存键与内容哈希：

```text
content_hash = SHA256(canonical_json(payload))
cache_key    = SHA256(canonical_json({
  "node": node,
  "node_version": node_version,
  "key_details": key_details
}))
```

`key_details` 按依赖范围纳入音频分块哈希、帧长/帧移、门限参数及版本、标注修正版本与内容哈希、上游节点内容哈希等。导出键额外纳入音频内容总哈希，确保可追溯。

每次增量完成后，应用会忽略所有缓存重新执行五节点并比较 JSONL 字节；出现差异时返回 `E_INCONSISTENT`，并报告首个差异区间。

## 快照与回放

快照目录结构：

```text
runtime/snapshots/
  CURRENT.json
  v000001/
    manifest.json
    entries/
      envelope/<cache_key>.json
      vad/<cache_key>.json
      refine/<cache_key>.json
      speaker/<cache_key>.json
      export/<cache_key>.json
  v000002/
    ...
```

- 每个条目保存节点名、节点版本、缓存键、上游哈希、完整键组成、负载、内容哈希和记录校验和。
- 一次分析的所有节点产物组成完整代次，先写入 staging 目录，再原子切换 `CURRENT.json`；不修改旧代次。
- 当前代次条目数受 `resources.max_snapshot_entries` 限制，包络驻留帧数受 `resources.max_envelope_frames` 限制。
- 旧代次保留用于历史回放，但活动版本只能递增；请求旧代次或旧配置版本返回 `E_SNAPSHOT_ROLLBACK`。
- 回放记录位于 `runtime/replay/records/` 与 `runtime/replay/exports/`，记录导出键、各节点精确代次、输入版本、JSONL 哈希和内容。
- 回放会重新加载完整 lineage、校验每级上游哈希、从说话人快照重新导出，并逐字节、逐项比较。缺文件返回 `E_LINEAGE_BROKEN`；内容被篡改返回 `E_CACHE_CORRUPT`。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_SAMPLE_RATE` | 采样率不是 16000 |
| `E_FRAME_MS` | 帧长非法 |
| `E_FRAME_SHIFT` | 帧移超过帧长 |
| `E_FRAME_MISMATCH` | 音频帧数与 `duration_ms` 推算不一致 |
| `E_DUPLICATE_CLIP` | 重复 `clip_id` |
| `E_SEGMENT_ID_MISSING` | 标注缺少 `segment_id` |
| `E_OVERLAP` | 区间重叠 |
| `E_OUT_OF_RANGE` | 区间越界 |
| `E_REFINE_INVALID` | 精修越出允许边界、吞并邻区间或窗口非法 |
| `E_INTERVAL_INVALID` | 置信区间上下界非法 |
| `E_SPEAKER_UNKNOWN` | 标签不在受控集合 |
| `E_RESOURCE_LIMIT` | 包络帧数或快照条目数超限 |
| `E_CACHE_CORRUPT` | 快照/清单/回放记录校验和不符 |
| `E_SNAPSHOT_ROLLBACK` | 快照或配置版本回退 |
| `E_LINEAGE_BROKEN` | 回放追溯链缺失或回放不一致 |
| `E_INCONSISTENT` | 增量导出与全量重算不一致 |

所有错误均以结构化对象返回：

```json
{
  "ok": false,
  "code": "E_OVERLAP",
  "message": "intervals overlap",
  "details": {
    "interval_id": "meeting-a:cand:0001",
    "previous_end_ms": 825.0,
    "start_ms": 820.0
  }
}
```

## 验证结果

当前环境执行 `pytest -q`：`36 passed in 1.69s`。执行 `python scripts/run_acceptance.py` 的结果写入 `runtime/acceptance_report.json`，摘要如下：

| 步骤 | 快照版本 | 重算节点 | 复用节点 | 全量逐字节一致 |
| --- | ---: | --- | --- | --- |
| 首次分析 | 1 | 全部 | 无 | 是 |
| 重复分析 | 1 | 无 | 全部 | 是 |
| 标注修正 `a-002 -> S01` | 2 | `speaker/export` | `envelope/vad/refine` | 是 |
| 底噪分位 `0.20 -> 0.25` | 3 | `vad/refine/speaker/export` | `envelope` | 是 |
| 帧移 `10ms -> 5ms` | 4 | 全部 | 无 | 是 |

- 标注修正时 5 个区间中仅 1 个重算，4 个复用。
- 版本历史为 `[1, 2, 3, 4]`，每代 5 个条目；回退尝试返回 `E_SNAPSHOT_ROLLBACK`。
- 版本 1、2、3、4 的导出在后续版本提交后仍可回放且逐项匹配。
- 初始导出缓存键：`8a2c76ce0ae1f10472b202417fbd8afc8362ad650ed2da65865d32d4f061c45c`。
- 标注修正后导出缓存键：`9caf127019b16ee34a2d57054d98f02512f561375a4f2dedf1ff99ac7ee9296b`。

详细设计与验证说明见 `docs/DESIGN.md` 和 `docs/VERIFICATION.md`。
