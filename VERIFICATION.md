# 实际验证记录

验证日期：2026-11-17。

## 环境

| 项目 | 实际值 |
| --- | --- |
| 操作系统 | WSL2 / Ubuntu 22.04（Linux） |
| Python | 3.11.0rc1；代码声明兼容 Python 3.10+ |
| NumPy | 2.4.6；要求 `numpy>=1.24,<3` |
| 并行方式 | `ThreadPoolExecutor` |
| 容器 | 未使用 |
| 网络/外部服务 | 运行与测试均未访问 |

Windows 命令已写入 `README.md`。本次无法在实际 Windows 主机上执行，因此不把 Linux 结果宣称为 Windows 实机通过；代码路径仅使用 `os.replace`、标准库线程/信号与 NumPy，没有 Linux 专用依赖。

## 固定种子输入

```bash
.venv/bin/python -m voiceprint_diag generate-demo --output demo_input
```

- 主种子：`20261117`。
- 正常样本：3 位说话人，各 2 个 16 kHz/单声道/16 bit PCM WAV。
- 故障样本：错误采样率、错误声道数、错误位深、RIFF 数据长度越界、有效帧不足。
- 合成信号包含受控基频均值/方差、三段共振峰、音节包络、停顿和平稳底噪。

## 主验收运行

```bash
.venv/bin/python -m voiceprint_diag run \
  --samples demo_input/samples \
  --config demo_input/config.json \
  --pairs demo_input/pairs.json \
  --anomalies demo_input/anomalies.json \
  --output output
```

结果：退出码 `0`，`output/report.json` 与 `output/canonical_results.json` 已生成。

- `result_fingerprint`：`6484eae8328a31b2ad5829ab959789b01ff6b3b53dfcd0bbe7aa6e330554efaa`
- 同说话人对：3；跨说话人对：3。
- 跨说话人误接收：0；同说话人漏判：0。
- 注入项命中：5/5。
- 失败清单：5 项（4 个 `E_AUDIO_FORMAT`，1 个 `E_INSUFFICIENT_FRAMES`）。
- 主运行阶段耗时：特征 715 ms、汇聚 100 ms、嵌入 66 ms、相似度 61 ms、误差追踪 84 ms、总计 1192 ms（耗时仅代表该次执行）。
- 报告写入 837 条逐帧特征记录（正常样本 6×136 帧，加有效但汇聚失败的静音样本 21 帧）。

### 相似度

| 对 | 关系 | 定点余弦 | 判定 | 双精度参考 |
| --- | --- | ---: | --- | ---: |
| `same-a-01` | same | 0.998766681 | `SAME_SPEAKER` | 0.998766674468 |
| `same-b-01` | same | 0.995770770 | `SAME_SPEAKER` | 0.995770764774 |
| `same-c-01` | same | 0.996193906 | `SAME_SPEAKER` | 0.996193900270 |
| `cross-ab-01` | cross | 0.108094929 | `DIFFERENT_SPEAKER` | 0.108094927878 |
| `cross-ac-01` | cross | 0.315809238 | `DIFFERENT_SPEAKER` | 0.315809235706 |
| `cross-bc-01` | cross | 0.348782584 | `DIFFERENT_SPEAKER` | 0.348782582863 |

### 异常命中

| 注入编号 | 实际检测 |
| --- | --- |
| `INJ-RATE-001` | `E_AUDIO_FORMAT`：`sample_rate=8000, expected=16000` |
| `INJ-CHANNELS-001` | `E_AUDIO_FORMAT`：`channels=2, expected=1` |
| `INJ-BITS-001` | `E_AUDIO_FORMAT`：`bits=8, expected=16` |
| `INJ-TRUNC-001` | `E_AUDIO_FORMAT`：`data` 块声明长度越过文件末尾 |
| `INJ-SILENCE-001` | `E_INSUFFICIENT_FRAMES`：0 个有效帧，最低要求 12 |

### 逐级误差（工作 Q12，高精度 Q16，容差 1e-4）

| 样本 | 首次超差 | 特征最大差 | 汇聚最大差 | 量化最大差 | 相似度最大差 | 来源 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `speaker_a/a1.wav` | feature | 0.0001220703125 | 0.0001220703125 | 0.0001220703125 | 0.000045055 | 量化舍入 |
| `speaker_a/a2.wav` | feature | 0.0001220703125 | 0.000289916992188 | 0.0001220703125 | 0.000020743 | 量化舍入 |
| `speaker_b/b1.wav` | feature | 0.0001220703125 | 0.000274658203125 | 0.000106811523438 | 0.000045055 | 量化舍入 |
| `speaker_b/b2.wav` | feature | 0.0001220703125 | 0.000625610351562 | 0.0001220703125 | 0.000020352 | 量化舍入 |
| `speaker_c/c1.wav` | feature | 0.0001220703125 | 0.0013427734375 | 0.0001220703125 | 0.000020743 | 量化舍入 |
| `speaker_c/c2.wav` | feature | 0.0001220703125 | 0.00352478027344 | 0.0001220703125 | 0.000020352 | 量化舍入 |

本次低/高精度判定全部一致，因此没有 `E_PRECISION_SENSITIVE`。专门把阈值设为 `same-b-01` 的定点相似度后，自动化测试确认：

- 该对命中阈值并列边界，`boundary=true`，固定规则仍判 `SAME_SPEAKER`。
- 高一位宽判定与工作位宽相反，产生 `LOW_VS_HIGH_PRECISION_DECISION`。
- 嵌入级参考判定与定点相似度判定相反，产生 `EMBEDDING_LEVEL_VS_FIXED_SIMILARITY_DECISION`。

## 定点真实性

对 `speaker_a/a1.wav` 的对比：

| 配置 | 投影整数范围 | 档位码前 8 维 | 最终嵌入实数向量 |
| --- | --- | --- | --- |
| Q12，256 档 | `[-2319, 2327]` | `[-7,1,-12,5,-9,14,-2,14]` | 与 Q8 不同 |
| Q8，256 档 | `[-145,146]` | `[-7,1,-12,5,-9,14,-2,14]` | 与 Q12 不同 |

Q12 的整数范围约为 Q8 的 16 倍，证明位宽真实进入投影整数段；最终单位化嵌入也不同。将量化档位从 256 改为 64 后，档位码范围收缩、最终向量和相似度均改变。

## 增量失效与复用

以下数字来自自动化测试或独立的干净基线复跑：

| 变更 | 复用 | 重算 | 失效 | 重算阶段 |
| --- | ---: | ---: | ---: | --- |
| 无变更复跑 | 30 | 0 | 0 | 无 |
| 相似度阈值 0.82 → 0.50 | 24 | 6 | 6 | 仅 `similarity` |
| 量化档位 256 → 64 | 18 | 12 | 12 | `embedding` + `similarity` |
| Mel 频带 26 → 27 | 0 | 30 | 30 | 特征、汇聚、嵌入、相似度及失败缓存 |
| 替换单个 `a2.wav` 为不同合成音 | 25 | 5 | 5 | 该样本特征/汇聚/嵌入 + 2 个引用对 |

单样本替换中，只有 `same-a-01` 与 `cross-ac-01` 重算；`cross-bc-01` 等不相关对直接复用。若替换内容与其他样本字节完全相同，内容寻址缓存会复用已有内容键产物，这是合法的去重。

## 并发一致

同一输入分别用 `concurrency=4` 和 `concurrency=1`、`snapshot_interval=1` 运行：

- `deterministic_results` 语义逐字段一致。
- `result_fingerprint` 均为 `6484eae8328a31b2ad5829ab959789b01ff6b3b53dfcd0bbe7aa6e330554efaa`。
- 样本顺序、帧顺序、对顺序、失败顺序、边界和精度追踪顺序一致。
- 只有报告外层的实际耗时不同；这是要求输出的运行元数据，不进入结果指纹。

## 恢复与信号

### 真实 SIGINT

启动完整运行后在特征阶段发送 `SIGINT`：

- 进程退出码：`0`。
- 标准错误：`received signal 2; finishing the current stage and committing an atomic snapshot`。
- `state.complete=false`、`state.interrupted=true`、`completed_stages=["feature"]`。
- 未生成 `report.json`，符合“受控退出不承诺完整退出报告”。

随后使用相同配置续跑：

- 退出码 `0`，`snapshot_mode=resume`，`resumed_from_stage_count=1`。
- 复用 11 个已完成产物，重算 19 个后续产物。
- `result_fingerprint` 与无中断基线完全相同。

### 配置不匹配

构造未完成快照后，将 `mel_bands` 从 26 改为 27 再启动：

- CLI 退出码：`3`。
- 写出 `refusal.json`，`code=E_CONFIG_MISMATCH`。
- 原 `state.json` 保持 `complete=false`，未被覆盖。

### 无状态缓存恢复

删除完整运行的 `state/state.json`、保留所有原子缓存文件后重跑：

- 30 个产物全部复用，重算 0 个。
- `result_fingerprint` 与基线一致。

## 自动化测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

实际结果：`Ran 11 tests in 17.132s`，`OK`（最后一次在加入逐帧完整报告、“中断后改变并发数续跑”、显式恢复必须有快照、缓存清单阶段排序和状态/阶段快照哈希校验后执行）。

覆盖项：

1. 固定种子主运行、相似度质量、尾帧、残差与异常命中。
2. 特征重复运行逐帧一致、四类损坏 WAV 拒绝。
3. 无变更全复用、并发 1/4 结果一致。
4. 阈值变更仅失效相似度。
5. 阈值并列边界与两种 `E_PRECISION_SENSITIVE` 原因。
6. Q8/Q12 与 256/64 档位的中间整数和最终嵌入差异。
7. Mel 参数变更全链路失效。
8. 单样本替换的精确下游失效。
9. 删除状态后的纯缓存恢复。
10. 受控停止和未完成快照配置不匹配。
11. 未完成快照同配置续跑。

## 已知限制

- 当前投影矩阵是确定性的伪随机投影，而非训练得到的声纹模型；本任务验证的是音色编码数值链路、定点可信度、缓存和恢复语义。
- 自相关基频与 Mel 倒谱实现针对确定性合成样本验证，未宣称真实噪声/混响场景的识别指标。
- 本次只在实际 Linux/WSL 环境运行，Windows 实机验证留给目标部署环境执行。
