use crate::error::{BusinessCode, Result, StoreError};
use serde::{Deserialize, Serialize};
use std::fmt;

pub const FORMAT_VERSION: u16 = 1;
pub const SUPPORTED_VERSIONS: &[u16] = &[1];
pub const LENGTH_PREFIX_LEN: usize = 4;
/// Fixed body header: version(2), operation(1), flags(1), key_len(4), value_len(4),
/// timestamp(8), checksum(4). The 4-byte frame length prefix is not part of this header.
pub const RECORD_HEADER_LEN: usize = 24;
pub const MAX_KEY_LEN: usize = 16 * 1024 * 1024;
pub const MAX_VALUE_LEN: usize = 64 * 1024 * 1024;
pub const MAX_RECORD_BODY_LEN: usize = RECORD_HEADER_LEN + MAX_KEY_LEN + MAX_VALUE_LEN;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Put,
    Delete,
    Expire,
}

impl Operation {
    pub fn code(self) -> u8 {
        match self {
            Operation::Put => 1,
            Operation::Delete => 2,
            Operation::Expire => 3,
        }
    }

    pub fn from_code(code: u8) -> Option<Self> {
        match code {
            1 => Some(Operation::Put),
            2 => Some(Operation::Delete),
            3 => Some(Operation::Expire),
            _ => None,
        }
    }
}

impl fmt::Display for Operation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Operation::Put => f.write_str("put"),
            Operation::Delete => f.write_str("delete"),
            Operation::Expire => f.write_str("expire"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LogRecord {
    pub version: u16,
    pub operation: Operation,
    pub key: String,
    pub value: String,
    pub logical_timestamp: u64,
}

impl LogRecord {
    pub fn put(key: impl Into<String>, value: impl Into<String>, logical_timestamp: u64) -> Self {
        Self {
            version: FORMAT_VERSION,
            operation: Operation::Put,
            key: key.into(),
            value: value.into(),
            logical_timestamp,
        }
    }

    pub fn delete(key: impl Into<String>, logical_timestamp: u64) -> Self {
        Self {
            version: FORMAT_VERSION,
            operation: Operation::Delete,
            key: key.into(),
            value: String::new(),
            logical_timestamp,
        }
    }

    pub fn expire(key: impl Into<String>, logical_timestamp: u64) -> Self {
        Self {
            version: FORMAT_VERSION,
            operation: Operation::Expire,
            key: key.into(),
            value: String::new(),
            logical_timestamp,
        }
    }

    pub fn frame_len(key_len: usize, value_len: usize) -> usize {
        LENGTH_PREFIX_LEN + RECORD_HEADER_LEN + key_len + value_len
    }

    pub fn validate_for_append(&self) -> Result<()> {
        validate_version(self.version)
            .map_err(|message| StoreError::new(BusinessCode::Log, message))?;
        if self.logical_timestamp == 0 {
            return Err(StoreError::input("logical timestamp must be positive"));
        }
        if self.key.len() > MAX_KEY_LEN {
            return Err(StoreError::log("key length exceeds format limit"));
        }
        if self.value.len() > MAX_VALUE_LEN {
            return Err(StoreError::log("value length exceeds format limit"));
        }
        if self.operation != Operation::Put && !self.value.is_empty() {
            return Err(StoreError::input(format!(
                "{} records must have an empty value",
                self.operation
            )));
        }
        Ok(())
    }

    /// Encode a complete frame: `u32 record_len` followed by the fixed header and payload.
    pub fn encode(&self) -> Result<Vec<u8>> {
        self.validate_for_append()?;
        let body_len = RECORD_HEADER_LEN + self.key.len() + self.value.len();
        if body_len > MAX_RECORD_BODY_LEN {
            return Err(StoreError::log("record body exceeds format limit"));
        }
        let mut out = vec![0u8; LENGTH_PREFIX_LEN + body_len];
        out[0..4].copy_from_slice(&(body_len as u32).to_le_bytes());
        let body = &mut out[LENGTH_PREFIX_LEN..];
        body[0..2].copy_from_slice(&self.version.to_le_bytes());
        body[2] = self.operation.code();
        body[3] = 0;
        body[4..8].copy_from_slice(&(self.key.len() as u32).to_le_bytes());
        body[8..12].copy_from_slice(&(self.value.len() as u32).to_le_bytes());
        body[12..20].copy_from_slice(&self.logical_timestamp.to_le_bytes());
        // body[20..24] is the checksum and remains zero while the digest is calculated.
        body[24..24 + self.key.len()].copy_from_slice(self.key.as_bytes());
        let value_start = 24 + self.key.len();
        body[value_start..value_start + self.value.len()].copy_from_slice(self.value.as_bytes());
        // The checksum field is still zero here, so the digest covers every header field,
        // the key and the value in one pass.
        let checksum = crc32(body);
        body[20..24].copy_from_slice(&checksum.to_le_bytes());
        Ok(out)
    }
}

fn validate_version(version: u16) -> std::result::Result<(), String> {
    if SUPPORTED_VERSIONS.contains(&version) {
        Ok(())
    } else {
        Err(format!("unsupported record format version {version}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodedRecord {
    pub record: LogRecord,
    pub frame_len: usize,
    pub checksum: u32,
}

#[derive(Debug, Clone)]
pub struct DecodeFailure {
    pub code: BusinessCode,
    pub offset: u64,
    pub message: String,
    /// True only when a final-segment frame is cut off by a clean end of file.
    pub incomplete: bool,
}

impl DecodeFailure {
    pub fn into_error(self, segment: impl AsRef<std::path::Path>) -> StoreError {
        StoreError::at(self.code, self.message, segment, self.offset)
    }
}

/// Strictly decode one frame from `bytes`. `base_offset` is the absolute file offset used in
/// errors. An incomplete frame is surfaced separately so the recovery layer can distinguish a
/// repairable tail truncation from corruption in the middle of the log.
pub fn decode_record(
    bytes: &[u8],
    base_offset: u64,
) -> std::result::Result<DecodedRecord, DecodeFailure> {
    if bytes.len() < LENGTH_PREFIX_LEN {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: "truncated length prefix".to_string(),
            incomplete: true,
        });
    }
    let body_len = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
    if body_len < RECORD_HEADER_LEN {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!(
                "record length {body_len} is smaller than the {RECORD_HEADER_LEN}-byte header"
            ),
            incomplete: false,
        });
    }
    if body_len > MAX_RECORD_BODY_LEN {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!("record length {body_len} exceeds format limit"),
            incomplete: false,
        });
    }
    let frame_len = LENGTH_PREFIX_LEN + body_len;
    if bytes.len() < frame_len {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!(
                "truncated record: need {frame_len} bytes, have {}",
                bytes.len()
            ),
            incomplete: true,
        });
    }
    let body = &bytes[LENGTH_PREFIX_LEN..frame_len];
    let version = u16::from_le_bytes(body[0..2].try_into().unwrap());
    if let Err(message) = validate_version(version) {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message,
            incomplete: false,
        });
    }
    let operation = match Operation::from_code(body[2]) {
        Some(operation) => operation,
        None => {
            return Err(DecodeFailure {
                code: BusinessCode::Log,
                offset: base_offset,
                message: format!("unknown operation code {}", body[2]),
                incomplete: false,
            })
        }
    };
    if body[3] != 0 {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!("reserved header flags must be zero, got {}", body[3]),
            incomplete: false,
        });
    }
    let key_len = u32::from_le_bytes(body[4..8].try_into().unwrap()) as usize;
    let value_len = u32::from_le_bytes(body[8..12].try_into().unwrap()) as usize;
    if key_len > MAX_KEY_LEN || value_len > MAX_VALUE_LEN {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: "key or value length exceeds format limit".to_string(),
            incomplete: false,
        });
    }
    let expected_body_len = RECORD_HEADER_LEN
        .checked_add(key_len)
        .and_then(|value| value.checked_add(value_len));
    if expected_body_len != Some(body_len) {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!(
                "length fields do not add up: header key_len={key_len} value_len={value_len} body_len={body_len}"
            ),
            incomplete: false,
        });
    }
    let timestamp = u64::from_le_bytes(body[12..20].try_into().unwrap());
    if timestamp == 0 {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: "logical timestamp must be positive".to_string(),
            incomplete: false,
        });
    }
    let stored_checksum = u32::from_le_bytes(body[20..24].try_into().unwrap());
    let mut checksum_input = body.to_vec();
    checksum_input[20..24].fill(0);
    let computed_checksum = crc32(&checksum_input);
    if stored_checksum != computed_checksum {
        return Err(DecodeFailure {
            code: BusinessCode::Corrupt,
            offset: base_offset,
            message: format!(
                "checksum mismatch: stored={stored_checksum:08x} computed={computed_checksum:08x}"
            ),
            incomplete: false,
        });
    }
    let key_start = LENGTH_PREFIX_LEN + RECORD_HEADER_LEN;
    let key_end = key_start + key_len;
    let value_end = key_end + value_len;
    let key = match std::str::from_utf8(&bytes[key_start..key_end]) {
        Ok(value) => value.to_string(),
        Err(_) => {
            return Err(DecodeFailure {
                code: BusinessCode::Log,
                offset: base_offset,
                message: "key is not valid UTF-8".to_string(),
                incomplete: false,
            })
        }
    };
    let value = match std::str::from_utf8(&bytes[key_end..value_end]) {
        Ok(value) => value.to_string(),
        Err(_) => {
            return Err(DecodeFailure {
                code: BusinessCode::Log,
                offset: base_offset,
                message: "value is not valid UTF-8".to_string(),
                incomplete: false,
            })
        }
    };
    if operation != Operation::Put && !value.is_empty() {
        return Err(DecodeFailure {
            code: BusinessCode::Log,
            offset: base_offset,
            message: format!("{operation} record contains a non-empty value"),
            incomplete: false,
        });
    }
    Ok(DecodedRecord {
        record: LogRecord {
            version,
            operation,
            key,
            value,
            logical_timestamp: timestamp,
        },
        frame_len,
        checksum: stored_checksum,
    })
}

/// CRC-32/IEEE, implemented locally so the on-disk format has no platform or crate dependency.
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in bytes {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_known_vector() {
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
    }

    #[test]
    fn record_round_trip() {
        let record = LogRecord::put("user:cn:alice", "Alice", 7);
        let frame = record.encode().unwrap();
        let decoded = decode_record(&frame, 100).unwrap();
        assert_eq!(decoded.record, record);
        assert_eq!(decoded.frame_len, frame.len());
    }
}
