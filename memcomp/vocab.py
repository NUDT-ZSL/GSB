"""受控词表：固定版本、固定顺序，摘要 token 只能取自该词表。"""

VOCAB_VERSION = "vocab-1.0.0"
VOCAB_SIZE = 64


def controlled_vocab():
    """返回受控词表（有序元组），顺序即字典序。"""
    return tuple(f"w{i:03d}" for i in range(VOCAB_SIZE))
