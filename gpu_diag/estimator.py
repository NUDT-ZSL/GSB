"""基于数据规模的显存需求估算。

估算只使用整数运算，相同 (作业曲线, 数据规模参数) 组合永远得到同一整数结果；
安全余量按整数百分比上取整，而不是浮点阈值。

单卡显存估算步骤（单位 MiB，全部为整数）：

1. token 数 T = batch_size * seq_len
2. 激活元素数 A = T * model_dim
3. 参数量整数估计 P = model_dim * model_dim
4. 激活显存（MiB）= ceil_div(A * 2, 1024 * 1024)，bf16/16 位按每元素 2 字节
5. 参数与优化器状态显存（MiB）= ceil_div(P * bytes_per_param, 1024 * 1024)
6. 基础需求 raw = base_mib + 激活 + 参数
7. 整数安全余量 margin = ceil_div(raw * safety_pct, 100)
8. 单卡需求 per_card_mib = raw + margin
9. 作业总需求 total_mib = per_card_mib * card_count
"""

from .errors import BusinessError, E_ESTIMATE
from .models import EstimateResult, ScaleParams

_MIB = 1024 * 1024
_SCALE_FIELDS = ("batch_size", "seq_len", "model_dim")


def _ceil_div(numerator, denominator):
    return (numerator + denominator - 1) // denominator


def validate_scale(scale_params, scale_bounds):
    """校验数据规模参数，缺失或越界抛出带字段位置的 E_ESTIMATE。"""
    if not isinstance(scale_params, ScaleParams):
        raise BusinessError(E_ESTIMATE, "scale params missing", field_path="$.scale_params")
    values = scale_params.to_dict()
    for name in _SCALE_FIELDS:
        value = values.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise BusinessError(
                E_ESTIMATE, "parameter {} missing or not an integer".format(name),
                field_path="$.scale_params.{}".format(name),
            )
        if value <= 0:
            raise BusinessError(
                E_ESTIMATE, "parameter {} must be positive, got {}".format(name, value),
                field_path="$.scale_params.{}".format(name),
            )
        if name in scale_bounds:
            low, high = scale_bounds[name]
            if not low <= value <= high:
                raise BusinessError(
                    E_ESTIMATE,
                    "parameter {}={} outside allowed range [{}, {}]".format(name, value, low, high),
                    field_path="$.scale_params.{}".format(name),
                )


def estimate_job(job, scale_params, parameter_version, scale_bounds=None):
    """返回带完整中间项的估算结果，所有计算均为确定性整数运算。"""
    validate_scale(scale_params, scale_bounds or {})
    batch_size = scale_params.batch_size
    seq_len = scale_params.seq_len
    model_dim = scale_params.model_dim

    tokens = batch_size * seq_len
    activation_elements = tokens * model_dim
    parameter_count = model_dim * model_dim
    activation_bytes = activation_elements * 2
    activation_mib = _ceil_div(activation_bytes, _MIB)
    parameter_bytes = parameter_count * job.curve.bytes_per_param
    parameter_mib = _ceil_div(parameter_bytes, _MIB)
    raw_mib = job.curve.base_mib + activation_mib + parameter_mib
    margin_mib = _ceil_div(raw_mib * job.curve.safety_pct, 100)
    per_card_mib = raw_mib + margin_mib
    total_mib = per_card_mib * job.card_count

    components = {
        "tokens": tokens,
        "activation_elements": activation_elements,
        "parameter_count": parameter_count,
        "activation_bytes": activation_bytes,
        "activation_mib": activation_mib,
        "parameter_bytes": parameter_bytes,
        "parameter_mib": parameter_mib,
        "base_mib": job.curve.base_mib,
        "raw_mib": raw_mib,
        "safety_pct": job.curve.safety_pct,
        "margin_mib": margin_mib,
        "formula": (
            "per_card_mib = base_mib + ceil(2*batch*seq*dim/1048576) "
            "+ ceil(dim^2*bytes_per_param/1048576) + ceil(raw*safety_pct/100)"
        ),
    }
    return EstimateResult(
        job_id=job.job_id,
        parameter_version=parameter_version,
        scale_params=ScaleParams(batch_size, seq_len, model_dim),
        per_card_mib=per_card_mib,
        total_mib=total_mib,
        card_count=job.card_count,
        margin_mib=margin_mib,
        components=components,
    )
