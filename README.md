# 本地确定性指令数据构造与精度诊断工作台

面向本地小样本的确定性构造、质量打分、误差传播诊断与近重复剔除工具。运行时只读取本地 UTF-8 文件，不调用模型，不访问网络。

## 快速运行

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m workbench run --input samples --output out --fresh
```

Windows CMD：

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m workbench run --input samples --output out_win --fresh
scripts\verify.bat
```

正常执行时，如果 `output/state/latest.json` 存在且版本、配置哈希、打分参数哈希与当前输入一致，会自动从快照游标恢复。需要丢弃旧状态时加 `--fresh`。

主要输出：

| 文件 | 内容 |
| --- | --- |
| `out/candidates.jsonl` | 确定性生成的全部候选及采样权重、模板和槽位来源 |
| `out/scored.jsonl` | 每条成功候选的 fp32/fp64 完整评分链路与误差传播 |
| `out/retained.jsonl` | 去重后的代表样本 |
| `out/removed.jsonl` | 被剔除样本、代表样本 ID 和参考相似度 |
| `out/failures.jsonl` | 隔离后的逐样本失败记录 |
| `out/diagnostics.jsonl` | 长度惩罚与相似度阈值边界诊断 |
| `out/report.json` | 汇总报告、敏感清单、注入命中、快照版本和阶段耗时 |
| `out/state/latest.json` | 原子快照，含阶段、游标、摘要和版本 |

## 输入约定

`samples/` 中包含完整可运行样例：

- `seeds.jsonl`：任务类型、模板变体、可选槽位来源、参考答案。
- `vocab.json` 与 `subword_rules.json`：本地词表及最大匹配子词切分规则。
- `config.json`：任务目标数、模板权重、槽位来源权重、答案质量权重、长度惩罚、去重阈值、精度档位、误差阈值、并发数。
- `scoring_params.json`：四个质量维度的权重/基准分和本地规则参数。
- `injections.json`：缺字段、越界权重、空子词集合、故意精度敏感样本四类注入。

样例故意包含零目标、全零模板权重、零槽位来源、恰好落在软长度边界和 `1/3` Jaccard 相似度样本。

## 核心实现

### 确定性生成

生成随机数不依赖 Python `random`，而是由种子/词表/规则/生成相关配置的 SHA-256 指纹派生 128 位均匀数。模板和槽位使用 `Decimal` 权重累积选择；候选 ID、模板 ID、槽位来源、生成序号和原始采样权重全部落盘。零目标记录 `E_ZERO_TARGET`，全零模板权重或必需槽位无正权重记录 `E_ZERO_WEIGHT`，不会产生候选。

### 精度链路

每条候选都用显式 `numpy.float32` 与 `numpy.float64` 标量完成两次评分：

1. 基础维度分：指令清晰度、答案正确性、格式合规性、长度合规性。
2. 分段线性长度惩罚：`[0, soft]` 为零，`(soft, hard]` 线性增长，超过 `hard` 后饱和。
3. 惩罚后的四维分数及固定顺序加权分量。
4. 按 `instruction_clarity → answer_correctness → format_compliance → length_compliance` 固定顺序求和。

每次运算后立即转回目标精度。`scored.jsonl` 保留基础分、惩罚、维度分、权重、加权分量、总分和子词集合。误差传播按 `dimension_scoring`、`length_penalty`、`weight_multiplication`、`weighted_sum` 四级记录阶段误差与累计最大误差；总分差超过阈值时标记精度敏感，并给出首个超阈值环节。

### 边界与去重

长度惩罚使用双精度参考值判定；Jaccard 同样分别以 fp32/fp64 计算。距离阈值不超过 `boundary_epsilon` 的长度边界或样本对会进入诊断清单。若两档位对“触发惩罚”或“判为重复”的布尔结论不同，诊断项写入 `E_PRECISION_DIVERGENCE` 和全部候选 ID。

去重分组只使用 fp64 参考相似度。候选按 `(主精度总分降序, fp64 总分降序, sample_id 升序)` 排序，代表样本吸收所有与它达到阈值的未分组候选。组 ID 由排序后的成员 ID 哈希得到，因此线程调度和输入遍历顺序不改变分组、代表或剔除结果。

### 并发稳定性

打分和相似度计算都使用 `ThreadPoolExecutor`，默认并发数来自配置。任务可以乱序完成，但主线程按稳定键顺序等待并归并：

- 评分键：`sample_id|scoring`
- 相似度键：`min_id|max_id|similarity`
- 去重键：`sample_id|dedupe`

最终 JSONL 和报告均再按稳定键排序。串行与并发运行的全部业务输出应逐字节一致。

### 快照、信号与恢复

所有快照和报告都先写同目录临时文件、`fsync`，再 `os.replace` 原子替换。快照包含 schema/version、配置哈希、打分参数哈希、注入哈希、输入指纹、精度档位、阶段游标、阶段耗时、结果摘要和阶段数据。生成逐候选、打分逐候选、相似度逐样本对检查点。

启动时会校验快照版本和全部输入哈希。不匹配时输出：

```json
{"status":"error","business_code":"E_CONFIG_MISMATCH","stage":"snapshot"}
```

并返回整数退出码 `2`。一致时从游标继续。

可处理的 `SIGINT`、`SIGTERM`、Windows `SIGBREAK` 会设置停止标志；当前检查点写盘后返回退出码 `0` 并打印中断状态。强制终止只依赖最后一个完整原子快照恢复，恢复结果与未中断运行一致。

## 验证

运行单元测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

运行跨进程、信号、强杀和恢复验收：

```bash
./scripts/verify.sh
```

Windows CMD 使用：

```bat
scripts\verify.bat
```

本工作区实际记录：

- Linux：Python 3.11.0rc1、NumPy 2.4.6、7 项单元测试全部通过；`verification/results-linux.json` 全部检查为 `true`。
- Windows 11：Python 3.14.7、NumPy 2.5.2、7 项单元测试全部通过；`verification/results-windows.json` 全部检查为 `true`。Windows 路径实际触发 `SIGBREAK`，进程以 `0` 退出并停在第 1 条评分游标；随后恢复成功。`taskkill /F` 强杀后恢复也与未中断结果一致。
- 两个平台得到相同的规范报告指纹：`c3b36bbd7a85dae57bfa448fccf205db0b1551531844d212fc4136c67cfcd4a4`。

样例运行结果：

```text
生成候选             13
成功打分             10
保留                  5
剔除                  5
逐样本隔离失败        3
精度敏感样本         10
阈值判定分歧          3
注入命中              4/4
边界诊断              4
```

三项分歧来自 `1/3` 相似度恰好贴着阈值 `0.33333334`：fp32 下 `1/3` 舍入为 `0.3333333432674408`，被判为重复；fp64 参考值严格小于阈值，按业务要求判为不重复。

`stage_timings` 是唯一会随机器负载变化而不参与规范指纹的字段。`canonical_report_fingerprint` 在去除该字段后计算，其余报告字段、诊断清单、保留/剔除清单和 JSONL 都参与一致性比较。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_ZERO_TARGET` | 任务目标数为零，跳过 |
| `E_ZERO_WEIGHT` | 任务模板或必需槽位无正权重，跳过 |
| `E_MISSING_FIELD` | 候选缺少必需字段 |
| `E_OUT_OF_RANGE_WEIGHT` | 维度权重不是有限 `[0,1]` 数值 |
| `E_EMPTY_SUBWORD_SET` | 指令或参考答案子词集合为空 |
| `E_PRECISION_DIVERGENCE` | 两精度档位在边界上得出不同布尔结论 |
| `E_CONFIG_MISMATCH` | 快照与当前配置/参数/注入/精度不一致，拒绝恢复 |
| `E_SNAPSHOT_VERSION` | 快照 schema 或版本不兼容 |
| `E_SNAPSHOT_INVALID` | 快照游标或结构非法 |
| `E_TEMPLATE_RENDER` | 模板无法按槽位渲染 |

## 项目结构

```text
workbench/      核心实现与 CLI
samples/        确定性输入样本
tests/          单元与集成测试
scripts/        跨平台验收脚本
verification/   Linux/Windows 实际验收结果
out/            样例运行产物
```
