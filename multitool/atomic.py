"""Atomic, version-aware output replacement.

Both the WAV and the report are first written to unique temp files in their
final directory.  They are only swapped into place together; a failure rolls
the previous files back so a half-written pair can never become visible.
"""

import os
import tempfile

from .errors import E_WRITE


class WriteError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.code = E_WRITE
        self.reason = reason


def temp_path_in(directory, prefix):
    os.makedirs(directory, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    os.close(handle)
    return path


def commit_pair(wav_path, wav_temp, report_path, report_temp, version_match):
    """Replace both outputs together.

    ``version_match`` indicates the existing WAV already carries the same
    frame count and sample rate as the new render, in which case nothing is
    replaced.  Returns one of ``"replaced"`` / ``"kept"``.
    """
    if version_match and os.path.isfile(wav_path) and os.path.isfile(report_path):
        os.unlink(wav_temp)
        os.unlink(report_temp)
        return "kept"

    backups = {}
    created = []
    try:
        for final_path, temp in ((wav_path, wav_temp), (report_path, report_temp)):
            if os.path.isfile(final_path):
                backup = final_path + ".bak"
                os.replace(final_path, backup)
                backups[final_path] = backup
            else:
                created.append(final_path)
            os.replace(temp, final_path)
        for backup in backups.values():
            os.unlink(backup)
        return "replaced"
    except OSError as exc:
        # Roll back: restore every backup and remove partially created files.
        for final_path, backup in backups.items():
            try:
                if os.path.isfile(final_path):
                    os.unlink(final_path)
                os.replace(backup, final_path)
            except OSError:
                pass
        for final_path in created:
            try:
                if os.path.isfile(final_path):
                    os.unlink(final_path)
            except OSError:
                pass
        raise WriteError(f"atomic replace failed: {exc}") from exc
