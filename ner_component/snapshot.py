"""Versioned snapshot store with atomic whole-directory replacement."""

import json
import os
import shutil


class SnapshotStore:
    def __init__(self, root):
        self.root = root

    @property
    def manifest_path(self):
        return os.path.join(self.root, "manifest.json")

    def clear(self):
        for path in (self.root, self.root + ".tmp", self.root + ".old"):
            if os.path.isdir(path):
                shutil.rmtree(path)

    def load(self):
        if not os.path.isfile(self.manifest_path):
            return None
        with open(self.manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        results = {}
        results_dir = os.path.join(self.root, "results")
        if os.path.isdir(results_dir):
            for name in sorted(os.listdir(results_dir)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(results_dir, name), "r",
                          encoding="utf-8") as fh:
                    item = json.load(fh)
                results[item["sent_id"]] = item
        return {"manifest": manifest, "results": results}

    def write(self, manifest, results):
        tmp = self.root + ".tmp"
        old = self.root + ".old"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.makedirs(os.path.join(tmp, "results"))
        for sid in sorted(results):
            with open(os.path.join(tmp, "results", sid + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump(results[sid], fh, ensure_ascii=False, sort_keys=True)
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, indent=2)
        if os.path.isdir(old):
            shutil.rmtree(old)
        if os.path.isdir(self.root):
            os.rename(self.root, old)
        os.rename(tmp, self.root)
        if os.path.isdir(old):
            shutil.rmtree(old)
