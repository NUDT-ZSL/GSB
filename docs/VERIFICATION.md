# 验证记录（可复现）

开发机环境：WSL2 Ubuntu 22.04，Python 3.10.12，numpy 1.26.4，pytest 8.2.0。
目标 Windows 环境为 Python 3.11+，代码仅依赖 numpy，跨平台路径均通过 `pathlib` 处理。
下列命令在 Windows PowerShell 中把路径分隔符换成 `\` 即可等价复现。

## 1. 自动化测试

```powershell
python -m pytest -q
```

结果：`26 passed`。覆盖噪声确定性/分块无关性、九类校验业务码、多网格整幅一致性、
权重归一化、原子持久化、版本敏感、可处理终止、强制终止子进程恢复、损坏摘要重做、
异常注入与内存预算排队。

## 2. 整幅与分块逐像素一致性

对固定样本四个材质，分别整幅直算与分块拼接后逐像素比较：

| 材质 | 网格 | 分块数 | 精确最大绝对误差 | 容差 | 结论 |
| --- | --- | --- | --- | --- | --- |
| mat_brick | 2x2 | 4 | 1.110e-16 | 1e-6 | 通过 |
| mat_metal | 2x4 | 8 | 2.220e-16 | 1e-6 | 通过 |
| mat_stone | 2x3 | 6 | 2.220e-16 | 1e-6 | 通过 |
| mat_fabric | 3x3 | 9 | 2.220e-16 | 1e-6 | 通过 |

误差仅来自浮点求和顺序，远严于规格容差；报告字段 `consistency.passed=true`。

## 3. 异常注入业务码

`run --injections samples/fixed/injections.json` 的 `run_report.json` 汇总：

| 注入项 | 类型 | 受影响材质 | 业务码 | 其余材质 |
| --- | --- | --- | --- | --- |
| INJ_AMPLITUDE | illegal_amplitude | mat_brick | E_AMPLITUDE_INVALID | 继续处理 |
| INJ_GRID | grid_indivisible | mat_metal | E_GRID_DIVISIBILITY | 继续处理 |
| INJ_CHANNEL | missing_channel | mat_stone | E_CHANNEL_MISSING | 继续处理 |
| INJ_DIGEST | corrupt_chunk_digest | mat_fabric | E_CHUNK_DIGEST_MISMATCH | 重做后通过 |

注入只改写内存中的输入数据或状态目录摘要文件，不修改源码与原始样本。

## 4. 中断与恢复

* 可处理终止：预置停止标记运行，退出码 `10`，仅原子提交 1 个分块并写
  `interrupted.json`（含 `next_index` 与版本标识）；`recover` 后复用该块并补齐，
  全部一致性通过。
* 强制终止：`--force-crash-after 2` 以退出码 `137` 硬退出，无报告、无 `.tmp` 残留，
  仅两块原子落盘；重启 `recover` 后恢复块索引 `[0,1]` 并补齐剩余分块。
* 损坏摘要：`corrupt-chunk` 后重跑返回 `E_CHUNK_DIGEST_MISMATCH`，该材质整体重做，
  无新旧版本切片混拼。

## 5. 并发与资源上限

内存预算小于单块体积时引擎不死锁（单块超预算且无在途块时放行），预算足够小时
分块按材质/索引序串行等待，倍频数与精度不变，一致性仍为机器精度。

## 6. 原子导出与版本归档

同版本重复运行原地原子替换；修改种子后版本标识变化，旧输出整体移动到
`build/archive/<material>/<old_version>_<timestamp>/`，新输出独立写入，
可通过 `archive-list` 查询。
