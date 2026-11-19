# 实际验证记录

## 环境

```text
OS: Ubuntu 22.04.5 LTS
Python: 3.11.15
NumPy: 2.4.6
第三方运行时依赖: 仅 NumPy
Docker/容器: 未使用
外部网络服务/账号: 未使用
```

Python 3.11 由 `uv python install 3.11` 安装到用户空间，项目虚拟环境位于
`.venv/`。

## 样本生成

命令：

```bash
.venv/bin/python scripts/generate_samples.py
```

结果：

```text
Generated samples in .../samples
Requests: req-0001, req-0002, req-0003
```

生成内容：

- `samples/requests.jsonl`：3 个请求，每个含 v1/v2/v3 原始配置和三组固定种子
  float64 logits 矩阵。
- `samples/vocabularies.json`：`tok-2024-01`（32）与 `tok-2025-02`（48）。
- `samples/expected.json`：三版本 token 序列、token 哈希、归一化与决策哈希。
- `samples/snapshots/bootstrap.json`：四类合法快照。
- `samples/errors/`：配置、请求和快照异常夹具。

生成器使用 SHA-256 派生 Box-Muller 样本并保留 10 位小数，不依赖无种子随机数。

## 单元与集成测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：

```text
Ran 46 tests in 0.733s
OK
```

覆盖范围：

- v1/v2/v3 读取、单位、科学计数法、字段集和原始字面量。
- 未知版本、截断头、缺失字段、非法数值、单位错误。
- v1 -> v2 补全与回读；v2 -> v1 丢弃、量化和回读。
- 参数归一化、默认项、top-k 截断和范围错误。
- 固定种子重复运行逐字节一致。
- 主采样器与独立标量 float64 参考实现逐 step 对齐。
- 三版本一致裁决和 v1 语义缺口。
- token 越界、logits 宽度错误、词表绑定错误及归因。
- 快照版本递增、原子整体替换、跨实例变化检测、损坏校验和。
- 自检正常通过和三类故意篡改检测。
- CLI demo 与菜单 EOF 退出。
- 单视图请求、默认 seed、可选 logits 和视图头不一致。

## 完整演示

命令：

```bash
.venv/bin/python app.py demo
```

关键结果：

```text
req-0001: [3, 12, 21, 7, 0, 25]
  CONSISTENT versions=['v1', 'v2', 'v3']

req-0002: [45, 47, 16, 15, 30, 16, 12]
  E_SEMANTIC_GAP
  fields=['top_p', 'repetition_penalty']
  affected_steps=[0, 1, 2, 3, 4, 5, 6]
  divergent_steps=[0, 1, 2, 3, 4, 5, 6]

req-0003: [27, 10, 16, 23, 10]
  CONSISTENT versions=['v1', 'v2', 'v3']
```

迁移演示 `req-0002`：

```text
v1 -> v2 补全:
  top_p=1.0
  repetition_penalty=1.0
  vocab_version='tok-2025-02'

v2 -> v1 丢弃:
  top_p=0.85                  SEMANTIC_LOSS
  repetition_penalty=1.1      SEMANTIC_LOSS
  vocab_version='tok-2025-02' metadata-only
  field_units                 metadata-only

v2 -> v1 量化:
  temperature '1.250e+0' -> '1.250000'
```

## 错误码演示

命令：

```bash
.venv/bin/python app.py errors
```

结果：12/12 PASS。

```text
truncated_v3_header          E_FORMAT_CORRUPT
unknown_version              E_VERSION_UNSUPPORTED
missing_top_p_v2             E_FORMAT_CORRUPT
temperature_range            E_PARAM_RANGE
top_p_range                  E_PARAM_RANGE
top_k_range                  E_PARAM_RANGE
repetition_penalty_range     E_PARAM_RANGE
token_range                  E_TOKEN_RANGE
logits_shape                 E_LOGITS_SHAPE
vocab_version_mismatch       E_VOCAB_VERSION_MISMATCH
snapshot_checksum            E_SNAPSHOT_CORRUPT
semantic_gap                 E_SEMANTIC_GAP
```

归因示例：

```text
E_TOKEN_RANGE            request_id=err-token-range
E_LOGITS_SHAPE           request_id=err-logits-shape step=2
E_VOCAB_VERSION_MISMATCH request_id=err-vocab-mismatch field=vocab_version
E_SNAPSHOT_CORRUPT       entry=sampling_decisions version=7
E_SEMANTIC_GAP           request_id=req-0002 field=top_p
```

## 自检结果

命令：

```bash
.venv/bin/python app.py self-check
```

结果：

```text
ok                      : True
decision recompute      : 3
float64 reference       : 3
migration traceability  : 3
adjudication            : 3
max reference weight Δ  : 1.110e-16
snapshot versions       : {all four entries monotonically versioned}
```

自检中的独立参考实现使用 Python 标量循环计算缩放、惩罚、排序、softmax、
top-p 和逆 CDF；主实现使用 NumPy float64 向量计算。权重最大差异
`1.1102230246251565e-16`，低于测试容差 `1e-12`。

## 快照验证

`app.py demo` 第一次运行提交四类快照，随后自检再次整体提交，版本从 1 增至 2。

验证点：

- 每个类别含独立 `version` 和 `checksum`。
- 提交通过临时文件 + `os.replace` 完成，测试确认 inode 变化。
- 第二实例提交后，第一实例重新加载检测到版本和内容变化并整体替换。
- 篡改 `samples/errors/snapshots/corrupt_checksum.json` 内容后返回
  `E_SNAPSHOT_CORRUPT entry=sampling_decisions version=7`。
- 自检比较快照内容与内存决策，篡改后返回 `E_INCONSISTENT`
  `category=snapshot_decisions`。

## 说明

- `top_p` 下界为开区间；`1.0` 作为显式“全候选保留/不截断”哨兵被接受，v1
  升级和跨版本等价样本都使用该哨兵。
- v1 没有词表绑定字段。正常请求仍用 `request.tokenizer_version` 校验词表；
  v2 -> v1 迁移明确记录绑定字段丢弃及重新水合规则。
- 运行时快照写入 `runtime/`，样本和预期结果位于 `samples/`。
