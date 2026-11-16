"""轻量级中文分词与词性标注流水线（纯本地、确定性、对数域数值稳定）。"""

__version__ = "1.0.0"

STAGE_SEGMENT = "segmentation"
STAGE_HMM = "hmm_oov"
STAGE_POS = "pos_tagging"
STAGES = (STAGE_SEGMENT, STAGE_HMM, STAGE_POS)

STAGE_VERSIONS = {
    STAGE_SEGMENT: 1,
    STAGE_HMM: 1,
    STAGE_POS: 1,
}
