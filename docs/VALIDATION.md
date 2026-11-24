# Validation Record

## Environment

- 运行环境：WSL2 Linux，`Linux-6.18.33.2-microsoft-standard-WSL2-x86_64`
- Python：3.12.13
- NumPy：1.26.4
- PySide6：6.6.1
- pytest：8.2.0
- 不使用 Docker、网络服务、账号或外部素材。

> 当前执行容器是 WSL2 Linux；Windows 路径/原子替换/PySide6 代码已按跨平台方式编写，但未在原生 Windows 上执行本轮信号测试。

## Reproduce

```bash
python -m pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q
QT_QPA_PLATFORM=offscreen python scripts/validate_acceptance.py
```

验收脚本会重新生成：

- `artifacts/validation/sample/`：固定配置、`height.npy`、`full.npy`、PNG 真值；
- `artifacts/validation/out/`：整幅图、逐块 PNG、块 manifest、拼接报告；
- `artifacts/validation/acceptance_summary.json`：本轮汇总；
- `artifacts/validation/gui-diagnostics.png`：桌面诊断视图截图。

## Actual Results

### Tests

```text
36 passed in 3.71s
```

覆盖：

- 配置字段、业务错误码、非法配置零输出；
- 全幅坐标高度重复性与子区域一致性；
- 跨块梯度邻域；
- 全局归一化；
- 预览后复用缓存；
- 并发 1/4 一致性；
- 内存预算与行优先等待；
- 分块异常取消；
- 摘要篡改/像素数不符补算；
- 版本变化整体失效；
- 可处理终止标记和恢复；
- 真实子进程 SIGTERM/SIGKILL；
- PySide6 诊断视图跳转。

### Fixed Sample

- 画布：512 x 384
- 网格：4 x 4
- 种子：20261125
- 配置版本：`54b891dafb31775df75b1031e43de5a0d84de6af36a242037a1698e0067655c7`
- 拼装 SHA-256：`8a7383b1049c372f35384efdb1c5ce2f6f37bd5e7d39717196926b97a1410100`
- 整幅参考 SHA-256：同上
- `max_abs_error`：`0`
- `mismatch_count`：`0`

### Preview Cache

- 首次预览（含全幅统计扫描）：约 `51 ms`
- 新进程二次预览（缓存命中）：约 `9 ms`
- 16 块全缓存重跑：`reused_tiles=16`, `rendered_tiles=0`

### Concurrency

```text
concurrency=1 SHA-256 = 8a7383b1049c372f35384efdb1c5ce2f6f37bd5e7d39717196926b97a1410100
concurrency=4 SHA-256 = 8a7383b1049c372f35384efdb1c5ce2f6f37bd5e7d39717196926b97a1410100
```

### Version Invalidation

仅改变种子后：

- `purged=true`
- `purge_reason=version_mismatch`
- `reused_tiles=0`
- 新结果 SHA-256 与旧结果不同

### Digest Repair

翻转块 `r2 c2` 的最后一个像素字节后重启：

- 检测到 `digest_mismatch`
- 记录 `E_DIGEST` 的 `cache_load` 事件
- 删除并回退补算
- 最终 `max_abs_error=0`

### Termination Recovery

`SIGTERM`：

- 首次退出码：`143`
- `interrupted.json`：存在
- 标记 signal：`15`
- 标记已完成：4 块
- 重启：复用 4 块，退出码 0，最终像素与真值哈希一致

`SIGKILL`：

- 首次退出码：`-9`
- 中断标记：不存在（不承诺报告）
- 重启：复用 4 个完整有效块，退出码 0，最终像素与真值哈希一致

### GUI

- 以 `QT_QPA_PLATFORM=offscreen` 实例化并截图成功。
- 截图：`artifacts/validation/gui-diagnostics.png`
- 可显示整幅图、4 x 4 网格、青色接缝、黄色块选中框、粉色像素十字；表格记录可跳转。

## Reports

- 汇总：`artifacts/validation/acceptance_summary.json`
- 拼接摘要：`artifacts/validation/stitch_report.json`
- 引擎拼接报告：`artifacts/validation/out/report.json`
- 块清单：`artifacts/validation/out/tiles_manifest.json`
- 整幅图：`artifacts/validation/out/full.png`
- GUI 截图：`artifacts/validation/gui-diagnostics.png`
