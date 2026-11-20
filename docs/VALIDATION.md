# 实际验证记录

验证日期：2026-11-21  
环境：Python 3.10.12，NumPy 1.26.4，Linux/WSL2，本地 CPU 进程池。

## 1. 测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 20 tests
OK
```

覆盖项包括：

- 样本生成确定性、异常比例与内部标记。
- 重复 ID、特征维度、未知方法、非正邻居/树数、非正预算。
- 三种方法重复执行、分块/整体一致性、同分 `sample_id` 排名。
- 指标独立重算与 `E_INCONSISTENT` 类别。
- 预算锚点、降级、拒绝理由与批次内存约束。
- `spawn` 进程池、worker 错误码/阶段/方法/批次归因、单任务失败隔离。
- 快照整体替换、回退、损坏、stage 忙、账本原子预留/释放。
- 完整双场景实验与两次运行规范化输出一致。

## 2. 样本

```bash
python3 -m anomaly_engine.cli generate --output data/demo.jsonl
```

- 样本数：600
- 特征维度：5
- 正态簇：6
- 注入异常：48
- 实际异常比例：0.08
- JSONL 文件 SHA-256：`4105c6e4828b6e85dbfeece66b92b2f727509ab1c15e1335597e6543ee7a21b7`

## 3. 基线

- 候选数：6
- 准入：6
- 降级：0
- 拒绝：0
- 估算比较次数：3,142,133 / 8,000,000
- 剩余比较：4,857,867
- 峰值批次估算内存：2,147,896 / 8,388,608 字节
- 并行/串行及分块/整体：全部一致，最大绝对偏差 0.0

| 方法 | 候选 | 关键参数 | AUC | AP | 配额检出率 |
| --- | --- | --- | ---: | ---: | ---: |
| isolation_forest | isolation_forest-00 | 48 trees, 160 rows | 0.931688 | 0.617258 | 0.666667 |
| isolation_forest | isolation_forest-01 | 96 trees, 192 rows | 0.942331 | 0.619565 | 0.666667 |
| knn | knn-00 | k=3 | 0.996754 | 0.972062 | 0.958333 |
| knn | knn-01 | k=7 | 0.995811 | 0.964458 | 0.958333 |
| knn | knn-02 | k=15 | 0.994188 | 0.954905 | 0.937500 |
| mahalanobis | mahalanobis-00 | full covariance | 0.973090 | 0.893644 | 0.854167 |

## 4. 预算冲突

- 比较上限：430,000
- 内存上限：350,000 字节
- 准入：3
- 降级：2
- 拒绝：3
- 实际账本：428,736 / 430,000
- 剩余比较：1,264
- 业务码：`E_COMPARE_BUDGET`、`E_MEMORY_LIMIT`

确定性裁决结果：

| 候选 | 结果 | 原参数 -> 有效参数 | 理由 |
| --- | --- | --- | --- |
| isolation_forest-01 | 降级 | 96 -> 17 trees | 内存由 1,802,880 降至 346,752 字节 |
| isolation_forest-00 | 降级 | 48 -> 14 trees | 内存降级后，再按剩余比较预算降至 14 trees |
| knn-02 | 准入 | k=15 | 368,400 次比较 |
| mahalanobis-00 | 拒绝 | full covariance | `E_COMPARE_BUDGET` |
| knn-01 | 拒绝 | k=7 | 基础两两比较成本已超过剩余预算 |
| knn-00 | 拒绝 | k=3 | 基础两两比较成本已超过剩余预算 |

冲突场景执行的三个任务仍全部通过并行/串行及分块/整体核对，最大偏差为 0.0。

## 5. 快照

完整实验后：

```json
{
  "current_versions": {
    "plan": 2,
    "budget": 2,
    "result": 2
  },
  "history_versions": {
    "plan": [1],
    "budget": [1],
    "result": [1]
  }
}
```

基线提交版本 1，冲突场景以版本 2 整体替换；旧版本对象未被修改。

## 6. 重跑哈希

两次独立 CLI 运行：

```bash
python3 -m anomaly_engine.cli run ... --output artifacts/repro_first.json
python3 -m anomaly_engine.cli run ... --output artifacts/repro_second.json
```

结果：

```text
9235dae380fe8737cfbf30159e7b22482876742c696fdfd660630894552b7b31  artifacts/repro_first.json
9235dae380fe8737cfbf30159e7b22482876742c696fdfd660630894552b7b31  artifacts/repro_second.json
BYTE_IDENTICAL
```

`--check-rerun` 的进程内双次验证：

```json
{
  "checked": true,
  "repeats": 2,
  "identical": true,
  "core_sha256": "a3b9ae5ee0dc8897fa8d6a56bd3974d27c7ac33192e53bdd8b8e699b74d5171f",
  "canonicalization": "sorted-keys/compact/utf8"
}
```
