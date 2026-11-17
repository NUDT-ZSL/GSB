"""Content-fingerprint result cache for incremental invalidation."""

import os

from .io_utils import atomic_write_json, hash_obj, load_json


class ResultCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def key_for(self, scheme, stage, config, sample_hash):
        payload = {
            "stage": stage,
            "scheme_params": scheme.cache_params(stage, config),
            "sample_hash": sample_hash,
        }
        return hash_obj(payload)

    def _path(self, key):
        return os.path.join(self.cache_dir, key + ".json")

    def get(self, key):
        path = self._path(key)
        if not os.path.exists(path):
            return None
        return load_json(path)

    def put(self, key, value):
        atomic_write_json(self._path(key), value)
