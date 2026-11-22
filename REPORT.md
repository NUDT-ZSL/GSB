# chunkmeta 技术说明与验证记录

本地对象存储分片与元数据管理组件。核心关注点：分片大小、缓存占用、
读取延迟三类换算数值在受控定点表示下参与装配、逐出与预算比较，
并诊断换算舍入与累计误差如何翻转边界结论。

## 模块结构

- `chunkmeta/errors.py` — 业务错误码与 `StoreError`（可跨进程 pickle，携带诊断字段）
- `chunkmeta/fixedpoint.py` — 受控定点换算：`to_fixed` 基于 `Fraction` 精确放大、
  最近舍入（平局向上），登记原始浮点值、舍入余量与方向；`FixedAccumulator`
  逐项保留未满一个定点单位的余量并累计误差上界
- `chunkmeta/samples.py` — 对象清单 / 桶与元数据声明 / 存储配置的解析校验，
  非法输入抛 `E_BAD_SAMPLE`，校验发生在任何落盘之前
- `chunkmeta/chunkstore.py` — 内容寻址分片目录：SHA-256 命名、临时文件 +
  硬链接/原子重命名落盘，相同内容复用同一摘要文件
- `chunkmeta/db.py` — SQLite 元数据层（WAL + BEGIN IMMEDIATE）：对象水位、
  分片提交状态、元数据版本、缓存条目版本、逻辑时钟水位、预算租约，
  单事务原子写入；建库遇并发 BUSY 退避重试
- `chunkmeta/latency.py` — 延迟装配：各环节（摘要校验/拼接/缓存查找）定点量
  累加，累计每步换算与加法的误差上界；实际耗时与预算之差小于比对容差时
  标记精度敏感并输出误差来源链（环节名、舍入余量、累加位置）
- `chunkmeta/cache.py` — 元数据缓存：容量上限校验（`E_CACHE_CAPACITY`），
  按（最近使用逻辑时钟, 占用, 键）确定性逐出；定点与 decimal 双通路
  逐条比对候选序列，分歧时输出首个分歧条目、两条通路中间值与换算步骤
- `chunkmeta/assembly.py` — 规模自适应装配：分片数超阈值切换到按摘要前缀
  分桶的批量装配（numpy 拼接），输出实际模式、触发条件、被否决模式原因与
  批量误差界；两种模式结果逐字节对照，不一致标记一致性异常
- `chunkmeta/recovery.py` — 恢复：中止半提交对象、清除半提交元数据、
  清理无引用孤片与失效登记行，只处理未完成部分
- `chunkmeta/service.py` — 编排层：文件锁 + 事务串行化提交，同对象同版本
  幂等提交，读取路径预留窗口预算（`E_BUDGET_EXHAUSTED`），
  整读 / 范围读 / 时钟回拨检测（`E_CLOCK_REGRESSION`）
- `chunkmeta/demo.py` — 端到端演示，输出 JSON 运行记录

## 错误码映射

| 场景 | 错误码 |
| --- | --- |
| 样本非法（键格式/序号断档/摘要不符/超限/类型非法/标度非正） | `E_BAD_SAMPLE` |
| 分片文件缺失 | `E_CHUNK_MISSING` |
| 分片摘要不匹配 | `E_DIGEST_MISMATCH` |
| 对象未提交即读取 | `E_OBJECT_NOT_COMMITTED` |
| 缓存容量为零或非法 | `E_CACHE_CAPACITY` |
| 逻辑时钟回拨超阈值 | `E_CLOCK_REGRESSION` |
| 并发窗口延迟预算超用 | `E_BUDGET_EXHAUSTED` |

## 关键设计

- 定点换算：`Fraction(float)` 对二进制浮点精确，同一输入重复换算逐位一致；
  余量以分数保留，累加真值 = 定点和 + 余量和，误差上界 = 逐项 |余量| 之和
- 精度敏感判定：`|total_fixed - budget_fixed| / scale < compare_tolerance`
  时，无论结论是通过还是拒绝，都标记敏感并输出误差来源链
- 双通路对照：定点通路是逐出裁决权威；decimal（50 位精度）通路作为真值
  对照。粗标度下 0.333… 与 0.25 均舍入到同一定点值（并列），decimal
  通路可分，候选序列首个分歧即被标记
- 崩溃恢复：分片落盘在事务外（内容寻址天然幂等），元数据在单事务内
  committing → committed；恢复只清理孤片与半提交行，已提交数据不受影响

## 验证记录（2026-11-23，Linux/Python 3.10，numpy+pytest）

- `python3 -m pytest tests/ -q`：**43 passed**（连续 5 轮全部通过）
  - 样本校验 11 项：合法加载 + 10 类非法输入均 `E_BAD_SAMPLE` 且不落盘
  - 定点换算 4 项：逐位一致、余量/方向登记、余量参与累加、误差上界增长
  - 分片落盘 3 项：内容寻址、重复内容单文件复用、无临时文件残留
  - 读写 7 项：清单登记、整读/范围读一致、幂等提交、元数据版本递增、
    未提交/缺片/篡改分别映射对应错误码
  - 延迟 4 项：环节累加、容差带内精度敏感（含误差链）、边界翻转、
    误差上界超预算
  - 缓存 5 项：容量非法拒绝、LRU+占用逐出、细标度双通路一致、
    粗标度分歧标记（含中间值与换算步骤）、批量模式切换
  - 自适应 3 项：阈值两侧模式选择、批量误差界、两模式逐字节一致
  - 恢复 3 项：孤片与半提交清理、恢复后整读与不中断一致、恢复幂等
  - 并发 3 项：4 进程 × 3 轮同版本只提交一次、摘要只物化一次、
    读取结果一致；时钟回拨与预算超用分别映射错误码
- `python3 -m chunkmeta.demo --samples samples --workdir /tmp/chunkmeta-demo
  --record artifacts/run_record.json`：
  - 2 个对象（3 片 / 9 片）落盘 12 个分片文件，整读与范围读全部一致
  - 3 片对象走 `precise`，9 片对象（> 阈值 4）走 `batch`，无一致性异常
  - 缓存双通路：scale=1000 无分歧；scale=10 时 `alpha`/`zeta` 定点并列、
    decimal 可分，首个分歧在索引 0 被标记
  - 恢复：识别并中止半提交对象、清除半提交元数据、清理 2 个孤片，
    恢复后重新提交整读逐字节一致
  - 完整记录见 `artifacts/run_record.json`

## 使用方式

```bash
pip install -e .[test]          # 或仅安装 numpy / pytest
python3 scripts/gen_samples.py  # 重新生成确定性样本
python3 -m chunkmeta.demo --samples samples --workdir /tmp/chunkmeta-demo
python3 -m pytest tests/ -q
```

```python
from chunkmeta import StoreService, load_samples
manifests, buckets, config = load_samples("samples")
svc = StoreService("/tmp/store", config)
for m in manifests:
    svc.ingest(m, buckets[0])
result = svc.read("media-store", "docs/readme.txt")
part = svc.range_read("media-store", "docs/readme.txt", 3, 17)
```
