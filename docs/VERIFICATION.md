# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 1. 环境

WSL 工作区：

```text
$ python --version
Python 3.10.12

$ python -c "import numpy; print(numpy.__version__)"
1.26.4
```

Windows 互操作环境：

```text
PS> python --version
Python 3.13.14

PS> python -m pip install "numpy>=1.26"
Requirement already satisfied: numpy>=1.26 in D:\Python3.13\Lib\site-packages (2.5.2)
```

`py -0p` 还列出 uv 管理的 CPython 3.11.16。Windows `D:\Python3.13\python.exe` 已实际运行项目测试，满足“Python 3.11 及以上”的要求。

另用 CPython 3.11.16 创建临时 Windows venv，安装 `numpy>=1.26`；实际安装版本为 NumPy 2.4.6，并通过同一套测试。

## 2. 自动化测试

命令：

```bash
python -m unittest discover -s tests
```

结果：

```text
..............
----------------------------------------------------------------------
Ran 14 tests in 1.611s

OK
```

Windows Python 3.13：

```text
..............
----------------------------------------------------------------------
Ran 14 tests in 3.893s

OK
```

Windows Python 3.11.16 临时 venv：

```text
Python 3.11.16
numpy 2.4.6
..............
----------------------------------------------------------------------
Ran 14 tests in 3.198s

OK
```

覆盖内容包括：

- 全部要求的输入校验业务码。
- 圆形/椭圆/方形与 2/6 量化级下，分块拼装和整幅渲染逐像素相同。
- 单像素以全局原点调用时与整幅图相同。
- `E_CONSIST` 的首个不一致坐标、分块、两侧值。
- 有理数重采样确定性。
- 块复用、版本变化归档、`E_DIGEST`。
- 内存预算峰值和等待。
- `workers=1` 与 `workers=4` 的端到端 PNG 逐像素相同。
- 优雅终止与恢复。
- CLI 异常注入、二次缓存复用和 `--term-after-tiles 2` 退出码 20。

## 3. 固定种子样本批处理

干净的 `state/`、`out/` 中仅保留 `.gitkeep` 后执行：

```bash
python -m halftone seed --root . --force
python -m halftone run --root . --workers 4 --check
```

实际摘要：

```json
{
  "exit_code": 0,
  "failed_jobs": 4,
  "injection_summary": {
    "all_matched": true,
    "matched": 4,
    "total": 4
  },
  "memory_peak_bytes": 25600,
  "status": "completed",
  "successful_jobs": 5
}
```

岗位与摩尔纹结果：

```text
job_circle_320x240  computed=12 reused=0  risk=0.580680 threshold=0.60 exceed=False valid_pixels=76800
job_ellipse_256     computed=16 reused=0  risk=0.551987 threshold=0.54 exceed=True  valid_pixels=65536
job_fine_288        computed=36 reused=0  risk=0.353542 threshold=0.40 exceed=False valid_pixels=82944
job_moire_512x384   computed=48 reused=0  risk=0.553908 threshold=0.52 exceed=True  valid_pixels=168960
job_square_192x288  computed=9  reused=0  risk=0.498254 threshold=0.50 exceed=False valid_pixels=55296
```

每个成功岗位的报告均包含：

```json
"consistency": {
  "matched": true,
  "checked_pixels": 76800,
  "first_mismatch": null
}
```

异常业务码匹配：

```text
inj_bad_angle          -> E_ANGLE
inj_bad_grid           -> E_GRID
inj_missing_shape      -> E_SHAPE
inj_corrupt_digest     -> E_DIGEST
```

`out/` 中只有 5 个正常岗位的 PNG、油墨预览、切片 ZIP 和 `report.json`；没有 `inj_*` 网点图。

## 4. 二次运行缓存恢复

再次执行相同命令：

```bash
python -m halftone run --root . --workers 4 --check
```

结果：

```text
job_circle_320x240  computed=0 reused=12
job_ellipse_256     computed=0 reused=16
job_fine_288        computed=0 reused=36
job_moire_512x384   computed=0 reused=48
job_square_192x288  computed=0 reused=9
```

## 5. 优雅终止

```bash
python -m halftone run \
  --jobs samples/jobs.json \
  --manifest samples/images.json \
  --state .termcheck/state --out .termcheck/out \
  --report .termcheck/report.json \
  --no-inject --workers 1 --term-after-tiles 2
```

实际结果：

```text
GRACE_EXIT=20
terminated 20 {'completed_tiles': 2, 'exit_code': 20, 'reason': 'tile-boundary-test-limit'}
RESUME_EXIT=0
resume success 5 circle reused/computed 2 10
```

## 6. 强制终止

使用 `--tile-delay-ms 500 --workers 1` 放慢块任务，启动后 1.25 秒发送 `SIGKILL`：

```text
promoted tiles before recovery: 2
temp files after kill: 0
resume_exit: 0
FORCE_RESULT completed 5 2 10 True
```

即恢复时只信任已原子替换且摘要有效的 2 个块，补算剩余 10 块，最终整幅一致性为 `True`。

## 7. E_CONSIST 演示

```bash
python -m halftone consistency-demo
```

实际输出要点：

```json
{
  "code": "E_CONSIST",
  "demonstration": true,
  "details": {
    "first_mismatch": {
      "x": 13,
      "y": 9,
      "tile_x": 0,
      "tile_y": 0,
      "tile_index": 0,
      "tile_value": 255,
      "full_value": 0
    }
  }
}
```
