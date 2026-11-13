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
