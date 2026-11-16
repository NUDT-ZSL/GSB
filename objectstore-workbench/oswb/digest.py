"""Deterministic digest helpers.

Only standard-library algorithms are offered so the workbench needs no third
party packages. Digests are always rendered as lowercase hex strings.
"""

import hashlib

SUPPORTED = ("sha256", "sha1", "md5", "blake2b", "sha512")
DEFAULT_ALGO = "sha256"


def normalize_algorithm(algorithm):
    if algorithm is None:
        return DEFAULT_ALGO
    algo = str(algorithm).strip().lower()
    if algo not in SUPPORTED:
        from .errors import BusinessError, E_PARAM

        raise BusinessError(
            E_PARAM,
            f"unsupported digest algorithm: {algorithm!r}",
            {"supported": list(SUPPORTED)},
        )
    return algo


def new_hasher(algorithm=DEFAULT_ALGO):
    return hashlib.new(normalize_algorithm(algorithm))


def digest_bytes(data, algorithm=DEFAULT_ALGO):
    hasher = new_hasher(algorithm)
    hasher.update(data)
    return hasher.hexdigest()


def digest_file(path, algorithm=DEFAULT_ALGO, buf_size=1024 * 1024):
    hasher = new_hasher(algorithm)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(buf_size)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def digest_iterable(chunks, algorithm=DEFAULT_ALGO):
    """Digest an ordered iterable of raw chunk bytes deterministically."""
    hasher = new_hasher(algorithm)
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()
