# 实际验证记录

执行日期：2026-11-23  
执行环境：Python 3.10.12，NumPy 1.26.4，Ubuntu 22.04 / WSL  
项目约束：本地进程运行，无 Docker、无外部服务、无未授权网络访问。

## 命令

```bash
PYTHONPATH=src python -m pytest
PYTHONPATH=src python -m panoweave demo \
  --samples samples \
  --out artifacts \
  --clean
```

## 单元与集成测试

```text
26 passed in 2.93s
```

覆盖：

- v1/v2/v3 分派、未知版本、字段数、必填字段、不可解析数值、字段偏移
- 统一内部表示与三版本语义哈希
- 升级补全、降级展平/丢弃、语义漂移、往返不一致
- 几何重采样、边界无效、视角顺序、固定种子复跑
- 曝光/颜色增益、羽化融合、三类融合业务码及位置
- 四阶段注入、归因记录、降级链路、未归因汇总
- 内存/并行/缓存资源限制
- 快照复用、整体替换、回退、损坏、并发忙
- 诊断报告一致性与 CLI 样本生成

## 全链路结果

```text
consistent: true
semantic_hash: ed1340b37691ef092cd07d199c441a7a287e172dd99de8c985f1f83891c09717
panorama_hash: d0be4aa9f696d8031dedf0dd4923c652eb7e778dd91aa5ab009a145fd27982a8
mask_hash:     d982ba67451fb973016c5c3cbd728c9923e96df777f0cdcc316032a98f67d1f6
geometry_hash: d30be79665fa72b06f042aa3a268c86e597f3738318f03a49a77745312c0b1c1
weight_hash:   89d32d37ff12356e94b6f092b9aff65ab3c952986073b62c6d049d358fd19e67
panorama.png:  956d75449bc1d865faa60a96b47943ee5ace4ce7a03be1a10552da0aaaa1619c
valid_mask.png: 84cbfb4b7246113401c4b162761058d082b1e0f3ce6737dc62c83f9d84a23e40
```

- v1、v2、v3 的全景、掩码、几何、权重和 PNG 文件哈希完全一致。
- 清空缓存后固定种子复跑，全部哈希不变。
- 有效像素 `57592`，无效边界像素 `8`；无效像素保持未填充。
- 四阶段注入分别触发 `E_FORMAT_CORRUPT`、`E_HOMOGRAPHY_SINGULAR`、`E_GAIN_INVALID`、`E_FEATHER_INVALID`，均有阶段记录、上下文和降级链路。
- 漂移探针触发 `E_SEMANTIC_DRIFT`，列出 `$.feather_radius` 与畸变链差异。
- 往返不一致探针触发 `E_ROUNDTRIP_MISMATCH`，首个差异字段为 `$.views[0].focal`。
- 快照同版本复用、跨版本整体替换、回退拒绝、校验和损坏定位、并发 `E_STAGE_BUSY` 均通过。
- 自检 15 项全部通过，`differences` 为空。

机器可读结果：`artifacts/validation_report.json`  
交互诊断页：`artifacts/diagnostic.html`

