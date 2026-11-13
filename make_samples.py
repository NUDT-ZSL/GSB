# -*- coding: utf-8 -*-
"""确定性样本生成器：生成“含漏洞”与“已修复”两套上传服务源码。

运行：python make_samples.py
输出目录（默认当前目录下 samples/）：
  samples/vulnerable/  8 个文件：7 个含漏洞上传服务 + 1 个语法错误文件
  samples/fixed/       7 个已修复文件（同文件名，便于指纹对比）

所有样例均只依赖 Python 标准库，入口函数签名统一为：
  save_upload(upload_dir, filename, content) -> str|None
"""
from __future__ import annotations

import os
import shutil
import textwrap

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 含漏洞样例
# ---------------------------------------------------------------------------
VULN_FILES: dict[str, str] = {}

VULN_FILES["vuln01_flask_open_concat.py"] = textwrap.dedent('''\
    # 样例1：Flask 风格，request.form 文件名直接与上传目录拼接后 open 写入（高危）
    import os


    class _Request:
        """测试桩：模拟 flask.request，提供 form 字典。"""
        def __init__(self, form):
            self.form = form


    request = _Request(form={})


    def save_upload(upload_dir, filename, content):
        # 污点源 request.form -> 字符串拼接 -> open(..., "wb")，无任何净化
        name = request.form.get("filename") or filename
        target = upload_dir + "/" + name
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

VULN_FILES["vuln02_os_path_join.py"] = textwrap.dedent('''\
    # 样例2：os.path.join 直接拼接用户文件名（高危；join 不会阻止绝对路径/../逃逸）
    import os


    def save_upload(upload_dir, filename, content):
        target = os.path.join(upload_dir, filename)
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

VULN_FILES["vuln03_weak_replace.py"] = textwrap.dedent('''\
    # 样例3：黑名单替换 "../"，可被 ....// 嵌套绕过（中危）
    import os


    def save_upload(upload_dir, filename, content):
        # 一次替换：....//  -> ../
        safe_name = filename.replace("../", "")
        target = os.path.join(upload_dir, safe_name)
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

VULN_FILES["vuln04_extension_only.py"] = textwrap.dedent('''\
    # 样例4：仅校验扩展名，未处理路径穿越（高危）
    import os

    ALLOWED_EXT = (".png", ".jpg", ".jpeg")


    def save_upload(upload_dir, filename, content):
        if not filename.endswith(ALLOWED_EXT):
            raise ValueError("仅允许上传图片文件")
        # 扩展名合法但路径中仍可包含 ../
        target = os.path.join(upload_dir, filename)
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

VULN_FILES["vuln05_pathlib_write.py"] = textwrap.dedent('''\
    # 样例5：pathlib Path 拼接后 write_bytes（高危）
    from pathlib import Path


    def save_upload(upload_dir, filename, content):
        target = Path(upload_dir) / filename
        target.write_bytes(content)
        return str(target)
''')

VULN_FILES["vuln06_shutil_copy.py"] = textwrap.dedent('''\
    # 样例6：临时文件经 shutil.copyfile 复制到用户指定目标（高危）
    import os
    import shutil
    import tempfile


    def save_upload(upload_dir, filename, content):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(content)
            tmp.close()
            target = os.path.join(upload_dir, filename)
            shutil.copyfile(tmp.name, target)
        finally:
            os.unlink(tmp.name)
        return target
''')

VULN_FILES["vuln07_arbitrary_read.py"] = textwrap.dedent('''\
    # 样例7：用户文件名直接拼入读取路径，任意文件读取（低危）
    from pathlib import Path


    def save_upload(upload_dir, filename, content):
        # 读取侧汇聚点 Path.read_bytes：可读取上传目录之外的文件
        target = Path(upload_dir) / filename
        data = target.read_bytes()
        return data
''')

VULN_FILES["vuln08_broken_syntax.py"] = textwrap.dedent('''\
    # 样例8：故意制造语法错误，扫描器应跳过并记入 errors，不中断其他文件检测
    import os


    def save_upload(upload_dir, filename, content)
        target = os.path.join(upload_dir, filename)
        with open(target, "wb") as fh
            fh.write(content)
''')

# ---------------------------------------------------------------------------
# 已修复样例（与含漏洞样例同名，除坏文件外一一对应）
# ---------------------------------------------------------------------------
FIXED_FILES: dict[str, str] = {}

FIXED_FILES["vuln01_flask_open_concat.py"] = textwrap.dedent('''\
    # 修复1：basename 剥离目录分量 + resolve/commonpath 边界校验
    import os


    class _Request:
        def __init__(self, form):
            self.form = form


    request = _Request(form={})


    def save_upload(upload_dir, filename, content):
        name = request.form.get("filename") or filename
        safe_name = os.path.basename(name)
        upload_root = os.path.realpath(upload_dir)
        target = os.path.realpath(os.path.join(upload_root, safe_name))
        if os.path.commonpath([upload_root, target]) != upload_root:
            raise ValueError("非法文件名：路径越界")
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

FIXED_FILES["vuln02_os_path_join.py"] = textwrap.dedent('''\
    # 修复2：PurePath.name 取纯文件名 + is_relative_to 边界校验（Python 3.9+）
    import os
    from pathlib import Path


    def save_upload(upload_dir, filename, content):
        safe_name = Path(filename).name
        root = Path(upload_dir).resolve()
        target = (root / safe_name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("非法文件名：路径越界")
        with open(target, "wb") as fh:
            fh.write(content)
        return str(target)
''')

FIXED_FILES["vuln03_weak_replace.py"] = textwrap.dedent('''\
    # 修复3：弃用黑名单替换，改用 basename + commonpath 白名单式校验
    import os


    def save_upload(upload_dir, filename, content):
        safe_name = os.path.basename(filename)
        upload_root = os.path.realpath(upload_dir)
        target = os.path.realpath(os.path.join(upload_root, safe_name))
        if os.path.commonpath([upload_root, target]) != upload_root:
            raise ValueError("非法文件名：路径越界")
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

FIXED_FILES["vuln04_extension_only.py"] = textwrap.dedent('''\
    # 修复4：扩展名校验之外补充 basename 与 resolve 边界校验
    import os

    ALLOWED_EXT = (".png", ".jpg", ".jpeg")


    def save_upload(upload_dir, filename, content):
        if not filename.endswith(ALLOWED_EXT):
            raise ValueError("仅允许上传图片文件")
        safe_name = os.path.basename(filename)
        upload_root = os.path.realpath(upload_dir)
        target = os.path.realpath(os.path.join(upload_root, safe_name))
        if os.path.commonpath([upload_root, target]) != upload_root:
            raise ValueError("非法文件名：路径越界")
        with open(target, "wb") as fh:
            fh.write(content)
        return target
''')

FIXED_FILES["vuln05_pathlib_write.py"] = textwrap.dedent('''\
    # 修复5：服务端 uuid 重命名 + Path.resolve/is_relative_to 双重约束
    import uuid
    from pathlib import Path


    def save_upload(upload_dir, filename, content):
        root = Path(upload_dir).resolve()
        safe_name = uuid.uuid4().hex
        target = (root / safe_name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("非法文件名：路径越界")
        target.write_bytes(content)
        return str(target)
''')

FIXED_FILES["vuln06_shutil_copy.py"] = textwrap.dedent('''\
    # 修复6：copyfile 目标路径同样做 basename + commonpath 校验
    import os
    import shutil
    import tempfile


    def save_upload(upload_dir, filename, content):
        safe_name = os.path.basename(filename)
        upload_root = os.path.realpath(upload_dir)
        target = os.path.realpath(os.path.join(upload_root, safe_name))
        if os.path.commonpath([upload_root, target]) != upload_root:
            raise ValueError("非法文件名：路径越界")
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(content)
            tmp.close()
            shutil.copyfile(tmp.name, target)
        finally:
            os.unlink(tmp.name)
        return target
''')

FIXED_FILES["vuln07_arbitrary_read.py"] = textwrap.dedent('''\
    # 修复7：读取侧也做 basename + 边界校验，防止任意文件读取
    from pathlib import Path


    def save_upload(upload_dir, filename, content):
        root = Path(upload_dir).resolve()
        safe_name = Path(filename).name
        target = (root / safe_name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("非法文件名：路径越界")
        data = target.read_bytes()
        return data
''')


def write_tree(base: str, files: dict[str, str]) -> None:
    os.makedirs(base, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(base, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)


def main() -> None:
    samples = os.path.join(ROOT, "samples")
    vuln_dir = os.path.join(samples, "vulnerable")
    fixed_dir = os.path.join(samples, "fixed")
    for d in (vuln_dir, fixed_dir):
        if os.path.isdir(d):
            shutil.rmtree(d)
    write_tree(vuln_dir, VULN_FILES)
    write_tree(fixed_dir, FIXED_FILES)
    print(f"已生成含漏洞样例: {vuln_dir}（{len(VULN_FILES)} 个文件，含 1 个语法错误文件）")
    print(f"已生成已修复样例: {fixed_dir}（{len(FIXED_FILES)} 个文件）")


if __name__ == "__main__":
    main()
