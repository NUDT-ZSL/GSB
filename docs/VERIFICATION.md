# 验证结果记录

验证环境：Python 3.10（代码仅使用 3.10 即支持的语法，同样兼容题目要求的
Windows Python 3.11/3.12）、numpy 1.26、pytest 8.2，全程离线、无容器。

## 自动化测试

执行 `python -m pytest -q`，结果：**55 passed**。覆盖映射如下：

| 测试文件 | 覆盖业务要求 |
| --- | --- |
| `test_validation.py` | 要求 1：全部 `E_xxx` 校验、非法输入零产出 |
| `test_geometry.py` | 要求 2：单侧/中心差分、边界、法线与分块无关 |
| `test_render.py` | 要求 3：求交、着色、深度、确定性、拼块=整幅 |
| `test_scheduler.py` | 要求 4：时间/内存预算、并发无关、注入时钟、隔离错误 |
| `test_edits_incremental.py` | 要求 5：失效闭包、增量=从零全量、分批稳定 |
| `test_persistence.py` | 要求 6/8：原子写、版本归档、摘要漂移、缓存预览 |
| `test_injections_signals.py` | 要求 7/8：注入业务码、优雅停机、硬终止恢复 |
| `test_cli_end_to_end.py` | CLI 全链路、append 游标、错误信封 |

## 关键人工/端到端核验

1. 全量渲染产出 `image.pgm`（P5，96x72）与 `depth.f32`（float32，
   全部为正的世界单位毫米距离，无背景像素）。
2. 冷启动 `preview` 首次 `rendered`、再次 `reused`，验证分块级缓存。
3. 跨进程 `append --batch 1/2/3` 的重渲/复用计数为 5/4、5/4、1/8；
   最终灰度与深度同“从零对最终地形全量渲染”逐像素一致
   （`gray parity: True`、`depth parity: True`、版本标识相同）。
4. 注入 `height_count_mismatch` 与 `corrupt_digest` 时，受影响两块分别
   返回 `E_HEIGHT_COUNT`、`E_DIGEST`，其余 7 块正常完成。
5. 优雅停机写出 `interrupted.json`，退出码 130，仅完整提交的块存在
   manifest；硬终止后重启复用摘要有效块并补渲缺失/损坏块。
6. 损坏 manifest（数组摘要/声明像素数）在恢复时被 `E_DIGEST` 检出并补渲。

## Windows 运行说明

代码全部使用 `pathlib` 与跨平台原子替换 `os.replace`，信号处理对缺少
`SIGTERM` 的平台回退到 `SIGINT`，无任何 POSIX 专属路径或 fork 依赖。
在 Windows PowerShell 中：

```powershell
py -3.12 -m pip install numpy pytest
py -3.12 scripts/generate_samples.py --out samples
py -3.12 -m terrain_engine.cli render samples/task.json --root work
py -3.12 -m terrain_engine.cli preview samples/task.json r0001_c0001 --root work
py -3.12 -m terrain_engine.cli append samples/task.json --root work `
    --edits samples/edits.json --batch 1
py -3.12 -m pytest -q
```

> 注：`--root` 是子命令参数，需写在 `render`/`append`/`preview`/`verify`
> 之后。
