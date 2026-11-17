"""编码变换注册表：变换名 -> (编码族, 方向)。

方向 encode 表示对污点值施加防护性编码/转义，decode 表示还原。
同一编码族下允许存在多个同义变换名（例如不同方言的 HTML 转义），
它们在归一后落入同一编码族等价类，这是等价类剪枝的基础。
"""
from __future__ import annotations

ENCODE = "encode"
DECODE = "decode"

# 变换名 -> (编码族, 方向)
TRANSFORMS: dict[str, tuple[str, str]] = {
    # HTML 族
    "html_escape": ("html", ENCODE),
    "htmlentities": ("html", ENCODE),
    "htmlspecialchars": ("html", ENCODE),
    "html_unescape": ("html", DECODE),
    "html_entity_decode": ("html", DECODE),
    # SQL 族
    "sql_quote": ("sql", ENCODE),
    "mysql_real_escape_string": ("sql", ENCODE),
    "parameterized_escape": ("sql", ENCODE),
    "sql_unescape": ("sql", DECODE),
    # Shell 族
    "shell_quote": ("shell", ENCODE),
    "escapeshellarg": ("shell", ENCODE),
    "shell_unescape": ("shell", DECODE),
    # JS 字符串族
    "js_string_escape": ("js-string", ENCODE),
    "json_escape": ("js-string", ENCODE),
    "js_string_unescape": ("js-string", DECODE),
}

KNOWN_FAMILIES = ("html", "sql", "shell", "js-string")


def known_transform(name: str) -> bool:
    return name in TRANSFORMS


def resolve(name: str) -> tuple[str, str]:
    """返回变换名对应的 (编码族, 方向)，未知变换名抛出 KeyError。"""
    return TRANSFORMS[name]
