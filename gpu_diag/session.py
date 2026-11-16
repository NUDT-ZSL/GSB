"""调度会话：状态维护、参数版本管理、快照持久化与整体替换恢复。"""

import datetime as _dt
import json
import os
import re

from .errors import BusinessError, E_SNAPSHOT, E_REPLAY
from .manifest import parse_manifest
from .models import ScaleParams
from .scheduler import run_schedule, compare_runs

SNAPSHOT_FORMAT_VERSION = 1
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Session:
    """保存一次调度会话的全部可变状态，所有修改后立即重新完整调度。"""

    def __init__(self, manifest):
        self.pools = {pool.name: pool for pool in manifest.pools}
        self.quotas = dict(manifest.quotas)
        self.jobs = list(manifest.jobs)
        self.scale_bounds = dict(manifest.scale_bounds)
        self.scale_params = manifest.scale_params
        self.parameter_version = 1
        self.session_revision = 1
        self.current_run = None
        self.stale_runs = []
        self.history = []
        self.snapshot_dir = os.path.join(os.getcwd(), "snapshots")
        self.reschedule("init")

    def _bounds_dict(self):
        return {name: list(value) for name, value in sorted(self.scale_bounds.items())}

    def reschedule(self, reason):
        previous = self.current_run
        run = run_schedule(
            jobs=self.jobs,
            pools_by_name=self.pools,
            quotas=self.quotas,
            scale_params=self.scale_params,
            parameter_version=self.parameter_version,
            drained_by_pool={name: pool.drained_cards for name, pool in self.pools.items()},
            scale_bounds=self.scale_bounds,
        )
        if previous is not None:
            self.history.append({
                "reason": reason,
                "stale": reason == "scale_change",
                "parameter_version": previous.parameter_version,
                "run": previous.to_dict(),
            })
        self.current_run = run
        self.session_revision += 1
        return run

    def update_scale(self, batch_size=None, seq_len=None, model_dim=None):
        """修改数据规模参数：参数版本号 +1，旧结论归档为陈旧并整体重调度。"""
        current = self.scale_params.to_dict()
        candidate = ScaleParams(
            current["batch_size"] if batch_size is None else batch_size,
            current["seq_len"] if seq_len is None else seq_len,
            current["model_dim"] if model_dim is None else model_dim,
        )
        from .estimator import validate_scale
        validate_scale(candidate, self.scale_bounds)
        self.scale_params = candidate
        self.parameter_version += 1
        self.stale_runs.append(self.current_run.to_dict())
        return self.reschedule("scale_change")

    def update_quota(self, group_id, max_cards=None, max_memory_mib=None):
        if group_id not in self.quotas:
            raise BusinessError(E_SNAPSHOT, "unknown quota group: {}".format(group_id),
                                field_path="quotas.{}".format(group_id))
        quota = self.quotas[group_id]
        if max_cards is not None:
            quota.max_cards = max_cards
        if max_memory_mib is not None:
            quota.max_memory_mib = max_memory_mib
        return self.reschedule("quota_change")

    def set_drained(self, pool_name, card_index, drained):
        pool = self.pools.get(pool_name)
        if pool is None:
            raise BusinessError(E_SNAPSHOT, "unknown pool: {}".format(pool_name),
                                field_path="pools.{}".format(pool_name))
        if not 0 <= card_index < pool.card_count:
            raise BusinessError(E_SNAPSHOT,
                                "card {} outside pool {}".format(card_index, pool_name),
                                field_path="pools.{}.drained_cards".format(pool_name))
        drained_set = set(pool.drained_cards)
        if drained:
            drained_set.add(card_index)
        else:
            drained_set.discard(card_index)
        pool.drained_cards = tuple(sorted(drained_set))
        return self.reschedule("pool_change")

    def summary(self):
        run = self.current_run
        admitted = [result for result in run.results if result.admitted]
        rejected = [result for result in run.results if not result.admitted]
        return {
            "parameter_version": self.parameter_version,
            "session_revision": self.session_revision,
            "scale_params": self.scale_params.to_dict(),
            "admitted_jobs": sorted(result.job_id for result in admitted),
            "rejected_jobs": sorted(result.job_id for result in rejected),
            "allocations": [
                {
                    "job_id": result.job_id,
                    "pool_name": result.pool_name,
                    "slots": list(result.slots),
                    "topology_group": result.topology_group,
                    "total_mib": result.estimate.total_mib,
                }
                for result in admitted
            ],
            "rejection_codes": sorted({result.code_name for result in rejected}),
            "quota_usage": run.quota_usage,
        }

    def to_snapshot_dict(self, snapshot_name):
        return {
            "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
            "snapshot_name": snapshot_name,
            "created_at": _now(),
            "parameter_version": self.parameter_version,
            "session_revision": self.session_revision,
            "scale_bounds": self._bounds_dict(),
            "scale_params": self.scale_params.to_dict(),
            "pools": [pool.to_dict() for pool in sorted(self.pools.values(), key=lambda item: item.name)],
            "quotas": [quota.to_dict() for quota in sorted(self.quotas.values(), key=lambda item: item.group_id)],
            "jobs": [job.to_dict() for job in sorted(self.jobs, key=lambda item: item.manifest_index)],
            "current_run": self.current_run.to_dict(),
            "stale_runs": list(self.stale_runs),
            "history": list(self.history),
        }

    def save_snapshot(self, snapshot_name, snapshot_dir=None):
        if not _NAME_RE.match(snapshot_name or ""):
            raise BusinessError(E_SNAPSHOT,
                                "snapshot name must match [A-Za-z0-9_.-]+",
                                field_path="snapshot_name")
        snapshot_dir = snapshot_dir or self.snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)
        path = os.path.join(snapshot_dir, "snapshot_{}.json".format(snapshot_name))
        payload = self.to_snapshot_dict(snapshot_name)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
        return path

    @staticmethod
    def load_snapshot(path):
        """从快照整体恢复：版本不一致时同样整体替换，绝不增量合并。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            raise BusinessError(E_SNAPSHOT, "snapshot file not found", field_path=path)
        except json.JSONDecodeError as exc:
            raise BusinessError(E_SNAPSHOT, "invalid snapshot JSON: {}".format(exc.msg),
                                field_path=path)
        if not isinstance(payload, dict) or payload.get("snapshot_format_version") != SNAPSHOT_FORMAT_VERSION:
            raise BusinessError(E_SNAPSHOT,
                                "unsupported snapshot format version",
                                field_path="snapshot_format_version")
        manifest_raw = {
            "scale_bounds": payload.get("scale_bounds"),
            "scale_params": payload.get("scale_params"),
            "pools": payload.get("pools"),
            "quotas": payload.get("quotas"),
            "jobs": payload.get("jobs"),
        }
        manifest = parse_manifest(manifest_raw)
        session = Session.__new__(Session)
        session.pools = {pool.name: pool for pool in manifest.pools}
        session.quotas = dict(manifest.quotas)
        session.jobs = list(manifest.jobs)
        session.scale_bounds = dict(manifest.scale_bounds)
        session.scale_params = manifest.scale_params
        session.parameter_version = int(payload["parameter_version"])
        session.session_revision = int(payload["session_revision"])
        session.stale_runs = list(payload.get("stale_runs", []))
        session.history = list(payload.get("history", []))
        session.snapshot_dir = os.path.dirname(os.path.abspath(path))
        session.current_run = run_schedule(
            jobs=session.jobs,
            pools_by_name=session.pools,
            quotas=session.quotas,
            scale_params=session.scale_params,
            parameter_version=session.parameter_version,
            drained_by_pool={name: pool.drained_cards for name, pool in session.pools.items()},
            scale_bounds=session.scale_bounds,
        )
        stored_run = payload["current_run"]
        version_match = stored_run.get("parameter_version") == session.parameter_version
        return session, {
            "path": os.path.abspath(path),
            "snapshot_name": payload.get("snapshot_name"),
            "snapshot_parameter_version": stored_run.get("parameter_version"),
            "loaded_parameter_version": session.parameter_version,
            "version_match": version_match,
            "restore_mode": "full_replace",
        }

    @staticmethod
    def replay_snapshot(path, quota_overrides=None, scale_overrides=None):
        """按快照记录的参数版本重放调度并逐项比对，首个分歧以 E_REPLAY 报告。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            raise BusinessError(E_REPLAY, "snapshot file not found", field_path=path)
        except json.JSONDecodeError as exc:
            raise BusinessError(E_REPLAY, "invalid snapshot JSON: {}".format(exc.msg),
                                field_path=path)

        recorded_manifest = parse_manifest({
            "scale_bounds": payload.get("scale_bounds"),
            "scale_params": payload.get("scale_params"),
            "pools": payload.get("pools"),
            "quotas": payload.get("quotas"),
            "jobs": payload.get("jobs"),
        })
        replay_raw = {
            "scale_bounds": payload.get("scale_bounds"),
            "scale_params": dict(payload.get("scale_params")),
            "pools": payload.get("pools"),
            "quotas": [dict(quota) for quota in payload.get("quotas")],
            "jobs": payload.get("jobs"),
        }
        if quota_overrides:
            for quota in replay_raw["quotas"]:
                override = quota_overrides.get(quota["group_id"])
                if override:
                    quota.update(override)
        recorded_version = payload["current_run"]["parameter_version"]
        replay_version = recorded_version
        if scale_overrides:
            replay_raw["scale_params"].update(scale_overrides)
            replay_version = recorded_version + 1
        manifest = parse_manifest(replay_raw)
        pools = {pool.name: pool for pool in manifest.pools}
        drained = {pool.name: pool.drained_cards for pool in manifest.pools}

        replay_run = run_schedule(
            jobs=manifest.jobs,
            pools_by_name=pools,
            quotas=manifest.quotas,
            scale_params=manifest.scale_params,
            parameter_version=replay_version,
            drained_by_pool=drained,
            scale_bounds=manifest.scale_bounds,
        )
        recorded_run = run_schedule(
            jobs=recorded_manifest.jobs,
            pools_by_name={pool.name: pool for pool in recorded_manifest.pools},
            quotas=recorded_manifest.quotas,
            scale_params=recorded_manifest.scale_params,
            parameter_version=recorded_version,
            drained_by_pool={pool.name: pool.drained_cards for pool in recorded_manifest.pools},
            scale_bounds=recorded_manifest.scale_bounds,
        )
        divergence = compare_runs(recorded_run, replay_run, ordered_jobs=manifest.jobs)
        if replay_version != recorded_version and divergence is None:
            divergence = {
                "job_id": "*",
                "field": "parameter_version",
                "expected": recorded_version,
                "actual": replay_version,
            }
        if divergence is not None:
            raise BusinessError(
                E_REPLAY,
                "snapshot replay not reproducible, first divergence at job {}".format(
                    divergence["job_id"]),
                field_path="current_run.results.{}".format(divergence["job_id"]),
                details={"first_divergence": divergence,
                         "recorded_parameter_version": recorded_version,
                         "replay_parameter_version": replay_version},
            )
        return {
            "code": 0,
            "code_name": "OK",
            "snapshot": os.path.abspath(path),
            "parameter_version": recorded_version,
            "compared_jobs": len(recorded_run.results),
            "message": "replay reproduced every admission decision, slot set and reason chain",
        }


def _rebuild_quotas(payload):
    from .models import QuotaLimit
    return [QuotaLimit(item["group_id"], item["max_cards"], item["max_memory_mib"])
            for item in payload["quotas"]]
