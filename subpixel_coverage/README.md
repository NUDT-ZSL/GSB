# 品牌包装次像素覆盖率组件

面向护肤品/食品包装打样的可集成 Python 领域组件。读入固定种子确定性生成的包装工程，
对细于一个像素的纤维与喷砂颗粒做**解析次像素覆盖率**计算，提供逐纤维可追溯诊断、
面积守恒缩略图、逆光轮廓连续性核验、局部增量重算与 SQLite 持久化，并附带本地 Flask 演示服务。

## 环境与安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt
pip install -e .
```

仅依赖 `numpy` 与 `Flask`，PNG 编码与 SQLite 均使用标准库实现。

## 一分钟跑通

```bash
# 1. 生成确定性包装工程与解析参考数据（data/ 目录）
python3 scripts/generate_sample.py

# 2. 全流程：校验 -> 覆盖率 -> 参考比对 -> 缩略图 -> 轮廓 -> 增量 -> 提交 SQLite
python3 scripts/run_pipeline.py

# 3. 启动本地演示服务（首次运行自动建表）
python3 scripts/run_server.py --port 5050
```

浏览器访问 `http://127.0.0.1:5050`（若环境配置了 HTTP 代理，请对 `127.0.0.1` 绕过代理）。

## 领域流水线与业务要求对应

| 业务要求 | 实现位置 |
| --- | --- |
| 工程装载与曲线一致性校验、摘要与问题清单 | `subpixel/validation.py`, `subpixel/models.py` |
| 相机投影 + 解析线段/像素方格求交的次像素覆盖率 | `subpixel/geometry.py`, `subpixel/rasterizer.py` |
| 不透明度累加、逐纤维贡献表、材质区域统计 | `subpixel/rasterizer.py`, `subpixel/materials.py` |
| 面积平均缩略图、守恒核验、直接渲染对比、衰减/断裂标记 | `subpixel/thumbnail.py` |
| 局部参数调整与增量重算、未触及像素冻结、耗时记录 | `subpixel/incremental.py` |
| 逆光轮廓连续性核验与责任纤维定位 | `subpixel/contour.py` |
| SQLite 提交、版本识别、断点续算 | `subpixel/persistence.py` |
| Flask 视图与局部重算参数接口 | `subpixel/app.py`, `subpixel/templates/` |

### 三套独立覆盖率估计

- `analytic_capsule`（主算法）：胶囊多边形与像素方格做 Sutherland-Hodgman 解析裁剪。
- `analytic_reference`（独立解析参考）：不同像素枚举策略 + 更密的多边形离散。
- `supersample_*`（随机超采样旁证）：抖动蒙特卡洛，均值误差极小，单像素含采样噪声。

验收以两套独立解析法对比为准，默认像素容差 `0.02`。

## HTTP 接口

- `GET /` 总览；`/fibers` 逐纤维贡献；`/thumbnail` 缩略图对比；`/contour` 轮廓诊断
- `GET /image/coverage.png`、`/image/reference.png`、`/image/contour.png`、`/image/thumbnail.png`
- `GET /api/fibers/<curve_id>` 单根纤维逐像素贡献
- `GET|POST /api/recompute` 触发局部重算，返回受影响像素与缩略图块范围
  - 曲线参数：`curve=<id>:<radius_px>:<opacity>`（半径/不透明度可留空）
  - 密度调整：`density=<kind>:<material>:<surface>:u0:v0:u1:v1:<add>:<remove>`
- `POST /api/commit` 提交当前结果；`POST /api/resume` 从最近一次提交续算

示例：

```bash
curl --noproxy '*' \
  "http://127.0.0.1:5050/api/recompute?curve=FB-0001:1.35:0.97&density=grain:frosted_glass:body:-1.8:0.4:-0.6:1.2:6:2"
```

## 测试

```bash
python3 -m unittest discover -s tests
```

覆盖几何裁剪正确性、确定性生成、曲线校验、独立解析参考容差、缩略图总量守恒、
增量与全量一致且未触及像素不变、轮廓断裂定位、SQLite 版本漂移拦截与全部 Flask 路由。

## 数据与产物

- `data/packaging_project.json` 确定性包装工程
- `data/reference_coverage.json` 解析参考覆盖摘要
- `data/coverage.sqlite3` 首次运行自动创建的 SQLite 数据库（路径可写时才启动）
