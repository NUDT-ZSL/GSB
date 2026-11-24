# Surface Stitch：确定性地表分块配准与拼接管线

本工程从空目录实现，仅依赖 Python 标准库、`numpy` 和 `Pillow`；SQLite 使用标准库 `sqlite3`，运行期不访问网络。

## 1. 环境准备

任务目标环境为 Windows + Python 3.11：

```powershell
py -3.11 -m pip install numpy==1.26.4 pillow==10.3.0 pytest==8.3.3
```

最终验收使用 Python 3.11.18、numpy 1.26.4、Pillow 10.3.0、pytest 8.3.3 实际执行；代码也可在 Python 3.10 环境运行。

## 2. 一键生成样本与验收

```bash
python generate_samples.py --output samples/generated
python scripts/run_acceptance.py
```

验收脚本会实际执行：

- 固定种子生成 4 块 8-bit 灰度 PNG、真值偏移清单、v1 单文件 JSON、v2 分片 JSON 和 SQLite；
- `workers=1` 与 `workers=3` 全量拼接并逐像素比较；
- FFT 配准、抛物线亚像素细化、亮度/对比度校正；
- 12 条 v1/v2 掩码归属冲突裁决；
- `--interrupt-after 1` 块边界中断、退出码 `130`、恢复；
- 篡改 `tile_02` 摘要后的 `E_DIGEST` 与回退补算；
- 非法布局输入返回 `E_*` JSON 且不生成拼接图；
- 全部 pytest。

机器可读结果：`artifacts/acceptance_report.json`；实际指标：`docs/VALIDATION.md`。

## 3. 常用命令

生成样本：

```bash
python -m surface_stitch generate --output samples/generated
# 或
python generate_samples.py --output samples/generated
```

运行管线：

```bash
python -m surface_stitch run \
  --input samples/generated \
  --output-dir outputs \
  --state-dir state \
  --workers 2
```

根目录也提供等价入口：

```bash
python run_pipeline.py --input samples/generated --output-dir outputs --state-dir state
```

仅校验输入和已持久化摘要：

```bash
python -m surface_stitch verify --input samples/generated --state-dir state
```

运行测试：

```bash
pytest -q
```

## 4. 样本契约

固定种子为 `20261125`。样本是 320×320 画布上的 2×2 分块：

- 每块 224×224；
- 步距 96，因此相邻块有 128 px 重叠；
- 真值亚像素偏移均在数十像素内；
- 每块有确定的对比度/亮度线性偏差；
- `truth_offsets.json` / `truth_offsets.csv` 记录真值；
- `descriptors/v1_registration.json`：相对秒、扁平整数偏移 `[dx, dy]`、逐像素 `0/255` 硬掩码；
- `descriptors/v2/shard_00.json`、`shard_01.json`：绝对毫秒、亚像素偏移、`[0,1]` 软权重、重复记录和墓碑；
- `descriptors/v2/registry.sqlite3`：`shards` 与 `records` 元数据表；
- `descriptors/v2/v2_manifest.json`：分片清单及 SHA-256。

坐标顺序统一为 `xy`。偏移语义为：目标标称网格 `d` 从重采样源图的 `d - offset` 位置取样，因此正 `dx` 会在目标左缘产生零填充区。

## 5. 处理口径

1. `loader.py` 先完成全部输入校验，之后才创建状态或产物。
2. `registration.py` 在声明重叠区内去均值，使用零填充线性 FFT 互相关搜索整数平移。
3. 整数峰 y/x 两侧三点按标准抛物线公式求亚像素分数；置信度为整数峰有效对应区的归一化 Pearson 相关系数。
4. `radiometry.py` 用固定 5×5 边缘盒式模糊后的对应像素做 OLS，估计 `reference ≈ scale * moving + offset`。
5. 全链路唯一校正/采样顺序为：原图 → 应用线性校正 → 双线性重采样；边界外 tap 按零处理。
6. `masking.py` 将 v1 的 `0/255` 归一为 `[0,1]`，与 v2 软权重统一。同代同 `record_id` 取最新毫秒时间；墓碑优先；跨代归属不一致按“代数优先级，再按生效毫秒”裁决，并输出 JSON Pointer/SQLite 分片来源、块 ID、画布坐标和块内像素坐标。
7. `fusion.py` 按块 ID 升序累加 `aligned * weight` 与权重，最后归一、四舍五入并截断到 `[0,255]`。另提供显式 y/x 行序参考融合，测试要求两者逐像素相同。
8. 线程池上限为 `--workers`；`ResourceGate` 同时限制在途像素槽位、FFT/频谱字节和活动 worker，报告记录实际峰值。

## 6. 状态、摘要与恢复

状态目录结构：

```text
state/
├── version.json
├── INTERRUPTED.json          # 仅优雅中断时存在
├── archive/<old-version>/...  # 输入版本变化时归档
└── tiles/
    ├── tile_00.manifest.json # 完成标记
    ├── tile_00.npz            # aligned/weight/contribution
    └── ...
```

- 版本 ID 由解码后素材像素摘要、真值 JSON、v1 JSON、v2 分片 JSON 和 SQLite 规范化行转储共同哈希得到。
- NPZ 先写 `*.tmp` 再 `os.replace`；manifest 最后原子写入，是块完成标记。
- manifest 含像素数、数组摘要、指标摘要和块摘要。
- 摘要缺失/损坏、像素数不符或版本不符时返回 `E_DIGEST`，记录事件并回退补算。
- 首次冷启动时，尚无 manifest 的块会记录为 `status=missing/code=E_DIGEST` 的非致命缓存事件并立即补算；补算后的终态摘要为 `valid`。同版本第二次运行且缓存有效时，业务码列表为空。
- 输入版本变化时，旧 `tiles/` 不会被覆盖，而是移动到 `archive/<old-version>[-n]/`。

## 7. 中断与导出

SIGINT/SIGTERM 处理器只设置 `threading.Event`。调度器保持至多 `workers` 个在途块；运行中的块走完后不再准入新块，因此终止只在块边界生效。中断时原子写 `INTERRUPTED.json`，CLI 返回约定退出码 `130`。未完成块没有有效 manifest，不会被误认为完成。

拼接图写入带 `surface_stitch_version` PNG 文本块；报告顶层也有 `version_id`。图片和报告均写同目录 `*.tmp`、fsync 后 `os.replace`。替换前读取旧产物版本；版本相同则跳过替换，版本缺失或不同才原子替换。

## 8. 主要业务码

| 业务码 | 含义 |
| --- | --- |
| `E_MANIFEST` | 工程清单/JSON 结构非法 |
| `E_IMAGE_MISSING` / `E_IMAGE_DECODE` / `E_IMAGE_SIZE` | 素材缺失、无法解码或尺寸不符 |
| `E_LAYOUT_COVERAGE` | 分块未覆盖整幅画布或越界 |
| `E_OVERLAP` | 重叠区声明与几何排布不一致 |
| `E_OFFSET_FINITE` / `E_OFFSET_DOMAIN` / `E_SUBPIXEL_DOMAIN` | 偏移非有限数或越界 |
| `E_TIME_CONVERSION` | v1 秒不能换算为合法毫秒或 v2 时间戳非法 |
| `E_MASK_SHAPE` / `E_MASK_DOMAIN` | 掩码尺寸、像素数或权重定义域非法 |
| `E_V1_DESCRIPTOR` / `E_V2_DESCRIPTOR` / `E_DATABASE` | 两代描述或 SQLite 元数据非法 |
| `E_DIGEST` | 块摘要、像素数或持久化版本校验失败 |
| `E_RESOURCE_LIMIT` / `E_STATE` / `E_EXPORT` | 资源预算、状态或导出失败 |

致命输入错误由 CLI 输出 `{"ok": false, "error": {"code": "E_..."}}` 并以退出码 `2` 结束；此时不会导出拼接图。

## 9. 目录导航

- `surface_stitch/generator.py`：固定种子样本与 SQLite 生成器。
- `surface_stitch/loader.py`：解析、校验、版本摘要。
- `surface_stitch/registration.py`：FFT 互相关和抛物线亚像素。
- `surface_stitch/radiometry.py`：亮度/对比度估计与校正。
- `surface_stitch/masking.py`：掩码归一、去重、墓碑和冲突裁决。
- `surface_stitch/fusion.py`：确定性加权融合与串行参考。
- `surface_stitch/storage.py`：原子写、摘要、归档、版本感知导出。
- `surface_stitch/resources.py`：线程/像素/频谱资源闸门。
- `surface_stitch/pipeline.py`：并行调度和报告组装。
- `scripts/run_acceptance.py`：本地验收与实际结果落盘。
- `tests/`：17 个聚焦测试。
