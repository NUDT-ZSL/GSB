# 本地确定性多视图三维几何重建工作台

这是一个不依赖网络、真实数据集或容器环境的本地工作台，用于验证多视图三维几何场景重建中的：

- 固定种子合成样本与可检测纹理标记；
- 特征提取、跨视图匹配、三角化、深度图融合、体素点云合并；
- 内容指纹缓存与精确失效；
- 阶段化原子快照、进程中断恢复、强杀恢复；
- 多线程执行与单线程结果逐字段一致；
- 独立故障注入配置、业务码、原子 JSON 报告。

仓库同时包含生成后的 8 视图样本、异常注入样本、自动化测试和可重复验收脚本。实际验证结果见 [docs/RESULTS.md](docs/RESULTS.md)，设计与依赖推导见 [docs/TECHNICAL.md](docs/TECHNICAL.md)。

## 环境与安装

目标环境为 Windows 10/11、Python 3.11+。运行时只依赖 `numpy` 和 `pillow`。

```powershell
cd <项目目录>
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

也可以用批处理入口：

```powershell
generate_samples.bat
run_workbench.bat run --input sample_data --config configs\default.json --state workbench_state --report reports\report.json
```

`sample_data` 已在仓库中生成。若要从固定种子重新生成：

```powershell
.\.venv\Scripts\python -m recon_workbench generate `
  --out sample_data --views 8 --seed 20261117 --width 640 --height 480 --markers 72
```

## 快速运行

```powershell
.\.venv\Scripts\python -m recon_workbench run `
  --input sample_data `
  --config configs\default.json `
  --state state\baseline `
  --report reports\baseline\report.json `
  --cloud-out reports\baseline\cloud.json `
  --depth-out reports\baseline\depth_layers.json `
  --features-out reports\baseline\features.json `
  --matches-out reports\baseline\matches.json
```

再次使用同一个 `--state` 即执行恢复；已完整的缓存产物会直接复用：

```powershell
.\.venv\Scripts\python -m recon_workbench run `
  --input sample_data --config configs\default.json --state state\baseline `
  --report reports\resume\report.json
```

需要主动忽略旧快照、但继续复用内容缓存时，加 `--fresh`。需要完全禁用缓存时，加 `--no-cache`。

## 输入格式

```text
input/
  views/
    view_0001.png
    view_0002.png
    ...
  cameras/
    view_0001.json
    view_0002.json
    ...
```

PNG 必须为固定分辨率，并以 `view_<序号>.png` 命名。相机文件与图像同序号，示例：

```json
{
  "schema_version": 1,
  "view_index": 1,
  "width": 640,
  "height": 480,
  "intrinsics": {"fx": 844.8, "fy": 633.6, "cx": 319.5, "cy": 239.5},
  "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
  "translation": [0.0, 0.0, 0.0]
}
```

`rotation` 为世界到相机旋转，`translation` 满足 `p_camera = R p_world + t`。文件索引、JSON 中的 `view_index`、图像和相机的分辨率必须一致。

## 重建配置

入口样例为 `configs/default.json`，包含：

| 段 | 参数 |
| --- | --- |
| `feature_detection` | 检测阈值、每视图最大特征数、最小像素间距、描述子采样半径 |
| `view_matching` | 描述子最大距离、最近邻比值、最小内点数、回环视图对 |
| `triangulation` | 最小视差角、最大重投影误差、深度上下界 |
| `depth_map` | 深度图宽高、量化档位、平滑迭代次数、平滑权重 |
| `pointcloud` | 体素尺寸 |
| `executor` | 线程池并发数 |
| `snapshot` | 每完成多少视图提交一次进度、快照版本 |

`seed` 写入配置。工作台本身没有未播种的蒙特卡洛随机源；合成样本中的场景、颜色、纹理码和相机扰动均由样本种子派生。

### 关键统计口径

- 视图特征数：该视图通过阈值和局部极大值抑制的特征数。
- 匹配对数：该视图参与且未被 `E_MATCH_INSUFFICIENT` 拒绝的成功匹配对数；全部尝试数见 `match_attempt_count`。
- 逐视图重建点数：该视图参与的每个成功匹配对的点数之和；一个点按其两个来源视图各计一次。
- 重建点数：所有成功匹配对的三角化点总数。
- 体素数：全局点云中非空体素数量。

## 异常注入

独立配置文件为 `configs/injection.json`。用 profile 选择互不冲突的注入集合：

```powershell
# 可恢复视图/匹配错误：损坏 PNG、相机缺失、视差越界、非有限描述子
.\.venv\Scripts\python -m recon_workbench run --input sample_data --config configs\default.json `
  --inject configs\injection.json --inject-profile recoverable --state state\inject_recoverable

# 特征、匹配、深度阶段任务异常
.\.venv\Scripts\python -m recon_workbench run --input sample_data --config configs\default.json `
  --inject configs\injection.json --inject-profile stage_exceptions --state state\inject_stage

# 体素尺寸为零、并发预算非法
.\.venv\Scripts\python -m recon_workbench run --input sample_data --config configs\default.json `
  --inject configs\injection.json --inject-profile config_invalid --state state\inject_config
```

生成带真实损坏 PNG 字节和缺失相机文件的样本副本：

```powershell
.\.venv\Scripts\python -m recon_workbench materialize-injections `
  --input sample_data --out sample_data_injected `
  --inject configs\injection.json --profile recoverable
```

所有命中的注入项编号都会出现在报告的 `injection.hits` 和 `result.injection_hits` 中。

## 业务码与退出码

| 业务码 | 含义 |
| --- | --- |
| `E_VIEW_INVALID` | 图像/相机缺失、PNG 容器或解码失败、分辨率不一致、视图序号冲突等 |
| `E_MATCH_INSUFFICIENT` | 匹配内点数低于阈值，或注入的视差越界 |
| `E_NONFINITE_FEATURE` | 特征描述向量含 `NaN`/`Inf` |
| `E_CONFIG_INVALID` | 重建配置或注入配置非法 |
| `E_CONFIG_MISMATCH` | 最新快照版本或配置哈希与本次配置不一致 |
| `E_STAGE_EXCEPTION` | 单任务在特征、匹配、深度或合并阶段抛出异常 |

| 进程退出码 | 含义 |
| --- | --- |
| `0` | 完整运行，或收到可处理退出信号后完成在途提交并中断 |
| `2` | 配置非法 |
| `3` | 快照配置/版本不匹配，拒绝续跑 |
| `4` | CLI 未预期内部错误 |

单视图错误不会终止其他视图；单匹配对失败不会终止其他匹配对。

## 输出

每轮完整运行原子写出：

- `report.json`：逐视图统计、匹配对、失败清单、业务码、注入命中、缓存复用/重算、快照和恢复点信息；
- `features.json`：视图序号、像素坐标、响应值、描述向量；
- `matches.json`：匹配对标识、来源视图、匹配标识、内点标记、三角化点；
- `depth_layers.json`：量化平滑后的完整深度层；
- `cloud.json`：全局体素点云、代表点规则、每体素合并点数；
- `state/snapshots/snapshot-XXXXXXXX.json`：阶段化进度与部分点云摘要。

点云代表点规则明确写入 `cloud.json.representative_rule`：同一体素内按有限 `(x, y, z, point_id)` 字典序排序取第一个；点云再按 `(最小来源视图, 最大来源视图, 体素 x, y, z, 代表点 id)` 输出。

## 确定性、恢复与比较

- 特征、匹配对、深度层、失败清单和点云均按显式稳定键排序，不依赖线程完成顺序。
- 全量配置哈希用于快照恢复拒绝；排除并发数和快照提交间隔的“语义配置哈希”用于结果与几何产物。因此 1 线程与多线程运行得到逐字节相同的 `features.json`、`matches.json`、`depth_layers.json`、`cloud.json` 和相同的 `report.result`。
- 恢复一致性的比较对象是 `report.json.result` 与四个几何产物。`report.run` 中允许包含恢复来源、缓存和快照序号等运行诊断字段。
- 收到 `SIGINT`/`SIGTERM`（Windows 另有 `SIGBREAK`）时，不再投递新任务，等待在途任务完成原子提交，写进度快照和中断报告，退出码 0。
- 强制终止不承诺生成最终报告；重启只读取完整原子替换的快照和内容缓存。

## 测试与验收

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts\validate_demo.py
```

验收脚本会检查 39 项条件，并生成 `validation/acceptance/summary.json`。2026-11-17 的实际执行结果：10 个自动化测试通过、39 项验收检查全部通过；8 视图基线为 641 个重建点、177 个体素，串行/并发、正常恢复、信号恢复和强杀恢复的 `report.result` 完全一致，四个几何产物逐字节一致。详细数据与 SHA-256 见 [docs/RESULTS.md](docs/RESULTS.md)。

## 当前环境说明

代码按 Windows 本地运行设计，包含 `os.replace` 原子替换、Windows `SIGBREAK` 处理、`.bat` 入口和无 POSIX 专用依赖。本次实际执行环境为 Windows 11 的 WSL2 Ubuntu（Linux 内核，Python 3.11.0rc1）；未在独立 Windows 原生 shell 中重复执行。Windows 安装与命令已在上文给出。
