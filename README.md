# 程序化网格地形分块离屏渲染引擎

一个零外部渲染依赖、确定性的 CPU 地形分块离屏渲染引擎。读取 JSON 渲染
任务与固定种子本地样本，完成整幅法线/光照、逐超采样光线求交、双预算
并行调度、编辑追加增量重渲、分块级原子持久化与中断恢复。

仅依赖 `numpy`；持久化使用标准库文件原子写（`sqlite3`/`json` 均为标准
库，无需外部数据库）。

## 快速开始

```bash
pip install -r requirements.txt
python scripts/generate_samples.py --out samples

# 全量分块渲染并发布灰度图与深度图
python -m terrain_engine.cli render samples/task.json --root work

# 预览单个分块（命中缓存时立即返回）
python -m terrain_engine.cli preview samples/task.json r0001_c0001 --root work

# 追加编辑批次，仅重渲受影响分块
python -m terrain_engine.cli append samples/task.json --root work \
    --edits samples/edits.json --batch 1

# 校验所有已持久化分块摘要
python -m terrain_engine.cli verify samples/task.json --root work

python -m pytest -q
```

## 输入与产物

* `samples/terrain.json`：行列数、三轴世界尺度、整数高度场；
* `samples/task.json`：两盏方向光、环境光、正交相机、输出分辨率、
  超采样数、分块规格、并发上限、逻辑时间/在途内存双预算、状态目录；
* `samples/edits.json`：按批次追加的抬升/压低/平滑编辑；
* `samples/injections.json`：高度数不符、不可整除、损坏摘要、越界编辑；
* `work/state/`：分块 `.npz` + `.manifest.json`、归档、编辑游标、
  地形快照、中断标记；
* `work/outputs/image.pgm`（P5 灰度）与 `depth.f32`（小端 float32，
  世界单位毫米，背景为 `-1.0`）。

## 设计与验收

详细架构、错误码与逐条验收映射见 `docs/ARCHITECTURE.md`，实际验证结果
与 Windows 运行说明见 `docs/VERIFICATION.md`。

## 错误信封

非法输入或运行期可处理失败均返回
`{"ok": false, "error": {"code": "E_xxx", ...}}`，CLI 退出码为 2；
优雅终止退出码为 130。校验阶段在任何输出文件创建之前完成。
