# 实际验证记录

验证日期：2026-11-25（Asia/Shanghai）

## 环境

- OS：Ubuntu 22.04.5 LTS on WSL2
- Python：3.12.13（同时用 Python 3.10.12 做了兼容性回归）
- NumPy：2.5.5（同时用 NumPy 1.26.4 做了兼容性回归）
- 无 Docker、无外部服务、样本生成不访问网络

## 自动化测试

命令：

```text
/home/zsl/.local/bin/python -m unittest discover -v
```

结果：

```text
Ran 16 tests in 2.008s
OK
```

覆盖项：

- 非法画布、网格、种子、倍频、频率、振幅、光照、浮雕、归一化、量化、资源预算、块范围均返回稳定 `E_xxx`，且不写状态/切片。
- 整幅高度场重复生成逐格完全相同；按网格拼接的块高度与整幅高度逐格完全相同。
- 1/2/3/4 列、1/2/3 行、并发度 1/2/3/4/8、预算 1 块/2 块/3 块/4 块，块灰度均与整幅对应裁剪逐像素相同。
- 块边界差分与整幅真实相邻像素差分相同。
- 任意顺序预览单块内容不变，缓存命中。
- 摘要篡改、声明像素数篡改和清单声明块文件缺失分别被识别为 `digest_mismatch`、`pixel_count_mismatch`、`missing`，并自动补算。
- 单块计算异常返回首个 `E_RENDER`，并取消剩余待处理工作。
- 版本变化时旧版本目录进入 `archive/`。
- SIGINT 在块边界退出码 75、写中断标记，续跑复用已完成块并与参照逐像素一致。
- SIGKILL 后重启仅复用有效持久化块，补齐后与参照逐像素一致。
- 首次不一致像素报告含整幅坐标、所属块和通道。

## 端到端验收

命令：

```text
/home/zsl/.local/bin/python verify_acceptance.py --root acceptance_run
```

关键结果：

```json
{
  "config_version": "75ea0c7b51d4e9e0a966e303f0c12999075ff601a448d662a0f6aa4a59ba8ce5",
  "preview": {
    "block": [1, 2],
    "first_ms": 67.133,
    "cached_ms": 1.224,
    "cached": true
  },
  "first_render": {
    "recomputed_blocks": 15,
    "reused_blocks": 1,
    "seam_max_abs_adjacent_delta": 41,
    "max_abs_diff_vs_full_reference": 0,
    "pixel_exact": true
  },
  "digest_recovery": {
    "invalid_blocks": [
      {
        "code": "E_DIGEST",
        "row": 0,
        "col": 0,
        "reason": "digest_mismatch",
        "detail": "stored content digest does not match data"
      }
    ],
    "recomputed_blocks": 1,
    "max_abs_diff_vs_full_reference": 0,
    "pixel_exact": true
  },
  "ok": true
}
```

两次预览对应同一块 `(1, 2)`；首算 67.133 ms，缓存复用 1.224 ms。预览块被整幅补齐流程直接复用，故首轮 `reused_blocks=1`、`recomputed_blocks=15`。所有 16 块拼装后与整幅直接生成结果最大逐像素差为 0。

摘要篡改演示中，块 `(0, 0)` 的内容摘要失效被明确标记为 `E_DIGEST/digest_mismatch`，只重算该块，恢复结果仍与整幅参照完全一致。

一键演示命令 `demo.py --root demo_run` 同样返回 `ok: true`、`pixel_exact: true`；预览首算 59.114 ms，缓存复用 0.815 ms，补齐 15 块并复用 1 块，接缝相邻灰度差最大 41。

## 样本确定性

分别用 Python 3.12/NumPy 2.5 与 Python 3.10/NumPy 1.26 独立运行 `generate_samples.py`，以下文件字节级一致：

```text
config.json
pattern.json
reference_height.npy
reference_gray.npy
reference_full.png
sample_manifest.json
```

样本配置版本：

```text
75ea0c7b51d4e9e0a966e303f0c12999075ff601a448d662a0f6aa4a59ba8ce5
```

整幅参照灰度：`(240, 320)`、`uint16`、值域 `0..255`、256 个离散灰度级。
