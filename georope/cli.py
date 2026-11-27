"""Command line entry points for generation, encoding, checking, and serving."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .generator import deterministic_json, write_incremental_sample, write_sample
from .jsonutil import to_jsonable
from .service import check_request, encode_request, incremental_request


def _read_payload(path: str) -> dict[str, Any]:
    if path == "-":
        return json.loads(Path("/dev/stdin").read_text(encoding="utf-8"))
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_payload(payload: Any, path: str) -> None:
    text = deterministic_json(payload)
    if path == "-":
        print(text, end="")
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m georope.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--seed", type=int, default=42)
    generate_parser.add_argument("--output", default="samples/request.json")

    generate_incremental_parser = subparsers.add_parser("generate-incremental")
    generate_incremental_parser.add_argument("--seed", type=int, default=42)
    generate_incremental_parser.add_argument(
        "--output", default="samples/incremental_request.json"
    )

    encode_parser = subparsers.add_parser("encode")
    encode_parser.add_argument("--input", default="samples/request.json")
    encode_parser.add_argument("--output", default="artifacts/encoding_result.json")

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--input", default="samples/request.json")
    check_parser.add_argument("--output", default="artifacts/check_result.json")

    incremental_parser = subparsers.add_parser("incremental")
    incremental_parser.add_argument("--input", required=True)
    incremental_parser.add_argument("--output", default="artifacts/incremental_result.json")

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)

    arguments = parser.parse_args(argv)
    if arguments.command == "generate":
        path = write_sample(arguments.output, arguments.seed)
        print(str(path))
        return 0
    if arguments.command == "generate-incremental":
        path = write_incremental_sample(arguments.output, arguments.seed)
        print(str(path))
        return 0
    if arguments.command == "encode":
        result = encode_request(_read_payload(arguments.input))
        _write_payload(result, arguments.output)
        print(
            json.dumps(
                to_jsonable(
                    {
                        "ok": result["ok"],
                        "request_id": result["request_id"],
                        "fields": len(result["encoded_fields"]),
                        "boundary_reports": len(result["boundary_continuity_report"]),
                        "cross_version_consistent": result[
                            "cross_version_differences"
                        ]["consistent"],
                    }
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if arguments.command == "check":
        result = check_request(_read_payload(arguments.input))
        _write_payload(result, arguments.output)
        print(
            json.dumps(
                to_jsonable(
                    {
                        "ok": result["ok"],
                        "passed": result["passed"],
                        "reference_consistent": result[
                            "reference_verification"
                        ]["consistent"],
                        "manual_cases_passed": result["manual_cases"]["passed"],
                    }
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if arguments.command == "incremental":
        payload = _read_payload(arguments.input)
        result = incremental_request(payload)
        _write_payload(result, arguments.output)
        print(
            json.dumps(
                to_jsonable(
                    {
                        "ok": result["ok"],
                        "recomputed": result[
                            "incremental_recomputation"
                        ]["total_recomputed_channel_positions"],
                        "preserved": result[
                            "incremental_recomputation"
                        ]["unaffected_encoding_preserved"],
                    }
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if arguments.command == "serve":
        from .server import create_app

        create_app().run(
            host=arguments.host,
            port=arguments.port,
            debug=False,
            use_reloader=False,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
