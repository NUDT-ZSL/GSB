#!/usr/bin/env python3
"""确定性生成审计演示样本（修复前 before/ 与修复后 after/）。

用法: python make_samples.py [输出根目录]   默认输出到 ./samples
同一台机器重复运行结果完全一致（先删后建，内容固定）。
"""
import os
import shutil
import sys

# (相对路径, 内容) —— 修复前样本：每条规则至少触发一次
BEFORE = {
    "app/main.py": (
        "import subprocess\n"
        "\n"
        "def run(user_input):\n"
        "    return eval(user_input)\n"                      # SEC001, 行4 列12
        "\n"
        "def shell(cmd):\n"
        "    subprocess.call(cmd, shell=True)\n"            # SEC003, 行7 列5
    ),
    "app/config.py": (
        "password = \"s3cret!\"  # TODO: 移到环境变量\n"     # SEC002 列1 / SEC004 列24
    ),
    "app/util.py": (
        "def twice(x):\n"
        "    return eval(x) + eval(x)\n"                    # SEC001 两次, 列13 与 列23
    ),
    "docs/notes.txt": (
        "eval( password = \"x\"  # 非 .py 文件，不应出现在报告中\n"
    ),
    "third_party/legacy.py": (
        "eval('1+1')  # 可用 --exclude third_party 排除\n"
    ),
}

# 修复后样本：同样结构，所有缺陷按 fix 说明修复，不再触发
AFTER = {
    "app/main.py": (
        "import ast\n"
        "import subprocess\n"
        "\n"
        "def run(user_input):\n"
        "    return ast.literal_eval(user_input)\n"
        "\n"
        "def shell(cmd):\n"
        "    subprocess.call([cmd])\n"
    ),
    "app/config.py": (
        "import os\n"
        "password = os.environ[\"APP_PASSWORD\"]\n"
    ),
    "app/util.py": (
        "import ast\n"
        "\n"
        "def twice(x):\n"
        "    return ast.literal_eval(x) + ast.literal_eval(x)\n"
    ),
    "docs/notes.txt": (
        "eval( password = \"x\"  # 非 .py 文件，不应出现在报告中\n"
    ),
    "third_party/legacy.py": (
        "import ast\n"
        "ast.literal_eval('1+1')  # 已修复\n"
    ),
}


def build(root, files):
    for rel, content in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "samples"
    if os.path.isdir(base):
        shutil.rmtree(base)
    build(os.path.join(base, "before"), BEFORE)
    build(os.path.join(base, "after"), AFTER)
    print("样本已生成: %s/before 与 %s/after" % (base, base))


if __name__ == "__main__":
    main()
