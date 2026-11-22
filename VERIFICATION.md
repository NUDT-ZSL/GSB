# Verification Record

验证对象：`GlyphTrace OCR Data Lab`  
验证方式：本地确定性样本、自动化单元/集成测试、独立验收脚本。  
网络：不需要，验证过程中未访问外部资源。

## 环境

| 项 | 值 |
|---|---|
| Python | 3.10.12 |
| NumPy | 1.26.4 |
| Pillow | 11.0.0 |
| 样本数量 | 6 页 / 30 行 |
| 模板库 | `glyph-v1`，升级目标 `glyph-v2` |

## 自动化测试

命令：

```bash
python3 -m unittest discover -v
```

结果：

```text
Ran 20 tests in 19.610s

OK
```

通过的领域检查：

1. 同一数据集与配置的报告、阶段内容和注入日志逐字节复现。
2. 四阶段页面集合完全一致，行框在分数相同时按 y 后 x 排序。
3. 分块 float32 路径与整页 float64 参考路径的最大坐标偏差为 0。
4. 四阶段注入均产生阶段、业务码、页面和行号记录，重复序列日志一致。
5. 预处理/检测/识别/聚合失败均进入显式降级链并携带上游触发码。
6. 未预期异常记录为未归因并使自检返回 `E_INCONSISTENT`。
7. 第 2 阶段提交后模拟强杀，恢复从第 3 阶段继续，最终报告与不间断运行逐字节一致。
8. 已提交阶段 1/2 的页面全部复用；阶段 3/4 从未提交起点重建。
9. 快照版本单调递增；旧版本提交返回 `E_SNAPSHOT_ROLLBACK`；篡改内容返回 `E_SNAPSHOT_CORRUPT`。
10. 线程内与跨进程并发提交均立即返回 `E_STAGE_BUSY`，不阻塞等待。
11. 单页行数、候选比较和特征内存上限分别触发 `E_RESOURCE_LIMIT` 或流式降级。
12. 重复 page_id、尺寸不符、未知字符集分别返回字段明确的业务码。
13. `glyph-v2` 升级只重算含 `7`/`Q` 的受影响页面；page-0006 复用。
14. 配置版本变化整体替换四阶段旧快照。
15. 页面编辑更新清单与 PNG，并使所有旧快照与旧报告失效。
16. 静态 UI、状态 API、图像 API、运行 API、注入演示 API 和编辑 API 通过 HTTP 冒烟。
17. 两个全新状态目录的阶段快照文件逐字节一致。
18. 未知字符集参数注入触发低分辨率模板回退，仍产出非空识别串。
19. 行数、候选比较、特征内存上限与不完整阶段提交分别产生明确的业务错误。
20. 含注入场景在强制终止后恢复，报告和注入日志均与不间断运行逐字节一致。

## 验收脚本

命令：

```bash
python3 -m ocrlab verify --output artifacts/verification.json
```

关键观测值：

| 检查 | 结果 |
|---|---|
| 总体状态 | `ok = true` |
| 基线报告字节数 | `111842` |
| 基线报告 SHA-256 | `60ec5dcba8e1e41993e250e9d26c9e027e68b1da4849226a390602f38511886b` |
| 分块/参考最大坐标偏差 | `0.0` |
| 分块/参考最大分数差 | `1.32e-07` |
| 注入日志 SHA-256 | `43218025177a6b4e52de4b9e66d73660ebd19d194ba7363803a6c4e658920913` |
| 注入业务码 | 4 个阶段各 1 个 |
| 恢复字节一致 | `true` |
| 恢复起始阶段 | `3`（阶段 1/2 已提交页面复用） |
| 模板升级受影响页面 | `page-0001` 至 `page-0005` |
| 模板升级复用页面 | `page-0006` |
| 快照校验 | 4/4 阶段通过 |

完整机器可读结果：`artifacts/verification.json`。

## 复现

```bash
python3 -m unittest discover -v
python3 -m ocrlab verify --output artifacts/verification.json
python3 -m ocrlab run --reset
python3 -m ocrlab demo injection
python3 -m ocrlab demo downgrade
python3 -m ocrlab demo recovery
```

若只需重新生成本地样本：

```bash
python3 -m ocrlab init --force
```

