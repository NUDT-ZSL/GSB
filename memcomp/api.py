"""可集成的领域组件接口：格式加载、双向迁移、窗口摘要、记忆合并、裁剪与失效报告。"""

from .cache import CacheStore
from .formats import load_entries
from .invalidation import invalidation_report
from .migrate import migrate
from .pipeline import run_pipeline
from .scoring import prune_to_budget, score_entries
from .selfcheck import (
    check_invalidation_report,
    check_migration_replay,
    check_three_format_views,
)
from .snapshot import SnapshotStore, StageLock
from .vocab import controlled_vocab
from .window import WindowSpec, split_windows, summarize_window


class MemoryCompressor:
    """长对话记忆压缩领域组件门面。"""

    def __init__(self, workdir, spec=None):
        self.workdir = workdir
        self.spec = spec or WindowSpec()
        self.vocab = controlled_vocab()
        self.cache = CacheStore(workdir)
        self.snapshots = SnapshotStore(workdir)

    def load(self, path):
        return load_entries(path)

    def migrate(self, entries, from_version, to_version):
        return migrate(entries, from_version, to_version)

    def split_windows(self, session):
        return split_windows(session, self.spec)

    def summarize_window(self, window):
        return summarize_window(window, self.spec, self.vocab)

    def merge_entries(self, sessions):
        return run_pipeline(sessions, self.spec, cache=self.cache, vocab=self.vocab)

    def score(self, entries, sessions):
        return score_entries(entries, sessions)

    def prune(self, entries, score_report, budget_tokens=None):
        return prune_to_budget(
            entries, score_report, budget_tokens or self.spec.budget_tokens
        )

    def invalidation_report(self, old_sessions, new_sessions, new_spec=None):
        return invalidation_report(
            old_sessions, new_sessions, self.spec, new_spec or self.spec
        )

    def self_check_views(self, entries_from_v1, entries_from_v2, entries_from_v3):
        return check_three_format_views(entries_from_v1, entries_from_v2, entries_from_v3)

    def self_check_replay(self, v1_entries, records, migrated):
        return check_migration_replay(v1_entries, records, migrated)

    def self_check_invalidation(self, report, all_window_ids, all_entry_ids):
        return check_invalidation_report(report, all_window_ids, all_entry_ids)
