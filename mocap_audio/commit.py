"""音频与报告的一致性提交。

流程：
1. 音频、报告分别写各自临时文件；
2. 读取已有音频的帧数/采样率与已有报告的快照版本，
   仅当版本不同才执行替换，版本相同直接保留旧文件；
3. 两个替换打包为提交序列，任一步失败则用备份回滚，
   保证不会出现只更新其一的中间状态；失败统一返回 E_WR。
"""

import json
import os

from .errors import WriteCommitError
from .wavio import read_header, write_wav


def _existing_version(report_path, wav_path):
    version = {"snapshot_id": None, "frames": None, "sample_rate": None}
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as fh:
                version["snapshot_id"] = json.load(fh).get("snapshot_id")
        except (OSError, ValueError):
            version["snapshot_id"] = None
    if os.path.isfile(wav_path):
        try:
            header = read_header(wav_path)
            version["frames"] = header.frames
            version["sample_rate"] = header.sample_rate
        except Exception:
            pass
    return version


def _backup(path):
    if not os.path.isfile(path):
        return None
    backup = path + ".oldbak"
    os.replace(path, backup)
    return backup


def _restore(backup, path):
    if backup is not None and os.path.isfile(backup):
        os.replace(backup, path)


def commit_outputs(wav_path, report_path, samples, sample_rate, bits,
                   report_text, snapshot_id):
    existing = _existing_version(report_path, wav_path)
    if existing["snapshot_id"] == snapshot_id and os.path.isfile(wav_path) \
            and os.path.isfile(report_path):
        header = read_header(wav_path)
        return {"replaced": False, "reason": "version_unchanged",
                "frames": header.frames, "sample_rate": header.sample_rate}

    wav_tmp = wav_path + ".tmp.wav"
    report_tmp = report_path + ".tmp.json"
    try:
        info = write_wav(wav_tmp, samples, sample_rate, bits)
        with open(report_tmp, "w", encoding="utf-8") as fh:
            fh.write(report_text)
    except WriteCommitError:
        _safe_unlink(wav_tmp)
        _safe_unlink(report_tmp)
        raise
    except OSError as exc:
        _safe_unlink(wav_tmp)
        _safe_unlink(report_tmp)
        raise WriteCommitError("报告临时文件写入失败", refs=[report_path],
                               detail={"reason": str(exc)})

    wav_backup = None
    report_backup = None
    replaced_wav = False
    try:
        wav_backup = _backup(wav_path)
        os.replace(wav_tmp, wav_path)
        replaced_wav = True
        report_backup = _backup(report_path)
        os.replace(report_tmp, report_path)
    except OSError as exc:
        if replaced_wav:
            _safe_unlink(wav_path)
            _restore(wav_backup, wav_path)
        else:
            _restore(wav_backup, wav_path)
        _restore(report_backup, report_path)
        _safe_unlink(wav_tmp)
        _safe_unlink(report_tmp)
        raise WriteCommitError("双文件一致性提交失败，已回滚旧版本",
                               refs=[wav_path, report_path],
                               detail={"reason": str(exc)})
    _safe_unlink(wav_backup)
    _safe_unlink(report_backup)
    return {"replaced": True, "reason": "version_changed",
            "frames": info.frames, "sample_rate": info.sample_rate}


def _safe_unlink(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass

