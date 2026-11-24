# 固定种子样本

`generated/` 由以下命令确定性生成：

```text
python samples/generate_samples.py --out samples/generated --seed 20261125
```

主要文件：

- `events.jsonl`：乱序事件流。
- `events_batch_1.jsonl` / `events_batch_2.jsonl`：用于逆序导入测试。
- `rules_snapshot.json`：v1/v2 基线规则、观测时钟、类别默认与布局。
- `rules_snapshot_layout_v2.json`：用于布局指纹变化整体重建。
- `rules_conflict.json`：同规则同代数冲突样本。
- `patches.json`：三个补丁的合并文件。
- `patch-*.json`：可乱序导入的独立补丁。
- `truth.json`：只含同步标记与发送端原始 payload 字节，不含脱敏结论。
- `manifest.json`：种子、观测时钟和文件 SHA-256。

生成器不把自身计算的掩码值写入 truth。报告校验使用 marker 映射和
`audit_masker.oracle` 的独立参考逻辑。
