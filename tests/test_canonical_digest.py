from __future__ import annotations

import hashlib

import pytest

from config_forensics.canonical import (
    PathNormalizationError,
    normalize_config_path,
    observation_digest_payload,
)
from config_forensics.codes import Code
from config_forensics.digests import verify_observation_digest
from tests.helpers import TEST_KEY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("service.port", "service.port"),
        ("features[0].enabled", "features[0].enabled"),
        ("features.0.enabled", "features[0].enabled"),
        ("root[1][2].name", "root[1][2].name"),
        ('["a.b"][0]', '["a.b"][0]'),
    ],
)
def test_normalize_config_path(raw: str, expected: str) -> None:
    assert normalize_config_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "a..b", "a[-1]", "a[0]b", "a.", "a[x]"])
def test_reject_invalid_paths(raw: str) -> None:
    with pytest.raises(PathNormalizationError):
        normalize_config_path(raw)


def test_sha256_digest_covers_normalized_path_and_value() -> None:
    path = normalize_config_path("features[0].enabled")
    value = True
    digest = hashlib.sha256(observation_digest_payload(path, value)).hexdigest()
    check = verify_observation_digest(
        normalized_path=path,
        value=value,
        algorithm="sha256",
        digest=digest,
        declared_bits=256,
        allowed_algorithms=("sha256",),
    )
    assert check.ok


def test_hmac_requires_key_and_width_is_separate() -> None:
    path = normalize_config_path("tls.enabled")
    payload = observation_digest_payload(path, True)
    import hmac

    digest = hmac.new(TEST_KEY.encode(), payload, hashlib.sha256).hexdigest()
    ok = verify_observation_digest(
        normalized_path=path,
        value=True,
        algorithm="hmac-sha256",
        digest=digest,
        declared_bits=256,
        allowed_algorithms=("hmac-sha256",),
        hmac_key=TEST_KEY,
    )
    assert ok.ok
    missing = verify_observation_digest(
        normalized_path=path,
        value=True,
        algorithm="hmac-sha256",
        digest=digest,
        declared_bits=256,
        allowed_algorithms=("hmac-sha256",),
    )
    assert missing.code == Code.HMAC_KEY_MISSING
    width = verify_observation_digest(
        normalized_path=path,
        value=True,
        algorithm="hmac-sha256",
        digest=digest,
        declared_bits=512,
        allowed_algorithms=("hmac-sha256",),
        hmac_key=TEST_KEY,
    )
    assert width.code == Code.DIGEST_WIDTH_MISMATCH
    non_hex = verify_observation_digest(
        normalized_path=path,
        value=True,
        algorithm="sha256",
        digest="z" * 64,
        declared_bits=256,
        allowed_algorithms=("sha256",),
    )
    assert non_hex.code == Code.DIGEST_INVALID
