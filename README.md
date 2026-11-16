# 本地确定性增量扩散渲染工作台

这是一个不使用预训练权重、GPU 或网络的本地文生图/图生图实验管线。全部数值计算由 NumPy 完成，Pillow 只负责 PNG 读写与依赖图绘制。工作台把渲染拆成可独立持久化的阶段，并以内容指纹和依赖指纹做精确失效判定。

## 已实现能力

- 阶段化管线：`prompt_encode` → `latent_init` / `img2img_encode` → `denoise_00..NN` → `decode`。
- 每阶段单文件 NPZ 产物，内含阶段名、内容指纹、参数指纹、依赖指纹、版本号和向量/像素数据。
- 文件采用临时文件 + `fsync` + 原子替换；最终 PNG、JSON 报告、依赖图也均为原子写盘。
- 增量规划只复用参数指纹和全部上游产物指纹都一致的阶段；任一上游失效会沿 DAG 传导到全部下游。
- 局部潜变量补丁只改变目标步及下游；补丁数值、坐标和顺序都计入指纹，即使像素相同也不会互相命中。
- 不可变版本快照 + 原子 `current` 指针切换。同一版本遇到不同配置或不同内容时返回 `E_SNAPSHOT_VERSION_CONFLICT`，绝不静默覆盖。
- staging 目录中的每个完整阶段都可恢复；SIGINT/SIGTERM 在当前阶段提交后退出 0；强制终止后重启只依据完整产物与进度继续。
- 配置/噪声/补丁错误分别返回 `E_CONFIG_INVALID`、`E_NOISE_MISMATCH`、`E_PATCH_OUT_OF_RANGE`。阶段异常记录阶段名和异常类型。
- JSON 诊断报告包含每次编辑的失效/复用阶段、逐阶段重算耗时、快照版本、业务码、像素差异摘要和依赖图。

## 环境

实际验证环境：

- Python 3.11.0rc1
- NumPy 2.2.6
- Pillow 12.3.0

安装：

```bash
python3 -m pip install -r requirements.txt
```

也可安装为本地命令：

```bash
python3 -m pip install -e .
imgdiff --help
```

## 快速开始

仓库已经包含 `inputs/` 下的小样本。需要重新生成时：

```bash
python3 scripts/make_inputs.py
```

首轮全量渲染：

```bash
python3 -m incremental_diffusion render \
  --config inputs/sample_config.json
```

按顺序执行提示词、种子、引导强度、补丁、初始图编辑：

```bash
python3 -m incremental_diffusion render \
  --config inputs/sample_config.json \
  --config inputs/edit_prompt.json \
  --config inputs/edit_seed.json \
  --config inputs/edit_guidance.json \
  --config inputs/edit_patch.json \
  --config inputs/edit_image.json
```

查询某个目标配置的失效/复用计划（不执行阶段计算）：

```bash
python3 -m incremental_diffusion query \
  --config inputs/edit_image.json \
  --workspace workspace
```

查询结果至少包含：

```json
{
  "snapshot_version": 6,
  "pipeline": ["prompt_encode", "..."],
  "recompute_stages": [],
  "reuse_stages": ["prompt_encode", "..."],
  "coverage_ok": true,
  "no_duplicates": true,
  "business_code": null
}
```

查看当前快照：

```bash
python3 -m incremental_diffusion snapshot --workspace workspace
```

## 配置格式

样本见 `inputs/sample_config.json`。所有相对路径都相对于配置文件所在目录。

```json
{
  "schema_version": 1,
  "snapshot_version": 1,
  "seed": 2026,
  "prompt": "sunset ocean clouds warm",
  "negative_prompt": "blur noise",
  "steps": 4,
  "guidance_scale": 3.0,
  "width": 32,
  "height": 32,
  "latent_channels": 4,
  "embedding_dim": 24,
  "vocab": ["sunset", "ocean", "clouds", "warm", "blur", "noise"],
  "noise_table": "noise_table.npy",
  "initial_image": "initial.png",
  "img2img_strength": 0.5,
  "patches": [],
  "workspace": "../workspace",
  "output_image": "../outputs/final.png",
  "report_json": "../outputs/diagnostic.json",
  "graph_png": "../outputs/dependency_graph.png",
  "intermediates_dir": "../outputs/intermediates"
}
```

必填字段：`schema_version`、`snapshot_version`、`seed`、`prompt`、`negative_prompt`、`steps`、`guidance_scale`、`width`、`height`、`latent_channels`、`embedding_dim`、`vocab`、`noise_table`、`output_image`、`report_json`、`graph_png`。`initial_image`、`img2img_strength`、`patches`、`workspace`、`intermediates_dir` 可选；存在初始图且未给强度时默认 `0.5`，否则默认 `0.0`。

约束：

- `seed`：`0..2^32-1`；`steps`：`1..16`；图像尺寸：`8..128`；`latent_channels`：`1..16`。
- 噪声表必须是 `.npy`，形状严格等于 `(steps, latent_channels, height, width)`，否则返回 `E_NOISE_MISMATCH`。
- 初始图必须是 PNG/可读图像，转换为 RGB 后尺寸必须等于 `(height, width)`。
- `img2img_strength` 取 `0..1`；`skip_steps = round(steps * strength)`。这些早期步直接由 `img2img_encode` 潜变量替代，之后再继续正常去噪。
- 补丁格式：

```json
{
  "step": 2,
  "x": 5,
  "y": 6,
  "width": 5,
  "height": 4,
  "values": [[[0.1, 0.2, 0.3, 0.4], "..."], "..."]
}
```

`values` 形状必须是 `(height, width, latent_channels)`，也接受同元素数的一维数组。坐标越界或 `step` 越界返回 `E_PATCH_OUT_OF_RANGE`。

## 阶段与依赖

提示词阶段在受限词表内做稳定哈希词袋嵌入；潜变量初始化用 `(seed, 提示词内容)` 派生固定 NumPy SeedSequence；去噪每步读取前一步/图像替代潜变量、该步噪声切片和条件引导；解码用确定性线性投影后四舍五入并裁剪到 `0..255`。

依赖图（`S` 为去噪步数，`K = skip_steps`）：

- `prompt_encode`: 无上游。
- `latent_init`: 依赖 `prompt_encode`。
- `img2img_encode`: 无阶段上游；初始图字节、尺寸和 `img2img` 参数进入自身参数指纹。
- `denoise_00`: 依赖 `prompt_encode`、`latent_init`；图生图启用时还依赖 `img2img_encode`。
- `denoise_i (0 < i < K)`: 依赖 `prompt_encode`、`img2img_encode`、`denoise_{i-1}`。
- `denoise_i (i >= K)`: 依赖 `prompt_encode`、`denoise_{i-1}`。
- `decode`: 依赖最后一步 `denoise_*`。

每个阶段的 `parameter_fingerprint` 包含算法版本和规范化参数；每步去噪只把该步噪声切片和该步补丁计入自身参数指纹。`content_fingerprint` 同时覆盖参数指纹、全部依赖指纹和实际数组内容。命中判定要求阶段参数指纹与 manifest 中依赖指纹全部相同，并且该阶段所有传递上游都在本次复用集合中。

## 快照与恢复

目录布局：

```text
workspace/
  current -> snapshots/v000006       # 原子切换的当前完整快照
  snapshots/v000001/artifacts/*.npz  # 不可变版本目录
  snapshots/v000006/manifest.json
  staging/v000007-<hash>/            # 未发布的可恢复写入区
```

一个快照只有在全部阶段完成、最终输出生成并写完整 manifest 后，才会从 staging 重命名为 `snapshots/vNNNNNN`，随后以 symlink 原子切换 `current`。因此：

- 已发布旧快照始终可完整读取，不因新编辑或阶段异常而损坏。
- 每个阶段的 NPZ 文件名带快照版本，manifest 还记录文件 SHA-256、内容指纹与依赖指纹，旧版本文件不会被误当命中。
- 同一 `snapshot_version` 必须对应同一配置和同一内容；否则返回 `E_SNAPSHOT_VERSION_CONFLICT`。
- 版本必须单调增加。仅提升版本、不改语义参数时，查询会显示全部复用，发布的新快照只复制已校验产物。
- 强制终止后，已提交到 staging manifest 的阶段可复用；未完成阶段自动进入重算集合。临时文件或 SHA 不匹配的产物不会被采用。

## 信号与异常

- 收到 SIGINT 或 SIGTERM：设置安全停止标志；当前阶段仍会完成 NPZ 与 manifest 进度落盘，然后以退出码 `0` 退出。未发布 staging 留给下次恢复。
- 阶段异常：`staging/manifest.json` 的 `progress.failure` 记录 `stage`、`exception_type`、`message`；CLI 退出码 `3`，报告业务码 `E_STAGE_FAILURE`。`current` 仍指向异常前最近完整快照。
- 强制 SIGKILL：不承诺写报告；已 fsync 的阶段产物和 manifest 用于恢复。

为可重复验收，代码支持测试注入环境变量：

- `IMGDIFF_FAIL_STAGE=<stage>`：在阶段计算中抛出测试异常。
- `IMGDIFF_INTERRUPT_AFTER_STAGE=<stage>`：在该阶段提交后模拟 SIGINT 安全点。
- `IMGDIFF_CRASH_AFTER_STAGE=<stage>`：在该阶段提交后执行 `os._exit(137)`。
- `IMGDIFF_DELAY_STAGE=<stage>` 与 `IMGDIFF_STAGE_DELAY_SECONDS=<seconds>`：拉长指定阶段，用于向真实进程发送 SIGTERM。

## 报告字段

`report_json` 的原子 JSON 示例结构：

```json
{
  "report_version": 1,
  "status": "ok",
  "business_code": null,
  "snapshot_version": 6,
  "config_hash": "sha256:...",
  "global_content_fingerprint": "sha256:...",
  "pipeline": ["prompt_encode", "..."],
  "dependencies": {"prompt_encode": [], "latent_init": ["prompt_encode"]},
  "edits": [
    {
      "edit_index": 4,
      "snapshot_version": 5,
      "invalidated_stages": ["denoise_02", "denoise_03", "decode"],
      "reused_stages": ["prompt_encode", "latent_init", "img2img_encode", "denoise_00", "denoise_01"],
      "recompute_stages": ["denoise_02", "denoise_03", "decode"],
      "hit_stages": ["prompt_encode", "latent_init", "img2img_encode", "denoise_00", "denoise_01"],
      "stage_status": {"prompt_encode": "reused", "denoise_02": "recomputed"},
      "stage_timings_ms": {"prompt_encode": 0.0, "denoise_02": 0.4123},
      "stage_compute_times_ms": {"prompt_encode": 0.0, "denoise_02": 0.3811},
      "total_elapsed_ms": 2.1,
      "final_pixel_diff": {
        "available": true,
        "identical": false,
        "changed_pixels": 411,
        "changed_channels": 733,
        "mean_abs_diff": 0.781,
        "max_abs_diff": 9,
        "identical_pixel_ratio": 0.5986
      },
      "business_code": null
    }
  ],
  "errors": [],
  "snapshot_path": "workspace/snapshots/v000006",
  "final_image": "outputs/final.png"
}
```

依赖图节点按 `reused` 绿色、`recomputed/invalidated` 红色、`failed` 深红色绘制；边上箭头由依赖指向使用方。

## 业务码与退出码

| 业务码 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `E_CONFIG_INVALID` | 缺字段、类型/范围错误、路径或图像不可用 | 2 |
| `E_PATCH_OUT_OF_RANGE` | 补丁步或矩形坐标越界 | 2 |
| `E_NOISE_MISMATCH` | 噪声表形状、长度、数值异常 | 2 |
| `E_SNAPSHOT_VERSION_CONFLICT` | 同版本不同配置/内容，或回退到旧版本 | 2 |
| `E_STAGE_FAILURE` | 单个阶段内部异常 | 3 |
| 无 | 成功或信号安全退出 | 0 |

所有业务错误同时写到 `stderr`（形如 `E_SNAPSHOT_VERSION_CONFLICT: ...`）和诊断 JSON。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：六步连续编辑失效范围；增量与空白全量重跑的 PNG/全部中间潜变量逐元素一致；不同补丁产生相同像素时仍不命中；同版本冲突不破坏旧快照；三类参数业务码；阶段失败后恢复；真实 SIGTERM 安全退出；模拟 SIGKILL 后 staging 恢复。详细实际结果见 `VERIFICATION.md`。
