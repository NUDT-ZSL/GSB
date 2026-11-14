"""确定性构造样本目录（纯标准库，无随机内容、无时间戳）。

用法：
    python build_samples.py [目标样本目录]

默认输出到 ./samples。构造的样本覆盖：
  01-04 漏洞用例（script 标签、双/单引号事件属性、SVG onload 事件属性）
  05-06 无漏洞用例（纯文本、含尖括号的非标签文本、数值）
  07-08 不合法用例（缺字段、字段类型不符）
  09    渲染抛异常用例（模板引用数据字典中不存在的键）
  10    不可读样本（JSON 语法损坏，记入问题列表）
"""

from __future__ import annotations

import json
import os
import sys

# 保持固定顺序，便于人工核对；文件本身按名排序，顺序不依赖字典迭代
SAMPLES: list[tuple[str, dict | str]] = [
    (
        "01_script_basic.json",
        {
            "template": "<div>欢迎你，{{name}}</div>",
            "data": {"name": "<script>alert('xss')</script>"},
        },
    ),
    (
        "02_event_attr.json",
        {
            "template": "<img src=\"{{logo}}\" alt=\"logo\">",
            "data": {"logo": "x\" onerror=\"alert(1)"},
        },
    ),
    (
        "03_event_attr_single_quote.json",
        {
            "template": "<input value='{{q}}'>",
            "data": {"q": "' onmouseover='alert(2)"},
        },
    ),
    (
        "04_onload_attr.json",
        {
            "template": "<svg><a xlink:href=\"{{u}}\">入口</a></svg>",
            "data": {"u": "y\" onload=\"alert(3)"},
        },
    ),
    (
        "05_plain_text.json",
        {
            "template": "你好，{{user}}，今天是{{day}}。",
            "data": {"user": "张三", "day": "星期六"},
        },
    ),
    (
        "06_benign_brackets_and_number.json",
        {
            "template": "算式 {{a}} < {{b}} 的结果为 {{c}}",
            "data": {"a": 1, "b": 2, "c": "成立"},
        },
    ),
    (
        "07_missing_field.json",
        {
            # 缺少 data 字段
            "template": "<p>{{msg}}</p>"
        },
    ),
    (
        "08_wrong_type.json",
        {
            # template 类型不符（应为字符串），data 类型不符（应为对象）
            "template": 12345,
            "data": "不是字典",
        },
    ),
    (
        "09_render_exception.json",
        {
            # 模板引用了 data 中不存在的键 missing_key，渲染时抛 KeyError
            "template": "用户={{user}}; 备注={{missing_key}}",
            "data": {"user": "李四"},
        },
    ),
]

# JSON 语法损坏文件：无法被解析，模拟“不可读样本”，应进入问题列表
BROKEN_FILE = ("10_unreadable_broken.json", "{ this is not valid json ::: ")


def build(target_dir: str) -> None:
    os.makedirs(target_dir, exist_ok=True)
    for name, payload in SAMPLES:
        with open(os.path.join(target_dir, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    name, raw = BROKEN_FILE
    with open(os.path.join(target_dir, name), "w", encoding="utf-8") as fh:
        fh.write(raw)
    print(f"已在 {target_dir} 确定性构造 {len(SAMPLES) + 1} 个样本文件")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join("samples")
    build(target)
