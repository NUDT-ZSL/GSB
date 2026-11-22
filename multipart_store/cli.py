"""Command-line interface for the local multipart store."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import DomainError, ErrorCode
from .manifest import load_manifest
from .service import MultipartStore


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    )
    sys.stdout.flush()


def _data_dir(args: argparse.Namespace) -> str:
    return args.data_dir or os.environ.get("MULTIPART_STORE_DIR", ".multipart-store")


def _load_if_requested(store: MultipartStore, manifest_path: str | None) -> dict[str, Any] | None:
    if not manifest_path:
        return None
    manifest = load_manifest(manifest_path)
    return store.load(manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multipart-store",
        description="Local deterministic multipart upload session manager",
    )
    parser.add_argument(
        "--data-dir",
        "--workdir",
        "--work-dir",
        dest="data_dir",
        default=os.environ.get("MULTIPART_STORE_DIR", ".multipart-store"),
        help="runtime directory for SQLite, parts and final objects",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a manifest")
    load.add_argument("manifest")

    upload = subparsers.add_parser(
        "upload", help="run one event round or submit one part directly"
    )
    upload.add_argument("manifest", nargs="?", help="manifest to load and upload")
    upload.add_argument("--manifest", dest="manifest_option")
    upload.add_argument("--round", type=int)
    upload.add_argument("--session-id")
    upload.add_argument("--part-number", type=int)
    upload.add_argument("--content-base64")
    upload.add_argument("--content-text")
    upload.add_argument("--content-file")
    upload.add_argument("--checksum")
    upload.add_argument("--logical-time-ms", type=int)
    upload.add_argument("--overwrite", action="store_true")
    upload.add_argument("--event-id", type=int)
    upload.add_argument("--expected-version", type=int)

    complete = subparsers.add_parser("complete", help="complete a session")
    complete.add_argument("session_id", nargs="?")
    complete.add_argument("--manifest")
    complete.add_argument("--session-id", dest="session_id_option")
    complete.add_argument("--expected-version", type=int)
    complete.add_argument("--logical-time-ms", type=int)

    abort = subparsers.add_parser("abort", help="abort a session")
    abort.add_argument("session_id", nargs="?")
    abort.add_argument("--session-id", dest="session_id_option")
    abort.add_argument("--reason", default="manual abort")
    abort.add_argument("--trigger-type", default="manual")
    abort.add_argument("--trigger-source", default="cli")
    abort.add_argument("--logical-time-ms", type=int)

    resume = subparsers.add_parser(
        "resume", help="recover interrupted operations and optionally clean orphans"
    )
    resume.add_argument("--manifest")
    resume.add_argument("--cleanup", action="store_true")

    status = subparsers.add_parser("status", help="emit stable status JSON")
    status.add_argument("--manifest", help=argparse.SUPPRESS)

    verify = subparsers.add_parser("verify", help="verify final objects and sessions")
    verify.add_argument("--session-id")

    return parser


def _resolve_manifest(args: argparse.Namespace) -> str | None:
    positional = getattr(args, "manifest", None)
    option = getattr(args, "manifest_option", None)
    if positional and option:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            "manifest was supplied both positionally and via --manifest",
            {},
        )
    return positional or option


def _content_from_args(args: argparse.Namespace) -> bytes:
    supplied = [
        value
        for value in (
            args.content_base64,
            args.content_text,
            args.content_file,
        )
        if value is not None
    ]
    if len(supplied) != 1:
        raise DomainError(
            ErrorCode.CONTENT_INVALID,
            "supply exactly one of --content-base64, --content-text, --content-file",
            {},
        )
    if args.content_base64 is not None:
        try:
            return base64.b64decode(args.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DomainError(
                ErrorCode.CONTENT_INVALID,
                "invalid base64 content",
                {"cause": str(exc)},
            ) from exc
    if args.content_text is not None:
        return args.content_text.encode("utf-8")
    try:
        return Path(args.content_file).read_bytes()
    except OSError as exc:
        raise DomainError(
            ErrorCode.CONTENT_INVALID,
            "cannot read content file",
            {"path": args.content_file, "cause": str(exc)},
        ) from exc


def _command_load(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    store = MultipartStore(_data_dir(args))
    return store.load(manifest)


def _command_upload(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = _resolve_manifest(args)
    if manifest_path:
        manifest = load_manifest(manifest_path)
        store = MultipartStore(_data_dir(args))
        loaded = store.load(manifest)
        round_result = store.upload_round(round_no=args.round)
        round_result["load"] = loaded
        return round_result

    store = MultipartStore(_data_dir(args))
    direct_fields = [
        args.session_id,
        args.part_number,
        args.checksum,
        args.logical_time_ms,
    ]
    any_direct = any(value is not None for value in direct_fields) or any(
        value is not None
        for value in (args.content_base64, args.content_text, args.content_file)
    )
    if not any_direct:
        return store.upload_round(round_no=args.round)
    missing = []
    for name, value in (
        ("--session-id", args.session_id),
        ("--part-number", args.part_number),
        ("--checksum", args.checksum),
        ("--logical-time-ms", args.logical_time_ms),
    ):
        if value is None:
            missing.append(name)
    if not any(
        value is not None
        for value in (args.content_base64, args.content_text, args.content_file)
    ):
        missing.append("--content-base64/--content-text/--content-file")
    if missing:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            "direct upload is missing required fields",
            {"missing": missing},
        )
    return store.upload(
        args.session_id,
        args.part_number,
        _content_from_args(args),
        args.checksum,
        args.logical_time_ms,
        overwrite=args.overwrite,
        event_id=args.event_id,
        expected_version=args.expected_version,
        round_no=args.round,
    )


def _command_complete(args: argparse.Namespace) -> dict[str, Any]:
    session_id = args.session_id or args.session_id_option
    if not session_id:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            "complete requires a session id",
            {},
        )
    store = MultipartStore(_data_dir(args))
    _load_if_requested(store, args.manifest)
    return store.complete(
        session_id,
        expected_version=args.expected_version,
        logical_time_ms=args.logical_time_ms,
    )


def _command_abort(args: argparse.Namespace) -> dict[str, Any]:
    session_id = args.session_id or args.session_id_option
    if not session_id:
        raise DomainError(
            ErrorCode.SCHEMA_INVALID,
            "abort requires a session id",
            {},
        )
    store = MultipartStore(_data_dir(args))
    return store.abort(
        session_id,
        reason=args.reason,
        trigger_type=args.trigger_type,
        trigger_source=args.trigger_source,
        logical_time_ms=args.logical_time_ms,
    )


def _command_resume(args: argparse.Namespace) -> dict[str, Any]:
    store = MultipartStore(_data_dir(args))
    _load_if_requested(store, args.manifest)
    return store.resume(cleanup=args.cleanup)


def _command_status(args: argparse.Namespace) -> dict[str, Any]:
    store = MultipartStore(_data_dir(args))
    return store.status()


def _command_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store = MultipartStore(_data_dir(args))
    result = store.verify(args.session_id)
    return result, 0 if result["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            result = _command_load(args)
            exit_code = 0
        elif args.command == "upload":
            result = _command_upload(args)
            exit_code = 0
        elif args.command == "complete":
            result = _command_complete(args)
            exit_code = 0
        elif args.command == "abort":
            result = _command_abort(args)
            exit_code = 0
        elif args.command == "resume":
            result = _command_resume(args)
            exit_code = 0
        elif args.command == "status":
            result = _command_status(args)
            exit_code = 0
        elif args.command == "verify":
            result, exit_code = _command_verify(args)
        else:
            parser.error(f"unknown command {args.command}")
            return 2
    except DomainError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        return exc.exit_code
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": "unexpected internal error",
                    "details": {"cause": str(exc)},
                },
            }
        )
        return 1
    _emit(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
