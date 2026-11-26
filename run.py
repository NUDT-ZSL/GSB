#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from prosody_diag.cli import main
from prosody_diag.errors import DiagnosticError


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DiagnosticError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "E_INVALID_ARGUMENT",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
