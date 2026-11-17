"""会话配置与快照格式版本。"""

CONFIG_VERSION = 1
SNAPSHOT_VERSION = 1

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "mss": 1000,
    "init_cwnd_mss": 1,
    "init_ssthresh": 65535,
    "granularity_ms": 1,
    "min_rto_ms": 100,
    "max_rto_ms": 60000,
    "initial_rto_ms": 500,
    "dup_ack_threshold": 3,
}
