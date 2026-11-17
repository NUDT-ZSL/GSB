"""Open a manifest directory: upgrade anything below the current version in
memory, entry by entry, checkpoint after each entry, rewrite as current
version, keep the old file as a read-only backup. Resumable after a kill."""
import json
import os
import shutil
import stat

from . import manifest as mf
from .errors import E_FORMAT, PlatformError

CHECKPOINT_NAME = ".upgrade_checkpoint.json"
BACKUP_DIR = "backup"


class InjectedKill(Exception):
    """Simulates a forced process termination mid-upgrade."""


def _checkpoint_path(directory):
    return os.path.join(directory, CHECKPOINT_NAME)


def _load_checkpoint(directory):
    try:
        with open(_checkpoint_path(directory), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint(directory, checkpoint):
    path = _checkpoint_path(directory)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(checkpoint, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _entry_source(doc, version):
    if version == 1:
        entries = doc.get("entries")
        if not isinstance(entries, dict):
            raise PlatformError(E_FORMAT, "v1 entries must be an object",
                                path="$.entries")
        return [{"key": k, "value": v} for k, v in sorted(entries.items())]
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise PlatformError(E_FORMAT, "entries must be a list", path="$.entries")
    return entries


def _convert_entry(raw, version, path):
    if version == 1:
        for field in ("key", "value"):
            if field not in raw:
                raise PlatformError(E_FORMAT, f"missing field '{field}'",
                                    path=f"{path}.{field}")
        return {"key": raw["key"], "value": raw["value"],
                "version": 1, "targets": ["*"]}
    # v2 -> v3 keeps the entry as-is (v3 additions are file-level)
    return {"key": raw["key"], "value": raw["value"],
            "version": raw["version"], "targets": sorted(raw["targets"])}


def upgrade_directory(directory, logger=None, fail_after=None):
    """Upgrade all sub-current manifests in `directory`.

    fail_after: test hook - raise InjectedKill after N entry conversions to
    simulate a forced kill; a rerun resumes from the checkpoint.
    Returns a report dict.
    """
    checkpoint = _load_checkpoint(directory)
    report = {"upgraded": [], "skipped_current": [], "resumed": bool(checkpoint)}
    converted_total = 0

    names = sorted(n for n in os.listdir(directory)
                   if n.endswith(".json") and not n.startswith("."))
    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise PlatformError(E_FORMAT, f"invalid JSON: {exc.msg}",
                                path=f"{name}[line {exc.lineno}]")
        version = mf.detect_version(doc)
        if version >= mf.CURRENT_VERSION:
            report["skipped_current"].append(name)
            continue

        state = checkpoint.get(name)
        if state is None:
            state = {"source_version": version, "converted": []}
            checkpoint[name] = state
        source_entries = _entry_source(doc, version)
        done = len(state["converted"])

        for index in range(done, len(source_entries)):
            raw = source_entries[index]
            state["converted"].append(
                _convert_entry(raw, version, f"{name}:$.entries[{index}]"))
            _save_checkpoint(directory, checkpoint)  # checkpoint per entry
            converted_total += 1
            if fail_after is not None and converted_total >= fail_after:
                if logger:
                    logger.log("upgrade_killed", file=name, entry_index=index)
                raise InjectedKill(f"killed after {converted_total} conversions")

        upgraded = mf.normalize({
            "format_version": mf.CURRENT_VERSION,
            "entries": state["converted"],
            "policy": dict(mf.DEFAULT_POLICY),
            "derive": [],
        })
        backup_dir = os.path.join(directory, BACKUP_DIR)
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, name)
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mf.dumps(upgraded))
        del checkpoint[name]
        _save_checkpoint(directory, checkpoint)
        report["upgraded"].append(name)
        if logger:
            logger.log("manifest_upgraded", file=name, from_version=version,
                       to_version=mf.CURRENT_VERSION,
                       digest=mf.canonical_digest(upgraded))

    if not checkpoint and os.path.exists(_checkpoint_path(directory)):
        os.remove(_checkpoint_path(directory))
    return report
