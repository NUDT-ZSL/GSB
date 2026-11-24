from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from haptic_engine.errors import HapticError
from haptic_engine.samples import DEFAULT_SEED, generate_workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic device and event samples"
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        paths = generate_workspace(
            Path(args.workspace).resolve(), seed=args.seed, force=args.force
        )
    except HapticError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    except OSError as error:
        io_error = HapticError(
            "E_IO", "failed to write sample files", {"message": str(error)}
        )
        print(
            json.dumps(
                io_error.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "seed": args.seed,
                "files": {name: str(path) for name, path in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
