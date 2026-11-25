"""多路稀疏召回融合前端链路组件。

模块划分：

- errors: 结构化业务错误码
- precision: float64 / 定点缩放整数两套同语义算子
- samplegen: 固定种子样本生成
- index: 倒排、前缀、类别、向量四类索引构建
- recall: 四路确定性召回打分
- fusion: 跨路对齐去重与加权融合
- diagnose: 以 float64 为真值的误差传播诊断
- validate: 输入校验与跨模块一致性自检
- scheduler: 进程池并行调度与双重资源预算准入
- cache: 内容寻址缓存、增量失效与版本化快照提交
- pipeline: 顶层编排
"""

from .errors import BusinessError, ErrorCode

__all__ = ["BusinessError", "ErrorCode"]
__version__ = "1.0.0"
