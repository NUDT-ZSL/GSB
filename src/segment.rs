use crate::digest::{digest_bytes, digest_file};
use crate::error::{Result, StoreError};
use crate::record::{decode_record, LogRecord, LENGTH_PREFIX_LEN};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub const SEGMENT_EXTENSION: &str = "log";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentInfo {
    pub name: String,
    pub bytes: u64,
    pub digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentSetSummary {
    pub digest: String,
    pub segments: Vec<SegmentInfo>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionedRecord {
    pub record: LogRecord,
    pub offset: u64,
    pub frame_len: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TailTruncation {
    pub segment: String,
    pub offset: u64,
    pub original_bytes: u64,
    pub valid_bytes: u64,
    pub removed_bytes: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanResult {
    pub records: Vec<PositionedRecord>,
    pub valid_bytes: u64,
    pub truncation: Option<TailTruncation>,
}

pub fn ensure_data_dir(dir: &Path) -> Result<()> {
    fs::create_dir_all(dir)
        .map_err(|error| StoreError::io(format!("failed to create {}: {error}", dir.display())))
}

pub fn discover_segment_paths(dir: &Path) -> Result<Vec<PathBuf>> {
    ensure_data_dir(dir)?;
    let entries = fs::read_dir(dir)
        .map_err(|error| StoreError::io(format!("failed to read {}: {error}", dir.display())))?;
    let mut paths = Vec::new();
    for entry in entries {
        let entry = entry
            .map_err(|error| StoreError::io(format!("failed to read directory entry: {error}")))?;
        let path = entry.path();
        if path.is_file()
            && path.extension().and_then(|value| value.to_str()) == Some(SEGMENT_EXTENSION)
        {
            paths.push(path);
        }
    }
    paths.sort_by_key(|left| segment_name(left));
    Ok(paths)
}

pub fn segment_name(path: &Path) -> String {
    path.file_name()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.display().to_string())
}

pub fn summarize_segments(paths: &[PathBuf]) -> Result<SegmentSetSummary> {
    let mut segments = Vec::with_capacity(paths.len());
    for path in paths {
        let metadata = fs::metadata(path).map_err(|error| {
            StoreError::io(format!("failed to stat {}: {error}", path.display()))
        })?;
        let digest = digest_file(path).map_err(|error| {
            StoreError::io(format!(
                "failed to read {} for digest: {error}",
                path.display()
            ))
        })?;
        segments.push(SegmentInfo {
            name: segment_name(path),
            bytes: metadata.len(),
            digest,
        });
    }
    Ok(SegmentSetSummary {
        digest: digest_segment_list(&segments),
        segments,
    })
}

pub fn digest_segment_list(segments: &[SegmentInfo]) -> String {
    let mut canonical = String::new();
    for segment in segments {
        canonical.push_str(&format!(
            "{}:{}:{}\n",
            segment.name, segment.bytes, segment.digest
        ));
    }
    digest_bytes(canonical.as_bytes())
}

/// Scan from `start_offset` through a segment. Only the final segment may contain an incomplete
/// trailing frame. A complete frame with a bad checksum is always corruption, never truncation.
pub fn scan_segment(path: &Path, start_offset: u64, is_last_segment: bool) -> Result<ScanResult> {
    let mut file = File::open(path)
        .map_err(|error| StoreError::io(format!("failed to open {}: {error}", path.display())))?;
    let file_len = file
        .metadata()
        .map_err(|error| StoreError::io(format!("failed to stat {}: {error}", path.display())))?
        .len();
    if start_offset > file_len {
        return Err(StoreError::snapshot(format!(
            "snapshot offset {start_offset} is beyond {} bytes in {}",
            file_len,
            path.display()
        )));
    }
    file.seek(SeekFrom::Start(start_offset))
        .map_err(|error| StoreError::io(format!("failed to seek {}: {error}", path.display())))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|error| StoreError::io(format!("failed to read {}: {error}", path.display())))?;

    let mut cursor = start_offset;
    let mut records = Vec::new();
    let mut valid_bytes = start_offset;
    while (cursor - start_offset) < bytes.len() as u64 {
        let relative = (cursor - start_offset) as usize;
        match decode_record(&bytes[relative..], cursor) {
            Ok(decoded) => {
                let frame_len = decoded.frame_len as u64;
                records.push(PositionedRecord {
                    record: decoded.record,
                    offset: cursor,
                    frame_len,
                });
                cursor = cursor.saturating_add(frame_len);
                valid_bytes = cursor;
            }
            Err(failure) if failure.incomplete && is_last_segment => {
                return Ok(ScanResult {
                    records,
                    valid_bytes,
                    truncation: Some(TailTruncation {
                        segment: segment_name(path),
                        offset: failure.offset,
                        original_bytes: file_len,
                        valid_bytes,
                        removed_bytes: file_len.saturating_sub(valid_bytes),
                    }),
                });
            }
            Err(failure) if failure.incomplete => {
                return Err(StoreError::corrupt_at(
                    format!(
                        "incomplete record outside the final segment: {}",
                        failure.message
                    ),
                    path,
                    failure.offset,
                ))
            }
            Err(failure) => return Err(failure.into_error(path)),
        }
    }
    Ok(ScanResult {
        records,
        valid_bytes,
        truncation: None,
    })
}

pub fn truncate_segment(path: &Path, new_len: u64) -> Result<()> {
    let file = OpenOptions::new()
        .write(true)
        .open(path)
        .map_err(|error| StoreError::io(format!("failed to open {}: {error}", path.display())))?;
    file.set_len(new_len).map_err(|error| {
        StoreError::io(format!("failed to truncate {}: {error}", path.display()))
    })?;
    file.sync_all()
        .map_err(|error| StoreError::io(format!("failed to sync {}: {error}", path.display())))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriteReceipt {
    pub segment: String,
    pub offset: u64,
    pub bytes: u64,
    pub logical_timestamp: u64,
}

pub fn append_record(
    dir: &Path,
    max_segment_bytes: u64,
    record: &LogRecord,
) -> Result<WriteReceipt> {
    ensure_data_dir(dir)?;
    let frame = record.encode()?;
    let frame_len = frame.len() as u64;
    let paths = discover_segment_paths(dir)?;
    let path = if let Some(last) = paths.last() {
        let length = fs::metadata(last)
            .map_err(|error| StoreError::io(format!("failed to stat {}: {error}", last.display())))?
            .len();
        if length > 0 && length.saturating_add(frame_len) > max_segment_bytes {
            next_segment_path(dir, &paths)?
        } else {
            last.clone()
        }
    } else {
        next_segment_path(dir, &paths)?
    };
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|error| StoreError::io(format!("failed to open {}: {error}", path.display())))?;
    let offset = file
        .metadata()
        .map_err(|error| StoreError::io(format!("failed to query {}: {error}", path.display())))?;
    let offset = offset.len();
    file.write_all(&frame)
        .map_err(|error| StoreError::io(format!("failed to append {}: {error}", path.display())))?;
    file.sync_data()
        .map_err(|error| StoreError::io(format!("failed to sync {}: {error}", path.display())))?;
    Ok(WriteReceipt {
        segment: segment_name(&path),
        offset,
        bytes: frame_len,
        logical_timestamp: record.logical_timestamp,
    })
}

fn next_segment_path(dir: &Path, existing: &[PathBuf]) -> Result<PathBuf> {
    let mut max_id = 0u64;
    for path in existing {
        if let Some(stem) = path.file_stem().and_then(|value| value.to_str()) {
            if let Ok(value) = stem.parse::<u64>() {
                max_id = max_id.max(value);
            }
        }
    }
    Ok(dir.join(format!("{:06}.{}", max_id + 1, SEGMENT_EXTENSION)))
}

/// Injection helper used by the CLI: flip one byte of the checksum field in a selected record.
/// The engine source is untouched; only the on-disk input file changes.
pub fn corrupt_record_checksum(path: &Path, record_index: usize) -> Result<u64> {
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(|error| StoreError::io(format!("failed to open {}: {error}", path.display())))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|error| StoreError::io(format!("failed to read {}: {error}", path.display())))?;
    let mut offset = 0usize;
    for index in 0..=record_index {
        if bytes.len().saturating_sub(offset) < LENGTH_PREFIX_LEN {
            return Err(StoreError::input(format!(
                "record index {record_index} does not exist in {}",
                path.display()
            )));
        }
        let body_len = u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()) as usize;
        let frame_len = LENGTH_PREFIX_LEN + body_len;
        if bytes.len().saturating_sub(offset) < frame_len {
            return Err(StoreError::input(format!(
                "record index {record_index} is truncated in {}",
                path.display()
            )));
        }
        if index == record_index {
            let checksum_offset = offset + LENGTH_PREFIX_LEN + 20;
            bytes[checksum_offset] ^= 0x80;
            file.seek(SeekFrom::Start(checksum_offset as u64))
                .map_err(|error| {
                    StoreError::io(format!("failed to seek {}: {error}", path.display()))
                })?;
            file.write_all(&bytes[checksum_offset..checksum_offset + 1])
                .map_err(|error| {
                    StoreError::io(format!("failed to write {}: {error}", path.display()))
                })?;
            file.sync_all().map_err(|error| {
                StoreError::io(format!("failed to sync {}: {error}", path.display()))
            })?;
            return Ok(offset as u64);
        }
        offset += frame_len;
    }
    Err(StoreError::input("record index was not found"))
}

pub fn truncate_tail_bytes(path: &Path, bytes_to_remove: u64) -> Result<u64> {
    let length = fs::metadata(path)
        .map_err(|error| StoreError::io(format!("failed to stat {}: {error}", path.display())))?
        .len();
    if bytes_to_remove == 0 || bytes_to_remove >= length {
        return Err(StoreError::input(format!(
            "cannot remove {bytes_to_remove} bytes from a {length}-byte segment"
        )));
    }
    let new_len = length - bytes_to_remove;
    truncate_segment(path, new_len)?;
    Ok(new_len)
}
