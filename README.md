# 本地确定性 CTR 精排预估管线

这是一个不依赖网络服务、GPU 或容器环境的本地 CTR 候选精排工具。它读取候选集 JSONL、特征权重参数包、精排 YAML 与异常注入清单，完成：

- 请求/候选/参数/特征组校验与失败隔离；
- 最小最大或 z 分数归一化；
- 定点整数量化（配置位宽和高一档位宽）；
- 固定索引顺序的加权融合、温度缩放、截断与确定性排序；
- 四阶段误差传播、边界敏感样本识别；
- 全局预算 + 单请求配额的确定性并发调度；
- 归一化/量化/融合/截断四阶段原子快照、游标续跑和强制终止恢复；
- 原子替换的 JSON 验证报告。

## 快速开始

要求 Windows 本地 Python 3.11，安装依赖：

```powershell
python -m pip install -r requirements.txt
```

生成确定性样本：

```powershell
python scripts\generate_samples.py
```

运行管线：

```powershell
python -m ctr_pipeline.cli run `
  --config samples\config.yaml `
  --candidates samples\candidates.jsonl `
  --params samples\params.json `
  --state-dir artifacts\state `
  --report artifacts\report.json
```

运行测试与完整验收矩阵：

```powershell
python -m unittest -v tests.test_pipeline
python scripts\verify.py
```

验收结果写入 `artifacts/validation_report.json` 与 `VALIDATION.md`。

## 数据格式

### 候选集 JSONL

支持两种行格式：

1. 每行一个候选（推荐，样本采用这种格式）：

```json
{"request_id":"req-001","item_id":"item-001-00","recall_scores":[0.42,0.17],"dense_features":[3.1,1.2],"sparse_counts":[2]}
```

2. 每行一个请求包：

```json
{"request_id":"req-001","candidates":[{"item_id":"item-001-00","recall_scores":[0.42,0.17],"dense_features":[3.1,1.2],"sparse_counts":[2]}]}
```

特征向量固定为 `recall_scores + dense_features + sparse_counts` 的索引顺序。候选 ID 在请求内必须唯一，稀疏计数必须是非负整数。

### 参数包

```json
{
  "feature_names": ["recall_a", "recall_b", "dense_ctr", "dense_price", "sparse_count"],
  "feature_weights": [0.8, 0.25, 0.55, -0.15, 0.1],
  "bias": -0.2,
  "feature_groups": {
    "recall": [0, 1],
    "dense": [2, 3],
    "sparse": [4]
  }
}
```

`feature_groups` 必须无重叠地覆盖全部权重索引，组名必须在配置的 `known_feature_groups` 中。

### 配置 YAML

关键字段如下，完整示例见 `samples/config.yaml`：

- `normalization.method`：`minmax` 或 `zscore`；
- `fixed_point.width/base/levels`：位宽、量化基数和量化档位；
- `fusion.temperature`：融合温度；
- `score.clip_min/clip_max/decimals`：截断区间与确定性舍入小数位；
- `scheduler.concurrency`：线程池最大并发；
- `scheduler.global_budget`：全局候选打分点数预算；
- `scheduler.per_request_quota`：单请求点数配额；
- `scheduler.snapshot_commit_interval`：清单提交间隔；
- `injection_list_path`：相对配置文件或绝对路径的注入清单；
- `known_feature_groups`：允许的特征组名；
- `trace.high_width`：必须等于 `fixed_point.width + 1`；
- `trace.tolerances`：四个环节的误差容差。

### 异常注入清单

```json
{
  "injections": [
    {"id":"inj-001","request_id":"req-004","stage":"normalize","candidate_id":"item-004-02","feature_index":2,"fault_type":"non_finite_feature"},
    {"id":"inj-002","request_id":"req-005","stage":"fusion","fault_type":"weight_length_mismatch"},
    {"id":"inj-003","request_id":"req-006","stage":"quantize","fault_type":"width_invalid","width":99}
  ]
}
```

支持阶段 `load/normalize/quantize/fusion/truncate` 和故障 `non_finite_feature/weight_length_mismatch/width_invalid/unknown_feature_group`。注入在对应真实阶段入口修改局部输入，再走与正常输入相同的校验路径；命中项编号会进入报告。

## 确定性计算规则

1. 每个请求按候选 ID 稳定排序，请求按请求 ID 稳定排序。
2. 归一化按请求内、特征索引维度计算：
   - `minmax`：`(x-min)/(max-min)`；分母为零时整维置零，并记录该维所有候选；
   - `zscore`：`(x-mean)/std`；标准差为零时整维置零，并记录该维所有候选。
3. 定点量化使用 `Decimal(str(value))` 与 `ROUND_HALF_UP`，缩放因子为 `base ** width`。
4. 融合公式为：
   `score = (bias + sum(q[i] * weight[i]) / scale) / temperature`
   求和严格按特征索引升序执行，再按 `score.decimals` 确定性舍入并裁剪。
5. 候选按 `score` 降序、原始多路召回分数之和降序、候选 ID 字典序排序。
6. 特征组贡献按固定组顺序输出，偏置贡献单列；修改特征权重会改变对应组贡献。
7. 高一档位宽路径与配置位宽路径对每个候选输出归一化、量化、融合、截断差值；首个超过容差的环节独立标注，排序翻转对和造成翻转的贡献组另列为边界敏感样本。

## 预算与并发

调度器先按请求 ID 顺序做确定性预算预规划：

- 每个候选消耗一个全局预算点；
- 单请求可用点数为 `min(候选数, 单请求配额, 全局剩余预算)`；
- 配额先于全局预算判定请求级失败；
- 配额超限记录 `E_REQ_QUOTA_EXCEEDED`；
- 全局预算不足记录 `E_BUDGET_EXHAUSTED`，并保留 `ready_queue_position`；
- 预算预规划结果包含完整扣减曲线。

线程池只负责纯计算。请求结果按预规划顺序由主线程提交快照，因此线程完成顺序不会影响得分、排序、贡献顺序、失败清单或预算序列。并发=1 与并发>1 使用同一预算预规划和同一哈希摘要。

## 快照与恢复

状态目录结构：

```text
state/
  manifest.json
  requests/<request-id-hash>/
    normalize.json
    quantize.json
    fusion.json
    truncate.json
    failure.json        # 仅失败请求
```

- 所有 JSON 先写同目录临时文件、`flush + fsync`，再用 `os.replace` 原子替换；
- 每个成功请求持久化四个阶段产物；
- `manifest.json` 记录 `global_version`、配置/参数/候选集哈希、`cursor_index`、已处理请求 ID、预算曲线、注入命中、失败清单和各阶段复合产物版本；
- 启动时先校验清单 schema 版本和配置哈希，不一致拒绝续跑并输出 `E_CONFIG_MISMATCH`；
- 参数包或候选集哈希不一致分别输出 `E_CONFIG_MISMATCH` / `E_INPUT_MISMATCH`；
- 一致时按游标跳过已提交前缀并继续，恢复结果与不中断运行相同；
- `--exit-after N` 用于模拟可处理退出：提交第 N 个请求后原子刷盘，报告状态 `interrupted`，退出码 0；
- `--crash-after N` 用于模拟强制终止：提交第 N 个请求并刷盘后调用 `os._exit(137)`，不执行清理；
- 真实 `SIGINT/SIGTERM` 会设置停止事件，完成当前并发窗口的提交后原子刷盘并以 0 退出。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_REQ_EMPTY` | 请求 ID 为空或请求没有候选 |
| `E_JSON_INVALID` | JSONL 行或 JSON 结构非法 |
| `E_GROUP_UNKNOWN` | 未知/缺失/重叠/越界特征组 |
| `E_WIDTH_INVALID` | 配置位宽或注入位宽越界 |
| `E_NON_FINITE` | 召回分数、稠密特征、权重或偏置非有限 |
| `E_CANDIDATE_ID_EMPTY` | 候选 ID 为空 |
| `E_CANDIDATE_ID_DUPLICATE` | 请求内候选 ID 重复 |
| `E_SHAPE_MISMATCH` | 特征容器或候选结构不匹配 |
| `E_SPARSE_INVALID` | 稀疏计数不是非负整数 |
| `E_WEIGHT_SHAPE` | 权重长度与特征维度不匹配 |
| `E_PARAM_INVALID` | 参数包或注入清单结构非法 |
| `E_CONFIG_INVALID` | 配置字段非法 |
| `E_CONFIG_MISMATCH` | 清单 schema 或配置/参数哈希与当前输入不一致 |
| `E_INPUT_MISMATCH` | 候选集哈希与快照不一致 |
| `E_STATE_INVALID` | 快照清单或游标损坏 |
| `E_REQ_QUOTA_EXCEEDED` | 单请求配额超限 |
| `E_BUDGET_EXHAUSTED` | 全局打分预算耗尽 |

## 验证结果

当前工作区已实际运行：5 个自动化测试通过；15 项端到端验收全部通过。样本运行结果：

- 16 个已处理动作：8 个成功请求、8 个失败记录；
- 全局预算 `52/52` 精确耗尽，`req-003` 被单请求配额中止，`req-013/req-014` 被全局预算中止；
- 串行与并发=4 的一致性摘要均为 `cc60b0e61126959124ad88863111dfb7dee114143a601d4693d68e84f98bb2b2`；
- 配置位宽 `3` 与高位宽 `4` 下逐字段比对均无差异，四阶段快照版本完全一致；
- `req-012` 产生一个边界敏感翻转对 `item-012-00` / `item-012-01`，翻转贡献组为 `dense`；
- 可处理退出在快照版本 3 中断并以 0 退出，续跑后摘要一致；
- 强制终止返回 137，清单游标停在 3，续跑后摘要一致；
- 配置哈希变化返回 2 与 `E_CONFIG_MISMATCH`；未知特征组与越界位宽分别返回 `E_GROUP_UNKNOWN`、`E_WIDTH_INVALID`；
- 所有快照及报告均无临时文件残留。

完整机器可读结果见 `artifacts/validation_report.json`，可读摘要见 `VALIDATION.md`。
