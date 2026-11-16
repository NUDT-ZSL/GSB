use serde::{Deserialize, Serialize};
use std::fmt;
use std::path::{Path, PathBuf};

/// Stable business codes used by the library, CLI reports and JSONL diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum BusinessCode {
    #[serde(rename = "OK")]
    Ok,
    #[serde(rename = "E_LOG")]
    Log,
    #[serde(rename = "E_CORRUPT")]
    Corrupt,
    #[serde(rename = "E_SNAPSHOT")]
    Snapshot,
    #[serde(rename = "E_DEPEND")]
    Depend,
    #[serde(rename = "E_VIEW")]
    View,
    #[serde(rename = "E_IO")]
    Io,
    #[serde(rename = "E_INPUT")]
    Input,
}

impl BusinessCode {
    pub fn as_str(self) -> &'static str {
        match self {
            BusinessCode::Ok => "OK",
            BusinessCode::Log => "E_LOG",
            BusinessCode::Corrupt => "E_CORRUPT",
            BusinessCode::Snapshot => "E_SNAPSHOT",
            BusinessCode::Depend => "E_DEPEND",
            BusinessCode::View => "E_VIEW",
            BusinessCode::Io => "E_IO",
            BusinessCode::Input => "E_INPUT",
        }
    }
}

impl fmt::Display for BusinessCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A deterministic, structured engine error. `segment` and `offset` are populated whenever
/// the failure can be tied to a concrete byte location on disk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoreError {
    pub code: BusinessCode,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offset: Option<u64>,
}

impl StoreError {
    pub fn new(code: BusinessCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            segment: None,
            offset: None,
        }
    }

    pub fn at(
        code: BusinessCode,
        message: impl Into<String>,
        segment: impl AsRef<Path>,
        offset: u64,
    ) -> Self {
        Self {
            code,
            message: message.into(),
            segment: Some(segment.as_ref().to_path_buf()),
            offset: Some(offset),
        }
    }

    pub fn log(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Log, message)
    }

    pub fn log_at(message: impl Into<String>, segment: impl AsRef<Path>, offset: u64) -> Self {
        Self::at(BusinessCode::Log, message, segment, offset)
    }

    pub fn corrupt(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Corrupt, message)
    }

    pub fn corrupt_at(message: impl Into<String>, segment: impl AsRef<Path>, offset: u64) -> Self {
        Self::at(BusinessCode::Corrupt, message, segment, offset)
    }

    pub fn snapshot(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Snapshot, message)
    }

    pub fn depend(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Depend, message)
    }

    pub fn view(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::View, message)
    }

    pub fn input(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Input, message)
    }

    pub fn io(message: impl Into<String>) -> Self {
        Self::new(BusinessCode::Io, message)
    }

    pub fn with_segment(mut self, segment: impl AsRef<Path>) -> Self {
        self.segment = Some(segment.as_ref().to_path_buf());
        self
    }
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "business_code={} message={}", self.code, self.message)?;
        if let Some(segment) = &self.segment {
            write!(f, " segment={}", segment.display())?;
        }
        if let Some(offset) = self.offset {
            write!(f, " offset={offset}")?;
        }
        Ok(())
    }
}

impl std::error::Error for StoreError {}

pub type Result<T> = std::result::Result<T, StoreError>;
