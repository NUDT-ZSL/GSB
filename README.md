# 确定性 OCR 数据处理管线与结果验证工具

纯本地、零外部数据集依赖的 OCR 文字检测/识别管线。样本由固定种子确定性生成，
仅依赖 Python 标准库与 NumPy，不使用 Docker、不访问网络。

## 环境准备（Python 3.10+）

Windows / Linux / macOS 通用：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m ocr_pipeline version
```

本次验证环境（实际执行）：Python 3.10.12、NumPy 2.2.6，独立虚拟环境 `.venv`。

## 一键演示与测试

```bash
bash scripts/run_demo.sh     # 端到端演示，产出 reports/consistency_report.json
bash scripts/run_tests.sh    # 22 个单元测试
```

也可直接调用：

```bash
python -m ocr_pipeline version
python -m ocr_pipeline demo --config config/demo_config.json --root "$PWD"
python -m ocr_pipeline validate-manifest --root "$PWD"
```

演示参数集中在 `config/demo_config.json`，其中 `injected_anomaly` 是一次注入
异常输入（负 `image_seed`），`over_budget_query` 是一次 0ms 预算的超预算查询。

## 目录结构

| 路径 | 职责 |
| --- | --- |
| `ocr_pipeline/sample.py` | 固定种子页面、真值框、任务清单生成 |
| `ocr_pipeline/templates.py` | 确定性笔画模板库、版本/校验和、字符集收缩 |
| `ocr_pipeline/detection.py` | 行投影 + 连通域检测、确定性排序、覆盖率统计 |
| `ocr_pipeline/recognition.py` | 页面级归一化、模板匹配、候选明细、去重 |
| `ocr_pipeline/chunking.py` | 分块识别、逐字节比对、延迟/内存裁决计划 |
| `ocr_pipeline/cache.py` | 复合缓存键与精确失效 |
| `ocr_pipeline/snapshots.py` | 版本化快照、原子整体替换、非阻塞提交锁 |
| `ocr_pipeline/engine.py` | 编排、缓存联动、带真实计时的交互查询 |
| `ocr_pipeline/validation.py` | 独立跨模块一致性自检 |
| `ocr_pipeline/cli.py` | 命令行演示入口 |
| `tests/` | 22 个单元测试，覆盖全部错误码与裁决分支 |
| `docs/ARCHITECTURE.md` | 架构、裁决优先级与错误码说明 |

## 八项业务要求落点

1. **确定性检测**：`detection.detect_lines` 用行投影 + 4 邻域连通域，整数坐标，
   `(y,x)` 平局规则；覆盖率统计可独立重算，测试 `test_determinism.py` 守护逐字节稳定。
2. **确定性识别与指标**：`recognition.recognize_line` 输出串/置信度/候选明细；
   `metrics` 支持由逐行明细独立重算；模板库版本变化改变打分并留痕。
3. **分块逐字节一致**：整页与分块共享同一 `NormStats`、去重与量化口径，
   `assert_byte_identical` 失败即 `E_CHUNK_MISMATCH`（含首个差异定位）。
4. **延迟预算**：`query_line` 用 `perf_counter` 实测，超预算 `E_LATENCY_BUDGET`，
   附带已完成阶段与已就绪行号，无线程/无 sleep。
5. **缓存与失效**：缓存键含图像哈希、行框摘要、库版本/校验和、参数、分块口径；
   精确失效并输出重算/复用清单，校验两者无交集且并集全覆盖。
6. **冲突裁决**：`plan_chunk_size` 按正确性 > 内存上限 > 首行延迟 > 归约开销的
   显式优先级决策，计划含理由、分块大小、受影响行与可复现哈希。
7. **快照版本化**：三类快照带单调版本与 SHA-256，原子整体替换，损坏即
   `E_SNAPSHOT_CORRUPT`，并发提交后到者立即 `E_STAGE_BUSY`。
8. **验证工具**：`validation.py` 复核覆盖、候选、缓存哈希、查询一致性、集合划分、
   行框重叠与指标重算，失败即 `E_INCONSISTENT`（含类别与 page_id）。

## 实际验证结果

- 单元测试：`Ran 22 tests ... OK`。
- 端到端演示：`status = PASS`，4 个页面字符准确率与行准确率均为 `1.0`。
- 分块（1/2/3/4/8）与整页识别逐字节一致，无 `E_CHUNK_MISMATCH`。
- 预期捕获错误：`E_LATENCY_BUDGET`（0ms 超预算查询）、`E_INVALID_INPUT`（注入异常）。
- 快照损坏演示被整体拒绝（`E_SNAPSHOT_CORRUPT`），随后以更高版本原子恢复。
- 完整报告：`reports/consistency_report.json`。
