# 定点搜索精排与重排组件 — 技术说明

面向本地确定性样本的多路召回候选精排组件，完成特征融合打分、窗口重排、多样性
惩罚与定点精度可信度诊断。全部打分链路在转换为定点整数后只使用整数运算，同一
输入在任意运行、任意并发度下产出逐元素一致的结果。

## 1. 目录结构

```
ranker/
  errors.py        业务码与异常（E_INPUT_INVALID/E_CONFIG_INVALID/E_CONFIG_MISMATCH）
  fixedpoint.py    定点原语：四种舍入、位宽缩放、量化档位、整数截断
  fingerprint.py   稳定排序键、内容/特征/参数指纹（SHA-256 截断）
  atomicio.py      跨平台原子写（同目录临时文件 + os.replace 整体替换）
  config.py        精排配置与特征分组表的加载、校验、配置哈希
  loader.py        JSONL 逐行解析与校验，失败条目进失败清单且不阻断其余条目
  normalize.py     定点 minmax / zscore 归一化（guard 位宽 + 纯整数统计量）
  fusion.py        定点加权融合、温度缩放、截断，记录各转换点残差
  rerank.py        窗口贪心重排、分组整数余弦相似度、定点多样性折减
  diagnostics.py   配置档位 vs 高一档位双跑，四环节误差传播与边界敏感候选
  cache.py         三级缓存键派生与命中/失效/重算记账
  snapshot.py      阶段化快照、清单（版本/哈希/游标/阶段版本）、恢复校验
  injection.py     独立故障注入配置与四类真实故障
  pipeline.py      单请求四阶段编排（缓存复用 + 注入 + 产物组装）
  runner.py        线程池并发、信号处理、游标提交、诊断报告组装
  cli.py           命令行入口
scripts/
  generate_samples.py  固定种子样本与参数文件生成
  run_scenarios.py     四类验收场景统一入口
params/              权重、特征分组表、精排配置、候选 JSONL、注入配置
tests/               24 个 unittest 用例
```

## 2. 定点运算链路

约定 Qm.F 定点数 `value = integer * 2**-frac_bits`。

1. **转换点**：原始浮点先按 guard 位宽 `F+4` 转定点整数，记录浮点舍入残差；
   此后统计量、归一化、融合、折减全部在整数域完成，无浮点参与。
2. **归一化**：
   - minmax：`(x-min)*2^G / (max-min)` 整数除法后缩回 F 位；常量特征取上界。
   - zscore：整数均值（带余数）、整数方差、整数平方根得到标准差，再做整数 z。
   - 统计量只由同一请求内的候选决定；量化档位 `levels` 把整数段压缩到固定档位数，
     真实改变中间整数取值集合，并记录量化残差。
3. **融合**：权重与温度各自定点化（记录残差）；特征码与权重码乘积为 Q2F，除以
   `2^F` 还原为 QF 后累加，再乘 `2^F/温度码` 做温度缩放，最后按截断区间整数裁剪，
   记录温度余数与裁剪残差。
4. **重排**：按融合分降序、物品 ID 字典序决断取窗口；分组计算整数余弦相似度
   （`sqrt(dot^2 * 2^(2F) / (normA*normB))`），超过门限则按
   `score * penalty_q / 2^F` 定点折减后重新插入；相似度相等、折减后分数相等都按
   物品 ID 字典序决断。惩罚系数为 0 时直接输出纯得分排序，不沿用任何旧顺序。

舍入规则（half_even/half_up/half_down/floor）取自配置并写入报告 `fixed_point_meta`。

## 3. 误差传播诊断

对每个请求分别用配置位宽 F 与高位宽 F+1 完整执行四个环节。高档位产物先定点缩放
回 F 位量纲再逐候选比较，输出：

- 归一化（逐特征）、融合、裁剪、重排折减四个环节的整数差值；
- 每个候选首次超过 `error_tolerance`（LSB）的环节标注；
- 两种位宽下重排名次不一致的**边界敏感候选**及其主导贡献特征；
- 按名次升序的最终排序差异清单。

## 4. 快照、恢复与增量缓存

- 阶段：load / normalize / fuse / rerank；每阶段产物写同目录临时文件后
  `os.replace` 原子整体替换，清单 `manifest.json` 记录全局版本号、配置哈希、
  分组表哈希、注入哈希、已处理请求游标与各阶段产物版本。
- 启动校验三个哈希，不一致且未声明 `--incremental` 时输出 `E_CONFIG_MISMATCH`
  并以退出码 2 拒绝续跑；一致时从游标之后继续，恢复结果与不中断运行逐字段一致。
- 三级缓存键由请求内容指纹、特征/归一化码指纹、参数指纹派生：
  - 改权重/温度/截断：归一化命中，融合与重排失效级联重算；
  - 改位宽/档位/舍入：三级全部失效；
  - 增删候选：仅内容指纹变化的请求失效，其余请求直接复用；
  - 改并发度/快照间隔：属于运行期参数，不进入配置哈希，全部命中。
- 复用（hit）、失效（invalidated）、重算（miss/recompute）在报告中逐请求逐阶段
  列出，并给出汇总计数。

## 5. 并发与确定性

线程池并发处理请求，所有共享写操作（清单、失败清单）由锁保护；每个候选任务携带
`request_id + item_id` 派生的稳定排序键，结果按请求首次出现行号归并、所有清单按
确定键排序。纯 Python 整数运算无浮点累加顺序问题，因此 1/2/4/8 线程的报告逐字段
一致。报告不含时间戳等非确定内容，重复运行字节级可比。

## 6. 故障注入与退出语义

独立注入文件支持四类真实故障，命中项编号写入报告：

| 注入类型 | 阶段 | 业务码 |
| --- | --- | --- |
| non_finite_feature | normalize | E_INPUT_INVALID |
| weight_length_mismatch | fuse | E_CONFIG_INVALID |
| frac_bits_out_of_range | normalize/fuse | E_CONFIG_INVALID |
| sim_equal_threshold | rerank | 门限覆盖为与候选对相似度定点码精确相等，记录等值折减 |

单请求异常只记录请求 ID、阶段、业务码、异常类型后继续其余请求。收到 SIGTERM/
SIGINT（或 `--stop-after N` / `RANKER_STOP_AFTER` 确定性中断）时，在当前阶段清单
原子替换完成后以退出码 0 退出，续跑后结果与不中断运行一致。

## 7. Windows 兼容性

- 原子写使用 `tempfile.mkstemp` 在目标同目录创建临时文件，再 `os.replace`
  整体替换；`os.replace` 在 Windows 上原子替换已存在目标文件，语义与 POSIX 一致。
- 路径全部通过 `os.path.join`/`os.path` 处理，请求 ID 中的分隔符在落盘文件名中转义。
- 信号处理在不支持的平台静默降级；确定性中断可跨平台使用 `--stop-after`。

## 8. 运行方式

```bash
# 1. 生成固定种子样本与参数文件
python3 scripts/generate_samples.py --out-dir params --requests 12

# 2. 完整运行
python3 -m ranker.cli --candidates params/candidates.jsonl \
  --config params/rank_config.json --groups params/feature_groups.json \
  --snapshot-dir snapshots/run --report reports/run.json

# 3. 四类场景统一入口（完整/中断续跑/并发变更/注入故障）
python3 scripts/run_scenarios.py

# 4. 单元测试
python3 -m unittest discover -s tests -v
```

## 9. 诊断报告字段

`config_fingerprint`、`groups_fingerprint`、`injection_fingerprint`、
`fixed_point_meta`（位宽/档位/舍入/温度/截断）、`requests`（每请求最终排序、
折减候选对与折减量、融合分、归一化码、统计量、残差、双档诊断、缓存事件）、
`error_attribution_summary`、`cache`（汇总与逐请求复用/重算清单）、`failures`
（含行号、请求、阶段、业务码、异常类型）、`injection_items`、`manifest`、
`exit_code`/`exit_reason`。
