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
