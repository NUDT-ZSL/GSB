桌游出版社的图形程序员与实体棋具设计师在打样阶段需要把规则书里描述的异形棋子与棋盘地形转成可直接送3D打印的三角网格：每件资产（城堡、树木、水晶簇、山脉、河流、洞穴）以球、胶囊、立方体、棱锥等有符号距离场基元的布尔组合表达，附带基元变换、组合算子、目标网格分辨率与等值面阈值。设计师调整基元参数后要立刻看到网格与可打印性结论，打样失误往往来自CSG求交后的非流形边、破面、棋子底面不平或薄壁断裂，而分辨率过低会丢失水晶簇棱脊、过高又让网格量失控。工作台还要保证简化后仍封闭、体积误差可控，让同一件棋子在多次参数修改之间可追溯、可对比。

任务环境与准备：
在WSL Ubuntu上准备Python 3运行环境，并用pip安装NumPy与Flask；SQLite使用Python标准库sqlite3。
题目自带确定性资产生成器，按固定种子产出SDF基元组合、棋盘布局与约束参数，无需外部模型或打印软件。
运行方式为执行本地入口脚本启动Flask服务，验收脚本调用其接口或命令行子命令完成提取、检查与版本对比。

业务要求：
1. 解析桌游资产工程：读取每个资产的SDF基元（类型、局部平移/旋转/缩放、并/交/差组合算子与布尔树结构）、目标网格分辨率、等值面阈值、简化目标面数、底面平整容差与最小壁厚阈值，并读取棋盘布局中每件棋子的放置坐标与朝向，构建从资产局部坐标到棋盘坐标的变换，输出包含资产名、基元数量、约束参数的资产清单。
2. 在三维规则网格上按给定分辨率采样布尔树组合后的SDF值，用Marching Cubes提取等值面生成三角网格，网格顶点应落在解析SDF零值附近，输出每件资产的顶点、面片、顶点数与面数统计，并对棱锥、水晶簇这类尖角结构保持棱脊不塌陷。
3. 对提取出的网格执行流形性与封闭性检查：识别边界边、非流形边、重复顶点引用的退化面，以及三角面之间的相交对，按资产输出网格缺陷清单，并给出每个资产是否可用于打印的判定。
4. 依据规则中的底面平整约束，检查棋子网格落在底面平面容差带内的顶点集合，计算其最大凸起与最大凹陷偏离，标记超差区域并在结果中输出底面平整性诊断结论。
5. 采用二次误差度量网格简化将每件资产降至简化目标面数，并比较简化前后网格的封闭体积变化与顶点到原表面的最大偏差，确保简化后仍保持封闭且误差在容限内，对超出容限的资产回退到部分简化并记录回退原因。
6. 依据棋子最小壁厚约束，沿网格表面探测对侧最近表面距离得到局部壁厚场，标记低于最小壁厚阈值的连通区域并输出可打印性诊断报告，薄壁区域需与棋盘上的棋子真实尺寸对应。
7. 提供本地Flask演示服务与SQLite持久化：资产参数修改后可重新提取与校验，每次生成的网格、校验报告和体积/偏差对比按版本号存入SQLite，可回看历史版本并比较两次版本在顶点数、缺陷项与壁厚不合格区域上的差异。
8. 对取值为零或负的分辨率、阈值与面数参数，对基元运算域不匹配（交运算无重叠、差运算被减体为空）等异常输入给出明确错误码与定位信息，异常不得产生半成品网格或破坏已有版本记录。

验收：
以确定性生成的资产工程与边界样本验证等值面零值性、流形缺陷清单、底面与壁厚判定及简化前后体积误差，并核对版本记录完整。

---

## 实现与使用（工作台说明）

### 结构

- `app.py` — 本地入口脚本（CLI 子命令 + Flask 服务启动）
- `workbench/generator.py` — 确定性资产生成器（固定种子 42：城堡/树木/水晶簇/山脉/河流/洞穴 + 2 个边界样本）
- `workbench/sdf.py` — SDF 基元（球/胶囊/立方体/圆柱/圆锥/棱锥/圆环）、TRS 变换、并/交/差布尔树求值
- `workbench/mesher.py` — 规则网格采样 + Marching Tetrahedra 等值面提取（Freudenthal 四面体剖分保证封闭流形），顶点沿网格边二分求根精化到解析等值面（残差 ~1e-11），近重合顶点焊接
- `workbench/meshcheck.py` — 边界边/非流形边/退化面/三角面相交对检测，封闭体积
- `workbench/flatness.py` — 底面平整诊断（朝下顶点容差带 + 超差连通区域）
- `workbench/simplify.py` — QEM 边坍缩简化（链接条件 + 法向翻转守卫 + 移动/边长钳制）
- `workbench/thickness.py` — 沿 SDF 梯度内法向的壁厚场（出射距离与中轴估计取 max，抑制凸角/凹痕误报），薄壁连通区域输出局部与棋盘坐标
- `workbench/db.py` — SQLite 版本持久化（参数、报告、网格文件，失败不产生记录）
- `workbench/server.py` — Flask API
- `tests/smoke_test.py` — 验收冒烟测试（41 项断言）

### 运行

```bash
pip install numpy flask          # 依赖
python3 app.py generate          # 生成确定性资产工程 data/project.json
python3 app.py list              # 资产清单（名称/基元数/约束/棋盘布局）
python3 app.py extract --all     # 提取+检查+简化+壁厚，全部资产各存一个版本
python3 app.py extract castle    # 单个资产
python3 app.py report castle --version 1
python3 app.py diff castle 1 2   # 版本对比：顶点数/缺陷项/薄壁区域
python3 app.py serve --port 5000 # Flask 演示服务
python3 tests/smoke_test.py      # 冒烟测试
```

### HTTP API

- `GET  /api/project` — 工程：资产清单（名称、基元数、约束参数）+ 棋盘布局
- `GET  /api/assets` — 资产清单
- `POST /api/assets/<name>/extract` — 重新提取与校验，按版本号入库
- `GET  /api/assets/<name>/versions` — 历史版本列表
- `GET  /api/assets/<name>/versions/<v>` — 某版本完整报告
- `GET  /api/assets/<name>/diff?a=1&b=2` — 两版本差异（顶点数/缺陷项/薄壁区域）
- `POST /api/assets/<name>/params` — 修改约束或基元参数（`{"constraints": {...}}` 或 `{"primitives": [{"path": [...], "params": {...}, "transform": {...}}]}`），保存到工程文件，下一次 extract 生效

### 错误码

非法输入返回 HTTP 400（CLI 退出码 2）与结构化 JSON，不产生半成品网格、不破坏版本记录：

- `E_INVALID_RESOLUTION` / `E_INVALID_THRESHOLD` / `E_INVALID_TARGET_FACES` / `E_INVALID_TOLERANCE` — 零或负的分辨率/阈值/面数/容差
- `E_CSG_EMPTY_INTERSECTION` — 交运算无重叠（含布尔树节点定位 `node_path`）
- `E_CSG_EMPTY_SUBTRAHEND` — 差运算被减体为空
- `E_CSG_EMPTY_RESULT` — 布尔树在采样域内无等值面
- `E_ASSET_NOT_FOUND` / `E_VERSION_NOT_FOUND`
