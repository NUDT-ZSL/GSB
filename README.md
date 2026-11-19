# RoPE 长上下文位置编码诊断工作台

面向 LLM 长上下文位置编码（RoPE 及扩展变体）的本地可视化与诊断工作台。
纯 Python 3.11 + NumPy，无其他第三方依赖，不访问网络与外部服务。

## 快速开始

```bash
python3.11 -m venv .venv && .venv/bin/pip install numpy   # 或 uv venv --python 3.11
.venv/bin/python scripts/gen_samples.py      # 生成确定性样本 data/ + cache/
.venv/bin/python scripts/acceptance.py       # 一键验收（含重跑哈希比对）
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m rope_workbench.workbench interactive   # 交互式工作台
```

## 架构

```
rope_workbench/
  errors.py     稳定业务码 + StageError（code/stage/case_id/field/message）
  config.py     RunConfig：容差、次正规保护、舍入模式、资源上限（冻结 dataclass）
  samples.py    确定性样本：JSONL 清单 + 固定 seed 的 Q/K/V（npz 缓存）
  rope.py       四变体角度表 + 交错半维配对旋转（float64）
  attention.py  分块打分 + 在线 softmax 归一化 + 整段参考路径
  validate.py   清单/参数/数组/dtype 校验 + 资源上限估算
  pipeline.py   四阶段流水线：注入点、降级链路、诊断汇总
  variants.py   跨变体一致性裁决（声明映射 + 偏差谱）
  verify.py     独立验证器（纯 Python 重算复核）
  report.py     规范化序列化 + SHA-256 复现哈希
  workbench.py  交互式诊断工作台（cmd.Cmd + ASCII 可视化）
scripts/        gen_samples.py / check_pool.py / acceptance.py
tests/          unittest 套件（31 例）
```

## 位置编码变体口径（角度映射，p=位置, i=维对, d=head_dim, s=pos_scale）

| 变体 | 角度公式 |
|---|---|
| `rope_classic` | `angle(p,i) = p * theta^(-2i/d)` |
| `position_interpolation` | `angle(p,i) = (p/s) * theta^(-2i/d)` |
| `ntk_aware` | `theta' = theta * s^(d/(d-2))`，`angle(p,i) = p * theta'^(-2i/d)` |
| `blockwise_freq` | `b=p//B`，`angle(p,i) = (p-b*B) * theta^(-2i/d) * theta^(-2b/d)` |

配对口径固定为交错半维 `(x[2i], x[2i+1])`，旋转保向量范数。
角度累计与归一化中间量均为 float64；理论角度用 longdouble 参考核对，
越界返回 `E_ANGLE_OVERFLOW`；次正规数保护介入在诊断中标记
（`subnormal_guard_triggered`），舍入模式 `nearest|toward_zero` 可配。

## 四阶段注入与降级

| 阶段 | 触发（输入/参数驱动，不改源码） | 业务码 | 降级类别 |
|---|---|---|---|
| angle | `pos_scale <= 0` | `E_INJECT_ANGLE` | `identity_angles`（恒等位置） |
| rotate | `head_dim` 为奇数，配对越界 | `E_INJECT_ROTATE` | `skip_rotation` |
| score | `block_size > seq_len` | `E_INJECT_SCORE` | `clamp_block` |
| normalize | `causal=false` 且 `mask="causal"` 冲突 | `E_INJECT_NORM` | `reuse_prev_stats`（首块均匀权重） |

另支持清单内显式注入：`{"inject": {"stage": "score", "kind": "fuzz"}}`。
注入后运行状态为 `degraded`，不得静默产出看似正常的结果；
未归因异常汇入 `unattributed` 并中止（`aborted`）。

## 业务码

`E_DUP_CASE` `E_SHAPE_INVALID` `E_PARAM_INVALID` `E_DTYPE_UNSUPPORTED`
`E_RESOURCE_LIMIT` `E_VARIANT_MISMATCH` `E_ANGLE_OVERFLOW` `E_VERIFY_FAIL`
`E_INJECT_ANGLE/ROTATE/SCORE/NORM` `E_UNATTRIBUTED`

资源上限（`RunConfig`）：`max_seq_len`、`max_block_resident_bytes`、
`max_comparisons`；`resource_policy=fail` 返回 `E_RESOURCE_LIMIT`，
`degrade` 则按 `resource_truncate` / `resource_block_clamp` 降级并标记。

## 跨变体裁决

`variants.adjudicate` 按声明映射（见 `variants.py` 模块 docstring）核对
两个变体的角表与注意力输出，输出逐维/逐位置偏差谱与映射说明；
`expect_equivalent=true` 且实测偏差超 `variant_tol` 时返回
`E_VARIANT_MISMATCH`，携带首个偏差位置 `(pos, dim)` 与变体名。

## 可复现性

- 样本由固定 seed 经 `np.random.default_rng` 生成，无系统时间/网络依赖；
- 结果哈希 = 张量 float64 原始字节 + sort_keys 规范化诊断 JSON 的 SHA-256；
- `rerun <case>` 或 `scripts/acceptance.py` 重跑比对两次完整输出哈希。

## 实测验证结果（本机，Python 3.11.15 + NumPy 2.4.6）

- 31 个 unittest 全部通过；
- 9 个内置 case：4 变体 `max_block_dev <= 3.4e-16`（容差 1e-12）；
- 四类注入均产生注入记录 + 降级记录，状态 `degraded`；
- 跨变体映射角偏差 <= 8.9e-16；独立验证器 0 问题；
- 9 个 case 重跑哈希全部 MATCH，跨进程 `run_hash` 一致
  （`7dcd1643df18f5a1...`）；
- `scripts/check_pool.py` 以 spawn 上下文创建进程池成功（Windows 兼容写法：
  `freeze_support` + `if __name__ == "__main__"` 保护）。

## 异常注入参数清单（可触发）

| 参数 | 触发值 | 效果 |
|---|---|---|
| `pos_scale` | `-1.0` | angle 阶段注入 |
| `head_dim` | `7`（奇数） | rotate 阶段配对越界 |
| `block_size` | `> seq_len` | score 阶段注入 |
| `causal=false, mask="causal"` | 组合 | normalize 阶段语义冲突 |
| `inject.stage/kind` | 任意阶段 | 显式声明注入 |
| `max_seq_len` 等上限 | 调低配置 | `E_RESOURCE_LIMIT` 或资源降级 |
| `angle_overflow_threshold` | 调低配置 | `E_ANGLE_OVERFLOW` |
