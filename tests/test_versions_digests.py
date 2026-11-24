from supply_chain_audit.digests import validate_digest
from supply_chain_audit.versions import validate_constraint_expression, validate_canonical_key


def test_version_constraint_grammar():
    valid = ["1.2.3", "=1.2.3", ">=1.2,<2.0", "^1.2.0", "~1.2", "1.2.x", "*", ">=1.0.0 || ^2.0.0"]
    invalid = ["", "latest", ">= ", "1.2.3.4", "=>1.0.0", "^", "1.2.3 && bad"]
    for expression in valid:
        ok, code = validate_constraint_expression(expression)
        assert ok, (expression, code)
    for expression in invalid:
        ok, code = validate_constraint_expression(expression)
        assert not ok, expression
        assert code == "E_CONSTRAINT_SYNTAX"


def test_canonical_key_and_digest_dispatch():
    assert validate_canonical_key("scope/name_1.2-rc@1.2.3+build.7")[0] is True
    assert validate_canonical_key("UPPER@1.2.3")[0] is False
    assert validate_canonical_key("name@1.2")[1] == "E_VERSION_SYNTAX"

    valid = validate_digest("A" * 64, "SHA-256")
    assert valid.ok
    assert valid.algorithm_normalized == "sha256"
    assert valid.digest_normalized == "a" * 64

    hmac = validate_digest("b" * 64, "HMAC_SHA256")
    assert hmac.ok
    assert hmac.algorithm_normalized == "hmac-sha256"

    width = validate_digest("ab", "sha256")
    assert not width.ok
    assert width.code == "E_HASH_ALGO"

    bad_hex = validate_digest("z" * 64, "sha256")
    assert not bad_hex.ok
    assert bad_hex.code == "E_HASH_HEX"
