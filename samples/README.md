# 固定样本说明

- `registry.json`：注册清单与状态机默认参数。
- `clocks.json`：注入逻辑时钟映射。
- `shards/`：4 个完整分片，包含乱序、精确重叠、v1/v2 等价重叠和故意缺洞。
- `streams/monolithic.json`：同一观测集合的单流对照，用于验证分片归并收敛。
- `invalid/`：可隔离的错误样本，覆盖摘要、实例引用、截断、冲突、版本冲突、时钟回拨和重复服务名。

重新生成：

```bash
python tools/build_samples.py
```

摘要会随固定内容确定生成，重复运行不会改变有效样本。
