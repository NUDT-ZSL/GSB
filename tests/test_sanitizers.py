"""两套脱敏实现的单元测试：覆盖各类变形写法与主要缺陷。"""

from __future__ import annotations

import pytest

from sanitizers.common import (
    contains_number,
    core_digits,
    detect_residuals,
    find_sensitive,
)
from sanitizers.sanitizer_fixed import sanitize as fix
from sanitizers.sanitizer_legacy import sanitize as legacy

PHONE = "13812345678"
ID_CARD = "110101199003071233"
ID_CARD_X = "31010419900307123X"

PHONE_VARIANTS = [
    PHONE,
    "138-1234-5678",
    "138 1234 5678",
    "138.1234.5678",
    "138_1234_5678",
    "138·1234·5678",
    "+86 138-1234-5678",
    "+8613812345678",
    "138 12 34 5678",
    "138　1234　5678",  # 全角空格
    "１３８１２３４５６７８",  # 全角数字
    "138​1234​5678",  # 零宽字符
]

ID_VARIANTS = [
    ID_CARD,
    "110101 19900307 1233",
    "110101-19900307-1233",
    "110101.19900307.1233",
    "１１０１０１１９９００３０７１２３３",
    ID_CARD_X,
]


@pytest.mark.parametrize("variant", PHONE_VARIANTS)
def test_fixed_masks_phone_variants(variant):
    output = fix(f"联系号码 {variant} 结束")
    assert "手机号已脱敏" in output
    assert not detect_residuals(output)
    assert not contains_number(output, PHONE)


@pytest.mark.parametrize("variant", ID_VARIANTS)
def test_fixed_masks_id_variants(variant):
    output = fix(f"证件号 {variant} 结束")
    assert "证件号已脱敏" in output
    assert not detect_residuals(output)
    assert not contains_number(output, core_digits(find_sensitive(variant)[0]))


@pytest.mark.parametrize("variant", ["138-1234-5678", "１３８１２３４５６７８", "138 1234 5678"])
def test_legacy_leaks_phone_variants(variant):
    output = legacy(f"联系号码 {variant} 结束")
    assert detect_residuals(output), f"修复前实现本应漏掉 {variant!r}"


def test_legacy_masks_plain_values():
    output = legacy(f"电话 {PHONE} 证件 {ID_CARD}")
    assert "手机号已脱敏" in output
    assert "证件号已脱敏" in output
    assert not detect_residuals(output)


def test_fixed_preserves_non_sensitive_text():
    text = '用户 "张三" 登录，耗时 42ms，路径 /api/v1/orders?id=88'
    assert fix(text) == text


def test_fixed_preserves_line_breaks():
    text = f"第一行 {PHONE}\n第二行\n第三行 {ID_CARD}\n"
    output = fix(text)
    assert output.count("\n") == text.count("\n")
    assert output.splitlines()[1] == "第二行"


def test_plain_long_order_number_not_treated_as_id_card():
    # 前两位 99 不是合法行政区划代码，不应被当作证件号。
    assert not find_sensitive("订单号 991234567890123456")
