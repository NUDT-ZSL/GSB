#!/usr/bin/env python3
"""safe_unzip.py - Securely stage an untrusted ZIP archive into a target directory.

Security guarantees
-------------------
1. Every archive entry is inspected BEFORE anything is written. An entry is
   "unsafe" when its extraction path would escape the target directory, which
   covers:
     - parent-directory traversal ("../evil", "a/../../evil", "..\\evil")
     - absolute POSIX paths ("/etc/passwd")
     - absolute Windows paths with a drive ("C:/Windows/x")
     - Windows drive-relative paths ("C:evil")
     - UNC paths ("//server/share/x")
   Path separators are normalised first, so backslash-based traversal works on
   every platform and ZIP entries using either separator are handled.
2. If any entry is unsafe, extraction is refused wholesale: no file or
   directory is created inside the target directory, every unsafe entry is
   listed (in archive order) and the process exits non-zero.
3. Only when every entry is safe are files written, preserving the archive
   structure and the recorded modification timestamps.
4. If a write fails partway through, every file/directory created by this run
   is removed so the target directory returns to its pre-operation state.
5. A corrupt / unreadable archive aborts with a clear error, no target
   directory is created and no report file is written.

Only the Python standard library is used (Python 3.10+).

CLI
---
    python safe_unzip.py <zip_path> <target_dir> [report_path]

Exit codes
----------
    0  success, all entries extracted
    2  bad arguments / invalid paths
    3  archive corrupt or unreadable
    4  rejected: one or more unsafe entries
    5  extraction failed (target rolled back)
    6  report could not be written (extraction itself still succeeded)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

# Indirection so tests can simulate write failures without replacing the
# built-in open() globally.
_open = open

# Exit codes (documented in the module docstring).
EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_CORRUPT = 3
EXIT_UNSAFE = 4
EXIT_EXTRACT_FAILED = 5
EXIT_REPORT_FAILED = 6


class SafeUnzipError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class BadArgumentsError(SafeUnzipError):
    exit_code = EXIT_BAD_ARGS


class CorruptArchiveError(SafeUnzipError):
    exit_code = EXIT_CORRUPT


class UnsafeArchiveError(SafeUnzipError):
    exit_code = EXIT_UNSAFE


class ExtractionFailedError(SafeUnzipError):
    exit_code = EXIT_EXTRACT_FAILED


class ReportWriteError(SafeUnzipError):
    exit_code = EXIT_REPORT_FAILED


@dataclass
class EntryResult:
    """Per-entry verdict used to build the JSON report."""

    name: str
    status: str  # "safe" | "unsafe"
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "reason": self.reason}


def classify_entry(entry_name: str, target_dir: str) -> EntryResult:
    """Decide whether *entry_name* would land inside *target_dir*.

    No filesystem access is required: the decision is based purely on the
    archive name and a normalised target path, so inspection cannot mutate the
    target directory.
    """
    # ZIP stores names with forward slashes, but malicious archives may use
    # backslashes; normalise so the same rules apply on every OS.
    normalised = entry_name.replace("\\", "/")

    # Absolute / rooted / drive-qualified paths never stay inside the target.
    if normalised.startswith("/"):
        return EntryResult(
            entry_name, "unsafe", "absolute or UNC path (leading slash)"
        )
    if _has_drive_prefix(normalised):
        return EntryResult(
            entry_name, "unsafe", "absolute or drive-relative Windows path"
        )

    parts = [p for p in normalised.split("/") if p != ""]
    if not parts:
        return EntryResult(entry_name, "unsafe", "empty entry name")

    if any(p == ".." for p in parts):
        return EntryResult(
            entry_name, "unsafe", "parent-directory traversal component '..'"
        )

    # Defence in depth: resolve the intended destination and require the
    # commonpath with the target to be the target itself. is_relative_to is
    # only available from 3.9, but compare via commonpath for clarity.
    target_real = os.path.realpath(target_dir)
    destination = os.path.realpath(os.path.join(target_real, *parts))
    try:
        common = os.path.commonpath([target_real, destination])
    except ValueError:
        # Different drives on Windows -> cannot be inside the target.
        return EntryResult(
            entry_name, "unsafe", "destination resolves outside target directory"
        )
    if common != target_real:
        return EntryResult(
            entry_name, "unsafe", "destination resolves outside target directory"
        )

    return EntryResult(entry_name, "safe", "")


def _has_drive_prefix(normalised_name: str) -> bool:
    """True for 'C:/...', 'C:...' style names (drive letter + colon)."""
    if len(normalised_name) < 2:
        return False
    return normalised_name[1] == ":" and normalised_name[0].isalpha()


def _make_dirs(path: str, created_dirs: list[str]) -> None:
    """os.makedirs while recording every directory this call creates."""
    existing = path
    newly_created: list[str] = []
    while not os.path.isdir(existing):
        parent = os.path.dirname(existing)
        if parent == existing:  # reached a filesystem root without finding it
            raise FileNotFoundError(f"cannot create directory: {path}")
        newly_created.append(existing)
        existing = parent
    os.makedirs(path, exist_ok=True)
    created_dirs.extend(reversed(newly_created))


def _apply_timestamp(destination: str, info: zipfile.ZipInfo) -> None:
    """Restore the ZIP-recorded modification time; warn instead of failing."""
    try:
        dt = datetime(
            info.date_time[0],
            info.date_time[1],
            info.date_time[2],
            info.date_time[3],
            info.date_time[4],
            info.date_time[5],
            tzinfo=timezone.utc,
        )
        epoch = dt.timestamp()
        os.utime(destination, (epoch, epoch))
    except (ValueError, OSError, OverflowError, NotImplementedError) as exc:
        print(
            f"[warn] could not set timestamp on '{destination}': {exc}",
            file=sys.stderr,
        )


def _rollback(created_files: list[str], created_dirs: list[str]) -> None:
    """Remove everything created during the failed extraction attempt."""
    for path in created_files:
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
        except OSError:
            pass
    # Directories were recorded in creation order; reverse gives leaf-first.
    for path in reversed(created_dirs):
        try:
            if os.path.isdir(path):
                os.rmdir(path)  # only succeeds when empty
        except OSError:
            pass


def safe_extract(zip_path: str, target_dir: str, report_path: str) -> dict:
    """Validate *zip_path*, extract into *target_dir*, write a JSON report.

    Returns the report object. Raises a SafeUnzipError subclass on failure.
    """
    if not os.path.isfile(zip_path):
        raise BadArgumentsError(
            f"ZIP path does not exist or is not a file: {zip_path}"
        )

    # ---- Open + structural validation (no writes yet) --------------------
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise CorruptArchiveError(f"corrupt or unreadable ZIP '{zip_path}': {exc}")
    except OSError as exc:
        raise CorruptArchiveError(f"cannot read ZIP '{zip_path}': {exc}")

    with zf:
        try:
            bad = zf.testzip()
            if bad is not None:
                raise CorruptArchiveError(
                    f"corrupt ZIP '{zip_path}': CRC/integrity check failed "
                    f"for entry '{bad}'"
                )
            infos = zf.infolist()
        except (zipfile.BadZipFile, zlib.error) as exc:
            raise CorruptArchiveError(
                f"corrupt ZIP '{zip_path}': {exc}"
            ) from exc

        # ---- Per-entry path inspection (still no writes) -----------------
        results = [classify_entry(info.filename, target_dir) for info in infos]
        unsafe = [r for r in results if r.status == "unsafe"]

        if unsafe:
            reasons = "; ".join(f"{r.name} ({r.reason})" for r in unsafe)
            raise UnsafeArchiveError(
                f"extraction refused; {len(unsafe)} unsafe entr"
                f"{'y' if len(unsafe) == 1 else 'ies'}: {reasons}"
            )

        # ---- All entries safe: create target and extract -----------------
        created_dirs: list[str] = []
        created_files: list[str] = []
        try:
            _make_dirs(target_dir, created_dirs)

            for info in infos:
                parts = [
                    p
                    for p in info.filename.replace("\\", "/").split("/")
                    if p != ""
                ]
                destination = os.path.join(target_dir, *parts)

                if info.is_dir():
                    _make_dirs(destination, created_dirs)
                    _apply_timestamp(destination, info)
                    continue

                parent = os.path.dirname(destination)
                if parent:
                    _make_dirs(parent, created_dirs)

                # Stream the payload to disk in fixed-size chunks.
                with zf.open(info, "r") as src, _open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=64 * 1024)
                created_files.append(destination)
                _apply_timestamp(destination, info)
        except (OSError, zipfile.BadZipFile) as exc:
            _rollback(created_files, created_dirs)
            report = {
                "zip_path": os.path.abspath(zip_path),
                "target_dir": os.path.abspath(target_dir),
                "result": "failed",
                "entry_count": len(results),
                "unsafe_count": 0,
                "error": f"extraction failed and changes were rolled back: {exc}",
                "entries": [r.to_dict() for r in results],
            }
            try:
                write_report(report, report_path)
            except ReportWriteError as report_exc:
                print(f"[warn] {report_exc}", file=sys.stderr)
            raise ExtractionFailedError(
                f"extraction failed; target directory rolled back: {exc}"
            ) from exc

    report = {
        "zip_path": os.path.abspath(zip_path),
        "target_dir": os.path.abspath(target_dir),
        "result": "extracted",
        "entry_count": len(results),
        "unsafe_count": 0,
        "error": None,
        "entries": [r.to_dict() for r in results],
    }
    write_report(report, report_path)
    return report


def write_report(report: dict, report_path: str) -> None:
    """Write *report* as pretty-printed UTF-8 JSON."""
    parent = os.path.dirname(os.path.abspath(report_path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        raise ReportWriteError(
            f"could not write report to '{report_path}': {exc}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safe_unzip.py",
        description=(
            "Security-check every entry of a ZIP archive and extract it into "
            "a target directory only when all entries are safe."
        ),
    )
    parser.add_argument("zip_path", help="path to the ZIP archive to inspect")
    parser.add_argument(
        "target_dir", help="staging directory the archive is extracted into"
    )
    parser.add_argument(
        "report_path",
        nargs="?",
        default=None,
        help="optional output path for the JSON report "
        "(default: <target_dir>.report.json next to the target directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    report_path = args.report_path or (
        os.path.join(
            os.path.dirname(os.path.abspath(args.target_dir)) or ".",
            os.path.basename(os.path.normpath(args.target_dir)) + ".report.json",
        )
    )

    try:
        report = safe_extract(args.zip_path, args.target_dir, report_path)
    except UnsafeArchiveError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        print(
            f"[error] no files were written to '{args.target_dir}'",
            file=sys.stderr,
        )
        return exc.exit_code
    except SafeUnzipError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return exc.exit_code

    print(
        f"[ok] {report['entry_count']} entr"
        f"{'y' if report['entry_count'] == 1 else 'ies'} checked and "
        f"extracted into '{args.target_dir}'"
    )
    print(f"[ok] JSON report written to '{report_path}'")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
