"""定点整数运算原语。

全部打分链路在转换为定点整数后仅使用 Python 整数运算（任意精度、无浮点
不确定性）。定点数约定为 Qm.F：``value = integer * 2**-frac_bits``。
量化档位 ``levels`` 限定整数段长度，真实改变中间整数的取值集合。
"""

ROUND_HALF_EVEN = "half_even"
ROUND_HALF_UP = "half_up"
ROUND_HALF_DOWN = "half_down"
ROUND_FLOOR = "floor"
ROUNDING_MODES = (ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_HALF_DOWN, ROUND_FLOOR)

MIN_FRAC_BITS = 0
MAX_FRAC_BITS = 31


def _divmod(a, b):
    q = abs(a) // abs(b)
    r = abs(a) - q * abs(b)
    if a < 0:
        q = -q
        r = -r
    return q, r


def round_div(num, den, rounding=ROUND_HALF_EVEN):
    """整数 num/den 按指定规则舍入为整数，den 必须为正。"""
    if den <= 0:
        raise ValueError("denominator must be positive")
    if rounding == ROUND_FLOOR:
        q = num // den
        return q, num - q * den
    trunc, rem = _divmod(num, den)
    if rem == 0:
        return trunc, 0
    twice = abs(rem) * 2
    away = 1 if num >= 0 else -1
    if twice < den:
        return trunc, rem
    if twice > den:
        return trunc + away, rem
    if rounding == ROUND_HALF_UP:
        return trunc + away, rem
    if rounding == ROUND_HALF_DOWN:
        return trunc, rem
    if trunc % 2 == 0:
        return trunc, rem
    return trunc + away, rem


def to_fixed(x, frac_bits, rounding=ROUND_HALF_EVEN):
    """浮点值转为 Q 定点整数，同时返回舍入残差（同量纲的浮点值）。"""
    scaled = x * (1 << frac_bits)
    num = int(scaled) if scaled >= 0 else -int(-scaled)
    if rounding == ROUND_FLOOR:
        q = num
    else:
        q, _ = round_div(num, 1, rounding)
    return q, scaled - q


def scale_int(x, from_bits, to_bits, rounding=ROUND_HALF_EVEN):
    """在两个定点位宽之间转换整数，全程整数运算，返回 (新整数, 舍入余数)。"""
    if to_bits == from_bits:
        return x, 0
    if to_bits > from_bits:
        return x << (to_bits - from_bits), 0
    shift = from_bits - to_bits
    den = 1 << shift
    return round_div(x, den, rounding)


def clip_int(x, lo, hi):
    """整数截断，返回 (截断后值, 是否被裁剪, 被裁减量)。"""
    if x < lo:
        return lo, True, lo - x
    if x > hi:
        return hi, True, hi - x
    return x, False, 0


def quantize_levels(code, levels, frac_bits, lo=None, hi=None):
    """把定点整数压缩到 ``levels`` 个量化档位。

    minmax 归一化后整数段天然落在 [0, 2**F]；z 分数没有自然边界，需用
    请求内观测到的最小/最大值映射到同一整数段。返回 (量化码, 量化残差)，
    量化残差为量化前整数与量化码之差（整数，单位为当前定点 LSB）。
    """
    if levels is None or levels <= 0:
        return code, 0
    if lo is None:
        lo = 0
    if hi is None:
        hi = 1 << frac_bits
    span = hi - lo
    if span <= 0 or levels == 1:
        return lo, lo - code
    step_num = span
    step_den = levels - 1
    rel = code - lo
    if rel <= 0:
        return lo, lo - code
    if rel >= span:
        return hi, hi - code
    idx, _ = round_div(rel * step_den, step_num, ROUND_HALF_EVEN)
    if idx < 0:
        idx = 0
    if idx > levels - 1:
        idx = levels - 1
    q, _ = round_div(idx * step_num, step_den, ROUND_HALF_EVEN)
    q += lo
    return q, q - code


class FixedSpec:
    """一组定点参数：小数位宽、量化档位、舍入规则。"""

    def __init__(self, frac_bits, levels=None, rounding=ROUND_HALF_EVEN):
        self.frac_bits = frac_bits
        self.levels = levels
        self.rounding = rounding

    def with_frac_bits(self, frac_bits):
        return FixedSpec(frac_bits, self.levels, self.rounding)

    def as_dict(self):
        return {
            "frac_bits": self.frac_bits,
            "levels": self.levels,
            "rounding": self.rounding,
        }
