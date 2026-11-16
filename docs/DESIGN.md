# 设计说明

## 分层与数据模型

线格式与 JSON 区最终都归一为 `model.Record`：

```text
{name(canonical), type, class, ttl, data(canonical text)}
```

JSON 加载先解析并验证全部记录，再一次性安装到新的区快照；线格式样本应答通过同一 `NormalizeRecord` 路径进入区。完全相同的 owner/type/class/data/TTL 会合并，并保留 `json` 与 `wire-sample` 双来源。若数据相同但 TTL 不同，则视为非法区并整体拒绝。

## 线格式错误定位

`dnswire.parseName` 在每个标签位置记录已访问偏移，并执行以下检查：

- 指针目标必须位于当前指针之前；
- 指针目标必须位于报文体内；
- 展开名称不超过 255 字节，单标签不超过 63 字节；
- 指针链/标签链不得复用偏移；
- RDATA 内展开后的下一写入位置必须恰好等于 RDLENGTH 边界。

失败统一为 `biz.Error{Code:E_WIRE, Offset:n, HasOffset:true}`。前向指针同时也可阻断指针环；因此环、前向和越界都稳定返回 `E_WIRE`，不会无限展开。

## 缓存快照

缓存内部只在互斥区内交换完整 `Entry` 值：

- `Entry` 写入后不再原地修改；
- `Lookup` 返回深拷贝，并按 `WrittenAt` 与当前逻辑时钟计算剩余 TTL；
- `Put` 总是整体替换同键条目，随后分配新版本；
- 过期检查发生在锁内，命中与删除不会交错；
- 淘汰候选通过 `(access, inserted, canonicalKey)` 三元组比较，结果与 map 迭代顺序无关。

因此读者只会观察到一个完整旧版本或完整新版本。

## 单飞与胜出者

`engine.Resolve` 的路径：

1. 先查缓存；完整命中立即返回快照。
2. 未命中进入按 `name/type/class` 分组的 singleflight。
3. leader 在组内再次检查缓存，避免前一个 leader 刚完成时的竞态。
4. leader 完成本地/上游解析、冲突仲裁后，整条写入缓存。
5. follower 只接收 leader 的完整结果和同一缓存版本。

每次成功 `Put` 都会更新 `Engine.winners[key]`，其中记录版本、主要来源和来源序列号。并发测试断言 40/32 个同键请求只触发一次回填。

## 来源与版本

每个缓存条目保存：

- `PrimarySource`：`authority`、`negative` 或 `upstream`；
- `SourceSeq`：本次获胜来源的序列号；
- `WrittenAt`：逻辑写入时间；
- `Version`：缓存全局单调版本；
- 每条 RR 的 `Provenance`：来源、详细来源、匹配方式、来源序列号；
- 可选 `Conflict`：本地/上游两侧的类型、序列号、来源、值。

来源序列号或写入基准变化时不会更新旧条目的局部字段，而是构造一个全新 `Entry` 并整体替换。

## 冲突仲裁

转发结果进入引擎后按 `(owner,type)` 分组与本地精确 RRset 比较（不含通配符）。数据集合相同则不构成冲突；不同则：

1. 以本地完整 RRset 替换该转发 RRset；
2. 获胜 RR 的来源标为 `authority`、匹配方式 `conflict-authority`；
3. 记录本地与上游两侧原始值和序列号；
4. 重新校验合并后的 CNAME 链；
5. 以本地来源序列号作为条目主来源序列号。

演示中 `portal.external.test` 经上游 CNAME 到 `clash.demo.test`；上游 A 为 `198.51.100.99`，本地权威 A 为 `192.0.2.44`。最终应答保留上游 CNAME，但终端 A 使用本地权威值。

## 传输隔离

- UDP 每个数据报复制后交给独立处理协程；响应只写回该数据报的来源地址。
- TCP 每条连接有独立读循环，每个长度前缀帧生成独立 DNS 消息状态，支持连续多帧。
- UDP 降级只作用当前 UDP 响应，不修改缓存条目或 TCP 编码路径。
- 服务关闭时取消根上下文、关闭监听器及活动 TCP 连接，再等待处理协程退出。

## 本地元数据扩展

标准 DNS 头无法表达 `E_WIRE` 偏移、缓存命中或来源。服务在附加段加入 OPT RR，私有选项号为 `65002`，载荷为固定字段顺序的 JSON。标准 DNS 客户端可忽略该 OPT；本项目的 CLI 客户端读取并展示。UDP 因超过 512 字节发生降级时，该元数据随其他 RR 一起被裁掉，`TC` 位仍准确指示需要 TCP 重试。

