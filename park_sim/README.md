# 乐园排队区策略小游戏 — 结算内核（parksim）

纯 Python 3 标准库实现，无第三方依赖。面向运营策划的本机可复核结算工具：
输入乐园拓扑、初始资源、逐回合天气/客流修正与玩家指令序列，输出逐回合消耗
明细、越界告警、设施满意度与可回放决策轨迹。

## 环境

- Linux（WSL Ubuntu）+ Python 3.8+，仅标准库。
- 克隆/拷贝本目录后即可运行，无需安装。

## 快速开始

```bash
cd park_sim

# 1. 生成确定性样本（固定种子，输出可复现）
python3 -m parksim gen --seed 42 --scenario normal   --out samples/normal.json
python3 -m parksim gen --seed 42 --scenario overflow --out samples/overflow.json

# 2. 校验配置（只校验不推演）
python3 -m parksim check samples/normal.json

# 3. 推演并输出报告（文本 + JSON）
python3 -m parksim run samples/normal.json --json reports/normal.json --text reports/normal.txt

# 4. 修改关卡参数后对比：列出消耗明细发生变化的回合
python3 -m parksim diff samples/normal.json samples/normal_modified.json

# 5. 跑手算对照测试
python3 -m unittest discover -s tests -v
```

`overflow` 样本故意造成体力/补给越界，用于验证告警与拒绝政策。

## 结算口径（全部整数运算，无浮点）

- 基础消耗：`stamina(d) = d`，`supply(d) = (d+1)//2`（d 为最短路径距离）。
- 修正规则：修正后值 = 修正前值 × 千分比因子 // 1000（整数除法，**向下取整**）。
  每回合先按配置顺序施加天气事件、再施加客流系数；每施加一个因子立即取整
  一次，并在报告中记录一条修正轨迹（来源、因子、修正前后值）。
- 不可达：任意两点不可达时距离为 `null`（JSON），绝不给出虚假数值；
  MOVE 路线含不可达段时整条指令被拒绝并告警。
- 多途经点路线：`MOVE A B C` 逐段取最短路径并汇总消耗。
- 拒绝政策（`--policy`）：
  - `skip`（默认）：整路线预检，资源不足则整条指令不执行，继续后续回合；
  - `truncate`：逐段行军，走到资源不足为止，已走段落生效；
  - `abort`：告警后立即中止整局推演。
- 满意度（0–100）：`40 + 剩余体力比*30//1000 + 补给结余比*20//1000 + 访问时长占比*10//1000`
  （各比值均为千分比整数）。未访问目标计 0 分并单独列出；整体评分 =
  总分 // 目标数（未访问计入分母）。

## 配置文件（关卡参数修改入口）

配置为单个 JSON（`schema_version: 1`），可直接编辑后重新推演：

- `topology.nodes`：`{name, kind}`，kind ∈ `facility/supply/rest/gate`
- `topology.edges`：`{a, b, distance, bidirectional}`，距离为正整数
- `total_rounds / start_node / init_stamina / init_supply / supply_capacity`
- `targets`：目标设施访问顺序
- `weather`：`{round, name, stamina_factor, supply_factor}`（千分比，1000=不修正）
- `crowd`：`{round, stamina_factor, supply_factor}`
- `commands`：`{kind: MOVE, waypoints:[...]}` / `{kind: RESUPPLY|REST, amount}` / `{kind: WAIT}`，
  第 i 条指令对应第 i 回合，不足部分按 WAIT 处理

修改配置后 `diff` 子命令会对新旧配置各自从零全量重算（确定性保证两者
逐字段一致），并列出消耗明细发生变化的回合号。

## 输入校验

校验失败即拒绝推演并给出可定位整数位置（列表序号/回合号，从 1 开始）：
节点悬空引用、负/零距离、重复路径、天气/客流回合越界、指令引用不存在
节点、目标非设施等。示例输出：`[edge #2] 路径距离为负数: -3`。

## 目录结构

```
parksim/
  model.py      数据模型          graph.py     图与基础消耗函数
  validate.py   输入校验          configio.py  配置读写（参数修改入口）
  engine.py     逐回合推演引擎    report.py    确定性报告序列化
  generator.py  确定性样本生成器  cli.py       命令行入口
tests/test_engine.py  手算对照测试（22 例）
samples/  生成的样本配置          reports/  推演报告输出
```
