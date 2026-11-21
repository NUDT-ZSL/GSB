"""业务错误码与结构化阶段异常。"""

from __future__ import annotations

# ---- 业务错误码 ----
E_CHUNK_MISMATCH = "E_CHUNK_MISMATCH"          # 分块路径与整块参考路径不一致
E_BAD_PROMPT_SEED = "E_BAD_PROMPT_SEED"        # prompt_seed 非法（条件嵌入阶段）
E_BAD_LATENT_SHAPE = "E_BAD_LATENT_SHAPE"      # latent_shape 非法（噪声构造阶段）
E_BLOCK_GRID_INDIVISIBLE = "E_BLOCK_GRID_INDIVISIBLE"  # block_grid 不整除潜帧
E_BAD_SAMPLER_STEPS = "E_BAD_SAMPLER_STEPS"    # sampler_steps 非法
E_INVALID_QUANT_SCALE = "E_INVALID_QUANT_SCALE"  # quant_scale 非正
E_UNKNOWN_ROUNDING_MODE = "E_UNKNOWN_ROUNDING_MODE"  # 未知舍入模式
E_UNKNOWN_DECODE_MODE = "E_UNKNOWN_DECODE_MODE"      # 未知解码模式
E_UNKNOWN_DTYPE_PROFILE = "E_UNKNOWN_DTYPE_PROFILE"  # 未知精度档
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"          # 资源上限
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"    # 快照回退尝试
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"      # 快照损坏/校验和不符
E_UNATTRIBUTED = "E_UNATTRIBUTED"              # 未被任何阶段归因的异常

# 四个可注入阶段
STAGE_COND_EMBED = "cond_embed"
STAGE_NOISE_CONSTRUCT = "noise_construct"
STAGE_CHUNK_DENOISE = "chunk_denoise"
STAGE_DECODE = "decode"


class StageError(Exception):
    """带阶段名、业务码与 task_id 的结构化异常。"""

    def __init__(self, stage, code, task_id, message, detail=None):
        super().__init__(f"[{code}] stage={stage} task={task_id}: {message}")
        self.stage = stage
        self.code = code
        self.task_id = task_id
        self.message = message
        self.detail = detail or {}

    def to_record(self):
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }
