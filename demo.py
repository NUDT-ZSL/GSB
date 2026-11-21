"""Convenience entry point for the local acceptance demonstration."""

from pathlib import Path

from quant_diag.demo import run_demo


if __name__ == "__main__":
    report = run_demo(
        Path(__file__).resolve().parent,
        progress=lambda step: print(f"[demo] {step}"),
    )
    print(
        f"demo passed={report['passed']} "
        f"result_digest={report['full_run']['result_digest']}"
    )
