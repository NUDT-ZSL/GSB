package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/logx"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/service"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/tools"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: orderqueue <serve|produce|consume|dump|status|verify|fault> [flags]")
		os.Exit(2)
	}
	var code int
	switch os.Args[1] {
	case "serve":
		code = runServe(os.Args[2:])
	case "produce":
		code = runProduce(os.Args[2:])
	case "consume":
		code = runConsume(os.Args[2:])
	case "dump":
		code = runDump(os.Args[2:])
	case "status":
		code = runStatus(os.Args[2:])
	case "verify":
		code = tools.Check("verify", os.Args[2:])
	case "fault":
		code = tools.Fault("fault", os.Args[2:])
	case "help", "--help", "-h":
		fmt.Println("usage: orderqueue <serve|produce|consume|dump|status|verify|fault> [flags]")
		fmt.Println("  verify --db output/orderqueue.db --out output/verification-report.json")
		fmt.Println("  fault --kind invalid-partition|corrupt-ledger|persistent-failure --out output/fault.json")
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		code = 2
	}
	os.Exit(code)
}

func runServe(args []string) int {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	listen := flags.String("listen", "127.0.0.1:19160", "loopback TCP address")
	dbPath := flags.String("db", "output/orderqueue.db", "SQLite database path")
	logPath := flags.String("log", "output/server.jsonl", "structured JSONL log path")
	recoveryPath := flags.String("recovery-report", "output/recovery-report.json", "startup recovery report path")
	readyPath := flags.String("ready-file", "output/server-ready.json", "readiness file path")
	maxRetries := flags.Int("max-retries", 3, "maximum retries per message offset")
	windowTimeout := flags.Duration("window-timeout", 5*time.Second, "default delivery window timeout")
	maxBatch := flags.Int("max-batch", 1000, "maximum pull batch size")
	maxBodyBytes := flags.Int("max-body-bytes", 64*1024, "maximum message body size")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	for _, path := range []string{*dbPath, *logPath, *recoveryPath, *readyPath} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
			fmt.Fprintf(os.Stderr, "create directory for %s: %v\n", path, err)
			return 1
		}
	}
	logger, err := logx.New(*logPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open structured log: %v\n", err)
		return 1
	}
	defer logger.Close()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	eng, err := engine.Open(ctx, engine.Options{
		DBPath:               *dbPath,
		Logger:               logger,
		MaxRetries:           *maxRetries,
		DefaultWindowTimeout: *windowTimeout,
		MaxBatch:             *maxBatch,
		MaxBodyBytes:         *maxBodyBytes,
		RecoveryReportPath:   *recoveryPath,
	})
	if err != nil {
		biz := protocol.AsBizError(err)
		logger.Log("startup_failed", map[string]any{"code": biz.Code, "message": biz.Message, "fields": biz.Fields})
		_ = writeJSONAtomic(*readyPath, map[string]any{"ready": false, "code": biz.Code, "message": biz.Message, "fields": biz.Fields, "pid": os.Getpid()})
		fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": biz.Code, "message": biz.Message, "fields": biz.Fields}))
		return 1
	}
	defer eng.Close()
	server := service.New(eng, logger)
	address, err := server.Listen(ctx, *listen)
	if err != nil {
		logger.Log("listen_failed", map[string]any{"listen": *listen, "error": err.Error()})
		_ = writeJSONAtomic(*readyPath, map[string]any{"ready": false, "code": protocol.CodeInternal, "message": err.Error(), "listen": *listen, "pid": os.Getpid()})
		fmt.Fprintf(os.Stderr, "listen %s: %v\n", *listen, err)
		return 1
	}
	ready := map[string]any{
		"ready":           true,
		"listen":          address,
		"pid":             os.Getpid(),
		"db":              *dbPath,
		"log":             *logPath,
		"recovery_report": *recoveryPath,
		"started_at":      time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := writeJSONAtomic(*readyPath, ready); err != nil {
		fmt.Fprintf(os.Stderr, "write ready file: %v\n", err)
		_ = server.Close()
		return 1
	}
	fmt.Println(mustJSON(ready))
	<-ctx.Done()
	logger.Log("shutdown_requested", map[string]any{"listen": address})
	_ = server.Close()
	logger.Log("shutdown_complete", map[string]any{"listen": address})
	return 0
}

func writeJSONAtomic(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	_ = os.Remove(path)
	return os.Rename(tmp, path)
}

func mustJSON(value any) string {
	data, _ := json.Marshal(value)
	return string(data)
}
