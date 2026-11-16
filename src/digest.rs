use std::fs;
use std::io;
use std::path::Path;

const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x100000001b3;

/// Small, dependency-free 64-bit FNV-1a digest. It is stable across platforms and Rust
/// releases, which is important for deterministic snapshots and reports.
pub fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = FNV_OFFSET;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

pub fn digest_bytes(bytes: &[u8]) -> String {
    format!("{:016x}", fnv1a64(bytes))
}

pub fn digest_str(value: &str) -> String {
    digest_bytes(value.as_bytes())
}

pub fn digest_file(path: &Path) -> io::Result<String> {
    Ok(digest_bytes(&fs::read(path)?))
}

pub fn digest_serialized<T: serde::Serialize>(value: &T) -> Result<String, serde_json::Error> {
    Ok(digest_bytes(&serde_json::to_vec(value)?))
}
