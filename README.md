# MatForge

MatForge 是一个纯本地、确定性的纹理与材质生成工作台。它面向固定种子的多材质样本，把参数校验、多倍频噪声、材质通道映射、分块切片、接缝归一化和拼接裁决组织为一条可追溯流水线，并提供浏览器交互界面、命令行演示、内容寻址缓存、版本化快照和自动化验收。

不依赖 Docker、GPU、外部服务或网络数据；运行时只需要 Python 3.11 与 NumPy。

## 快速开始

```bash
python3.11 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"

# 完整可重复演示，写入 artifacts/demo
python -m matforge demo --output artifacts/demo --fresh

# 固定种子材质预设与独立渲染
python -m matforge presets
python -m matforge render --preset oxidized-iron --output artifacts/iron

# 本地浏览器应用
python -m matforge serve --port 8765
# 打开 http://127.0.0.1:8765
```

Windows 上默认缓存目录为 `%LOCALAPPDATA%\MatForge\cache`。可用以下命令显式探测任意缓存路径：

```bash
python -m matforge cache-check
python -m matforge cache-check --path D:\MatForgeCache
```

## 主要能力

- 五阶段确定性流水线：参数校验与全局归一化、多倍频噪声、7 通道材质映射、可配置网格切片、接缝权重与拼接。
- 全局坐标噪声：每个切片以与整幅完全相同的种子、倍频相位、权重和归一化统计计算，块边缘法线额外读取 1 像素 halo。
- 先预览后生成：首块查询返回像素区间、内容摘要、实际耗时和成本估算；其余切片可并发生成。
- 字节级裁决：逐块拼接结果与一次性整幅结果逐字节比较；不一致时返回 `E_BLOCK_MISMATCH`，包含首个偏差像素、切片编号与 7 通道偏差谱。
- 内容寻址缓存：切片、接缝权重、上游噪声摘要、参数摘要、种子、网格、倍频配置和归一化摘要全部入键；磁盘对象按内容哈希寻址并在读取时复核。
- 修订失效：比较每个块的依赖语义键，从首个失效块起形成重算后缀；展示区变化等不影响像素的修订可复用全部内容；全局材质参数修订保守地使全部内容键失效。
- 版本化快照：参数、计划、就绪切片清单各有单调版本与校验和，使用原子整文件替换；拒绝回退，拒绝损坏。
- 过期拒绝与重放：切片记录缓存键、参数修订、计划版本和节点路径；旧依赖仍被引用时返回 `E_STALE_SLICE`；重放会重新生成并核对摘要与缓存键。
- 并发与资源控制：预览可并行；计划提交锁非阻塞，冲突立即返回 `E_STAGE_BUSY`；限制材质像素、单块驻留内存和并行切片数，支持串行流式降级标记。
- 一致性自检：检查切片像素集合、缓存键/摘要、重算/复用分区、快照校验和。

## 本地交互

浏览器界面提供：

- 分块网格叠加、块选择和切片就绪进度；
- 首块实际观测耗时、估算输入、归一化摘要和阶段状态；
- 修订表单、计划整体替换、重放与过期记录查询；
- 逐字节裁决、自检结果、快照版本和最近业务错误码；
- 缓存目录实时探测。

也可以用行式交互终端：

```bash
python -m matforge interactive
```

支持 `preview`、`rest`、`verify`、`selfcheck`、`revise`、`plan`、`replay`、`stale`、`probe`。

## HTTP API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/state` | 当前阶段、参数/计划、块进度、快照、缓存和最近错误 |
| GET | `/api/presets` | 固定种子材质预设列表 |
| GET | `/api/texture.png` | 已就绪块的局部纹理 PNG |
| POST | `/api/preview` | 查询/生成单块，参数为 `block_id`、`budget_ms` |
| POST | `/api/remaining` | 生成其余块，支持 `parallelism`、`allow_degrade` |
| POST | `/api/preview-many` | 并发预览多个块 |
| POST | `/api/verify` | 逐字节裁决 |
| POST | `/api/selfcheck` | 跨模块一致性自检 |
| POST | `/api/revision` | 提交更高参数修订并按后缀重算 |
| POST | `/api/plan` | 整体替换分块计划 |
| POST | `/api/replay` | 按记录重新生成并核对切片 |
| POST | `/api/stale` | 查询过期切片并触发 `E_STALE_SLICE` |
| POST | `/api/cache-probe` | 探测缓存可写并校验快照 |

业务错误统一为：

```json
{"ok": false, "code": "E_...", "message": "...", "details": {}}
```

## 验收状态

当前环境：WSL2 Linux、Python 3.11.18、NumPy 2.4.6。

```text
19 passed in 1.79s
demo ok: true
first slice observed: 7.519 ms
ready-state reuse:     0.053 ms
stitched vs full:      0 mismatched pixels
snapshot rollback:     E_SNAPSHOT_ROLLBACK
snapshot corruption:   E_SNAPSHOT_CORRUPT
```

Windows 可见缓存路径已实测可写：

```text
/mnt/c/Users/27857/AppData/Local/Temp/MatForge-cache-probe-329
writable=true, platform=windows-via-wsl
```

完整数据和产物见：

- `artifacts/demo/demo-report.json`
- `artifacts/demo/full-texture.png`
- `artifacts/demo/full-channels.npy`
- `artifacts/demo/snapshot.json`
- `artifacts/presets/{mineral-slate,oxidized-iron,desert-sandstone}/full-texture.png`

实现原理与验收细节见 `docs/architecture.md` 和 `docs/acceptance.md`。

## 测试

```bash
python -m pytest
```

测试覆盖确定性、整幅/切片逐字节一致、首块预算错误、并发预览、计划提交忙冲突、资源降级、快照重启/回退/损坏、缓存篡改、过期切片、追溯重放、偏差谱和自检分区。

