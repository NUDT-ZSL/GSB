# LatentFlow Studio

面向文生视频（T2V）与图生视频（I2V）的本地确定性生成/预览工作台。重点不是训练模型，而是把“低延迟逐块预览”和“与整段解码逐字节一致”同时做成可验证的工程约束。

## 快速启动

本机已验证环境：WSL2 Ubuntu、Python 3.12.13、NumPy 2.5.5、32 核、15 GiB 内存。应用只绑定 `127.0.0.1`，不下载样本、不访问远程服务。

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate

python scripts/make_sample.py
PYTHONPATH=src python run.py --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。页面中可切换预览块大小和比对块大小，逐块查看进度、首块实际计时、快照版本和一致性结论；另有两个故障演示按钮：

- `注入锚点异常`：破坏解码首帧后触发 `E_ANCHOR_MISMATCH`。
- `超预算演示`：把预算压到 `0.001 ms`，规划器用真实解码循环探测后返回 `E_LATENCY_BUDGET`。

无需浏览器也可运行完整验收：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -v
PYTHONPATH=src .venv/bin/python scripts/acceptance_demo.py
```

验收脚本会写入 `artifacts/acceptance-report.json`，记录真实计时、SHA-256、错误码、分块计划和快照版本。

## 已实现能力

- **四阶段确定性流水线**：提示词 SHA-256 条件向量 → 种子初始潜帧 → 逐步去噪 → RGB 解码；每个中间潜帧都有稳定哈希。
- **共享上下文的流式分块**：归一化统计量固定为整段潜帧口径，时序混合只读取前一潜帧，量化与锚定按帧索引执行，不依赖块局部状态。
- **可观测首块延迟**：计划阶段运行真实解码探针，记录按“帧数 × 4 个解码阶段”的本地计时依据；首块执行后再次用实际计时硬校验预算。
- **逐字节一致性门禁**：预览块拼接与一次性整段解码全像素比较；再以另一个块大小独立解码并比较。
- **显式裁决**：一致性是硬不变量；资源限制次之；随后在请求块大小和预算内选择最大可行块；边界越密，校验强度越高。
- **I2V 锚定**：本地 PPM/RGB 输入图严格作为首帧，后续帧按 `anchor_strength ∈ [0,1]` 回归参考均值/方差；锚定不破坏分块一致性。
- **版本化整体替换快照**：`decode_params`、`chunk_plan`、`ready_blocks` 各自具有单调版本和内容 SHA-256，活动文件原子替换。
- **并发与资源上限**：同一时刻一个活动分块计划，冲突立即 `E_STAGE_BUSY`；帧数、块驻留内存、并发解码任务数均有可配置上限。
- **无第三方 Web 依赖**：标准库 `ThreadingHTTPServer` + 原生 HTML/CSS/JS；帧以本地 PNG 输出，完整视频提供原始 RGB 下载。

## 验收结果（本机实测）

`23` 项单元/集成测试全部通过。最近一次 `scripts/acceptance_demo.py` 的关键结果：

| 项目 | 实测 |
|---|---|
| 12 帧、块大小 4、3 个块 | 逐块拼接与整段解码 `746,496` 字节完全一致 |
| 同一输入改用块大小 6 重跑 | 视频 SHA-256 仍为 `f69057a3...cfc80f7d` |
| 对照块大小 3 独立解码 | 与整段解码完全一致 |
| 首块实际解码 | `9.791332 ms`；计划探针估算 `10.412605 ms`；预算 `500 ms` |
| 锚定首帧 | 与 `samples/reference.ppm` 逐字节一致 |
| 超预算演示 | `E_LATENCY_BUDGET`，`decoded_blocks=0`，附带已完成阶段与候选实测 |
| 锚点注入 | `E_ANCHOR_MISMATCH`，定位 `frame=0, x=0, y=0, channel=0` |
| 并发计划冲突 | 后到者立即 `E_STAGE_BUSY` |
| 快照损坏 | `E_SNAPSHOT_CORRUPT`，类别 `decode_params`，版本 `1` |

计时会随机器负载变化；哈希和逐字节结论不依赖计时。完整数据见 `artifacts/acceptance-report.json`。

## API 摘要

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/jobs` | 生成潜帧、规划并提交一个活动分块计划 |
| `POST` | `/api/jobs/{id}/chunk` | 解码指定块并整体提交就绪块快照 |
| `POST` | `/api/jobs/{id}/chunks` | 受并发上限约束的并行块解码 |
| `POST` | `/api/jobs/{id}/complete` | 整段解码、双块大小比对、锚点校验 |
| `GET` | `/api/jobs/{id}/ready` | 显式查询已就绪块清单与快照版本 |
| `GET` | `/api/jobs/{id}/frames/{n}.png` | 查询已就绪帧 |
| `GET` | `/api/jobs/{id}/video.raw` | 下载完整 RGB uint8 帧序列 |
| `GET` | `/api/state` | 当前阶段、进度、快照、最近错误 |
| `GET` | `/api/snapshots/verify` | 主动加载并校验全部活动快照 |
| `POST` | `/api/demo/run` | 无 UI 的同步完整演示 |

错误响应统一为：

```json
{
  "ok": false,
  "error": {
    "code": "E_CHUNK_MISMATCH",
    "message": "分块拼接结果与整段解码不一致",
    "details": {
      "block_index": 2,
      "frame_index": 5,
      "pixel_position": {"x": 11, "y": 9, "channel": 2}
    }
  }
}
```

## 项目结构

```text
src/latentflow/
  canonical.py    稳定 JSON/哈希与 SHA-256 计数器 PRNG
  pipeline.py     提示词、种子潜帧、逐步去噪
  decoder.py      共享上下文分块解码、拼接、差异定位、锚定校验
  planner.py      延迟/资源/一致性裁决与解码计划
  snapshot.py     版本化快照、校验和、原子整体替换
  service.py      任务状态、并发租约、资源限制、完整演示
  web.py          本地 HTTP API 与静态应用
static/           专业交互控制台
tests/            23 项验收测试
scripts/          样本生成与可重复验收脚本
docs/TECHNICAL.md 一致性证明、裁决规则与实现说明
```

详细设计见 [docs/TECHNICAL.md](docs/TECHNICAL.md)。
