#!/usr/bin/env python3
"""Entry point for the SDF mesh workbench.

Subcommands:
    generate                      write the deterministic project file
    list                          print the asset manifest
    extract <asset|--all>         run the pipeline and store a version
    report  <asset> [--version N] print a stored report
    diff    <asset> A B           compare two stored versions
    serve   [--port P]            start the Flask demo service
"""

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_PATH = os.path.join(BASE_DIR, "data", "project.json")
DB_PATH = os.path.join(BASE_DIR, "data", "workbench.db")


def _json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    sub.add_parser("list")
    p_extract = sub.add_parser("extract")
    p_extract.add_argument("asset", nargs="?", default=None,
                           help="asset name (omit with --all)")
    p_extract.add_argument("--all", action="store_true",
                           help="extract every non-boundary asset")
    p_report = sub.add_parser("report")
    p_report.add_argument("asset")
    p_report.add_argument("--version", type=int, default=None)
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("asset")
    p_diff.add_argument("a", type=int)
    p_diff.add_argument("b", type=int)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    from workbench.db import Store
    from workbench.errors import WorkbenchError
    from workbench.generator import (asset_summary, generate_project,
                                     get_asset, load_project,
                                     save_project)

    try:
        if args.cmd == "generate":
            project = generate_project()
            save_project(project, PROJECT_PATH)
            _json({"written": PROJECT_PATH, "seed": project["seed"],
                   "assets": [a["name"] for a in project["assets"]]})
            return 0

        if args.cmd == "list":
            project = load_project(PROJECT_PATH)
            _json({"assets": [asset_summary(a) for a in project["assets"]],
                   "board_layout": project["board_layout"]})
            return 0

        if args.cmd == "extract":
            from workbench import pipeline
            project = load_project(PROJECT_PATH)
            store = Store(DB_PATH)
            if args.all:
                names = [a["name"] for a in project["assets"]
                         if not a.get("boundary_sample")]
            elif args.asset:
                names = [args.asset]
            else:
                parser.error("extract requires an asset name or --all")
            results = []
            for name in names:
                asset = get_asset(project, name)
                report, meshes = pipeline.run_asset(project, asset)
                version = store.save_version(
                    name, asset["constraints"], report, meshes)
                results.append({"asset": name, "version": version,
                                "report": report})
            _json({"results": results} if len(results) > 1 else results[0])
            return 0

        if args.cmd == "report":
            store = Store(DB_PATH)
            if args.version is None:
                versions = store.list_versions(args.asset)
                if not versions:
                    raise WorkbenchError("E_VERSION_NOT_FOUND",
                                         f"no versions for {args.asset!r}",
                                         asset=args.asset)
                args.version = versions[-1]["version"]
            _json(store.get_version(args.asset, args.version))
            return 0

        if args.cmd == "diff":
            store = Store(DB_PATH)
            _json(store.diff_versions(args.asset, args.a, args.b))
            return 0

        if args.cmd == "serve":
            from workbench.server import create_app
            app = create_app(PROJECT_PATH, DB_PATH)
            app.run(host=args.host, port=args.port)
            return 0
    except WorkbenchError as err:
        _json(err.to_dict())
        return 2


if __name__ == "__main__":
    sys.exit(main())
