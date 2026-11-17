# 解码策略互操作工作台（uid82）

面向本地确定性样本的 LLM 推理采样与解码策略工作台：解析 v1/v2 两个版本的
解码策略交换文件为统一中间表示，支持版本间有损/无损转换、按请求覆盖、
批量重放、增量解码与缓存失效判定、版本化快照恢复、并发解码与异常注入，
并输出可逐字段比对的采样轨迹与 JSON 诊断报告。全程不依赖网络、真实模型
权重或外部服务。

## 环境准备

```bash
python3 -m venv .venv          # Python 3.10+
.venv/bin/pip install -r requirements.txt   # numpy + pytest（离线可用缓存包）
```

Windows 控制台先执行 `chcp 65001` 确认 UTF-8 输出；所有产物路径均为相对
工作区的本地目录（默认 `out/`）。

## 目录结构

```
workbench/          # 工作台包
  policy.py         # 统一中间表示 + v1/v2 解析器（业务码校验）
  convert.py        # 双向版本转换器 + 降级损失清单/旁路元数据
  decoders.py       # greedy / sampling / beam 确定性解码器
  scoring.py        # 本地打分表（上下文指纹 -> 逐步 logits）
  requests_io.py    # 请求流 JSONL
  cache.py          # 内容指纹缓存键、原子写、失效来源分类
  snapshot.py       # 版本化快照清单与恢复判定
  runner.py         # 编辑操作集、增量运行、线程池并发、异常注入
  report.py         # JSON 诊断报告（临时文件 + 原子替换）
  cli.py            # 命令行入口
scripts/gen_samples.py   # 固定种子样本与基线生成器
samples/            # 词表、子词规则、打分表、请求流、双版本策略、
                    # 注入配置、并发/快照配置、编辑示例、基线
tests/              # 54 个 pytest 用例
```

## 快速开始

```bash
# 生成确定性样本与基线（固定种子，可重复）
.venv/bin/python scripts/gen_samples.py

# 基线运行（v2 JSON 策略，beam 解码器，4 线程）
.venv/bin/python -m workbench.cli run --config samples/config.json

# 施加编辑序列后运行（全局修改/单请求覆盖/批量重放/版本切换/删除请求）
.venv/bin/python -m workbench.cli run --config samples/config.json \
    --edits samples/edits.example.json

# 测试
.venv/bin/python -m pytest tests/ -q
```

## 交换格式

- **v1**：单行扁平键值文本，如
  `decoder=sampling temp_milli=800 top_k=40 top_p_milli=950 rp_milli=1100 seed32=12345`。
  温度/top_p/重复惩罚为千分比整数，种子为 32 位无符号整数，解码器仅
  `greedy|sampling`。
- **v2**：JSON，温度与重复惩罚为浮点，`top_p ∈ [0,1]`，种子为 64 位整数，
  解码器新增 `beam`（`beam_width`、`length_alpha`）。

解析统一为 `Policy` 中间表示（携带 `source_version` 与原始字段快照）。
未知键、类型不符、越界分别报 `E_POLICY_UNKNOWN_KEY`、`E_POLICY_TYPE`、
`E_POLICY_RANGE` 并进入失败清单，绝不静默忽略。

## 版本转换与损失旁路

- v1→v2 精确升级：千分比按精确换算表（milli/1000）转浮点，32 位种子零
  扩展为 64 位，greedy/sampling 语义保持，损失清单为空。
- v2→v1 有损降级：温度按可配置量化档位（`temp_quantum`）折叠为千分比；
  top_p 钳制到 [0,1000]；64 位种子超界时按 `seed_width_rule`
  （`truncate`/`error`）截断或报 `E_SEED_WIDTH`；beam 降级为 sampling，
  `beam_width`/`length_alpha` 写入 `out/bypass_metadata.json`。
- 降级损失清单逐字段记录原值、新值与损失类别
  （`precision`/`clamp`/`seed_width`/`capability`），按字段名稳定排序，
  同一策略重复转换逐字段一致（`tests/test_convert.py` 验证）。

## 确定性解码

- sampling 流水线顺序：重复惩罚 → 温度缩放 → top_k 截断 → top_p 核采样；
  温度为 0 退化为贪心，top_k 为 0 不截断，top_p 达 1.0（千分比 1000）
  不截断。
- 随机源：`sha256(请求指纹|种子|步号)` 派生的固定序列，同请求同策略任意
  运行逐 token 一致。
- beam 按固定移进顺序展开，按归一化分数与序列字典序稳定决断。
- 三条路径均输出每步候选得分与前 N 个候选分数，便于逐字段比对。

## 缓存、快照与恢复

- 缓存键 = `sha256(上下文指纹|策略规范化指纹|打分表指纹|解码器实现版本)`；
  失效来源区分 `policy` / `request` / `scoretable` 三级，未失效请求直接
  复用轨迹产物。
- 策略、轨迹、旁路元数据与清单均以临时文件 + `os.replace` 原子替换；
  清单记录全局版本号、策略来源版本、策略/打分表指纹、已完成请求列表、
  各产物版本与失效判定状态。
- 启动校验清单版本号与配置指纹，不符输出 `E_SNAPSHOT_STALE`，并按
  `recover_from_snapshot` 决定是否从已完整替换的产物恢复；收到
  SIGTERM/SIGINT 时完成当前请求落盘后以退出码 0 退出；强制终止后重启仅
  依据已完整替换的产物与进度记录恢复并输出恢复点（不承诺生成退出报告）。

## 并发与异常注入

- 线程池并发度取自 `max_workers`；结果按请求 ID 稳定顺序合并，并发与
  串行运行的完整结果逐字段一致（`test_concurrency_matches_serial`）。
- `samples/injection.json` 注入缺字段策略行（INJ-001）、越界参数
  （INJ-002）、未知解码器（INJ-003）、超 32 位种子降级（INJ-004）与空
  打分表条目（INJ-005）；命中编号写入报告，单请求异常记录请求 ID、阶段
  与业务码后继续其余请求。

## 等价性语义说明

需求 6 的“增量 == 全量”按如下可判定语义验证：对同一编辑序列，增量路径
（每次编辑后立即运行）的最终结果与空白工作区按相同最终策略与请求集全量
解码的结果，在逐请求 token 序列、每步候选得分、候选排序、损失标注与失败
清单上逐字段一致（`tests/test_equivalence.py`，覆盖 greedy/sampling/beam
× v1/v2 六种组合）；复用/失效清单的正确性由独立状态转移校验
（`tests/test_cache_baseline.py`）保证——空白全量解码的失效来源恒为
`missing`，故不直接参与逐字段相等比较。

## 验证结果（2026-11-17，Python 3.11，numpy 2.4.6，pytest 9.2.0）

- `pytest tests/ -q`：**54 passed**。
- 基线运行：5 个请求成功（beam），req-006 命中注入空打分表条目报
  `E_SCORE_EMPTY` 并被隔离；5 个注入项全部命中。
- 增量 == 全量：6 种解码器 × 来源版本组合逐字段一致。
- 并发 4 线程与串行结果完全一致；快照版本不符报 `E_SNAPSHOT_STALE`；
  优雅停止后进度落盘，重启从恢复点续跑且最终结果与不间断全量一致。
