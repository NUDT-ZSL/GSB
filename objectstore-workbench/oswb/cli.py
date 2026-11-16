"""Command line entry point for the workbench."""

import argparse
import json
import os
import sys

from . import __version__
from .client import StorageClient
from .errors import BusinessError
from .logging_utils import configure_logger
from .protocol import validate_port
from .sample import generate_sample, DEFAULT_SAMPLE_SIZE, DEFAULT_SAMPLE_NAME
from .server import run_server
from .uploader import UploadWorkflow

DEFAULT_BASE_DIR = os.path.join(os.getcwd(), "runtime", "workbench-data")
DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "runtime", "logs")
DEFAULT_SAMPLE_DIR = os.path.join(os.getcwd(), "runtime", "samples")
DEFAULT_PORT = 9101


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def parse_labels(raw):
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(f"--labels must be JSON: {error}")
    if not isinstance(value, dict):
        raise SystemExit("--labels must be a JSON object")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="oswb",
        description="Local chunked-upload and metadata consistency workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print workbench version")

    sample = sub.add_parser("make-sample", help="generate the deterministic sample")
    sample.add_argument("--size", type=int, default=DEFAULT_SAMPLE_SIZE)
    sample.add_argument("--output")

    serve = sub.add_parser("serve", help="run the loopback TCP storage service")
    serve.add_argument("--echo", action="store_true")

    up = sub.add_parser("upload", help="create a session and upload all chunks")
    up.add_argument("--file", required=True)
    up.add_argument("--object-key", required=True)
    up.add_argument("--chunk-size", type=int, required=True)
    up.add_argument("--algorithm", default="sha256")
    up.add_argument("--labels", default="{}")
    up.add_argument("--global-concurrency", type=int, default=4)
    up.add_argument("--session-quota", type=int, default=2)
    up.add_argument("--expect-version", type=int)

    resume = sub.add_parser("resume", help="resume an interrupted session")
    resume.add_argument("--file", required=True)
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--chunk-size", type=int, required=True)
    resume.add_argument("--algorithm", default="sha256")
    resume.add_argument("--complete", action="store_true")
    resume.add_argument("--expect-version", type=int)

    commit = sub.add_parser("commit", help="verify and merge a session")
    commit.add_argument("--session-id", required=True)
    commit.add_argument("--expect-version", type=int)

    sessions = sub.add_parser("sessions", help="list sessions")
    sessions.add_argument("--state")

    audit = sub.add_parser("audit", help="run the full consistency audit")

    versions = sub.add_parser("versions", help="list object metadata versions")
    versions.add_argument("--object-key", required=True)

    diff = sub.add_parser("diff", help="diff two metadata versions")
    diff.add_argument("--object-key", required=True)
    diff.add_argument("--left", type=int, required=True)
    diff.add_argument("--right", type=int, required=True)

    snap = sub.add_parser("snapshot", help="create or restore a snapshot")
    snap.add_argument("action", choices=("create", "restore", "list"))
    snap.add_argument("--snapshot-id")

    inject = sub.add_parser("inject", help="inject one of the three faults")
    inject.add_argument("fault", choices=("chunk_truncation",
                                          "metadata_digest_corruption",
                                          "version_conflict"))
    inject.add_argument("--session-id")
    inject.add_argument("--index", type=int)
    inject.add_argument("--object-key")
    inject.add_argument("--keep-bytes", type=int, default=0)

    demo = sub.add_parser("demo", help="end-to-end deterministic demo (in-process)")
    demo.add_argument("--chunk-size", type=int, default=128 * 1024)
    demo.add_argument("--global-concurrency", type=int, default=4)
    demo.add_argument("--session-quota", type=int, default=2)
    demo.add_argument("--keep-data", action="store_true")

    gui = sub.add_parser("gui", help="launch the Tkinter workbench")
    gui.add_argument("--in-process", action="store_true", default=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        emit({"version": __version__})
        return 0
    if args.command == "serve":
        validate_port(args.port)
        configure_logger("oswb.server", log_dir=args.log_dir, echo=args.echo)
        run_server(args.host, args.port, args.base_dir, log_dir=args.log_dir,
                   echo=args.echo)
        return 0
    if args.command == "make-sample":
        output = args.output or os.path.join(DEFAULT_SAMPLE_DIR, DEFAULT_SAMPLE_NAME)
        path = generate_sample(output, args.size)
        emit({"sample": path, "size": args.size})
        return 0

    configure_logger("oswb.client", log_dir=args.log_dir)
    client = StorageClient(args.host, args.port)
    workflow = UploadWorkflow(client, args.global_concurrency if hasattr(
        args, "global_concurrency") else 4,
        args.session_quota if hasattr(args, "session_quota") else 2)
    try:
        return dispatch(args, client, workflow)
    except BusinessError as error:
        emit({"code": error.code, "message": error.message, "detail": error.detail})
        return 1
    except (ConnectionError, OSError) as error:
        emit({"code": "E_TRANSPORT", "message": str(error), "detail": {}})
        return 1


def dispatch(args, client, workflow):
    if args.command == "upload":
        result = workflow.start_upload(
            args.file, args.object_key, args.chunk_size,
            algorithm=args.algorithm, labels=parse_labels(args.labels),
            expect_version=args.expect_version)
        emit({"code": "OK", "session": result["session"],
              "attempts": result["attempts"]})
    elif args.command == "resume":
        result = workflow.resume_upload(
            args.file, args.session_id, args.chunk_size, algorithm=args.algorithm)
        outcome = {"code": "OK", "session": result["session"],
                   "attempts": result["attempts"], "skipped": result["skipped"]}
        if args.complete:
            outcome["commit"] = client.commit_session(
                args.session_id, expect_version=args.expect_version)
        emit(outcome)
    elif args.command == "commit":
        emit({"code": "OK", "result": client.commit_session(
            args.session_id, expect_version=args.expect_version)})
    elif args.command == "sessions":
        emit({"code": "OK", "sessions": client.list_sessions(args.state)})
    elif args.command == "audit":
        emit({"code": "OK", "result": client.audit()})
    elif args.command == "versions":
        emit({"code": "OK", "versions": client.list_versions(args.object_key)})
    elif args.command == "diff":
        emit({"code": "OK",
              "diff": client.diff_versions(args.object_key, args.left, args.right)})
    elif args.command == "snapshot":
        if args.action == "create":
            emit({"code": "OK", "result": client.create_snapshot(args.snapshot_id)})
        elif args.action == "restore":
            emit({"code": "OK",
                  "result": client.restore_snapshot(args.snapshot_id)})
        else:
            emit({"code": "OK", "snapshots": client.list_snapshots()})
    elif args.command == "inject":
        params = {}
        if args.session_id is not None:
            params["session_id"] = args.session_id
        if args.index is not None:
            params["index"] = args.index
        if args.object_key is not None:
            params["object_key"] = args.object_key
        params["keep_bytes"] = args.keep_bytes
        emit({"code": "OK", "result": client.inject_fault(args.fault, **params)})
    elif args.command == "demo":
        return run_demo(args)
    elif args.command == "gui":
        from .gui import launch_gui
        launch_gui(args.host, args.port, args.base_dir, args.log_dir)
    return 0


def run_demo(args):
    from .demo import run_end_to_end
    result = run_end_to_end(
        base_dir=args.base_dir, log_dir=args.log_dir,
        chunk_size=args.chunk_size,
        global_concurrency=args.global_concurrency,
        session_quota=args.session_quota, keep_data=args.keep_data)
    emit(result)
    return 0 if result["code"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
