# 实际验证结果

验证环境：Python 3.11（stdlib only），Linux/WSL（代码跨平台，Windows 运行方式见 docs/USAGE.md）。
本机无 tkinter 模块，Tkinter 界面通过 `py_compile` 语法校验；全部一致性逻辑以无头方式实测。

## 1. 测试套件（23 个用例全部通过）

```
$ python3 -m unittest discover -s tests
Ran 23 tests in 2.3s
OK
```

覆盖：
- 配置强校验：TTL/容量非正整数（含 bool 陷阱）、键字段重复、不支持字段、路径重复、
  非法逐出规则、非法样例文件 → 全部以 `E_CONFIG` 拒绝且错误位置精确（如 `routes[0].ttl`）。
- 缓存机制：LRU/LFU/FIFO 逐出选择、单一逻辑时钟下剩余 TTL 递减（5→2→0）。
- 一致性：冷未命中→新鲜命中同一条目；键组成不同不命中同一条目；未入键字段不同触发
  `KEY_COLLISION` 警告；TTL 到期触发 `REVALIDATED`；源站更新后整体替换且条目版本号 +1；
  源站不可达时陈旧条目标记 `HIT_STALE`/`STALE_SERVED`/`E_UPSTREAM`，且重新校验前绝不作为新鲜命中返回。
- 编排：端口被占用抛 `E_PORT` 且已启动进程全部清理（`processes == []`，无孤儿）。
- 注入业务码顺序：`[E_STALE, E_UPSTREAM, E_UPSTREAM, E_EVICT]` 与轨迹逐条一致。
- 快照/重放：同一输入序列跑两遍轨迹逐字节相同；重放 4 条请求全部一致；
  配置版本不一致时重放报 `E_REPLAY`（reason=config_version_mismatch）。

## 2. 四场景演示（python -m cdnlab.demo）

- 场景1 冷启动：`MISS_COLD → HIT_FRESH`，剩余 TTL 轨迹 `[4,3,2]` 单时钟递减；
  `lang` 不同的请求各自地未命中（键组成隔离）；全部结论 `CONSISTENT`。
- 场景2 源站更新：注入记录 `E_STALE`，源站版本 1→2；强制校验后 ETag
  `"f234f625a6322c9d" → "9acc410c79771637"`，条目版本号 +1，过期路径归因 `expired`。
- 场景3 逐出压力：容量 3 + LRU，注入 3 个填充键产生 4 条逐出事件，
  首个被逐出键 `path=/article.txt&lang=zh`，重新请求归因 `MISS_EVICTED`，注入码 `E_EVICT`。
- 场景4 恢复重放：快照恢复后重放 5 条请求全部一致（`replay_ok=true`，
  `first_diff_seq=null`），连续两次重放结论完全相同。

## 3. 可复现性实测

```
$ python3 -m cdnlab.demo > run1.json && python3 -m cdnlab.demo > run2.json
$ diff run1.json run2.json   # 无差异
REPRODUCIBLE: two demo runs identical
```

演示结束后 `pgrep -f cdnlab` 无残留进程（无孤儿进程）。

## 4. 已知限制

- Tkinter 界面未在本机（无显示/无 tkinter）做运行态验证，仅语法编译通过；
  在 Windows 官方 Python 3.11 环境执行 `scripts\run_workbench.ps1` 即可打开。
