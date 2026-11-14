"""员工信息录入填报工具。

模块划分：
- csv_reader: 读取 CSV 并保留源文件物理行号
- browser_form: Playwright 驱动本地表单页面完成提交
- status_client: 按记录标识回读最终状态
- reporter: 生成成功/失败/不可用三组 JSON 报告
- cli: 命令行入口
"""

__all__ = ["csv_reader", "browser_form", "status_client", "reporter", "cli"]
