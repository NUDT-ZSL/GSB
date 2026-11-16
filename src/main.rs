use log_structured_store::graph::{
    Dependency, DependencyGraph, GraphDocument, ViewDefinition, ViewKind,
};
use log_structured_store::segment::{
    corrupt_record_checksum, discover_segment_paths, segment_name, truncate_tail_bytes,
};
use log_structured_store::{
    BusinessCode, DiagnosticLogger, GraphLoadOptions, RecoveryReport, Result, Store, StoreConfig,
    StoreError, ViewStatus, WriteReceipt,
};
use std::env;
use std::fs;
use std::io::{self, BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
struct CliOptions {
    data_dir: PathBuf,
    graph: Option<PathBuf>,
    session: Option<PathBuf>,
    log: Option<PathBuf>,
    max_segment_bytes: u64,
    auto_recover: bool,
    inject_tail_truncate: Option<String>,
    inject_corrupt_checksum: Option<String>,
    inject_dependency_cycle: bool,
    inject_missing_snapshot_segment: Option<String>,
}

impl Default for CliOptions {
    fn default() -> Self {
        Self {
            data_dir: PathBuf::from("data"),
            graph: None,
            session: None,
            log: None,
            max_segment_bytes: 4096,
            auto_recover: false,
            inject_tail_truncate: None,
            inject_corrupt_checksum: None,
            inject_dependency_cycle: false,
            inject_missing_snapshot_segment: None,
        }
    }
}

fn main() {
    let code = match run() {
        Ok(code) => code,
        Err(error) => {
            println!("{}", error);
            2
        }
    };
    std::process::exit(code);
}

fn run() -> std::result::Result<i32, StoreError> {
    let raw_args: Vec<String> = env::args().skip(1).collect();
    if raw_args.first().map(String::as_str) == Some("generate-samples") {
        let root = raw_args
            .get(1)
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("samples"));
        generate_samples(&root)?;
        println!("generated_samples={}", root.display());
        return Ok(0);
    }
    if raw_args
        .iter()
        .any(|value| value == "--help" || value == "-h")
    {
        print_help();
        return Ok(0);
    }

    let options = parse_options(&raw_args)?;
    fs::create_dir_all(&options.data_dir).map_err(|error| {
        StoreError::io(format!(
            "failed to create data directory {}: {error}",
            options.data_dir.display()
        ))
    })?;
    let log_path = options
        .log
        .clone()
        .unwrap_or_else(|| options.data_dir.join("diagnostics.jsonl"));
    let logger = DiagnosticLogger::new(log_path);

    if let Some(spec) = &options.inject_tail_truncate {
        match apply_tail_truncation(&options.data_dir, spec) {
            Ok((segment, new_len, removed)) => {
                let _ = logger.event(
                    BusinessCode::Log,
                    Some(segment.clone()),
                    Some(new_len),
                    format!("injected tail truncation removing {removed} bytes"),
                );
                println!(
                    "injection=tail-truncate segment={} removed_bytes={} new_bytes={}",
                    segment, removed, new_len
                );
            }
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                return Ok(2);
            }
        }
    }
    if let Some(spec) = &options.inject_corrupt_checksum {
        match apply_checksum_corruption(&options.data_dir, spec) {
            Ok((segment, offset)) => {
                let _ = logger.event(
                    BusinessCode::Corrupt,
                    Some(segment.clone()),
                    Some(offset),
                    "injected checksum corruption",
                );
                println!(
                    "injection=checksum-corruption segment={} offset={}",
                    segment, offset
                );
            }
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                return Ok(2);
            }
        }
    }

    let mut store = Store::open(
        &options.data_dir,
        StoreConfig {
            max_segment_bytes: options.max_segment_bytes,
        },
    )?;
    store.set_diagnostic_logger(logger.clone());

    if let Some(graph_path) = &options.graph {
        match store.load_graph_file(
            graph_path,
            GraphLoadOptions {
                inject_dependency_cycle: options.inject_dependency_cycle,
            },
        ) {
            Ok(()) => println!("graph_loaded={}", graph_path.display()),
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                return Ok(2);
            }
        }
    }

    if let Some(session_path) = &options.session {
        match store.open_session_snapshot(
            session_path,
            options.inject_missing_snapshot_segment.as_deref(),
        ) {
            Ok(report) => println!("{report}"),
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                return Ok(2);
            }
        }
    }

    let mut last_recovery: Option<RecoveryReport> = None;
    if options.auto_recover {
        match store.recover() {
            Ok(report) => {
                println!("{report}");
                last_recovery = Some(report);
            }
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                return Ok(2);
            }
        }
    }

    let stdin = io::stdin();
    let interactive = stdin.is_terminal();
    let mut exit_code = 0;
    loop {
        if interactive {
            print!("logstore> ");
            let _ = io::stdout().flush();
        }
        let mut line = String::new();
        let read = stdin
            .lock()
            .read_line(&mut line)
            .map_err(|error| StoreError::io(format!("failed to read stdin: {error}")))?;
        if read == 0 {
            break;
        }
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        match execute_command(line, &mut store, &mut last_recovery) {
            Ok(true) => break,
            Ok(false) => {}
            Err(error) => {
                let _ = logger.error(&error);
                println!("{}", error);
                exit_code = 2;
            }
        }
    }
    Ok(exit_code)
}

fn parse_options(args: &[String]) -> Result<CliOptions> {
    let mut options = CliOptions::default();
    let mut index = 0usize;
    while index < args.len() {
        let arg = &args[index];
        match arg.as_str() {
            "--data-dir" => {
                options.data_dir = PathBuf::from(next_arg(args, &mut index, arg)?);
            }
            "--graph" => options.graph = Some(PathBuf::from(next_arg(args, &mut index, arg)?)),
            "--session" => options.session = Some(PathBuf::from(next_arg(args, &mut index, arg)?)),
            "--log" => options.log = Some(PathBuf::from(next_arg(args, &mut index, arg)?)),
            "--max-segment-bytes" => {
                let value = next_arg(args, &mut index, arg)?;
                options.max_segment_bytes = value
                    .parse::<u64>()
                    .map_err(|_| StoreError::input(format!("invalid segment size {value}")))?;
                if options.max_segment_bytes < 64 {
                    return Err(StoreError::input("--max-segment-bytes must be at least 64"));
                }
            }
            "--auto-recover" => options.auto_recover = true,
            "--inject-tail-truncate" => {
                options.inject_tail_truncate = Some(next_arg(args, &mut index, arg)?)
            }
            "--inject-corrupt-checksum" => {
                options.inject_corrupt_checksum = Some(next_arg(args, &mut index, arg)?)
            }
            "--inject-dependency-cycle" => options.inject_dependency_cycle = true,
            "--inject-missing-snapshot-segment" => {
                options.inject_missing_snapshot_segment = Some(next_arg(args, &mut index, arg)?)
            }
            _ => return Err(StoreError::input(format!("unknown argument {arg}"))),
        }
        index += 1;
    }
    Ok(options)
}

fn next_arg(args: &[String], index: &mut usize, option: &str) -> Result<String> {
    *index += 1;
    args.get(*index)
        .cloned()
        .ok_or_else(|| StoreError::input(format!("{option} requires a value")))
}

fn parse_segment_spec(spec: &str) -> Result<(String, u64)> {
    let (segment, value) = spec.rsplit_once(':').ok_or_else(|| {
        StoreError::input(format!(
            "invalid segment specification {spec}; expected SEGMENT:VALUE"
        ))
    })?;
    let number = value.parse::<u64>().map_err(|_| {
        StoreError::input(format!(
            "invalid numeric value {value} in segment specification"
        ))
    })?;
    Ok((segment.to_string(), number))
}

fn resolve_segment(data_dir: &Path, name: &str) -> Result<PathBuf> {
    if name == "last" {
        return discover_segment_paths(data_dir)?
            .last()
            .cloned()
            .ok_or_else(|| StoreError::input("no segment files exist for 'last'"));
    }
    let path = data_dir.join(name);
    if !path.is_file() {
        return Err(StoreError::input(format!(
            "segment {} does not exist",
            path.display()
        )));
    }
    Ok(path)
}

fn apply_tail_truncation(data_dir: &Path, spec: &str) -> Result<(String, u64, u64)> {
    let (name, removed) = parse_segment_spec(spec)?;
    let path = resolve_segment(data_dir, &name)?;
    let new_len = truncate_tail_bytes(&path, removed)?;
    Ok((segment_name(&path), new_len, removed))
}

fn apply_checksum_corruption(data_dir: &Path, spec: &str) -> Result<(String, u64)> {
    let (name, record_index) = parse_segment_spec(spec)?;
    let path = resolve_segment(data_dir, &name)?;
    let offset = corrupt_record_checksum(&path, record_index as usize)?;
    Ok((segment_name(&path), offset))
}

fn execute_command(
    line: &str,
    store: &mut Store,
    last_recovery: &mut Option<RecoveryReport>,
) -> Result<bool> {
    let (command, rest) = split_command(line);
    match command {
        "quit" | "exit" => return Ok(true),
        "help" => print_help(),
        "recover" => {
            let report = store.recover()?;
            println!("{report}");
            *last_recovery = Some(report);
        }
        "report" => match last_recovery {
            Some(report) => println!("{report}"),
            None => print_current_report(store)?,
        },
        "put" => {
            let (key, value) = split_key_value(rest)?;
            let receipt = store.put(key, value)?;
            print_append("put", &receipt);
        }
        "delete" => {
            let key = require_rest(command, rest)?;
            let receipt = store.delete(key)?;
            print_append("delete", &receipt);
        }
        "expire" => {
            let key = require_rest(command, rest)?;
            let receipt = store.expire(key)?;
            print_append("expire", &receipt);
        }
        "snapshot" => {
            let point = store.create_snapshot_point()?;
            println!("report=snapshot-point");
            println!("business_code=OK");
            println!("segment={}", point.segment);
            println!("offset={}", point.offset);
            println!("logical_timestamp={}", point.logical_timestamp);
            println!("state_digest={}", point.state_digest);
        }
        "snapshot-save" | "session-save" => {
            let path = require_rest(command, rest)?;
            store.save_session_snapshot(Path::new(&path))?;
            println!("report=session-save");
            println!("business_code=OK");
            println!("path={path}");
        }
        "session-open" => {
            let path = require_rest(command, rest)?;
            let report = store.open_session_snapshot(Path::new(&path), None)?;
            println!("{report}");
            *last_recovery = None;
        }
        "graph-load" => {
            let path = require_rest(command, rest)?;
            store.load_graph_file(Path::new(&path), GraphLoadOptions::default())?;
            println!("report=graph-load");
            println!("business_code=OK");
            println!("path={path}");
            println!("graph_version={}", store.graph().document.graph_version);
            println!("graph_digest={}", store.graph().digest);
        }
        "view-declare" => {
            let path = require_rest(command, rest)?;
            let bytes = fs::read(&path).map_err(|error| {
                StoreError::depend(format!("failed to read view definition {path}: {error}"))
            })?;
            let definition: ViewDefinition = serde_json::from_slice(&bytes).map_err(|error| {
                StoreError::depend(format!("invalid view definition JSON {path}: {error}"))
            })?;
            let id = definition.id.clone();
            store.declare_view(definition)?;
            println!("report=view-declare");
            println!("business_code=OK");
            println!("view={id}");
            println!("graph_version={}", store.graph().document.graph_version);
        }
        "view-rebuild" | "rebuild" => {
            let target = require_rest(command, rest)?;
            if target == "all" {
                let reports = store.rebuild_all_views()?;
                println!("report=view-rebuild");
                println!("business_code=OK");
                for report in reports {
                    print_rebuild(&report);
                }
            } else {
                let report = store.rebuild_view(&target)?;
                println!("report=view-rebuild");
                println!("business_code=OK");
                print_rebuild(&report);
            }
        }
        "validate" => {
            let report = store.validate_views()?;
            println!("{report}");
        }
        "views" => {
            println!("report=views");
            println!("business_code=OK");
            for view in store.view_statuses() {
                print_view(&view);
            }
        }
        "trace" => {
            println!("report=trace");
            println!("business_code=OK");
            if store.invalidation_trace().is_empty() {
                println!("trace=none");
            } else {
                for step in store.invalidation_trace() {
                    println!(
                        "trace={} trigger={} affected_view={} relation={} action={}",
                        step.step, step.trigger, step.affected_view, step.relation, step.action
                    );
                }
            }
        }
        "segments" => {
            let summary = store.current_segments()?;
            println!("report=segments");
            println!("business_code=OK");
            println!("segment_set_digest={}", summary.digest);
            for segment in summary.segments {
                println!(
                    "segment={} bytes={} digest={}",
                    segment.name, segment.bytes, segment.digest
                );
            }
        }
        "state" => {
            let key = require_rest(command, rest)?;
            println!("report=state");
            println!("business_code=OK");
            println!("key={key}");
            println!(
                "value={}",
                store
                    .state()
                    .get(&key)
                    .map(String::as_str)
                    .unwrap_or("none")
            );
        }
        "diagnose-snapshot" => {
            let path = require_rest(command, rest)?;
            let snapshot = store.diagnose_snapshot(Path::new(&path))?;
            println!("report=snapshot-diagnose");
            println!("business_code=OK");
            println!("snapshot_version={}", snapshot.snapshot_version);
            println!("logical_timestamp={}", snapshot.logical_timestamp);
            println!("segment_set_digest={}", snapshot.segment_set_digest);
            println!("graph_version={}", snapshot.graph_version);
            println!("graph_digest={}", snapshot.graph_digest);
            for segment in snapshot.segments {
                println!(
                    "segment={} bytes={} digest={}",
                    segment.name, segment.bytes, segment.digest
                );
            }
            for view in snapshot.views {
                println!(
                    "view={} version={} valid={}",
                    view.id, view.version, view.valid
                );
            }
        }
        "injection-info" => {
            println!("report=injection-info");
            println!("business_code=OK");
            println!("note=injections are applied by startup arguments before recovery");
        }
        _ => {
            return Err(StoreError::input(format!(
                "unknown command {command}; use help"
            )))
        }
    }
    Ok(false)
}

fn split_command(line: &str) -> (&str, &str) {
    match line.find(char::is_whitespace) {
        Some(index) => (&line[..index], line[index..].trim()),
        None => (line, ""),
    }
}

fn require_rest(command: &str, rest: &str) -> Result<String> {
    if rest.is_empty() {
        Err(StoreError::input(format!("{command} requires an argument")))
    } else {
        Ok(rest.to_string())
    }
}

fn split_key_value(rest: &str) -> Result<(String, String)> {
    if rest.is_empty() {
        return Err(StoreError::input("put requires a key"));
    }
    match rest.find(char::is_whitespace) {
        Some(index) => Ok((
            rest[..index].to_string(),
            rest[index..].trim_start().to_string(),
        )),
        None => Ok((rest.to_string(), String::new())),
    }
}

fn print_append(operation: &str, receipt: &WriteReceipt) {
    println!("report=append");
    println!("business_code=OK");
    println!("operation={operation}");
    println!("segment={}", receipt.segment);
    println!("offset={}", receipt.offset);
    println!("bytes={}", receipt.bytes);
    println!("logical_timestamp={}", receipt.logical_timestamp);
}

fn print_rebuild(report: &log_structured_store::RebuildReport) {
    println!(
        "view={} version={} entries={} digest={} previous_digest={}",
        report.view,
        report.version,
        report.entry_count,
        report.digest,
        report.previous_digest.as_deref().unwrap_or("none")
    );
}

fn print_view(view: &ViewStatus) {
    println!(
        "view={} kind={} version={} valid={} digest={}",
        view.id,
        view.kind,
        view.version,
        view.valid,
        view.digest.as_deref().unwrap_or("none")
    );
}

fn print_current_report(store: &Store) -> Result<()> {
    println!("report=current");
    println!("business_code=OK");
    println!("logical_timestamp={}", store.logical_timestamp());
    match store.snapshot_point() {
        Some(point) => {
            println!("snapshot_segment={}", point.segment);
            println!("snapshot_offset={}", point.offset);
            println!("snapshot_timestamp={}", point.logical_timestamp);
        }
        None => {
            println!("snapshot_segment=none");
            println!("snapshot_offset=none");
            println!("snapshot_timestamp=none");
        }
    }
    for view in store.view_statuses() {
        print_view(&view);
    }
    if store.invalidation_trace().is_empty() {
        println!("trace=none");
    } else {
        for step in store.invalidation_trace() {
            println!(
                "trace={} trigger={} affected_view={} relation={} action={}",
                step.step, step.trigger, step.affected_view, step.relation, step.action
            );
        }
    }
    Ok(())
}

fn print_help() {
    println!("logstore - deterministic append-only KV store for Windows teaching");
    println!("usage: logstore [--data-dir DIR] [--graph FILE] [--session FILE] [--log FILE]");
    println!("                 [--max-segment-bytes N] [--auto-recover]");
    println!("                 [--inject-tail-truncate SEGMENT:BYTES]");
    println!("                 [--inject-corrupt-checksum SEGMENT:RECORD_INDEX]");
    println!("                 [--inject-dependency-cycle]");
    println!("                 [--inject-missing-snapshot-segment SEGMENT]");
    println!("       logstore generate-samples [DIR]");
    println!("stdin commands:");
    println!("  recover | report | snapshot | snapshot-save PATH | session-save PATH");
    println!("  put KEY VALUE | delete KEY | expire KEY");
    println!("  session-open PATH | graph-load PATH | view-declare FILE");
    println!("  view-rebuild ID|all | validate | views | trace | segments | state KEY");
    println!("  diagnose-snapshot PATH | help | quit");
}

fn generate_samples(root: &Path) -> Result<()> {
    let basic = root.join("basic");
    if basic.exists() {
        fs::remove_dir_all(&basic).map_err(|error| {
            StoreError::io(format!("failed to clean {}: {error}", basic.display()))
        })?;
    }
    fs::create_dir_all(&basic).map_err(|error| {
        StoreError::io(format!("failed to create {}: {error}", basic.display()))
    })?;
    let graph_document = sample_graph_document();
    let graph = DependencyGraph::from_document(graph_document, GraphLoadOptions::default())?;
    let graph_path = basic.join("graph.json");
    let graph_bytes = serde_json::to_vec_pretty(&graph.document)
        .map_err(|error| StoreError::depend(format!("failed to encode sample graph: {error}")))?;
    fs::write(&graph_path, graph_bytes).map_err(|error| {
        StoreError::io(format!("failed to write {}: {error}", graph_path.display()))
    })?;

    let data_dir = basic.join("data");
    let mut store = Store::open(
        data_dir,
        StoreConfig {
            max_segment_bytes: 128,
        },
    )?;
    store.install_graph_document(graph.document.clone())?;
    store.put("user:cn:alice", "Alice")?;
    store.put("user:us:bob", "Bob")?;
    store.rebuild_all_views()?;
    store.create_snapshot_point()?;
    store.save_session_snapshot(&basic.join("session.json"))?;
    // This record is intentionally appended after the snapshot. Incremental recovery starts at
    // the snapshot position and replays only this record (logical timestamp 3).
    store.put("order:cn:100", "Book")?;
    Ok(())
}

fn sample_graph_document() -> GraphDocument {
    GraphDocument {
        graph_version: 7,
        views: vec![
            ViewDefinition {
                id: "order_count".to_string(),
                kind: ViewKind::PrefixCount {
                    prefix: "order:".to_string(),
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "order:".to_string(),
                }],
            },
            ViewDefinition {
                id: "total_count".to_string(),
                kind: ViewKind::PrefixCount {
                    prefix: String::new(),
                },
                dependencies: vec![
                    Dependency::View {
                        view: "order_count".to_string(),
                    },
                    Dependency::View {
                        view: "user_count".to_string(),
                    },
                ],
            },
            ViewDefinition {
                id: "user_count".to_string(),
                kind: ViewKind::PrefixCount {
                    prefix: "user:".to_string(),
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "user:".to_string(),
                }],
            },
            ViewDefinition {
                id: "user_country_index".to_string(),
                kind: ViewKind::SecondaryIndex {
                    index: "by_country".to_string(),
                    prefix: "user:".to_string(),
                    delimiter: ":".to_string(),
                    field: 1,
                },
                dependencies: vec![Dependency::Prefix {
                    prefix: "user:".to_string(),
                }],
            },
        ],
    }
}
