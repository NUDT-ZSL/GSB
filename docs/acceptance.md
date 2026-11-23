# 验收记录

执行日期：2026-11-24

## 环境

```text
WSL2 Linux
Python 3.11.18
NumPy 2.4.6
无 GPU、无 Docker、无外部服务
```

Python 3.11 虚拟环境位于 `.venv`。

## 自动化测试

```text
$ .venv/bin/python -m pytest
.................                                                        [100%]
19 passed in 1.79s
```

覆盖点：

- 同一参数/种子/计划重复生成逐字节一致；
- 三个固定种子材质预设彼此不同且各自可重复；
- 同块独立生成等于整幅对应区域，拼接字节等于整幅字节；
- 种子和相位变化改变结果；
- 首块部分结果、实际计时、估算字段；
- 零预算返回 `E_LATENCY_BUDGET` 且带完成块清单；
- 并行预览、计划提交非阻塞忙冲突；
- 材质像素/块内存/并行数资源限制与串行降级；
- 元数据修订全复用、全局参数修订后缀重算；
- 过期切片 `E_STALE_SLICE` 字段；
- 重放重新生成并核对缓存键/摘要；
- 快照重启加载、回退拒绝、JSON/校验和损坏拒绝；
- 缓存文件篡改检测；
- `E_BLOCK_MISMATCH` 首偏差像素、块号和 7 通道偏差谱；
- 自检四项全部通过。

## 可重复演示

```text
$ .venv/bin/python -m matforge demo --output artifacts/demo --fresh
ok: true
```

关键结果：

```text
首块实际观测                 7.519 ms
估算成本                     0.283 ms
已就绪状态再次查询           0.053 ms
首块像素区间                 x=0..64, y=0..48
首块内容哈希                 c2a94b695f05ffecf2f887212075769c8f81fc610ff5014cb54b8d5dcf79dfad

生成其余切片：复用 [0]，重算 [1..15]
拼接/整幅不匹配像素：0
拼接摘要：a62259dfd93ba3d530dd2f64aa462052344517cd1571b5e9a61a71a76b0aedf8

纯名称修订：复用 [0..15]，重算 []
基础色/噪声/法线修订：影响后缀 [0..15]，重算 [0..15]，复用 []
旧切片检查：E_STALE_SLICE
计划整体替换后重新生成：20/20 块
替换计划裁决不匹配像素：0
计划替换后摘要：3d290917b98cd1bfd3b01d06784b66786cc6bba27b84e347b44eb43fab0b3dc8

快照回退尝试：E_SNAPSHOT_ROLLBACK (ready 75 -> 74)
快照损坏：E_SNAPSHOT_CORRUPT
四项自检：全部 ok
```

完整输出在 `artifacts/demo/demo-report.json`。

## HTTP 应用实测

通过本地 `ThreadingHTTPServer` 实际请求：

```text
GET  /                         24,766 bytes HTML
POST /api/preview              首块 9.218 ms，计算 1.824 ms
POST /api/remaining            15 块并发生成
POST /api/verify               0 mismatched pixels
POST /api/selfcheck            4/4 checks ok
POST /api/revision             16 块后缀重算，覆盖关系成立
POST /api/replay               regenerated_and_compared_digest
POST /api/stale                E_STALE_SLICE
POST /api/preset               切换 desert-sandstone，16 块重算，裁决通过
GET  /api/texture.png          24,853 bytes RGBA PNG
```

## Windows 缓存目录

通过 WSL 访问 Windows `AppData\Local` 路径执行真实写入、读回和删除探测：

```json
{
  "ok": true,
  "path": "/mnt/c/Users/27857/AppData/Local/Temp/MatForge-cache-probe-329",
  "writable": true,
  "platform": "windows-via-wsl",
  "probe_bytes": 38
}
```

应用在原生 Windows 上默认使用 `%LOCALAPPDATA%\MatForge\cache`，同样使用 Python 标准文件系统原子替换。

## 产物

- `artifacts/demo/full-texture.png`：最终整幅纹理。
- `artifacts/demo/full-channels.npy`：最终 7 通道数组。
- `artifacts/demo/studio-preview.png`：应用同口径预览。
- `artifacts/demo/snapshot.json`：最终原子快照。
- `artifacts/demo/cache/`：内容寻址切片与接缝对象。
- `artifacts/demo/demo-report.json`：完整验收数据。
- `artifacts/presets/<name>/full-texture.png`：三个固定种子材质预设的独立样本。

