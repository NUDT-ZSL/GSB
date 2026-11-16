use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_DIR: AtomicU64 = AtomicU64::new(1);

fn temp_dir(label: &str) -> PathBuf {
    let id = NEXT_DIR.fetch_add(1, Ordering::SeqCst);
    let path =
        std::env::temp_dir().join(format!("logstore-cli-{}-{label}-{id}", std::process::id()));
    if path.exists() {
        fs::remove_dir_all(&path).unwrap();
    }
    fs::create_dir_all(&path).unwrap();
    path
}

fn copy_dir(source: &Path, target: &Path) {
    fs::create_dir_all(target).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let destination = target.join(entry.file_name());
        if entry.path().is_dir() {
            copy_dir(&entry.path(), &destination);
        } else {
            fs::copy(entry.path(), destination).unwrap();
        }
    }
}

fn sample_path(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("samples/basic")
        .join(name)
}

fn run_logstore(data_dir: &Path, extra: &[&str]) -> Output {
    let binary = env!("CARGO_BIN_EXE_logstore");
    let mut command = Command::new(binary);
    command.arg("--data-dir").arg(data_dir);
    command.args(extra);
    command.output().unwrap()
}

#[test]
fn cli_recovers_incrementally_and_reports_truncation_and_business_codes() {
    let root = temp_dir("success-and-truncation");
    let data_dir = root.join("data");
    copy_dir(&sample_path("data"), &data_dir);
    let session = sample_path("session.json");
    let output = run_logstore(
        &data_dir,
        &["--session", session.to_str().unwrap(), "--auto-recover"],
    );
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("replayed_records=1"));
    assert!(stdout.contains("business_code=OK"));
    assert!(stdout.contains("view=order_count kind=prefix_count version=2 valid=false"));

    let truncated = temp_dir("cli-truncation");
    let truncated_data = truncated.join("data");
    copy_dir(&sample_path("data"), &truncated_data);
    let output = run_logstore(
        &truncated_data,
        &[
            "--session",
            session.to_str().unwrap(),
            "--auto-recover",
            "--inject-tail-truncate",
            "000002.log:8",
        ],
    );
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("business_code=E_LOG"));
    assert!(stdout.contains("truncation.removed_bytes=36"));
    let diagnostics = fs::read_to_string(truncated_data.join("diagnostics.jsonl")).unwrap();
    assert!(diagnostics.contains("\"business_code\":\"E_LOG\""));
}

#[test]
fn cli_returns_stable_nonzero_codes_for_corruption_cycle_and_missing_segment() {
    let root = temp_dir("cli-errors");

    let corrupt_data = root.join("corrupt/data");
    copy_dir(&sample_path("data"), &corrupt_data);
    let output = run_logstore(
        &corrupt_data,
        &[
            "--inject-corrupt-checksum",
            "000001.log:1",
            "--auto-recover",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("business_code=E_CORRUPT"));
    assert!(stdout.contains("offset=46"));

    let cycle_data = root.join("cycle/data");
    fs::create_dir_all(&cycle_data).unwrap();
    let output = run_logstore(
        &cycle_data,
        &[
            "--graph",
            sample_path("graph.json").to_str().unwrap(),
            "--inject-dependency-cycle",
            "--auto-recover",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("business_code=E_DEPEND"));

    let missing_data = root.join("missing/data");
    copy_dir(&sample_path("data"), &missing_data);
    let output = run_logstore(
        &missing_data,
        &[
            "--session",
            sample_path("session.json").to_str().unwrap(),
            "--inject-missing-snapshot-segment",
            "000001.log",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("business_code=E_SNAPSHOT"));
}
