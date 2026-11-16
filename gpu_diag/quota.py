"""配额组准入台账：卡与显存两个维度分别核算已占用与在途申请。"""

from .models import RejectReason
from .errors import E_QUOTA, code_name


class QuotaLedger:
    """按配额组维护已占用量，准入检查同时校验卡总量与显存总量。"""

    def __init__(self, quota_limits):
        self._limits = quota_limits
        self.used_cards = {group_id: 0 for group_id in sorted(quota_limits)}
        self.used_memory = {group_id: 0 for group_id in sorted(quota_limits)}

    @property
    def group_ids(self):
        return sorted(self._limits)

    def snapshot_usage(self):
        return {
            group_id: {
                "used_cards": self.used_cards[group_id],
                "max_cards": self._limits[group_id].max_cards,
                "used_memory_mib": self.used_memory[group_id],
                "max_memory_mib": self._limits[group_id].max_memory_mib,
            }
            for group_id in self.group_ids
        }

    def check(self, job, estimate):
        """返回拒绝原因列表；为空表示两个维度均满足准入。"""
        reasons = []
        limit = self._limits[job.quota_group]
        current_cards = self.used_cards[job.quota_group]
        current_memory = self.used_memory[job.quota_group]
        if current_cards + estimate.card_count > limit.max_cards:
            reasons.append(RejectReason(
                code=E_QUOTA,
                code_name=code_name(E_QUOTA),
                dimension="cards",
                message="quota group {} card quota exhausted".format(job.quota_group),
                current_usage=current_cards,
                requested=estimate.card_count,
                limit=limit.max_cards,
                field_path="quotas.{}.max_cards".format(job.quota_group),
            ))
        if current_memory + estimate.total_mib > limit.max_memory_mib:
            reasons.append(RejectReason(
                code=E_QUOTA,
                code_name=code_name(E_QUOTA),
                dimension="memory_mib",
                message="quota group {} memory quota exhausted".format(job.quota_group),
                current_usage=current_memory,
                requested=estimate.total_mib,
                limit=limit.max_memory_mib,
                field_path="quotas.{}.max_memory_mib".format(job.quota_group),
            ))
        return reasons

    def commit(self, job, estimate):
        self.used_cards[job.quota_group] += estimate.card_count
        self.used_memory[job.quota_group] += estimate.total_mib
