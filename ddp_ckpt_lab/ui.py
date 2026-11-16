"""Tkinter desktop interface for deterministic checkpoint demonstrations."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .checkpoints import CheckpointInfo, CheckpointStore
from .config import TrainingConfig, load_config
from .demo import prepare_output_directory
from .errors import BusinessError
from .training import InitBundle, Trainer, initialize_training
from .verification import VerificationService, assert_bitwise_equal


class SingleInstanceLock:
    """Non-blocking cross-platform lock held for the lifetime of the UI."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any = None
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0, os.SEEK_END)
                if self.stream.tell() == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise BusinessError(
                "E_CHECKPOINT",
                "同一 UI 输出目录已被另一个进程占用",
                path=str(self.path),
                error=str(exc),
            ) from exc

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt

                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.stream.close()
            self.stream = None
            self.acquired = False


class LabController:
    """Headless state operations used by the Tkinter view.

    Keeping this layer independent from Tk makes the state transitions easy to
    inspect and avoids hidden GUI coupling in the verification logic.
    """

    def __init__(self, config: TrainingConfig, output: Path) -> None:
        self.config = config
        self.output = output
        prepare_output_directory(output, reset=True)
        self.store = CheckpointStore(output / "checkpoints")
        self.storage_report = self.store.ensure_ready()
        self.bundle: InitBundle = initialize_training(config)
        self.trainer = Trainer(config, self.bundle.dataset, self.bundle.state)
        self.display_state = self.trainer.state.clone()
        self.last_message = "initialized"

    def reset_cold_start(self) -> list[CheckpointInfo]:
        prepare_output_directory(self.output, reset=True)
        self.store = CheckpointStore(self.output / "checkpoints")
        self.storage_report = self.store.ensure_ready()
        self.bundle = initialize_training(self.config)
        self.trainer = Trainer(self.config, self.bundle.dataset, self.bundle.state)
        checkpoint_infos: list[CheckpointInfo] = []
        for _ in range(self.config.train_steps):
            self.trainer.step()
            if (
                self.trainer.state.global_step % self.config.checkpoint_every
                == 0
                or self.trainer.state.global_step == self.config.train_steps
            ):
                checkpoint_infos.append(
                    self.store.save(self.trainer.state, self.config)
                )
        self.display_state = self.trainer.state.clone()
        self.last_message = (
            f"cold_start_checkpoints={len(checkpoint_infos)} "
            f"final_step={self.trainer.state.global_step}"
        )
        return checkpoint_infos

    def verify_resume(self, info: CheckpointInfo) -> dict[str, Any]:
        steps = max(1, self.config.train_steps - info.global_step)
        report = VerificationService(self.store).verify_resume_consistency(
            self.config, info, steps
        )
        restored = self.store.load(info, self.config)
        self.display_state = restored.state.clone()
        self.last_message = (
            f"resume_consistency=true checkpoint={info.checkpoint_id} "
            f"final_step={report['final_step']}"
        )
        return report

    def config_evolution(self, info: CheckpointInfo) -> dict[str, Any]:
        incompatible = self.config.with_changes(
            hidden_dim=self.config.hidden_dim + 2,
            sharding="contiguous",
        )
        incompatible_probe = None
        try:
            self.store.load(info, incompatible)
        except BusinessError as exc:
            incompatible_probe = exc.to_dict()
        migrated_config = self.config.with_changes(
            learning_rate=self.config.learning_rate + 15
        )
        migrated = self.store.load(info, migrated_config)
        migrated_trainer = Trainer(
            migrated_config, self.bundle.dataset, migrated.state
        )
        migrated_trainer.train_steps(2)
        self.display_state = migrated_trainer.state.clone()

        legacy_root = self.output / "checkpoints_legacy_ui"
        shutil.rmtree(legacy_root, ignore_errors=True)
        legacy_store = CheckpointStore(legacy_root)
        legacy_store.ensure_ready()
        legacy_info = legacy_store.save_legacy_v1(
            migrated.state, migrated_config
        )
        legacy_restored = legacy_store.load(legacy_info, migrated_config)
        if legacy_restored.state.payload_digest() != migrated.state.payload_digest():
            raise BusinessError(
                "E_RESTORE",
                "界面格式迁移后内容摘要不一致",
            )
        self.last_message = (
            f"E_CONFIG conflicts=hidden_dim,sharding; "
            f"lr_migrated={self.config.learning_rate}->{migrated_config.learning_rate}; "
            "format_migrated=v1->v2 replace_rebuild"
        )
        return {
            "incompatible": incompatible_probe,
            "compatible": migrated.as_report(),
            "format_migration": legacy_restored.as_report(),
        }

    def replay(self, info: CheckpointInfo) -> dict[str, Any]:
        steps = max(1, min(6, self.config.train_steps - info.global_step))
        report = VerificationService(self.store).verify_replay(
            self.config, info, steps
        )
        self.last_message = (
            f"replay_reproducible=true checkpoint={info.checkpoint_id} "
            f"final_step={report['final_step']}"
        )
        return report

    def divergence_probe(self) -> dict[str, Any]:
        baseline = self.display_state.clone()
        tampered = baseline.clone()
        value = tampered.workers[0].parameters["W1"].reshape(-1)[0]
        tampered.workers[0].parameters["W1"].reshape(-1)[0] = value + 1.0
        try:
            assert_bitwise_equal(
                baseline,
                tampered,
                step=baseline.global_step,
                context={"source": "ui_divergence_probe"},
            )
        except BusinessError as exc:
            if exc.code != "E_DIVERGE":
                raise
            self.last_message = "E_DIVERGE located worker=0 parameter=W1"
            return exc.to_dict()
        raise BusinessError("E_DIVERGE", "界面差异定位探测未触发")


def run_ui(
    config_path: str | Path = "configs/demo.json",
    output_path: str | Path = "artifacts/ui_session",
) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        print(
            "Tkinter unavailable. On Windows use the official Python installer "
            "with the Tcl/Tk option enabled. "
            f"error={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(config_path)
        output = Path(output_path)
        lock = SingleInstanceLock(output.with_name(f"{output.name}.ui.lock"))
        lock.acquire()
        atexit.register(lock.release)
        controller = LabController(config, output)
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title(f"DDPLAB {__version__} · Distributed Checkpoint Lab")
    root.geometry("1280x820")
    root.minsize(1040, 680)

    try:
        default_font = ("Microsoft YaHei UI", 10)
        root.option_add("*Font", default_font)
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure("Treeview", rowheight=24)
    style.configure("Title.TLabel", font=(default_font[0], 16, "bold"))
    style.configure("Status.TLabel", padding=4)

    outer = ttk.Frame(root, padding=10)
    outer.pack(fill=tk.BOTH, expand=True)
    header = ttk.Frame(outer)
    header.pack(fill=tk.X)
    ttk.Label(
        header,
        text="Deterministic DDP Checkpoint & Restore Verifier",
        style="Title.TLabel",
    ).pack(side=tk.LEFT)
    state_label = ttk.Label(header, text="", style="Status.TLabel")
    state_label.pack(side=tk.RIGHT)

    info_text = tk.StringVar()
    ttk.Label(
        outer,
        textvariable=info_text,
        anchor=tk.W,
        justify=tk.LEFT,
    ).pack(fill=tk.X, pady=(4, 8))

    controls = ttk.Frame(outer)
    controls.pack(fill=tk.X, pady=(0, 8))

    panes = ttk.Panedwindow(outer, orient=tk.VERTICAL)
    panes.pack(fill=tk.BOTH, expand=True)
    workers_frame = ttk.LabelFrame(panes, text="Worker shards (stable order)", padding=6)
    checkpoints_frame = ttk.LabelFrame(
        panes, text="Checkpoints (sorted by step, format, id)", padding=6
    )
    log_frame = ttk.LabelFrame(panes, text="Recovery conclusion / divergence location", padding=6)
    panes.add(workers_frame, weight=1)
    panes.add(checkpoints_frame, weight=1)
    panes.add(log_frame, weight=2)

    worker_columns = (
        "worker_id",
        "step",
        "params_digest",
        "momentum_digest",
        "cursor",
        "worker_state_digest",
    )
    worker_tree = ttk.Treeview(
        workers_frame, columns=worker_columns, show="headings", height=5
    )
    worker_headings = {
        "worker_id": "worker",
        "step": "step",
        "params_digest": "parameter digest",
        "momentum_digest": "momentum digest",
        "cursor": "data cursor",
        "worker_state_digest": "worker state digest",
    }
    worker_widths = {
        "worker_id": 70,
        "step": 70,
        "params_digest": 250,
        "momentum_digest": 250,
        "cursor": 170,
        "worker_state_digest": 260,
    }
    for column in worker_columns:
        worker_tree.heading(column, text=worker_headings[column])
        worker_tree.column(column, width=worker_widths[column], stretch=False, anchor=tk.W)
    worker_scroll = ttk.Scrollbar(
        workers_frame, orient=tk.HORIZONTAL, command=worker_tree.xview
    )
    worker_tree.configure(xscrollcommand=worker_scroll.set)
    worker_tree.pack(fill=tk.BOTH, expand=True)
    worker_scroll.pack(fill=tk.X)

    checkpoint_columns = (
        "step",
        "format",
        "state_digest",
        "manifest_digest",
        "checkpoint_id",
    )
    checkpoint_tree = ttk.Treeview(
        checkpoints_frame, columns=checkpoint_columns, show="headings", height=5
    )
    checkpoint_headings = {
        "step": "step",
        "format": "format",
        "state_digest": "state digest",
        "manifest_digest": "manifest digest",
        "checkpoint_id": "checkpoint",
    }
    checkpoint_widths = {
        "step": 70,
        "format": 80,
        "state_digest": 300,
        "manifest_digest": 300,
        "checkpoint_id": 220,
    }
    for column in checkpoint_columns:
        checkpoint_tree.heading(column, text=checkpoint_headings[column])
        checkpoint_tree.column(
            column, width=checkpoint_widths[column], stretch=False, anchor=tk.W
        )
    checkpoint_scroll = ttk.Scrollbar(
        checkpoints_frame, orient=tk.HORIZONTAL, command=checkpoint_tree.xview
    )
    checkpoint_tree.configure(xscrollcommand=checkpoint_scroll.set)
    checkpoint_tree.pack(fill=tk.BOTH, expand=True)
    checkpoint_scroll.pack(fill=tk.X)

    log_text = tk.Text(log_frame, height=14, wrap=tk.NONE, undo=False)
    log_scroll_y = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text.yview)
    log_scroll_x = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=log_text.xview)
    log_text.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll_y.grid(row=0, column=1, sticky="ns")
    log_scroll_x.grid(row=1, column=0, sticky="ew")
    log_frame.rowconfigure(0, weight=1)
    log_frame.columnconfigure(0, weight=1)

    status_var = tk.StringVar(value="ready")
    status = ttk.Label(outer, textvariable=status_var, style="Status.TLabel", relief=tk.SUNKEN)
    status.pack(fill=tk.X, pady=(8, 0))

    checkpoint_by_id: dict[str, CheckpointInfo] = {}
    log_sequence = 0

    def append_log(message: str) -> None:
        nonlocal log_sequence
        log_sequence += 1
        log_text.insert(tk.END, f"[{log_sequence:04d}] {message}\n")
        log_text.see(tk.END)

    def refresh_workers() -> None:
        for item in worker_tree.get_children():
            worker_tree.delete(item)
        state = controller.display_state
        for worker in sorted(state.workers, key=lambda item: item.worker_id):
            cursor = worker.cursor
            worker_tree.insert(
                "",
                tk.END,
                values=(
                    worker.worker_id,
                    state.global_step,
                    worker.params_digest,
                    worker.momentums_digest,
                    f"epoch={cursor.epoch} pos={cursor.position} order={cursor.order.size}",
                    worker.payload_digest(),
                ),
            )

    def selected_checkpoint() -> CheckpointInfo | None:
        selection = checkpoint_tree.selection()
        if selection:
            return checkpoint_by_id.get(selection[0])
        checkpoints = sorted(
            checkpoint_by_id.values(),
            key=lambda info: (info.global_step, info.format_version, info.checkpoint_id),
        )
        return checkpoints[-1] if checkpoints else None

    def refresh_checkpoints() -> None:
        checkpoint_by_id.clear()
        for item in checkpoint_tree.get_children():
            checkpoint_tree.delete(item)
        scan = controller.store.scan()
        for info in scan.checkpoints:
            checkpoint_by_id[info.checkpoint_id] = info
            checkpoint_tree.insert(
                "",
                tk.END,
                iid=info.checkpoint_id,
                values=(
                    info.global_step,
                    f"v{info.format_version}",
                    info.state_digest,
                    info.manifest_digest,
                    info.checkpoint_id,
                ),
            )
        checkpoints = sorted(
            checkpoint_by_id.values(),
            key=lambda info: (info.global_step, info.format_version, info.checkpoint_id),
        )
        if checkpoints and not checkpoint_tree.selection():
            checkpoint_tree.selection_set(checkpoints[-1].checkpoint_id)
            checkpoint_tree.focus(checkpoints[-1].checkpoint_id)
        if scan.residues:
            status_var.set(f"E_CHECKPOINT residue_count={len(scan.residues)}")
            append_log(
                "E_CHECKPOINT incomplete checkpoint residue: "
                + json.dumps(list(scan.residues), ensure_ascii=False, sort_keys=True)
            )

    def refresh_all() -> None:
        refresh_workers()
        refresh_checkpoints()
        info_text.set(
            "config_digest={}  step={}  data_digest={}  initial_param_digest={}  "
            "atomic_rename={}  output={}".format(
                config.config_digest,
                controller.display_state.global_step,
                controller.bundle.data_digest,
                controller.bundle.parameter_digest,
                controller.storage_report.get("atomic_directory_rename"),
                controller.output,
            )
        )
        state_label.configure(text=controller.last_message)

    def guarded(action_name: str, operation: Any) -> None:
        status_var.set(f"running: {action_name}")
        root.update_idletasks()
        try:
            operation()
        except BusinessError as exc:
            append_log(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
            status_var.set(f"{exc.code}: {exc.message}")
        except Exception as exc:
            append_log(f"UNEXPECTED {type(exc).__name__}: {exc}")
            status_var.set(f"UNEXPECTED: {type(exc).__name__}")
        else:
            status_var.set("OK")
        finally:
            refresh_all()

    def do_cold_start() -> None:
        infos = controller.reset_cold_start()
        append_log(
            "cold_start checkpoints="
            + ",".join(
                f"{info.checkpoint_id}:v{info.format_version}"
                for info in infos
            )
        )

    def do_resume() -> None:
        info = selected_checkpoint()
        if info is None:
            raise BusinessError("E_RESTORE", "没有选中的完整检查点")
        report = controller.verify_resume(info)
        append_log(
            f"resume_consistency=true checkpoint={info.checkpoint_id} "
            f"continuation={report['continuation_steps']} final_step={report['final_step']} "
            f"baseline={report['final_baseline_state_digest']} "
            f"resumed={report['final_resumed_state_digest']}"
        )

    def do_evolution() -> None:
        info = selected_checkpoint()
        if info is None:
            raise BusinessError("E_RESTORE", "没有选中的完整检查点")
        report = controller.config_evolution(info)
        append_log(
            "E_CONFIG incompatible fields="
            + ",".join(
                conflict["field"]
                for conflict in report["incompatible"]["details"]["conflicts"]
            )
        )
        migration = report["compatible"]["migration"]
        append_log(
            "compatible migration strategy={} lr_changed={} target_step={}".format(
                migration["strategy"],
                migration["learning_rate_changed"],
                report["compatible"]["global_step"],
            )
        )
        legacy = report["format_migration"]
        append_log(
            "format migration v{}->v{} strategy={} payload_preserved=true".format(
                legacy["migration"]["source_format_version"],
                legacy["migration"]["target_format_version"],
                legacy["migration"]["strategy"],
            )
        )

    def do_replay() -> None:
        info = selected_checkpoint()
        if info is None:
            raise BusinessError("E_RESTORE", "没有选中的完整检查点")
        report = controller.replay(info)
        append_log(
            f"replay_reproducible=true steps={report['replay_steps']} "
            f"first={report['first_run_state_digest']} "
            f"second={report['second_run_state_digest']} "
            f"unmodified={report['checkpoint_unmodified']}"
        )

    def do_divergence() -> None:
        details = controller.divergence_probe()
        location = details["first_divergence"]
        append_log(
            "E_DIVERGE first divergence worker={} parameter={} kind={} step={}".format(
                location["worker_id"],
                location["parameter_name"],
                location["state_kind"],
                location["step"],
            )
        )

    ttk.Button(
        controls,
        text="Cold start + checkpoint",
        command=lambda: guarded("cold start", do_cold_start),
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        controls,
        text="Resume consistency",
        command=lambda: guarded("resume consistency", do_resume),
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        controls,
        text="Config evolution",
        command=lambda: guarded("config evolution", do_evolution),
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        controls,
        text="Replay verification",
        command=lambda: guarded("replay verification", do_replay),
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        controls,
        text="Locate divergence",
        command=lambda: guarded("divergence probe", do_divergence),
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        controls,
        text="Refresh",
        command=lambda: guarded("refresh", lambda: None),
    ).pack(side=tk.LEFT)

    refresh_all()
    append_log(
        "UI ready. Buttons execute cold start, resume bitwise verification, "
        "configuration/format migration, replay, and E_DIVERGE location probe."
    )
    root.mainloop()
    lock.release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the Tkinter checkpoint lab UI.")
    parser.add_argument("--config", default="configs/demo.json")
    parser.add_argument("--output", default="artifacts/ui_session")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_ui(args.config, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
