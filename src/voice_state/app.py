"""Interactive local console application."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes
from .config import AnalysisConfig
from .dataset import load_dataset
from .demo import run_demo
from .diagnostics import recent_errors, record_error
from .engine import AnalysisEngine
from .errors import BusinessError, E_CONFIG_INVALID
from .selfcheck import run_selfcheck
from .snapshot import STAGE_ORDER, SnapshotStore
from .synth import generate_dataset


HELP_TEXT = """Commands:
  status                 Show stages, versions, dialog counts, and recent errors
  generate               Generate the deterministic local sample dataset
  run                    Run or resume all four stages
  step <stage>           Advance through one named stage, preserving snapshots
  demo [stage]           Crash after a committed stage and compare recovery output
  selfcheck              Run cross-module consistency checks
  report                 Show the dialogue-level ranking
  errors                 Show the latest business error codes
  help                   Show this help
  quit                   Exit
"""


class InteractiveApp:
    def __init__(
        self,
        workdir: Path,
        *,
        dataset_dir: Path | None = None,
        state_dir: Path | None = None,
        config: AnalysisConfig | None = None,
    ) -> None:
        self.workdir = Path(workdir)
        self.dataset_dir = dataset_dir or self.workdir / "dataset"
        self.state_dir = state_dir or self.workdir / "state"
        self.config = config or AnalysisConfig()
        self.session_errors: list[dict[str, Any]] = []

    def run(self) -> int:
        print("Voice Emotion State Lab")
        print(f"Workspace: {self.workdir}")
        print(HELP_TEXT)
        self._dashboard()
        while True:
            try:
                command = input("\nvoice-emotion> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not command:
                continue
            name, _, argument = command.partition(" ")
            name = name.lower()
            try:
                if name in {"quit", "exit"}:
                    return 0
                if name == "help":
                    print(HELP_TEXT)
                elif name == "status":
                    self._dashboard()
                elif name == "generate":
                    result = generate_dataset(self.dataset_dir, force=True)
                    print(f"Generated {result['dialog_count']} dialogs at {result['dataset_root']}")
                elif name == "run":
                    self._run_engine()
                elif name == "step":
                    suffix = argument.strip()
                    if suffix not in STAGE_ORDER:
                        raise BusinessError(
                            E_CONFIG_INVALID,
                            "step requires one of: " + ", ".join(STAGE_ORDER),
                            {"field": "stage", "actual": suffix},
                        )
                    self._step_engine(suffix)
                elif name == "demo":
                    stage = argument.strip() or "segmentation"
                    result = run_demo(self.workdir, stop_after=stage, config=self.config)
                    print(
                        "Demo recovered successfully: "
                        f"{result['full_report_hash']} == {result['recovered_report_hash']}"
                    )
                    self._dashboard()
                elif name == "selfcheck":
                    engine = self._engine()
                    result = engine.run()
                    check = run_selfcheck(
                        engine.store,
                        expected_dialog_ids=[item.dialog_id for item in engine.dataset.dialogs],
                        full_report_bytes=result.report_bytes,
                        recovered_report_bytes=result.report_bytes,
                    )
                    print(f"Self-check passed: {check['checksum']}")
                elif name == "report":
                    engine = self._engine()
                    result = engine.run()
                    self._print_ranking(result.report)
                    atomic_write_bytes(self.state_dir / "report.json", result.report_bytes)
                    print(f"Report bytes: {self.state_dir / 'report.json'}")
                elif name == "errors":
                    self._print_errors()
                else:
                    print(f"Unknown command: {name}. Type 'help'.")
            except BusinessError as exc:
                self._handle_error(exc)
            except (OSError, ValueError) as exc:
                error = BusinessError("E_IO_ERROR", str(exc), {"path": str(self.workdir)})
                self._handle_error(error)

    def _engine(self) -> AnalysisEngine:
        return AnalysisEngine(self.dataset_dir, self.state_dir, self.config)

    def _run_engine(self) -> None:
        engine = self._engine()
        result = engine.run()
        print(
            f"Run complete: {result.report_hash}; computed={result.computed_stages}; "
            f"reused={result.reused_stages}"
        )
        self._print_ranking(result.report)
        atomic_write_bytes(self.state_dir / "report.json", result.report_bytes)
        self._dashboard()

    def _step_engine(self, stage: str) -> None:
        engine = self._engine()
        result = engine.run_stage(stage)
        print(
            f"Stage step complete through {stage}: computed={result.computed_stages}; "
            f"reused={result.reused_stages}"
        )
        self._dashboard()

    def _dashboard(self) -> None:
        print("\n== Dashboard ==")
        print(f"Dataset: {self.dataset_dir} ({'present' if self.dataset_dir.exists() else 'missing'})")
        try:
            if self.dataset_dir.is_dir():
                engine = self._engine()
                status = engine.status()
            else:
                status = SnapshotStore(self.state_dir).status()
        except BusinessError as exc:
            self._handle_error(exc)
            return
        print(
            f"Stage: {status.get('current_stage')} | last committed: "
            f"{status.get('last_committed_stage')} | resume from: {status.get('resume_from_stage')}"
        )
        print(
            f"Generation: {status.get('generation', 0)} | epoch: {status.get('epoch', 0)} | "
            f"report hash: {status.get('report_hash')}"
        )
        print("Stage                  Status     Version  Dialogs")
        stages = status.get("stages", {})
        for stage in STAGE_ORDER:
            record = stages.get(stage, {"status": "pending"})
            print(
                f"{stage:<22} {record.get('status','pending'):<10} "
                f"{record.get('version','-'):<8} {record.get('dialog_count','-')}"
            )
        self._print_errors()

    def _handle_error(self, error: BusinessError) -> None:
        try:
            record_error(self.state_dir, error)
        except OSError:
            pass
        self.session_errors.append(error.as_dict()["error"])
        print(f"[{error.code}] {error.message}")
        if error.details:
            print(f"details: {dict(error.details)}")
        self._print_errors()

    def _errors(self) -> list[dict[str, Any]]:
        return [*recent_errors(self.state_dir, limit=5), *self.session_errors][-5:]

    def _print_errors(self) -> None:
        errors = self._errors()
        print("Recent business errors:", "none" if not errors else "")
        for error in errors:
            print(f"  [{error['code']}] {error.get('message','')}")

    @staticmethod
    def _print_ranking(report: dict[str, Any]) -> None:
        print("Dialogue ranking (arousal desc, valence desc, dialog_id asc):")
        for row in report.get("ranking", []):
            print(
                f"  {row['rank']:>2}. {row['dialog_id']}  "
                f"valence={row['weighted_valence']:.6f}  arousal={row['weighted_arousal']:.6f}"
            )


def run_interactive(
    workdir: Path,
    *,
    dataset_dir: Path | None = None,
    state_dir: Path | None = None,
    config: AnalysisConfig | None = None,
) -> int:
    return InteractiveApp(
        workdir,
        dataset_dir=dataset_dir,
        state_dir=state_dir,
        config=config,
    ).run()

