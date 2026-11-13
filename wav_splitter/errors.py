"""统一的致命错误类型。

输入文件缺失、格式不合法等场景抛出 :class:`SplitterError`，
CLI 捕获后以非 0 退出码终止，且在任何输出文件产生之前完成校验。
"""


class SplitterError(Exception):
    """可预期的业务/输入错误，错误信息可直接展示给用户。"""
