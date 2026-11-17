# shardcheck：本地小型模型检查点管理与并行调度

`shardcheck` 是一个不依赖 GPU、训练集群或外部存储服务的确定性模拟器。它把数据并行训练、参数分片、并发检查点写入、存储预算淘汰、分片合并/拆分、阶段化快照和恢复判定放在同一个可审计流水线中。

全部训练参数使用 `int64` 定点整数表示。初始化、伪梯度、分片偏移、内容指纹、策略选择和清单版本均可由配置、数据分片描述、策略表与固定种子重复生成。

## 快速开始

系统需要 Python 3.10+。本工作区使用的缓存轮子位于 `vendor/wheels/`，默认解释器为 Python 3.11：

```bash
./scripts/bootstrap_offline.sh
.venv/bin/python scripts/generate_samples.py --out samples
```

正常路径：

```bash
.venv/bin/python -m shardcheck run \
  --samples samples --run-dir runs/baseline --fresh
.venv/bin/python -m shardcheck verify --samples samples --run-dir runs/baseline
```

异常注入路径：

```bash
.venv/bin/python -m shardcheck run \
  --samples samples --run-dir runs/injected \
  --injections samples/injections.json --fresh
```

恢复与结果比较：

```bash
.venv/bin/python -m shardcheck run --samples samples --run-dir runs/baseline
.venv/bin/python -m shardcheck compare runs/baseline runs/injected
.venv/bin/python -m shardcheck inspect --run-dir runs/injected
```

运行测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 离线依赖

`scripts/bootstrap_offline.sh` 只使用以下本地轮子：

- `pip-22.0.2-py3-none-any.whl`
- `numpy-2.4.6-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`
- `PyYAML-5.4.1-py3-none-any.whl`

虚拟环境先以 `--without-pip` 创建，再用缓存 pip 轮子引导，随后以 `--no-index --find-links vendor/wheels` 安装依赖。PyYAML 轮子是从系统已安装的 5.4.1 纯 Python 模块打包出的本地离线轮子；不使用系统 C 扩展，也不访问网络。

## 输入文件

`samples/` 由 `scripts/generate_samples.py` 以固定种子 `20261117` 生成：

- `train_config.yaml`：3 层、隐藏维度 8、词表 96、4 个数据并行工作数、2 KiB 单逻辑分片上限、4 个并发写线程、36,000 字节全局预算、12 轮训练、每轮检查点、最多保留 3 代。
- `data_shards.jsonl`：16 条稳定数据分片描述，每行含 `shard_id`、`sample_count`、`feature_digest`、`label_digest`。
- `scheduler_policy.yaml`：按当前预算占用率和剩余轮数匹配有序规则；规则显式给出保留代数、合并模式、合并阈值、写入优先级和预算动作。
- `injections.json`：七类独立异常样例，编号会进入报告。
- `sample_manifest.json`：上述输入的 SHA-256 与字节数。

## 核心模型

参数张量模板支持 `{layer}`、`hidden_size`、`vocab_size` 和 `ffn_size` 等变量。展开后形成有序张量布局，每个张量记录名称、形状、全局字节偏移和长度。

参数初值和伪梯度通过 SHAKE-256 生成。参数采用 `1e-6` 定点单位；每个工作进程的伪梯度以 `1e-9` 单位产生，元素加法后进行半远离零定点舍入。计算只使用整数，避免浮点平台差异。

每轮按 `(round, worker)` 确定数据分片，四个工作进程依次施加伪梯度。相同配置、种子、数据描述和轮次会得到逐字节相同的参数。

## 分片与检查点

模型参数流按 `shard_size_limit_bytes` 切分。每个逻辑分片包含：

- 逻辑分片 ID、工作进程 ID、块序号；
- 参数流偏移与字节数；
- 所属张量段及张量内偏移；
- 分片内容 SHA-256 指纹。

写入流程：

1. 根据策略把逻辑分片分组；`merge_small` 可把多个小分片拼接成一个物理分片，成员偏移、成员指纹和原始张量段均保留。
2. 在 `checkpoint_store/.staging-gen-NNNNNN/` 写临时分片。每个文件使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync` 提交。
3. `write_concurrency` 控制线程池大小。相同物理分片 ID 重复提交时，若已有文件指纹相同则返回 `idempotent_skip`；指纹不同则抛出 `E_SHARD_CONFLICT`，旧文件保持不变。
4. 任一分片写入失败时，其余已提交到暂存目录的任务仍会完成并被收集，但暂存代不会进入清单，整轮检查点不提交。
5. 所有分片成功后，暂存目录原子重命名为不可变代目录，再以原子替换写 `checkpoint_store/manifest.json`。清单写入是提交点。
6. 清单记录全局代数、当前代、清单版本、配置哈希、分片版本和实际分片字节数；淘汰代在清单切换完成后才删除，因此写入期间旧快照始终可读。

合并记录 `MERGE-...`，拆分记录 `SPLIT-...`。读取合并分片时按成员偏移和指纹拆回逻辑分片，并重新验证参数总长度与摘要。

## 预算与保留

每轮写入前计算：

```text
当前所有保留代的分片字节数 + 本轮待写分片字节数
```

若超过 `global_budget_bytes`，按匹配策略执行：

- `evict_then_write`：按代从旧到新淘汰，直到可容纳本轮；仍无法容纳则拒写。
- `reject`：拒写本轮，保留上一完整清单，并输出 `E_STORAGE_BUDGET_EXCEEDED`。

策略保留代数先被限制在配置上限内；非法请求会被钳制并输出 `E_ILLEGAL_RETENTION`，同时记录请求值和生效值。淘汰项包含顺序、代 ID、轮次、字节数、原因和目录是否已删除，报告可逐条复核。

## 阶段快照与恢复

每轮包含 `train`、`write`、`retention` 三个独立阶段。每个阶段完成后都原子替换 `state/snapshot.json`，其中含：

- 快照模式版本和单调递增快照版本；
- 阶段名、配置哈希、策略哈希；
- 轮次游标、活动轮次；
- 清单摘要、参数摘要、检查点代数。

启动时校验快照版本、配置哈希和策略哈希。不一致会输出 `E_SNAPSHOT_VERSION_MISMATCH` 并拒绝恢复；当前实现返回退出码 `2`，同时写 `restore_rejection.json`。

恢复时：

1. 只读取清单 `current_generation` 引用的完整代，验证物理文件大小、内容指纹、成员偏移、逻辑清单摘要和参数摘要。
2. 按固定种子重放训练到清单轮次，与恢复参数逐字节核对。
3. 依据轮次游标重放尚未形成检查点的已完成轮次，继续下一轮。
4. 清理未进入清单的暂存目录与孤儿代目录，避免混用旧分片。

这种设计使预算拒写或注入失败造成的“训练已推进、检查点未提交”轮次仍可在恢复时确定性重放。

## 可处理退出与强制终止

`SIGTERM` 和 `SIGINT` 由主进程捕获，只设置停止标志。当前轮的训练、分片提交、清单原子替换、阶段快照、进度记录和报告完成后，进程以退出码 `0` 结束。报告标识为：

```json
{"exit": {"path": "graceful_signal", "clean": true, "signal_received": 15}}
```

`SIGKILL` 无法被处理，因此不承诺退出报告。重启时如果上次 `run_state.json` 仍为 `running`，报告标识为：

```json
{"recovery": {"mode": "forced_termination_resume", "clean_previous_exit": false}}
```

本地演示脚本同时验证两类路径，并比较最终参数和清单：

```bash
.venv/bin/python scripts/termination_demo.py
```

输出位于 `artifacts/termination-demo/summary.json`。

## 异常注入

`injections.json` 中每一项都有稳定编号。支持：

- `budget_occupancy_out_of_range`：把策略查询占用率设为越界值，钳制后继续，记录 `E_BUDGET_OCCUPANCY_OUT_OF_RANGE`。
- `illegal_retention`：请求超过配置上限的保留代数，钳制后继续，记录 `E_ILLEGAL_RETENTION`。
- `oversized_shard`：在生成/校验阶段制造超限分片，记录 `E_SHARD_SIZE_EXCEEDED`。
- `merge_exception`：单个合并组抛异常，其余组合并继续，记录 `E_MERGE_FAILED`。
- `fingerprint_conflict`：同一物理分片以不同指纹重复提交，记录 `E_SHARD_CONFLICT`，原内容不覆盖。
- `generate_exception`：单逻辑分片生成阶段异常，记录 `E_SHARD_GENERATION_FAILED`。
- `write_exception`：单物理分片写入异常，其余写入继续，记录 `E_SHARD_WRITE_FAILED`。

所有失败项包含 `round`、`shard_id`、`stage`、`code`、`exception_type`、`injection_id` 和 `fatal`。注入命中另行汇总在 `injection_hits`。

## 报告结构

每次完整运行以临时文件加原子替换写 `report.json`，主要字段：

- `config_hash`、`policy_hash`、`data_hash`、`injection_source_hash`；
- `rounds[]`：逐轮策略规则、预算占用、决策、合并/拆分、写操作、淘汰和失败；
- `generations[]` 与 `final_shards[]`：保留代和当前物理分片清单；
- `operations`：合并、拆分、淘汰、写入操作编号与顺序；
- `final_params_digest`、`parameter_layout`、`final_parameters.bin`；
- `recovery` 与 `exit`：恢复路径和退出路径；
- `failures[]`、`injection_hits[]`；
- `consistency.checks`：预算、保留上限、清单字节、指纹、无损拆分、恢复参数、快照哈希等检查。

`compare` 比较两个报告的确定性字段，排除只表示运行路径的 `recovery` 与 `exit`。

## 已执行验证

- 正常路径：12 轮全部提交，预算内最终保留 1 代，合并/拆分无损，最终参数摘要 `46762009...f4d5b1`。
- 注入路径：7 个注入编号全部命中；第 6–10 轮按注入失败且不提交，第 11–12 轮继续提交；最终参数与正常路径完全一致。
- 预算拒写：将策略预算动作改为 `reject` 后，第 2 轮输出 `E_STORAGE_BUDGET_EXCEEDED`，当前清单仍指向第 1 轮完整代。
- 快照拒绝：篡改快照配置哈希后返回 `E_SNAPSHOT_VERSION_MISMATCH` 和退出码 `2`。
- 退出路径：`SIGTERM` 完成当轮后退出码 `0`；继续运行后报告确定性字段与基线一致。
- 强制终止：在写暂存阶段 `SIGKILL`，无退出报告；重启识别 `forced_termination_resume`，清理暂存目录，最终参数与清单摘要与基线一致。
- 自动化测试：12 个用例全部通过（约 13 秒）。

持久化的验证报告位于 `artifacts/verification/`。
