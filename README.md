# 全景分割本地专业工作台（pano_workbench）

面向全景分割（像素掩码）的本地离线工作台，核心业务问题是**交互预览延迟**与
**批量计算结果一致性**。所有样本由固定种子在本地确定性生成，不依赖 GPU、外部
数据集、账号或网络服务，也不使用 Docker。

## 环境

- Python 3.10.12（要求 >= 3.10）
- NumPy 1.26.4
- pytest 8.3.3（仅测试需要）

## 多级流水

```
三格式解析与校验 (formats.py)
  -> 掩码合并为统一全景掩码 (merge.py)
  -> 语义 mIoU 与实例 PQ 指标聚合 (metrics.py)
  -> 结果快照提交 (snapshot.py)
交互查询路径：预构建索引 (index.py) + 延迟预算控制 (query.py)
一致性保障：consistency.py（查询侧 vs 批量侧）+ selfcheck.py（跨模块自检）
```

## 业务对象与格式

- 样本：固定种子生成固定尺寸 RGB 数组（`generator.py`），真值为 v2 三元组
  （类别、实例、置信度）加忽略区。
- **v1**（version=1）：二进制头 `PANO` + 单通道类别索引掩码。
- **v2**（version=2）：二进制头 + 类别 / 实例 / 像素置信度三段载荷。
- **RLE**（version=3）：COCO 风格 JSON，含类别与实例列表、忽略区游程、
  每段实例的游程与逐像素置信度（Fortran 序，counts 以零游程开头）。
- 类别 0 为背景（参与计分），255 为忽略区哨兵（不参与计分）。

## 错误码契约

| 错误码 | 触发条件 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知版本头 / 未知魔数 / 映射表版本不一致 |
| `E_FORMAT_CORRUPT` | 头部截断、游程越界、像素总数不符（附字段偏移） |
| `E_PIXEL_CONFLICT` | 像素被赋予两个类别或两个实例 |
| `E_INSTANCE_DUP` | 实例编号在同图内跨类别复用或重复出现 |
| `E_LATENCY_BUDGET` | 单次查询超预算（附已完成阶段与已检查条目数） |
| `E_INCONSISTENT` | 查询侧与批量侧偏差 / 自检失败（附位置与两侧取值） |
| `E_SNAPSHOT_CORRUPT` | 快照损坏、校验和不符或版本非单调 |

## 使用

```bash
python3 run_demo.py                    # 脚本化端到端演示
python3 -m pano_workbench.workbench    # 交互式工作台（REPL）
python3 -m pytest tests/ -q            # 测试套件
```

REPL 命令：`view`（掩码视图）、`pan <dx> <dy>`（拖动视口）、
`query pixel <x> <y>`、`query instance <id>`、`metrics`、`snapshot`、
`errors`、`consistency`、`selfcheck`、`demo`、`quit`。

## 关键设计

- **确定性**：解析、合并、索引均提供 `canonical_bytes()` / `checksum()`，
  同一输入 + 同一配置逐字节可复现。
- **延迟预算**：以本地查询循环实际计时为观测（时钟可注入以便测试），并按
  `0.8us * 索引条目数 + 0.001us * 掩码像素数` 给出估算记录；每个阶段后检查
  预算，超预算抛 `E_LATENCY_BUDGET`。
- **一致性**：查询侧只读预构建索引（索引内嵌配置指纹、映射版本、快照版本与
  真值上下文，不依赖批量流水之外的隐式状态）；`consistency.py` 对全部计分
  像素与全部实例做查询侧 vs 批量侧逐点比对，声明精度 1e-6。
- **快照**：单调递增版本号 + 内容校验和；版本或校验和变化即整体替换；磁盘侧
  `os.replace` 原子发布，内存侧单引用原子切换，查询侧不会读到新旧混合状态；
  损坏或校验和不符抛 `E_SNAPSHOT_CORRUPT`。
- **指标**：mIoU 与 PQ 只统计双侧均未忽略的像素；实例匹配按 IoU 降序、同分
  按实例编号升序贪心；所有聚合可由逐图逐像素明细经独立代码路径
  （`recompute_from_details`）重算一致。

## 验证记录（本地确定性样本，seed=20241122，64x48）

- `python3 -m pytest tests/ -q`：**43 passed**（覆盖三格式解析与异常偏移、
  合并冲突、索引可复现、像素/实例查询、超预算、指标已知值与同分平局、
  一致性偏差定位、快照替换与损坏、自检三类别）。
- `python3 run_demo.py`：mIoU=0.842192，PQ(mean)=0.730453；逐像素查询观测
  延迟约 17us（估算 8.7us，预算 5ms）；一致性全量比对 3024 像素 + 7 实例
  全部一致；快照 v1→v2 整体替换且旧快照引用仍可校验；自检三项全部通过；
  强制超预算返回 `E_LATENCY_BUDGET` 并附 `stages_completed=["locate"]`。
