from __future__ import annotations

import importlib.util
import json
import platform
import sys


def main() -> int:
    numpy_spec = importlib.util.find_spec("numpy")
    numpy_version = None
    if numpy_spec is not None:
        import numpy

        numpy_version = numpy.__version__
    payload = {
        "python_version": platform.python_version(),
        "python_ok": sys.version_info >= (3, 11),
        "numpy_optional_available": numpy_spec is not None,
        "numpy_version": numpy_version,
        "core_third_party_dependencies": [],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["python_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
