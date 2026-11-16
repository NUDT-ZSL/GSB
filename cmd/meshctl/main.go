package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"localmesh/internal/biz"
	"localmesh/internal/diff"
	"localmesh/internal/logx"
	"localmesh/internal/mockupstream"
	"localmesh/internal/model"
	"localmesh/internal/recovery"
	"localmesh/internal/requestspec"
	"localmesh/internal/rules"
	"localmesh/internal/scheduler"
	"localmesh/internal/snapshot"
	"localmesh/internal/verify"
)

const version = "1.0.0"

func main() {
	if err := runCLI(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(biz.ExitCode(err))
	}
}

func runCLI(args []string) error {
	if len(args) == 0 {
		return usageError()
	}
	switch args[0] {
	case "run":
		return commandRun(args[1:])
	case "verify":
		return commandVerify(args[1:])
	case "diff":
		return commandDiff(args[1:])
	case "recover":
		return commandRecover(args[1:])
	case "mock":
		return commandMock(args[1:])
	case "version", "--version", "-version":
		fmt.Printf("meshctl %s\n", version)
		return nil
	case "help", "--help", "-h":
		printUsage()
		return nil
	default:
		return usageError()
	}
}

func usageError() error {
	printUsage()
	return biz.New(biz.CodeRule, "cli", "unknown or missing command")
}

func printUsage() {
	fmt.Fprintln(os.Stderr, `meshctl - local Windows service-mesh traffic governor

Commands:
  meshctl run     -rules FILE -requests FILE -round NAME [-out DIR] [-log FILE] [-inject-failure none|rule|upstream|snapshot]
  meshctl verify  -snapshot FILE [-trace FILE] [-report FILE] [-log FILE]
  meshctl diff    -from SNAPSHOT -to SNAPSHOT [-out FILE]
  meshctl recover [-checkpoint FILE] -out DIR [-log FILE]
  meshctl mock    -endpoint ADDRESS=MODE:DELAY_MS[:FAIL_COUNT] ... [-ready-file FILE]
  meshctl version

Business codes: E_RULE E_VERIFY E_UPSTREAM E_SNAPSHOT E_REPLAY`)
}

type vaultFlags struct {
	rules, requests, round, out, logPath, inject string
}

func commandRun(args []string) error {
	var f vaultFlags
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.StringVar(&f.rules, "rules", "", "validated rules JSON file")
	fs.StringVar(&f.requests, "requests", "", "request batch JSON file")
	fs.StringVar(&f.round, "round", "", "round id (defaults to requests file round)")
	fs.StringVar(&f.out, "out", "out", "snapshot/checkpoint output directory")
	fs.StringVar(&f.logPath, "log", "", "structured JSONL log file (default OUT/mesh.log)")
	fs.StringVar(&f.inject, "inject-failure", "none", "none, rule, upstream or snapshot")
	if err := fs.Parse(args); err != nil {
		return biz.New(biz.CodeRule, "cli", err.Error())
	}
	if f.rules == "" || f.requests == "" {
		return biz.WithField(biz.New(biz.CodeRule, "cli", "-rules and -requests are required"), "flags")
	}
	if f.logPath == "" {
		f.logPath = filepath.Join(f.out, "mesh.log")
	}
	logger, err := logx.Open(f.logPath)
	if err != nil {
		return fmt.Errorf("open log: %w", err)
	}
	defer logger.Close()
	if !validInjection(f.inject) {
		return biz.WithField(biz.New(biz.CodeRule, "cli", "invalid -inject-failure value"), "inject_failure")
	}
	if f.inject == "rule" {
		be := biz.WithField(biz.New(biz.CodeRule, "load_rules", "rules file corruption injected by command line"), "inject_failure")
		logger.Error(f.round, "business_error", biz.Fields(be))
		return be
	}
	set, err := rules.Load(f.rules)
	if err != nil {
		logger.Error(f.round, "business_error", biz.Fields(err))
		return err
	}
	reqFile, err := requestspec.Load(f.requests)
	if err != nil {
		logger.Error(f.round, "business_error", biz.Fields(err))
		return err
	}
	if f.round == "" {
		f.round = reqFile.Round
	}
	if f.round == "" {
		return biz.WithField(biz.New(biz.CodeRule, "cli", "round id is required"), "round")
	}
	store := snapshot.New(f.out)
	engine := scheduler.New(scheduler.Options{
		Rules:         set,
		Requests:      reqFile.Requests,
		RoundID:       f.round,
		Store:         store,
		Logger:        logger,
		InjectFailure: f.inject,
	})
	result, runErr := engine.Run(context.Background())
	if result != nil {
		events, loadErr := store.LoadTrace(result.TracePath)
		if loadErr != nil {
			logger.Error(f.round, "business_error", biz.Fields(loadErr))
			return loadErr
		}
		report := verify.Run(verify.VerifyOptions{
			Document:     result.Document,
			Events:       events,
			SnapshotPath: result.SnapshotPath,
			TracePath:    result.TracePath,
		})
		reportPath, reportErr := store.WriteReport("verify-"+f.round+".json", report)
		if reportErr != nil {
			logger.Error(f.round, "business_error", biz.Fields(reportErr))
			return reportErr
		}
		fmt.Printf("RUN round=%s rules_version=%s snapshot=%s trace=%s report=%s\n", result.Document.Round.ID, result.Document.Round.RulesVersion, result.SnapshotPath, result.TracePath, reportPath)
		printVerification(report)
		if !report.OK {
			be := verify.Required(report)
			logger.Error(f.round, "business_error", biz.Fields(be))
			return be
		}
	}
	if runErr != nil {
		logger.Error(f.round, "business_error", biz.Fields(runErr))
		return runErr
	}
	return nil
}

func commandVerify(args []string) error {
	var snapshotPath, tracePath, reportPath, logPath string
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.StringVar(&snapshotPath, "snapshot", "", "snapshot JSON file")
	fs.StringVar(&tracePath, "trace", "", "trace JSONL (defaults to snapshot trace_path)")
	fs.StringVar(&reportPath, "report", "", "verification report JSON output")
	fs.StringVar(&logPath, "log", "", "structured JSONL log file")
	if err := fs.Parse(args); err != nil {
		return biz.New(biz.CodeRule, "cli", err.Error())
	}
	if snapshotPath == "" {
		return biz.WithField(biz.New(biz.CodeVerify, "cli", "-snapshot is required"), "flags")
	}
	logger, err := openOptionalLogger(logPath)
	if err != nil {
		return err
	}
	defer logger.Close()
	store := snapshot.New(filepath.Dir(snapshotPath))
	doc, err := store.LoadDocument(snapshotPath)
	if err != nil {
		logger.Error("", "business_error", biz.Fields(err))
		return err
	}
	if tracePath == "" {
		tracePath = doc.TracePath
	}
	events, err := store.LoadTrace(tracePath)
	if err != nil {
		logger.Error(doc.Round.ID, "business_error", biz.Fields(err))
		return err
	}
	report := verify.Run(verify.VerifyOptions{Document: doc, Events: events, SnapshotPath: snapshotPath, TracePath: tracePath})
	if reportPath == "" {
		reportPath = filepath.Join(filepath.Dir(snapshotPath), "verify-"+doc.Round.ID+".json")
	}
	reportStore := snapshot.New(filepath.Dir(reportPath))
	if _, err := reportStore.WriteReport(filepath.Base(reportPath), report); err != nil {
		logger.Error(doc.Round.ID, "business_error", biz.Fields(err))
		return err
	}
	printVerification(report)
	if !report.OK {
		be := verify.Required(report)
		logger.Error(doc.Round.ID, "business_error", biz.Fields(be))
		return be
	}
	logger.Info(doc.Round.ID, "verify_end", map[string]any{"ok": true, "report": reportPath})
	return nil
}

func commandDiff(args []string) error {
	var fromPath, toPath, outPath string
	fs := flag.NewFlagSet("diff", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.StringVar(&fromPath, "from", "", "older snapshot JSON")
	fs.StringVar(&toPath, "to", "", "newer snapshot JSON")
	fs.StringVar(&outPath, "out", "route-diff.json", "diff JSON output")
	if err := fs.Parse(args); err != nil {
		return biz.New(biz.CodeRule, "cli", err.Error())
	}
	if fromPath == "" || toPath == "" {
		return biz.WithField(biz.New(biz.CodeRule, "cli", "-from and -to are required"), "flags")
	}
	fromStore := snapshot.New(filepath.Dir(fromPath))
	from, err := fromStore.LoadDocument(fromPath)
	if err != nil {
		return err
	}
	toStore := snapshot.New(filepath.Dir(toPath))
	to, err := toStore.LoadDocument(toPath)
	if err != nil {
		return err
	}
	report := diff.Compare(from, to)
	store := snapshot.New(filepath.Dir(outPath))
	if _, err := store.WriteReport(filepath.Base(outPath), report); err != nil {
		return err
	}
	fmt.Printf("DIFF from=%s to=%s version_changed=%t changed_routes=%d changed_instances=%d out=%s\n",
		report.FromVersion, report.ToVersion, report.VersionChanged, len(report.ChangedRoutes), len(report.ChangedInstances), outPath)
	for _, change := range report.ChangedRoutes {
		fmt.Printf("ROUTE %s %s\n", change.Change, change.Prefix)
	}
	return nil
}

func commandRecover(args []string) error {
	var checkpointPath, out, logPath string
	fs := flag.NewFlagSet("recover", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.StringVar(&checkpointPath, "checkpoint", "", "checkpoint JSON (default OUT/checkpoint-current.json)")
	fs.StringVar(&out, "out", "out", "snapshot output directory")
	fs.StringVar(&logPath, "log", "", "structured JSONL log file (default OUT/mesh.log)")
	if err := fs.Parse(args); err != nil {
		return biz.New(biz.CodeRule, "cli", err.Error())
	}
	if checkpointPath == "" {
		checkpointPath = filepath.Join(out, "checkpoint-current.json")
	}
	if logPath == "" {
		logPath = filepath.Join(out, "mesh.log")
	}
	logger, err := logx.Open(logPath)
	if err != nil {
		return fmt.Errorf("open log: %w", err)
	}
	defer logger.Close()
	result, err := recovery.Recover(recovery.Options{CheckpointPath: checkpointPath, Store: snapshot.New(out), Logger: logger})
	if err != nil {
		logger.Error("", "business_error", biz.Fields(err))
		return err
	}
	fmt.Printf("RECOVER %s\n", recovery.HumanSummary(result))
	return nil
}

type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}

func commandMock(args []string) error {
	var endpoints multiFlag
	var readyFile string
	fs := flag.NewFlagSet("mock", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.Var(&endpoints, "endpoint", "ADDRESS=MODE:DELAY_MS[:FAIL_COUNT] (repeatable)")
	fs.StringVar(&readyFile, "ready-file", "", "write this file after all listeners are bound")
	if err := fs.Parse(args); err != nil {
		return biz.New(biz.CodeRule, "cli", err.Error())
	}
	if len(endpoints) == 0 {
		return biz.WithField(biz.New(biz.CodeRule, "cli", "at least one -endpoint is required"), "endpoint")
	}
	specs := make([]mockupstream.EndpointSpec, 0, len(endpoints))
	for _, raw := range endpoints {
		spec, err := mockupstream.ParseEndpoint(raw)
		if err != nil {
			return biz.WithField(biz.Wrap(biz.CodeRule, "mock", err.Error(), err), "endpoint")
		}
		specs = append(specs, spec)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return mockupstream.Run(ctx, specs, readyFile)
}

func printVerification(report model.VerificationReport) {
	fmt.Printf("VERIFY %s\n", verify.HumanSummary(report))
	if report.OK {
		fmt.Println("VERIFY_OK")
		return
	}
	if report.FirstDeviation != nil {
		fmt.Printf("E_VERIFY: first deviation request=%q field=%q: %s\n", report.FirstDeviation.RequestID, report.FirstDeviation.Field, report.FirstDeviation.Message)
	} else {
		fmt.Println("E_VERIFY: verification failed")
	}
}

func validInjection(value string) bool {
	switch value {
	case "", "none", "rule", "upstream", "snapshot":
		return true
	default:
		return false
	}
}

func openOptionalLogger(path string) (*logx.Logger, error) {
	if path == "" {
		return logx.NewWriter(io.Discard), nil
	}
	return logx.Open(path)
}
