"""确定性样本生成器：在指定目录下生成小型 .py 样例。

全程离线、无随机内容，重复运行生成字节一致的文件。
目录布局：
  <base>/before/   修复前样例（含 SEC001/SEC002/SEC003）
  <base>/after/    修复版样例（SEC001/SEC002 已修复，SEC003 保留且行号后移）
  <base>/syntax_err/  一个语法错误文件 + 一个正常文件
  <base>/empty/    空目录
"""
from __future__ import annotations

import pathlib

# -- 修复前 ----------------------------------------------------------------

BEFORE_PATH_OPEN = '''"""SEC001 样例：外部输入拼接后直接打开路径。"""
import os


def load_template(template_dir):
    name = input("template name: ")           # 外部输入
    path = os.path.join(template_dir, name)   # 拼接后污点仍在
    with open(path) as fh:                    # 应报 SEC001
        return fh.read()
'''

BEFORE_RECURSION = '''"""SEC002 样例：递归展开缺少终止条件。"""


def expand_all(items):
    expanded = []
    for item in items:
        expanded.extend(expand_all(item))     # 自递归，函数体内无终止分支
    return expanded
'''

BEFORE_SECRET = '''"""SEC003 样例：敏感配置明文写入标准输出与日志。"""
import logging

api_token = "tok-1234567890"
password = "hunter2"
logger = logging.getLogger("demo")


def run():
    print("token=", api_token)                # SEC003 第 1 条
    logger.info("password is %s", password)   # SEC003 第 2 条
    print("job started")                      # 固定文案，不应报
    print(api_token, password)                # 同行两个敏感变量，应产生两条
'''

# -- 修复后 ----------------------------------------------------------------

AFTER_PATH_OPEN = '''"""SEC001 修复样例：白名单查表，外部输入不参与路径构成。"""
import os

SAFE_TEMPLATES = {
    "welcome": "templates/welcome.txt",
    "invoice": "templates/invoice.txt",
}


def load_template():
    name = input("template name: ")
    safe_path = SAFE_TEMPLATES.get(name)      # 查表结果不受输入值污染
    if safe_path is None:
        raise ValueError("unknown template")
    with open(safe_path) as fh:               # 不再报 SEC001
        return fh.read()
'''

AFTER_RECURSION = '''"""SEC002 修复样例：加入递归终止条件。"""


def expand_all(items):
    if not items:                             # 终止分支
        return []
    expanded = []
    for item in items:
        expanded.extend(expand_all(item))
    return expanded
'''

AFTER_SECRET = '''"""SEC003 保留样例：缺陷仍在，行号因新增注释整体后移。"""
import logging

# 修复版新增的说明行：制造行号漂移，验证“行号变化不视为新问题”
api_token = "tok-1234567890"
password = "hunter2"
logger = logging.getLogger("demo")


def run():
    print("token=", api_token)                # SEC003 仍存在
    logger.info("password is %s", password)   # SEC003 仍存在
    print("job started")                      # 固定文案，不应报
    print(api_token, password)                # 同行两个敏感变量，仍产生两条
'''

# -- 语法错误目录 ----------------------------------------------------------

BROKEN_SYNTAX = '''def broken(:
    pass
'''

GOOD_IN_SYNTAX_DIR = '''"""语法错误目录中的正常文件。"""
api_token = "tok-abc"


def run():
    print(api_token)                          # 应照常报 SEC003
'''

FILES: dict[str, dict[str, str]] = {
    "before": {
        "path_open_demo.py": BEFORE_PATH_OPEN,
        "recursion_demo.py": BEFORE_RECURSION,
        "secret_leak.py": BEFORE_SECRET,
    },
    "after": {
        "path_open_demo.py": AFTER_PATH_OPEN,
        "recursion_demo.py": AFTER_RECURSION,
        "secret_leak.py": AFTER_SECRET,
    },
    "syntax_err": {
        "broken_syntax.py": BROKEN_SYNTAX,
        "good_one.py": GOOD_IN_SYNTAX_DIR,
    },
    "empty": {},
}


def build_sample_tree(base_dir: str | pathlib.Path) -> pathlib.Path:
    """在 base_dir 下写出全部样例，返回 base 路径。确定性、可重复调用。"""
    base = pathlib.Path(base_dir)
    for sub_dir, files in FILES.items():
        target = base / sub_dir
        target.mkdir(parents=True, exist_ok=True)
        for name, content in sorted(files.items()):
            (target / name).write_text(content, encoding="utf-8", newline="\n")
    return base
