"""业务错误码定义。"""

# 解析规范化层（请求 1）
E_SCHEMA = "E_SCHEMA"            # 条目结构/字段类型非法
E_DUP_GROUP = "E_DUP_GROUP"      # 规则组标识重复
E_DUP_RULE = "E_DUP_RULE"        # 规则标识重复
E_RANGE = "E_RANGE"              # 条件区间下界大于上界
E_ENDPOINT = "E_ENDPOINT"        # 端点标记取值非法
E_ACTION = "E_ACTION"            # 动作字段非法
E_PRIORITY = "E_PRIORITY"        # 优先级不是整数
E_DEADLINE = "E_DEADLINE"        # 期望返回时限不是正整数
E_SUMMARY = "E_SUMMARY"          # 期望结论摘要格式非法
E_DUP_REQUEST = "E_DUP_REQUEST"  # 请求标识重复
E_UNKNOWN_GROUP = "E_UNKNOWN_GROUP"  # 请求引用了不存在的规则组

# 交互核对层（请求 4 / 6 / 7）
E_MISMATCH = "E_MISMATCH"        # 服务摘要与本地独立计算不一致
E_STALE = "E_STALE"              # 服务返回陈旧版本摘要，拒绝采纳
E_TIMEOUT = "E_TIMEOUT"          # 单请求真实耗时超过硬上限
E_QUEUE_FULL = "E_QUEUE_FULL"    # 待处理队列超限，拒绝入队
E_LOCK = "E_LOCK"                # 检查点/报告文件锁冲突

OK = "OK"
