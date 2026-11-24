# Relief Tile Workbench

本地桌面工作台：以固定种子生成程序化浮雕高度场，按方向光生成灰度图，再按可配置网格分块渲染、持久化、恢复和逐像素核验。

核心约束是“同口径”：噪声相位、方向光梯度、全幅归一化统计都只依据整幅像素坐标，任何切片都不得从局部原点重新起算。

## Quick Start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# 生成固定样本配置、height/full 真值与 PNG
python -m relief_workbench sample --dir samples

# 先预览一个切片（会写入块缓存）
python -m relief_workbench preview \
  --config samples/sample_config.json \
  --state .relief-state --output out --row 1 --col 1

# 再请求其余切片，写整幅图、逐块 PNG 与拼接验证报告
python -m relief_workbench render \
  --config samples/sample_config.json \
  --state .relief-state --output out

# 打开桌面诊断视图
python -m relief_workbench gui --config samples/sample_config.json
```

若直接启动 `gui` 且未提供配置，工作台会在本地 `samples/` 下按固定种子生成样本和真值参考，不需要网络、账号或外部资源。

## Configuration

```json
{
  "canvas": {"width": 512, "height": 384},
  "grid": {"rows": 4, "cols": 4},
  "seed": 20261125,
  "octaves": [
    {"frequency": 1.0, "amplitude": 1.0},
    {"frequency": 2.5, "amplitude": 0.5},
    {"frequency": 5.75, "amplitude": 0.25},
    {"frequency": 12.0, "amplitude": 0.125}
  ],
  "light": {
    "direction": [0.48, -0.64, 0.6],
    "relief_strength": 2.4
  },
  "normalization_tolerance": 1e-6,
  "concurrency": 4,
  "memory_budget_mb": 64,
  "preview_tile": [0, 0]
}
```

校验规则：

- 画布宽高、网格行列均为正整数，且宽可被列数整除、高可被行数整除。
- `seed` 为非负整数，布尔值不视为整数。
- `octaves` 非空，频率有限且严格递增，振幅有限且为正。
- 方向光为三维有限单位向量；浮雕强度、归一化容差为有限正数。
- 并发数为正整数；内存预算为正数，按 80 字节/切片像素估算在途工作缓冲。
- 非法配置返回 `E_xxx` JSON，不创建状态目录、输出目录或结果文件。

## What Gets Written

状态目录（默认 `.relief-state/`）：

- `manifest.json`：配置规范化摘要版本、全局归一化统计及其摘要。
- `tiles/tile_rRRR_cCCC.tile`：魔数 + JSON 头 + `uint8` 像素。头内包含版本、行列、整幅偏移、覆盖范围、像素数、dtype、内容 SHA-256。
- `interrupted.json`：可处理终止在分块边界写入的中断标记。

输出目录（默认 `out/`）：

- `full.png`：整幅拼装结果。
- `tiles/tile_rRRR_cCCC.png`：逐块结果。
- `tiles_manifest.json`：逐块偏移、覆盖范围、版本与内容摘要。
- `report.json`：拼接验证报告，含整幅/参考 SHA-256、首错坐标、所属块、两侧值、残差分类、缓存命中、内存峰值和 `E_DIGEST` 补算事件。

块文件与报告都采用临时文件、`fsync`、`os.replace` 的原子替换。强制终止后只复用摘要有效且版本一致的完整块。

## Verification

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
QT_QPA_PLATFORM=offscreen python scripts/validate_acceptance.py
```

最近一次实际验证（Linux/WSL2，Python 3.12.13，NumPy 1.26.4）：

- 测试：`36 passed`。
- 512 x 384、4 x 4 固定样本：16 块拼装与整幅直接生成 `max_abs_error=0`，两侧 SHA-256 均为 `8a7383b1049c372f35384efdb1c5ce2f6f37bd5e7d39717196926b97a1410100`。
- 并发度 1 与 4 的拼装 SHA-256 一致。
- 预览块缓存化后，二次进程启动命中缓存；16 块全缓存重跑时 `rendered_tiles=0`。
- 配置版本变化时缓存整体作废，`reused_tiles=0`。
- 篡改块摘要后产生 `E_DIGEST` 事件并回退补算，最终仍逐像素一致。
- `SIGTERM` 退出码 `143`，写入中断标记；重启复用有效块后完成。
- `SIGKILL` 后无报告承诺；本轮重启从 4 个原子持久化的有效块恢复并完成。

详细设计与验证记录见：

- `docs/DESIGN.md`
- `docs/VALIDATION.md`
- `artifacts/validation/acceptance_summary.json`
- `artifacts/validation/stitch_report.json`
- `artifacts/validation/out/report.json`
- `artifacts/validation/gui-diagnostics.png`

## Exit Codes

- `0`：成功；或桌面 GUI 正常关闭。
- `130`：可处理的 `SIGINT` 中断。
- `143`：可处理的 `SIGTERM`/`SIGBREAK` 中断。
- `1`：业务错误、校验错误或未处理失败；JSON 中给出 `E_xxx` 码。

## Project Layout

```text
relief_workbench/
  config.py       配置解析、校验、规范化版本
  pattern.py      全幅坐标确定性梯度噪声
  renderer.py     全局统计、方向光、切片与诊断变体
  storage.py      原子块缓存、摘要、版本与中断标记
  engine.py       预览、并发调度、内存预算、恢复与裁决
  gui.py          PySide6 桌面诊断视图
  cli.py          命令行入口
  sample.py       固定本地样本与真值
tests/            单元、并发、恢复、信号、GUI 测试
scripts/          验收脚本
```

## Notes

- 首块预览需要先完成一次全幅统计扫描，以保证归一化严格使用整幅极值；后续预览和块渲染由缓存加速。
- 噪声表由固定 `PCG64(seed)` 生成，锁定的 NumPy 版本保证跨运行确定性。
- 当前验证环境是 WSL2 Linux；路径、原子替换与 PySide6 代码按 Windows 兼容方式编写，但本轮未在原生 Windows 上执行信号集成测试。
