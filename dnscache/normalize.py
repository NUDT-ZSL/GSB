"""Domain name canonicalization."""

import re

from .errors import BizError, E_NAME_INVALID

_LABEL_RE = re.compile(r"^[a-z0-9_*-]+$")


def canon(name):
    """Canonicalize a domain name: lowercase, strip trailing dot, validate."""
    if not isinstance(name, str):
        raise BizError(E_NAME_INVALID, "domain name must be a string",
                       {"name": repr(name)})
    n = name.strip().lower()
    if n.endswith("."):
        n = n[:-1]
    if not n or len(n) > 253:
        raise BizError(E_NAME_INVALID, "domain name empty or too long",
                       {"name": name})
    for label in n.split("."):
        if not label or len(label) > 63 or not _LABEL_RE.match(label):
            raise BizError(E_NAME_INVALID, "invalid label",
                           {"name": name, "label": label})
    return n
