# 位置编码外推诊断工作台 — 技术说明

## 项目结构

```
peworkbench/
  __init__.py      # 全局版本号、业务码、整数退出码
  io_utils.py      # 规范化 JSON、内容哈希、临时文件+原子替换
  schemes.py       # rope / linear_interp / ntk_aware 统一位置向量接口
  pipeline.py      # encode / score / track 三阶段计算与输入校验
  injection.py     # 异常注入清单应用（真实输入故障）
  cache.py         # 内容指纹缓存键与增量失效
  snapshots.py     # 阶段快照、清单、续跑一致性判定
  sensitivity.py   # 基频/缩放因子邻域扫描与边界检测
  runner.py        # 线程池编排、稳定排序键、信号处理、报告组装
  cli.py           # 命令行入口（整数退出码）
scripts/pew        # 本地启动器（装配 .deps 中的 NumPy，调用 python3.10）
scripts/generate_samples.py
tests/test_workbench.py
```

## 环境

- Python 3.10+，NumPy（本仓库通过 `apt-get download` 将 numpy/blas/lapack/gfortran/quadmath
  的 deb 包本地解压到 `.deps/`，由 `scripts/pew` 注入 `PYTHONPATH`/`LD_LIBRARY_PATH`，无需 root）。
- 无 GPU、无网络、无外部服务；不使用 Docker。

## 使用

```bash
scripts/pew scripts/generate_samples.py --outdir data          # 生成确定性样本
scripts/pew -m peworkbench.cli run \
  --config data/config.json --samples data/samples.jsonl \
  --injections data/injections.json --workdir artifacts/demo
```

产物：`artifacts/demo/report.json`（诊断报告）、`visual_summary.txt`（ASCII 可视化摘要）、
`snapshots/`（阶段快照与清单）、`cache/`（内容指纹缓存）。

退出码：`0` 成功；`2` 配置/版本不匹配（E_CONFIG_MISMATCH，拒绝恢复）；
`3` 未知方案名（E_SCHEME_UNKNOWN）。均为整数，Windows 安全。

## 关键设计

- **统一方案接口**：三种方案均实现 `position_vectors(positions, dim, base, scale, precision)`，
  输出 (n, dim) 余弦/正弦旋转对位置向量，与查询/键特征按元素相加后做点积打分。
  方案名与版本号写入报告 `meta.schemes`。
- **内外分层**：相对距离 `|query_pos - key_pos| < train_ctx_len` 为训练长度内，否则为外推区间；
  报告给出各方案内外区间的分布（count/mean/min/max/std）、极值、分层样本计数与按距离剖面。
- **逐级误差追踪**：以 float64 高精度结果为参考，依次计算位置向量、点积打分、softmax
  注意力权重三个环节的偏差（float64 运算，JSON 保留完整有效位），标注首个超过
  `tolerance` 的环节为 `dominant_stage`，并记录位置与最大相对距离。
- **边界敏感性**：对 base_freq（×0.8–1.25）与 scale_factor（×0.5–2.0）邻域扫描外推区间
  平均打分偏差，按偏差跨度排序；检测维度级截断（精度档位下频率下溢为 0 而 float64 非零）
  与缩放溢出（频率非有限），命中时记录边界样本及其 float64 参考值。
- **异常注入**：注入清单对真实输入施加故障（NaN/Inf 特征、键位置越界、基频为零、缩放
  因子非正），由流水线校验器检出并输出 E_NONFINITE_FEATURE / E_POS_OUT_OF_RANGE /
  E_BASE_ZERO / E_SCALE_INVALID；注入命中清单含注入项编号，注入配置哈希写入报告 meta。
- **增量失效**：缓存键 = sha256(方案参数指纹(按方案实际依赖裁剪) + 样本内容指纹 +
  精度档位 + 阶段)。rope 不依赖 scale_factor，修改 scale 时 rope 全部复用；修改样本仅
  失效该样本；修改 base/train_len 失效对应阶段集合。复用与重算清单写入报告 `cache` 节。
- **并发确定性**：线程池大小取自 `max_workers`；任务排序键为 (方案名, 样本ID, 阶段名)，
  收集后统一排序，输出顺序与单线程按方案名再按样本 ID 执行逐字段一致（测试覆盖
  max_workers=4 vs 8 报告逐节相等）。
- **快照与恢复**：每阶段结果写入 `snapshots/stage-<name>.json`（临时文件 + `os.replace`
  原子替换），清单记录全局版本号、配置哈希、注入哈希、样本哈希、已完成方案与样本。
  版本/配置/注入哈希不符 → E_CONFIG_MISMATCH 拒绝恢复（`--allow-reconfigure` 可重置
  快照但保留内容缓存）；样本内容变化 → 快照失效、按缓存键部分重算。SIGINT/SIGTERM
  在完成当前阶段快照后以退出码 0 退出；SIGKILL 后仅从已完整替换的快照恢复。
- **报告**：原子写盘，含各方案内外区间统计、误差追踪摘要、敏感性排序、注入命中清单、
  复用/重算清单、快照版本号与失败清单；不含时间戳，同配置同输入字节级可复现。

## 验证结果（本地确定性样本，seed=20240501，24 样本 × 6 键 × dim=64）

- `scripts/pew -m unittest discover -s tests`：12 项测试全部通过，覆盖
  复现性（两次运行报告字节一致）、并发一致（4 vs 8 线程逐字段一致）、
  未知方案退出码 3、E_CONFIG_MISMATCH 退出码 2、单样本修改仅重算 9 项
  （3 方案 × 3 阶段）、base 修改全部重算、scale 修改 rope 全部复用（60 项）、
  快照无残留临时文件、边界截断检测、敏感性结构。
- 端到端（`artifacts/demo/report.json`）：三方案内外区间各 56/64 个打分；
  误差追踪 60 条外推记录，54 条超差（tolerance=1e-6），主导环节均为 `score`
  （位置向量偏差 ~3e-8，打分偏差最大 ~1.1e-5）；注入 4 项全部命中对应业务码；
  敏感性排序 rope/linear_interp 为 base_freq 优先，ntk_aware 为 scale_factor 优先。
- 信号与恢复：SIGTERM 中途退出码 0 且已完成阶段快照齐全；SIGKILL 后恢复运行，
  诊断各节与干净运行完全一致（复用 153 项、重算 27 项）。
