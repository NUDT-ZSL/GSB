package tools

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/logx"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/verify"
)

// Check runs the independent ledger verifier and returns a process exit code.
func Check(name string, args []string) int {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	dbPath := flags.String("db", "output/orderqueue.db", "SQLite database path")
	outPath := flags.String("out", "output/verification-report.json", "verification report path")
	logPath := flags.String("log", "output/verify.jsonl", "structured JSONL log path")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	logger, err := logx.New(*logPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open structured log: %v\n", err)
		return 1
	}
	defer logger.Close()
	logger.Log("verify_started", map[string]any{"db": *dbPath, "report": *outPath})
	report, err := verify.Run(context.Background(), verify.Options{DBPath: *dbPath, ReportPath: *outPath})
	if err != nil {
		logger.Log("verify_failed", map[string]any{"code": protocol.CodeVerify, "error": err.Error()})
		fmt.Fprintf(os.Stderr, "verify: %v\n", err)
		return 1
	}
	if err := report.Write(*outPath); err != nil {
		fmt.Fprintf(os.Stderr, "write verification report: %v\n", err)
		return 1
	}
	fields := map[string]any{
		"ok":                 report.OK,
		"code":               report.Code,
		"partitions_checked": report.PartitionsChecked,
		"messages_checked":   report.MessagesChecked,
		"events_checked":     report.EventsChecked,
		"violations":         len(report.Violations),
		"report":             *outPath,
	}
	if report.FirstViolation != nil {
		fields["first_violation"] = report.FirstViolation
	}
	logger.Log("verify_completed", fields)
	fmt.Println(mustJSON(fields))
	if report.OK {
		return 0
	}
	return 2
}
