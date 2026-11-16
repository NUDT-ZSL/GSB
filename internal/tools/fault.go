package tools

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/logx"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type faultReport struct {
	OK                bool           `json:"ok"`
	Pass              bool           `json:"pass"`
	Kind              string         `json:"kind"`
	ExpectedCode      string         `json:"expected_code"`
	ActualCode        string         `json:"actual_code"`
	PartitionKey      string         `json:"partition_key,omitempty"`
	BlockedOffset     int64          `json:"blocked_offset,omitempty"`
	BlockedRetryCount int            `json:"blocked_retry_count,omitempty"`
	Message           string         `json:"message,omitempty"`
	Fields            map[string]any `json:"fields,omitempty"`
	WorkDir           string         `json:"work_dir"`
	GeneratedAt       string         `json:"generated_at"`
}

// Fault runs one deterministic fault-injection scenario and returns a process exit code.
func Fault(name string, args []string) int {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	kind := flags.String("kind", "", "invalid-partition, corrupt-ledger, or persistent-failure")
	outPath := flags.String("out", "", "fault report path")
	workDir := flags.String("work-dir", "", "working directory for the isolated fault database")
	logPath := flags.String("log", "", "optional structured JSONL log path")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	report := faultReport{Kind: *kind, GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	if *outPath == "" {
		*outPath = filepath.Join("output", "fault-"+*kind+".json")
	}
	if err := os.MkdirAll(filepath.Dir(*outPath), 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "create fault report directory: %v\n", err)
		return 1
	}
	if *workDir == "" {
		if err := os.MkdirAll("output", 0o755); err != nil {
			fmt.Fprintf(os.Stderr, "create output directory: %v\n", err)
			return 1
		}
		var err error
		*workDir, err = os.MkdirTemp("output", "fault-"+*kind+"-")
		if err != nil {
			fmt.Fprintf(os.Stderr, "create fault workdir: %v\n", err)
			return 1
		}
	}
	if err := os.MkdirAll(*workDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "create fault workdir: %v\n", err)
		return 1
	}
	report.WorkDir = *workDir
	if *logPath == "" {
		*logPath = filepath.Join(*workDir, "fault.jsonl")
	}
	logger, err := logx.New(*logPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open structured log: %v\n", err)
		return 1
	}
	defer logger.Close()
	logger.Log("fault_started", map[string]any{"kind": *kind, "work_dir": *workDir})
	switch *kind {
	case "invalid-partition":
		report.ExpectedCode = protocol.CodeAppend
		runInvalidPartition(context.Background(), *workDir, &report)
	case "corrupt-ledger":
		report.ExpectedCode = protocol.CodeLedger
		runCorruptLedger(context.Background(), *workDir, &report)
	case "persistent-failure":
		report.ExpectedCode = protocol.CodePoison
		runPersistentFailure(context.Background(), *workDir, &report)
	default:
		report.Message = "unknown fault kind"
		logger.Log("fault_rejected", map[string]any{"kind": *kind, "message": report.Message})
		_ = writeJSONAtomic(*outPath, report)
		return 2
	}
	report.Pass = report.ActualCode == report.ExpectedCode
	report.OK = report.Pass
	_ = writeJSONAtomic(*outPath, report)
	logger.Log("fault_completed", map[string]any{
		"kind":                report.Kind,
		"expected_code":       report.ExpectedCode,
		"actual_code":         report.ActualCode,
		"pass":                report.Pass,
		"partition_key":       report.PartitionKey,
		"blocked_offset":      report.BlockedOffset,
		"blocked_retry_count": report.BlockedRetryCount,
		"fields":              report.Fields,
		"report":              *outPath,
	})
	fmt.Println(mustJSON(map[string]any{"ok": report.OK, "kind": report.Kind, "expected_code": report.ExpectedCode, "actual_code": report.ActualCode, "pass": report.Pass, "report": *outPath}))
	if report.Pass {
		return 0
	}
	return 1
}

func runInvalidPartition(ctx context.Context, dir string, report *faultReport) {
	opts := faultEngineOptions(dir)
	eng, err := engine.Open(ctx, opts)
	if err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.AsBizError(err).Code
		return
	}
	defer eng.Close()
	_, biz := eng.Append(ctx, "fault-invalid-key", "contains a space", "producer-fault", 1, "sample")
	if biz == nil {
		report.Message = "invalid partition key was accepted"
		report.ActualCode = protocol.CodeOK
		return
	}
	report.ActualCode = biz.Code
	report.Message = biz.Message
	report.Fields = biz.Fields
	if key, ok := biz.Fields["partition_key"].(string); ok {
		report.PartitionKey = key
	}
}

func runCorruptLedger(ctx context.Context, dir string, report *faultReport) {
	opts := faultEngineOptions(dir)
	eng, err := engine.Open(ctx, opts)
	if err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.AsBizError(err).Code
		return
	}
	if _, biz := eng.Append(ctx, "fault-seed", "fault-ledger", "producer-fault", 1, "sample"); biz != nil {
		_ = eng.Close()
		report.Message = biz.Message
		report.ActualCode = biz.Code
		return
	}
	if _, biz := eng.Pull(ctx, "fault-pull", "fault-ledger", "consumer-fault", 1, 5000); biz != nil {
		_ = eng.Close()
		report.Message = biz.Message
		report.ActualCode = biz.Code
		return
	}
	if _, biz := eng.Commit(ctx, "fault-commit", "fault-ledger", "consumer-fault", 1); biz != nil {
		_ = eng.Close()
		report.Message = biz.Message
		report.ActualCode = biz.Code
		return
	}
	if err := eng.Close(); err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.CodeInternal
		return
	}
	db, err := sql.Open("sqlite3", filepath.Join(dir, "queue.db"))
	if err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.CodeInternal
		return
	}
	defer db.Close()
	if _, err := db.Exec(`PRAGMA ignore_check_constraints=ON`); err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.CodeInternal
		return
	}
	if _, err := db.Exec(`UPDATE partitions SET next_offset=99, committed_offset=50, confirmed_offset=40 WHERE partition_key='fault-ledger'`); err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.CodeInternal
		return
	}
	_ = db.Close()
	_, err = engine.Open(ctx, opts)
	if err == nil {
		report.Message = "corrupt ledger was accepted"
		report.ActualCode = protocol.CodeOK
		return
	}
	var biz *protocol.BizError
	if errors.As(err, &biz) {
		report.ActualCode = biz.Code
		report.Message = biz.Message
		report.Fields = biz.Fields
		if key, ok := biz.Fields["partition_key"].(string); ok {
			report.PartitionKey = key
		}
		return
	}
	report.ActualCode = protocol.CodeInternal
	report.Message = err.Error()
}

func runPersistentFailure(ctx context.Context, dir string, report *faultReport) {
	opts := faultEngineOptions(dir)
	opts.MaxRetries = 2
	eng, err := engine.Open(ctx, opts)
	if err != nil {
		report.Message = err.Error()
		report.ActualCode = protocol.AsBizError(err).Code
		return
	}
	defer eng.Close()
	if _, biz := eng.Append(ctx, "fault-append", "fault-poison", "producer-fault", 1, "sample"); biz != nil {
		report.Message = biz.Message
		report.ActualCode = biz.Code
		return
	}
	report.PartitionKey = "fault-poison"
	for attempt := 1; attempt <= opts.MaxRetries+2; attempt++ {
		_, biz := eng.Pull(ctx, "fault-pull", "fault-poison", "consumer-fault", 1, 5000)
		if biz != nil {
			setFaultCode(report, biz)
			return
		}
		_, biz = eng.Fail(ctx, "fault-fail", "fault-poison", "consumer-fault", 1, fmt.Sprintf("persistent-failure-%d", attempt))
		if biz != nil && biz.Code == protocol.CodePoison {
			setFaultCode(report, biz)
			return
		}
	}
	report.Message = "partition was not poisoned before the retry budget was exhausted"
	report.ActualCode = protocol.CodeOK
}

func setFaultCode(report *faultReport, biz *protocol.BizError) {
	report.ActualCode = biz.Code
	report.Message = biz.Message
	report.Fields = biz.Fields
	if offset, ok := biz.Fields["blocked_offset"].(int64); ok {
		report.BlockedOffset = offset
	}
	if count, ok := biz.Fields["blocked_retry_count"].(int); ok {
		report.BlockedRetryCount = count
	}
}

func faultEngineOptions(dir string) engine.Options {
	return engine.Options{
		DBPath:               filepath.Join(dir, "queue.db"),
		RecoveryReportPath:   filepath.Join(dir, "recovery.json"),
		MaxRetries:           2,
		DefaultWindowTimeout: 5 * time.Second,
		DisableSweeper:       true,
	}
}
