# 本地确定性样本

- `v1/manifest.yaml`：v1 单文件清单，含三个环境、9 个服务、逻辑时间标度与 v1-only 服务。
- `v2/meta.sqlite`：v2 包元数据、参数声明、兼容默认语义与 6 个分片哈希。
- `v2/shards/*.json`：v2 分片清单，含绝对生效时间、代数、依赖与墓碑。
- `v2_next/*`：版本 `2.2.0` 的完整替换样本。
- `config.json`：环境顺序、容差、回退策略和输入路径。
- `clock_steps.json`：固定注入逻辑时钟序列。
- `injections.json`：默认关闭的六类受控异常。

样本故意包含：

- `orders-api` 的 v1/v2 超时冲突
- `auth-service` 同逻辑时钟下的 v1/v2 来源优先级裁决
- `payments-api` production 重试倍率分歧
- `legacy-worker` v2 墓碑
- `retired-cache` 过期条目
- `reporting-api` v1-only 兼容默认迁移

通过 `scripts/build_samples.py` 可确定性重建全部文件。
