"""状态目录、输入快照识别、中断标记与强制终止恢复。

布局：
  <state_dir>/<snapshot_id>/segments/<device>__<segment>.json  段摘要
  <state_dir>/<snapshot_id>/interrupt.flag                     中断标记
  <state_dir>/<snapshot_id>/manifest.json                      段清单
仅当快照标识一致且段摘要通过 sha256 校验时才复用，否则整体重做。
"""

import hashlib
import json
import os

from .errors import Interrupted


def file_sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def compute_snapshot_id(session):
    """快照标识覆盖会话内容与全部段 WAV 字节，输入变化即变化。"""
    input_only = {
        key: value for key, value in session.raw.items()
        if key not in ("state_dir", "output_wav", "output_report",
                       "concurrency", "memory_budget_bytes", "snapshot_id")
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(input_only, sort_keys=True,
                             ensure_ascii=False).encode("utf-8"))
    for _, seg in session.iter_segments():
        digest.update(seg.segment_id.encode("utf-8"))
        digest.update(seg.file.encode("utf-8"))
        digest.update(file_sha256(session.resolve(seg.file)).encode("ascii"))
    return digest.hexdigest()


class StateStore:
    def __init__(self, state_dir, snapshot_id):
        self.root = os.path.join(state_dir, snapshot_id)
        self.seg_dir = os.path.join(self.root, "segments")
        self.interrupt_path = os.path.join(self.root, "interrupt.flag")
        self.manifest_path = os.path.join(self.root, "manifest.json")

    def prepare(self):
        os.makedirs(self.seg_dir, exist_ok=True)

    def reset(self):
        if os.path.isdir(self.root):
            if os.path.isdir(self.seg_dir):
                for name in os.listdir(self.seg_dir):
                    os.remove(os.path.join(self.seg_dir, name))
            if os.path.isfile(self.manifest_path):
                os.remove(self.manifest_path)
        os.makedirs(self.seg_dir, exist_ok=True)
        if os.path.exists(self.interrupt_path):
            os.remove(self.interrupt_path)

    @staticmethod
    def _seg_name(seg):
        return "%s__%s.json" % (seg.device_id, seg.segment_id)

    def save_segment(self, seg, payload):
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        record = {"sha256": digest, "payload": payload}
        path = os.path.join(self.seg_dir, self._seg_name(seg))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)

    def load_segment(self, seg):
        path = os.path.join(self.seg_dir, self._seg_name(seg))
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            body = json.dumps(record["payload"], sort_keys=True,
                              ensure_ascii=False).encode("utf-8")
            if hashlib.sha256(body).hexdigest() != record["sha256"]:
                return None
            return record["payload"]
        except (OSError, KeyError, ValueError, TypeError):
            return None

    def write_manifest(self, segment_refs):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"segments": segment_refs}, fh, sort_keys=True,
                      ensure_ascii=False)
        os.replace(tmp, self.manifest_path)

    def mark_interrupted(self, boundary_kind, boundary_index):
        tmp = self.interrupt_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"boundary": boundary_kind,
                       "boundary_index": boundary_index}, fh, sort_keys=True)
        os.replace(tmp, self.interrupt_path)

    def interrupted(self):
        return os.path.isfile(self.interrupt_path)


class BoundarySignaler:
    """集中管理终止信号；只允许在段/时间片边界生效。"""

    def __init__(self):
        self.requested = False

    def request(self, *_args):
        self.requested = True

    def check(self, store, boundary_kind, boundary_index):
        if self.requested:
            store.mark_interrupted(boundary_kind, boundary_index)
            raise Interrupted(
                "收到终止信号，已在 %s 边界 #%d 安全中断"
                % (boundary_kind, boundary_index),
                detail={"boundary": boundary_kind, "boundary_index": boundary_index})

