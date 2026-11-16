use log_structured_store::graph::{
    Dependency, DependencyGraph, GraphDocument, ViewDefinition, ViewKind,
};
use log_structured_store::record::{decode_record, LogRecord, Operation};
use log_structured_store::segment::{append_record, corrupt_record_checksum, truncate_tail_bytes};
use log_structured_store::snapshot::SessionSnapshot;
use log_structured_store::{BusinessCode, GraphLoadOptions, Store, StoreConfig};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_DIR: AtomicU64 = AtomicU64::new(1);

fn temp_dir(label: &str) -> PathBuf {
    let id = NEXT_DIR.fetch_add(1, Ordering::SeqCst);
    let path =
        std::env::temp_dir().join(format!("logstore-it-{}-{label}-{id}", std::process::id()));
    if path.exists() {
        fs::remove_dir_all(&path).unwrap();
    }
    fs::create_dir_all(&path).unwrap();
    path
}

fn sample_graph() -> GraphDocument {
    GraphDocument {
        graph_version: 7,
        views: vec![
            ViewDefinition {
                id: "order_count".into(),
                kind: ViewKind::PrefixCount {
                    prefix: "order:".into(),
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "order:".into(),
                }],
            },
            ViewDefinition {
                id: "total_count".into(),
                kind: ViewKind::PrefixCount {
                    prefix: String::new(),
                },
                dependencies: vec![
                    Dependency::View {
                        view: "order_count".into(),
                    },
                    Dependency::View {
                        view: "user_count".into(),
                    },
                ],
            },
            ViewDefinition {
                id: "user_count".into(),
                kind: ViewKind::PrefixCount {
                    prefix: "user:".into(),
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "user:".into(),
                }],
            },
            ViewDefinition {
                id: "user_country_index".into(),
                kind: ViewKind::SecondaryIndex {
                    index: "by_country".into(),
                    prefix: "user:".into(),
                    delimiter: ":".into(),
                    field: 1,
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "user:".into(),
                }],
            },
        ],
    }
}

fn make_incremental_store(root: &Path) -> (PathBuf, PathBuf) {
    let data_dir = root.join("data");
    let session_path = root.join("session.json");
    let mut store = Store::open(
        &data_dir,
        StoreConfig {
            max_segment_bytes: 128,
        },
    )
    .unwrap();
    store.install_graph_document(sample_graph()).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.put("user:us:bob", "Bob").unwrap();
    store.rebuild_all_views().unwrap();
    store.create_snapshot_point().unwrap();
    store.save_session_snapshot(&session_path).unwrap();
    store.put("order:cn:100", "Book").unwrap();
    (data_dir, session_path)
}

fn view_version(statuses: &[log_structured_store::ViewStatus], id: &str) -> u64 {
    statuses.iter().find(|view| view.id == id).unwrap().version
}

#[test]
fn incremental_recovery_replays_only_post_snapshot_records_and_propagates_indirectly() {
    let root = temp_dir("incremental");
    let (data_dir, session_path) = make_incremental_store(&root);
    let mut store = Store::open(data_dir, StoreConfig::default()).unwrap();
    let opened = store.open_session_snapshot(&session_path, None).unwrap();
    assert_eq!(opened.replacement, "full");
    assert!(!opened.segment_digest_match);
    assert_eq!(opened.added_segments, vec!["000002.log"]);
    assert_eq!(store.logical_timestamp(), 2);
    assert_eq!(store.state().len(), 2);

    let report = store.recover().unwrap();
    assert_eq!(report.business_code, "OK");
    assert_eq!(report.replayed_records, 1);
    assert_eq!(report.logical_timestamp, 3);
    assert_eq!(
        report
            .segments
            .iter()
            .map(|segment| segment.records_replayed)
            .sum::<usize>(),
        1
    );
    assert_eq!(
        store.state().get("order:cn:100").map(String::as_str),
        Some("Book")
    );
    assert!(report
        .trace
        .iter()
        .any(|step| step.affected_view == "order_count" && step.trigger.ends_with("@3")));
    assert!(report.trace.iter().any(|step| {
        step.affected_view == "total_count"
            && step.relation == "view:order_count"
            && step.trigger.ends_with("@3")
    }));
    let statuses = store.view_statuses();
    assert!(
        !statuses
            .iter()
            .find(|view| view.id == "order_count")
            .unwrap()
            .valid
    );
    assert!(
        !statuses
            .iter()
            .find(|view| view.id == "total_count")
            .unwrap()
            .valid
    );
    assert!(
        statuses
            .iter()
            .find(|view| view.id == "user_count")
            .unwrap()
            .valid
    );
    assert!(
        statuses
            .iter()
            .find(|view| view.id == "user_country_index")
            .unwrap()
            .valid
    );

    // A second recover resumes at the last applied position instead of replaying record 3 again.
    let second = store.recover().unwrap();
    assert_eq!(second.replayed_records, 0);
    assert_eq!(second.logical_timestamp, 3);
}

#[test]
fn tail_truncation_keeps_complete_prefix_and_reports_e_log() {
    let root = temp_dir("truncate");
    let data_dir = root.join("data");
    let mut store = Store::open(
        &data_dir,
        StoreConfig {
            max_segment_bytes: 60,
        },
    )
    .unwrap();
    store.put("a", "1").unwrap();
    store.put("b", "2").unwrap();
    store.put("c", "3").unwrap();
    let paths: Vec<PathBuf> = fs::read_dir(&data_dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("log"))
        .collect();
    let last = paths.iter().max().unwrap();
    truncate_tail_bytes(last, 7).unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    let report = recovered.recover().unwrap();
    assert_eq!(report.business_code, "E_LOG");
    assert_eq!(replayed_records(&report), 2);
    assert_eq!(report.logical_timestamp, 2);
    assert!(report.truncation.is_some());
    assert_eq!(recovered.state().get("a").map(String::as_str), Some("1"));
    assert_eq!(recovered.state().get("b").map(String::as_str), Some("2"));
    assert!(!recovered.state().contains_key("c"));
    assert_eq!(
        fs::metadata(last).unwrap().len(),
        report.truncation.as_ref().unwrap().valid_bytes
    );
}

fn replayed_records(report: &log_structured_store::RecoveryReport) -> usize {
    report.replayed_records
}

#[test]
fn checksum_corruption_in_non_final_segment_aborts_with_e_corrupt() {
    let root = temp_dir("corrupt");
    let data_dir = root.join("data");
    let mut store = Store::open(
        &data_dir,
        StoreConfig {
            max_segment_bytes: 60,
        },
    )
    .unwrap();
    store.put("a", "1").unwrap();
    store.put("b", "2").unwrap();
    store.put("c", "3").unwrap();
    let first = data_dir.join("000001.log");
    corrupt_record_checksum(&first, 0).unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    let error = recovered.recover().unwrap_err();
    assert_eq!(error.code, BusinessCode::Corrupt);
    assert!(error.offset.is_some());
    assert!(error.segment.as_ref().unwrap().ends_with("000001.log"));
    assert!(
        recovered.state().is_empty(),
        "failed recovery must not commit partial state"
    );
}

#[test]
fn malformed_length_is_reported_as_e_log_with_offset() {
    let root = temp_dir("bad-length");
    let data_dir = root.join("data");
    let mut store = Store::open(data_dir.clone(), StoreConfig::default()).unwrap();
    store.put("a", "1").unwrap();
    let path = data_dir.join("000001.log");
    let mut bytes = fs::read(&path).unwrap();
    bytes[0..4].copy_from_slice(&6u32.to_le_bytes());
    fs::write(&path, &bytes).unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    let error = recovered.recover().unwrap_err();
    assert_eq!(error.code, BusinessCode::Log);
    assert_eq!(error.offset, Some(0));
    assert!(error.segment.as_ref().unwrap().ends_with("000001.log"));
}

#[test]
fn logical_timestamp_gap_is_e_snapshot() {
    let root = temp_dir("timestamp-gap");
    let data_dir = root.join("data");
    let session_path = root.join("session.json");
    let mut store = Store::open(data_dir.clone(), StoreConfig::default()).unwrap();
    store.install_graph_document(sample_graph()).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.put("user:us:bob", "Bob").unwrap();
    store.create_snapshot_point().unwrap();
    store.save_session_snapshot(&session_path).unwrap();
    append_record(&data_dir, 4096, &LogRecord::put("order:cn:100", "Book", 4)).unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    recovered
        .open_session_snapshot(&session_path, None)
        .unwrap();
    let error = recovered.recover().unwrap_err();
    assert_eq!(error.code, BusinessCode::Snapshot);
    assert!(error.message.contains("expected 3, found 4"));
    assert!(error.offset.is_some());
}

#[test]
fn snapshot_referencing_missing_segment_returns_e_snapshot() {
    let root = temp_dir("missing-segment");
    let data_dir = root.join("data");
    let session_path = root.join("session.json");
    let mut store = Store::open(&data_dir, StoreConfig::default()).unwrap();
    store.install_graph_document(sample_graph()).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.rebuild_all_views().unwrap();
    store.create_snapshot_point().unwrap();
    store.save_session_snapshot(&session_path).unwrap();
    fs::remove_file(data_dir.join("000001.log")).unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    let error = recovered
        .open_session_snapshot(&session_path, None)
        .unwrap_err();
    assert_eq!(error.code, BusinessCode::Snapshot);
    assert!(error.message.contains("missing segment"));
}

#[test]
fn session_open_replaces_state_instead_of_merging() {
    let root = temp_dir("replace");
    let data_dir = root.join("data");
    let session_path = root.join("session.json");
    let mut store = Store::open(&data_dir, StoreConfig::default()).unwrap();
    store.install_graph_document(sample_graph()).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.rebuild_all_views().unwrap();
    store.create_snapshot_point().unwrap();
    store.save_session_snapshot(&session_path).unwrap();
    store.put("order:cn:100", "Book").unwrap();

    let mut recovered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    recovered.recover().unwrap();
    assert_eq!(recovered.state().len(), 2);
    let report = recovered
        .open_session_snapshot(&session_path, None)
        .unwrap();
    assert_eq!(report.replacement, "full");
    assert_eq!(recovered.state().len(), 1);
    assert!(recovered.state().contains_key("user:cn:alice"));
    assert!(!recovered.state().contains_key("order:cn:100"));
    assert_eq!(recovered.logical_timestamp(), 1);
}

#[test]
fn dependency_cycle_is_rejected_before_mutating_store() {
    let document = GraphDocument {
        graph_version: 1,
        views: vec![
            ViewDefinition {
                id: "a".into(),
                kind: ViewKind::PrefixCount { prefix: "a".into() },
                dependencies: vec![Dependency::View { view: "b".into() }],
            },
            ViewDefinition {
                id: "b".into(),
                kind: ViewKind::PrefixCount { prefix: "b".into() },
                dependencies: vec![Dependency::View { view: "a".into() }],
            },
        ],
    };
    let error = DependencyGraph::from_document(document, GraphLoadOptions::default()).unwrap_err();
    assert_eq!(error.code, BusinessCode::Depend);
    assert!(error.message.contains("cycle"));

    let injected = DependencyGraph::from_document(
        sample_graph(),
        GraphLoadOptions {
            inject_dependency_cycle: true,
        },
    )
    .unwrap_err();
    assert_eq!(injected.code, BusinessCode::Depend);
}

#[test]
fn view_validation_reports_e_view_with_first_different_view_and_key() {
    let root = temp_dir("view-validation");
    let data_dir = root.join("data");
    let session_path = root.join("session.json");
    let mut store = Store::open(&data_dir, StoreConfig::default()).unwrap();
    let document = GraphDocument {
        graph_version: 2,
        views: vec![ViewDefinition {
            id: "user_count".into(),
            kind: ViewKind::PrefixCount {
                prefix: "user:".into(),
            },
            dependencies: vec![Dependency::Prefix {
                prefix: "user:".into(),
            }],
        }],
    };
    store.install_graph_document(document).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.rebuild_all_views().unwrap();
    store.create_snapshot_point().unwrap();
    store.save_session_snapshot(&session_path).unwrap();

    let bytes = fs::read(&session_path).unwrap();
    let mut snapshot: SessionSnapshot = serde_json::from_slice(&bytes).unwrap();
    let runtime = snapshot
        .views
        .iter_mut()
        .find(|view| view.id == "user_count")
        .unwrap();
    runtime
        .content
        .as_mut()
        .unwrap()
        .insert("user:".into(), vec!["999".into()]);
    runtime.content_digest = Some("tampered".into());
    fs::write(&session_path, serde_json::to_vec_pretty(&snapshot).unwrap()).unwrap();

    let mut tampered = Store::open(&data_dir, StoreConfig::default()).unwrap();
    tampered.open_session_snapshot(&session_path, None).unwrap();
    let error = tampered.validate_views().unwrap_err();
    assert_eq!(error.code, BusinessCode::View);
    assert!(error.message.contains("view=user_count"));
    assert!(error.message.contains("first_different_key=user:"));
}

#[test]
fn missing_snapshot_version_is_e_snapshot_and_decode_errors_have_stable_codes() {
    let root = temp_dir("missing-version");
    let data_dir = root.join("data");
    let session_path = root.join("broken-session.json");
    Store::open(&data_dir, StoreConfig::default()).unwrap();
    fs::write(&session_path, br#"{"snapshot_point":{}}"#).unwrap();
    let error = SessionSnapshot::load(&session_path, &data_dir, None).unwrap_err();
    assert_eq!(error.code, BusinessCode::Snapshot);
    assert!(error.message.contains("snapshot_version"));

    let record = LogRecord::put("k", "v", 1);
    let mut frame = record.encode().unwrap();
    frame[4..6].copy_from_slice(&9u16.to_le_bytes());
    let failure = decode_record(&frame, 12).unwrap_err();
    assert_eq!(failure.code, BusinessCode::Log);
    assert_eq!(failure.offset, 12);

    let mut checksum_bad = record.encode().unwrap();
    let checksum_index = 4 + 20;
    checksum_bad[checksum_index] ^= 0x01;
    let failure = decode_record(&checksum_bad, 30).unwrap_err();
    assert_eq!(failure.code, BusinessCode::Corrupt);
    assert_eq!(failure.offset, 30);
}

#[test]
fn delete_and_expire_remove_base_state_and_invalidate_views() {
    let root = temp_dir("delete-expire");
    let data_dir = root.join("data");
    let mut store = Store::open(data_dir, StoreConfig::default()).unwrap();
    let document = GraphDocument {
        graph_version: 1,
        views: vec![ViewDefinition {
            id: "user_count".into(),
            kind: ViewKind::PrefixCount {
                prefix: "user:".into(),
            },
            dependencies: vec![Dependency::Prefix {
                prefix: "user:".into(),
            }],
        }],
    };
    store.install_graph_document(document).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    store.rebuild_all_views().unwrap();
    let delete = store.delete("user:cn:alice").unwrap();
    assert_eq!(delete.logical_timestamp, 2);
    assert!(!store.state().contains_key("user:cn:alice"));
    assert!(!store.view_statuses()[0].valid);
    store.put("user:us:bob", "Bob").unwrap();
    store.rebuild_all_views().unwrap();
    let expire = store.expire("user:us:bob").unwrap();
    assert_eq!(expire.logical_timestamp, 4);
    assert!(!store.state().contains_key("user:us:bob"));
    assert!(store
        .invalidation_trace()
        .iter()
        .any(|step| step.trigger.starts_with("delete:user:cn:alice@2")));
    assert!(store
        .invalidation_trace()
        .iter()
        .any(|step| step.trigger.starts_with("expire:user:us:bob@4")));
}

#[test]
fn view_versions_advance_in_topological_order() {
    let root = temp_dir("topology");
    let data_dir = root.join("data");
    let mut store = Store::open(data_dir, StoreConfig::default()).unwrap();
    store.install_graph_document(sample_graph()).unwrap();
    store.put("user:cn:alice", "Alice").unwrap();
    let record_steps: Vec<String> = store
        .invalidation_trace()
        .iter()
        .filter(|step| step.trigger.ends_with("@1"))
        .map(|step| step.affected_view.clone())
        .collect();
    assert_eq!(
        record_steps,
        vec!["user_count", "total_count", "user_country_index"]
    );
    assert_eq!(view_version(&store.view_statuses(), "user_count"), 2);
    assert_eq!(view_version(&store.view_statuses(), "total_count"), 2);
}

#[test]
fn record_operation_codes_are_stable() {
    let put = LogRecord::put("k", "v", 1);
    let delete = LogRecord::delete("k", 2);
    let expire = LogRecord::expire("k", 3);
    assert_eq!(put.operation, Operation::Put);
    assert_eq!(
        decode_record(&put.encode().unwrap(), 0).unwrap().record,
        put
    );
    assert_eq!(
        decode_record(&delete.encode().unwrap(), 0).unwrap().record,
        delete
    );
    assert_eq!(
        decode_record(&expire.encode().unwrap(), 0).unwrap().record,
        expire
    );
}
