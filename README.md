# 本地确定性 T2V / I2V 渲染工作台

这是一个完全本地运行、可重复验证的文本生视频（T2V）与图生视频（I2V）渲染工作台。它把渲染拆成可缓存的阶段产物，支持参数编辑、低延迟首帧预览、局部潜变量补丁、线程池并发、可处理信号退出和强制终止后的清单恢复。

运行时不访问网络、不需要账号或外部服务，也不使用 Docker 或其他容器运行时。输入词表、噪声表、初始图、配置、命令流和异常样例均由固定种子脚本生成。

## 快速开始

当前工作区已经创建 `.venv` 并安装固定版本依赖。重新搭建时可执行：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python -m video_workbench.cli generate-samples
.venv/bin/python -m video_workbench.cli render --config samples/t2v.json
.venv/bin/python -m video_workbench.cli render --config samples/i2v.json
```

若系统 Python 缺少 `ensurepip`，可先创建 `--without-pip` 虚拟环境，再用官方 `get-pip.py` 引导 pip；本工作区实际采用该方式完成本地安装。

完整测试与验收：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/verify.py
```

第二条命令会重新生成样本、运行 6 个自动化测试、执行两种模式的一致性/预算/信号验收，并写出 `verification/verification_summary.json` 与 `verification/diagnostics/` 下的诊断报告。

## 目录

```text
video_workbench/
  config.py       配置校验、受限词表、噪声表、初始图、补丁与业务码
  artifacts.py    版本化 NPZ 阶段产物、原子清单、指纹校验与复用
  pipeline.py     编码、初始化、去噪、解码、预览、并发与诊断报告
  interactive.py  首帧预算响应、后台任务和最近预览复用
  cli.py          本地 CLI
scripts/
  generate_samples.py  固定种子输入生成器
  verify.py            可重复验收脚本
samples/          词表、噪声表、初始图、配置、命令流、异常样例
tests/            标准库 unittest 测试
verification/     实际验收结果与诊断报告
```

## 配置契约

样例见 `samples/t2v.json` 与 `samples/i2v.json`。相对路径以 CLI 的 `--base-dir`（默认当前目录）为基准。

必需字段：

| 字段 | 说明 |
| --- | --- |
| `config_version` | 当前为 `1` |
| `mode` | `t2v` 或 `i2v` |
| `prompt` | 必须只含受限词表中的 token |
| `seed` | 整数，范围 `0..2^32-1` |
| `steps` | 去噪步数 |
| `guidance` | 有限非负浮点数 |
| `frames` | 输出帧数 |
| `latent_shape` | `[height, width, channels]` |
| `output_size` | `[width, height]`，也是初始图要求的像素尺寸 |
| `embed_dim` | 哈希词袋嵌入维度 |
| `vocab_path` | 受限词表 JSON |
| `noise_table_path` | 噪声表 `.npy`，第一维必须等于 `steps` |
| `max_workers` | 线程池最大并发数 |
| `preview_size` | 低分辨率首帧预览尺寸 |
| `preview_budget_ms` | 交互首帧预算，毫秒 |
| `output_dir` | 可写输出目录 |

I2V 额外要求 `init_image_path` 与 `i2v_replacement_steps`。初始图缺失、不可解码或尺寸不等于 `output_size` 时返回 `E_INITIMAGE_INVALID`，不会回退成 T2V。可选字段：

- `patch`：局部潜变量补丁，`{"step", "frame"|null, "x", "y", "values"}`，`values` 形状为 `[height, width, latent_channels]`。
- `inject_failures`：测试用确定性阶段异常注入，条目为 `{"stage", "step", "frame", "error_type"}`。
- `stage_delay_ms`：仅用于本地延迟/信号验收的阶段停顿；不影响像素语义，默认 0。

## 渲染语义

1. **提示词编码**：token 先由受限词表校验；unigram 与 bigram 经 SHA-256 哈希到固定维度词袋，带符号权重并做 L2 归一化。
2. **潜变量初始化**：由提示词嵌入、种子、帧数、潜变量形状和路径模式派生。I2V 还会把初始图确定性地投影为图像潜变量。
3. **去噪步**：每步读取上一步潜变量，叠加该步预置噪声、条件引导和步号偏置。T2V 与 I2V 在替代步之后调用同一个纯函数 `denoise_step`。
4. **I2V 分支**：前 `i2v_replacement_steps` 步输出由初始图潜变量确定性地替代；其后与 T2V 共享相同去噪计算语义。显式记录 `replaced` 标志，不做静默降级。
5. **逐帧解码**：最终潜变量经确定性线性投影、缩放、逐像素裁剪到 `0..255` 并取整为 `uint8`。
6. **输出**：逐帧 PNG/GIF、接触表、`frames.npy`、版本化 `frames_vNNNNNN.npy`、低分辨率首帧 PNG 和 JSON 诊断报告。

所有浮点计算使用固定顺序的 `float64` 运算。线程调度只改变任务完成时间，不改变数组拼接、失败清单或诊断摘要的顺序。

## 阶段产物与精确失效

阶段图为：

```text
encode -> init -> denoise_000 ... denoise_N -> decode_frame_000 ... decode_frame_M -> preview
```

- 每个阶段产物是一个带版本号的 `.npz` 文件，内含阶段名、阶段种类、步/帧序号、内容指纹、依赖指纹、局部输入、快照版本、潜变量或像素数组。
- 产物先写同目录临时文件，`fsync` 后 `os.replace`；清单 `manifest.json` 同样原子替换。
- 读取复用前会校验文件 SHA-256、清单内容指纹、重算的内容指纹、阶段名/序号、依赖清单与缓存键。任一不符，目标阶段及下游重算。
- 缓存键包含阶段语义、局部参数、上游内容指纹和噪声步指纹。
- 补丁指纹只进入目标步及其下游的缓存键；目标步之前的编码、初始化和去噪阶段保持命中。
- 步数从 4 改为 3 时，如果前缀噪声行一致，已有前缀去噪阶段仍可复用；样例噪声表按步号稳定生成。

## 交互与首帧预算

`InteractiveController` 使用单后台任务队列，内部渲染器再用线程池并发执行帧任务。命令触达后：

- 立即复用最近一次已完成、可读取的低分辨率首帧，并附当前进度摘要。
- 若预算内最新参数的首帧尚未完成，返回 `E_PREVIEW_BUDGET_PENDING`，同时标记 `preview_stale=true`（若有旧预览）与 `background.continues=true`。
- 返回 pending 不是终止或丢弃任务：后台队列继续运行，最终报告仍会落盘。

命令流示例：

```bash
.venv/bin/python -m video_workbench.cli interactive \
  --commands samples/interactive_commands.json \
  --output-dir out/interactive \
  --budget-ms 20
```

## 中断与恢复

- `SIGINT` / `SIGTERM` 会设置停止事件。当前阶段完成计算、产物原子落盘、进度记录更新后，进程以整数退出码 `0` 退出，并写 `status=interrupted` 的诊断报告与 `E_INTERRUPTED`。
- `SIGKILL` 不可处理。被杀进程不生成退出报告，清单保留 `status=running`。
- 重启时只信任清单中文件存在且 SHA-256/内容指纹验证通过的产物；临时文件、孤儿产物和旧帧不会被混入。
- 新报告输出 `recovery_point`，包括来源快照版本、来源状态/进程号、最后完成阶段、已验证阶段清单和上次进度。
- 即使快照版本号相同，也会按文件哈希与内容指纹重新验证；版本号本身不作为复用依据。

## 并发与确定性

- 每个去噪步内部按帧提交并发任务；每帧解码任务同样使用线程池。
- 每个任务携带由阶段名、步序号、帧序号派生的稳定排序键。
- 最终帧顺序、中间潜变量清单、失败清单、业务码顺序和稳定诊断摘要不受线程调度影响。
- 报告中的 `stage_timings_ms` 是实际测量值，天然会波动；一致性比较使用 `summary` / `diagnostic_signature`，其中包含产物指纹、帧指纹、失效/复用阶段和失败清单，不包含墙钟耗时。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_CONFIG_INVALID` | 缺字段、类型错误、种子越界、引导强度非有限等 |
| `E_INITIMAGE_INVALID` | I2V 初始图缺失、不可解码或尺寸不符 |
| `E_PATCH_OUT_OF_RANGE` | 补丁步/帧或矩形坐标越界 |
| `E_NOISE_MISMATCH` | 噪声表长度/形状与配置不匹配 |
| `E_STAGE_FAILED` | 某阶段失败；可在其他帧/阶段继续的失败会进入失败清单 |
| `E_INTERRUPTED` | 收到可处理退出信号，快照已落盘 |
| `E_PREVIEW_BUDGET_PENDING` | 首帧预算耗尽，后台仍在继续 |

## 诊断报告

每次运行写 `output_dir/diagnostic_report.json`，包含：

- `reused_stages`、`invalidated_stages`、`recomputed_stages` 及失效原因。
- 每阶段实际重算耗时 `stage_timings_ms`。
- 快照版本、产物版本、内容指纹、依赖指纹和缓存键。
- 稳定失败清单（阶段、步、帧、异常类型、业务码）。
- 最终帧指纹、有效帧、预览指纹，以及相对上一快照的逐帧差异摘要。
- `summary.diagnostic_signature`，用于串行/并发或恢复前后的稳定比较。

## 实际验证结果

验收命令：

```bash
.venv/bin/python scripts/verify.py
```

结果全部通过，证据位于：

- `verification/verification_summary.json`
- `verification/unittest.log`
- `verification/interactive_responses.json`
- `verification/diagnostics/`

覆盖项：

- 6 个标准库自动化测试全部通过。
- T2V 与 I2V 的 4 线程并行渲染和单线程串行渲染：最终帧、有效掩码、初始化/去噪中间潜变量、稳定诊断摘要完全一致。
- 从 T2V 到 I2V、包含提示词/种子/引导/帧数/步数/初始图/补丁变更的增量编辑序列，最终多帧像素与中间潜变量和空白目录全量串行渲染逐像素/逐数组一致。
- 补丁生效后的增量结果也单独与对应全量串行渲染逐帧、逐中间潜变量比较，无差异。
- 补丁目标步为 1 时，`encode`、`init`、`denoise_000` 保持命中；`denoise_001` 及其下游全部失效。
- 5ms 首帧预算首条命令返回 `E_PREVIEW_BUDGET_PENDING`，后台继续并最终完成；第二条命令复用快照 1 的低分辨率首帧，同时后台继续。
- `SIGTERM` 退出码 `0`，报告状态 `interrupted`；`SIGKILL` 后无退出报告，清单保持 `running`，重启识别 `crash_recovery`，复用 `encode`/`init` 并完成全量输出。
- 样例异常覆盖种子越界、配置类型错误、补丁越界、噪声表长度不符、初始图缺失和单帧阶段异常隔离。

## 设计边界

- 去噪步之间存在数据依赖，因此步与步不能真正并行；并发发生在同一去噪步的各帧任务和逐帧解码任务，符合每步读取上一步的语义。
- 报告中的耗时是墙钟值，不用于跨并发度逐字段比较；稳定摘要用于确定性比较。
- `stage_delay_ms` 只用于本地延迟预算和信号窗口的可重复测试，不参与阶段缓存键，也不改变像素结果。
