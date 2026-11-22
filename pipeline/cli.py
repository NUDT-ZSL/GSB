"""Command-line entry: generate / build / query / verify."""

import argparse
import json
import os
import sys

from .builder import BuildConfig, IndexBuilder
from .errors import PipelineError
from .index_reader import IndexService
from .sample_gen import generate_docs, generate_queries
from .verify import run_full_verification


def main(argv=None):
    parser = argparse.ArgumentParser(prog="index-pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--out", default="data")
    p_gen.add_argument("--docs", type=int, default=200)
    p_gen.add_argument("--queries", type=int, default=20)
    p_gen.add_argument("--seed", type=int, default=42)

    p_build = sub.add_parser("build")
    p_build.add_argument("--docs", default="data/docs.jsonl")
    p_build.add_argument("--store", default="store")
    p_build.add_argument("--block-size", type=int, default=8)
    p_build.add_argument("--max-postings", type=int, default=1_000_000)

    p_query = sub.add_parser("query")
    p_query.add_argument("--store", default="store")
    p_query.add_argument("--queries", default="data/queries.jsonl")
    p_query.add_argument("--max-cache-blocks", type=int, default=1024)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--store", default="store")
    p_verify.add_argument("--docs", default="data/docs.jsonl")
    p_verify.add_argument("--queries", default="data/queries.jsonl")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "generate":
            docs = generate_docs(os.path.join(args.out, "docs.jsonl"),
                                 num_docs=args.docs, seed=args.seed)
            queries = generate_queries(
                os.path.join(args.out, "queries.jsonl"),
                num_queries=args.queries, seed=args.seed + 81)
            print(json.dumps({"docs": docs, "queries": queries}))
        elif args.cmd == "build":
            config = BuildConfig(block_size=args.block_size,
                                 max_postings=args.max_postings)
            builder = IndexBuilder(args.store, config)
            summary = builder.build(args.docs)
            print(json.dumps(summary, sort_keys=True))
        elif args.cmd == "query":
            service = IndexService(
                os.path.join(args.store, "index.json"),
                max_cache_blocks=args.max_cache_blocks)
            out = []
            with open(args.queries, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    q = json.loads(line)
                    res = service.query_checked(q["query_id"], q["terms"],
                                                q["top_k"], 1e-9)
                    out.append({"query_id": q["query_id"], "results": res})
            report = {"answers": out,
                      "degradation_categories": service.degradation_categories,
                      "degraded_terms": sorted(service.degraded_terms)}
            print(json.dumps(report, sort_keys=True))
        elif args.cmd == "verify":
            report = run_full_verification(
                os.path.join(args.store, "index.json"),
                args.docs, args.queries)
            print(json.dumps(report, sort_keys=True))
        return 0
    except PipelineError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
