package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"docquery"
)

const (
	defaultCollection = "samples/products.json"
	defaultQuery      = "queries/hardware.json"
	defaultPatch      = "samples/products-patch.json"
	defaultCorrupt    = "samples/corrupt-products.json"
	defaultIndexes    = "category,price,stock.warehouse"
	defaultDemoSnap   = "artifacts/demo-session.json"
	defaultInjectSnap = "artifacts/injection-session.json"
)

type commandOutput struct {
	Status   string                     `json:"status"`
	Code     string                     `json:"code"`
	Result   json.RawMessage            `json:"result,omitempty"`
	Report   *docquery.ValidationReport `json:"report,omitempty"`
	Restore  *docquery.RestoreReport    `json:"restore,omitempty"`
	Snapshot string                     `json:"snapshot,omitempty"`
	Error    *docquery.BusinessError    `json:"error,omitempty"`
}

type stageReport struct {
	Stage                 string          `json:"stage"`
	Code                  string          `json:"code"`
	QueryID               string          `json:"query_id,omitempty"`
	CollectionVersion     string          `json:"collection_version,omitempty"`
	IndexSetVersion       string          `json:"index_set_version,omitempty"`
	PlanPath              string          `json:"plan_path,omitempty"`
	IndexPath             string          `json:"index_path,omitempty"`
	ScanDocuments         int             `json:"scan_documents,omitempty"`
	MatchedDocuments      int             `json:"matched_documents,omitempty"`
	ReturnedDocuments     int             `json:"returned_documents,omitempty"`
	ResultSummary         string          `json:"result_summary,omitempty"`
	RepeatByteIdentical   *bool           `json:"repeat_byte_identical,omitempty"`
	PlanCacheStale        *bool           `json:"plan_cache_stale,omitempty"`
	PlanCacheEntries      *int            `json:"plan_cache_entries,omitempty"`
	CacheRecompiled       *bool           `json:"cache_recompiled,omitempty"`
	StatisticsFresh       *bool           `json:"statistics_fresh,omitempty"`
	StatisticsInvalidated *bool           `json:"previous_statistics_invalidated,omitempty"`
	Snapshot              string          `json:"snapshot,omitempty"`
	VersionMatched        *bool           `json:"version_matched,omitempty"`
	StateReplaced         *bool           `json:"state_replaced,omitempty"`
	ReplacementMode       string          `json:"replacement_mode,omitempty"`
	ReplayedQueries       *int            `json:"replayed_queries,omitempty"`
	SkippedStaleHistory   *int            `json:"skipped_stale_history,omitempty"`
	SummaryMatched        *bool           `json:"summary_matched,omitempty"`
	Result                json.RawMessage `json:"result,omitempty"`
}

type injectionReport struct {
	Injection string                  `json:"injection"`
	Triggered bool                    `json:"triggered"`
	Code      string                  `json:"code"`
	Message   string                  `json:"message,omitempty"`
	Snapshot  string                  `json:"snapshot,omitempty"`
	Error     *docquery.BusinessError `json:"error,omitempty"`
}

type nopCloser struct{}

func (nopCloser) Close() error { return nil }

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		printUsage(stderr)
		return 2
	}
	switch args[0] {
	case "demo":
		return runDemo(args[1:], stdout, stderr)
	case "query":
		return runQuery(args[1:], stdout, stderr)
	case "validate":
		return runValidate(args[1:], stdout, stderr)
	case "snapshot":
		return runSnapshot(args[1:], stdout, stderr)
	case "inject":
		return runInject(args[1:], stdout, stderr)
	case "help", "-h", "--help":
		printUsage(stdout)
		return 0
	default:
		fmt.Fprintf(stderr, "unknown command %q\n", args[0])
		printUsage(stderr)
		return 2
	}
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "docquery - deterministic local document query engine")
	fmt.Fprintln(w, "")
	fmt.Fprintln(w, "Usage:")
	fmt.Fprintln(w, "  docquery demo      [-root .] [-collection FILE] [-query FILE] [-patch FILE] [-indexes LIST] [-snapshot FILE] [-log FILE]")
	fmt.Fprintln(w, "  docquery query     [-root .] [-collection FILE] [-query FILE] [-indexes LIST] [-mode auto|scan|index] [-log FILE]")
	fmt.Fprintln(w, "  docquery validate  [-root .] [-collection FILE] [-query FILE] [-indexes LIST] [-log FILE]")
	fmt.Fprintln(w, "  docquery snapshot  -action save|restore [-root .] [-collection FILE] [-query FILE] [-indexes LIST] -name FILE [-log FILE]")
	fmt.Fprintln(w, "  docquery inject    -kind doc-corruption|cache-stale|replay-mismatch [-root .] [-collection FILE] [-query FILE] [-patch FILE] [-indexes LIST] [-snapshot FILE] [-log FILE]")
}

func runDemo(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("demo", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	collectionPath := fs.String("collection", defaultCollection, "collection JSON file")
	queryPath := fs.String("query", defaultQuery, "query JSON file")
	patchPath := fs.String("patch", defaultPatch, "incremental patch JSON file")
	indexSpec := fs.String("indexes", defaultIndexes, "comma-separated index paths")
	snapshotPath := fs.String("snapshot", defaultDemoSnap, "snapshot output file")
	logPath := fs.String("log", "artifacts/demo.log", "structured JSONL log file; empty disables logging")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger, closer, err := openLogger(*root, *logPath)
	if err != nil {
		fmt.Fprintf(stderr, "open log: %v\n", err)
		return 1
	}
	defer closer.Close()

	collection, err := docquery.LoadCollection(resolve(*root, *collectionPath))
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	session, err := docquery.NewSession(collection, docquery.SessionOptions{
		IndexPaths: parseIndexes(*indexSpec),
		Logger:     logger,
	})
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	query, err := loadQuery(resolve(*root, *queryPath))
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeQuery, nil)
	}
	initialCollection := session.Collection()

	first, err := session.Execute(query)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	second, err := session.Execute(query)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	firstJSON, err := first.StableJSON()
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	secondJSON, err := second.StableJSON()
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	identical := bytes.Equal(firstJSON, secondJSON)
	if !identical {
		return emitError(stdout, logger, docquery.AsBusinessError(docquery.CodePlan, fmt.Errorf("repeated query bytes differ")), docquery.CodePlan, nil)
	}
	stageOne := stageReport{
		Stage:               "1_initial_full_query",
		Code:                docquery.CodeOK,
		QueryID:             first.QueryID,
		CollectionVersion:   first.CollectionVersion,
		IndexSetVersion:     first.IndexSetVersion,
		PlanPath:            first.Plan.Path,
		IndexPath:           first.Plan.IndexPath,
		ScanDocuments:       first.Stats.ScanDocuments,
		MatchedDocuments:    first.Stats.MatchedDocuments,
		ReturnedDocuments:   first.Stats.ReturnedDocuments,
		ResultSummary:       first.Summary,
		RepeatByteIdentical: boolPtr(identical),
		Result:              json.RawMessage(firstJSON),
	}
	if code := writePretty(stdout, stageOne); code != 0 {
		return code
	}

	beforeCache := session.PlanCacheStatus()
	beforeStats := session.StatisticsSnapshot()
	if err := session.ApplyPatchFile(resolve(*root, *patchPath)); err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	staleAfterPatch := session.PlanCacheStatus()
	incremental, err := session.Execute(query)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	incrementalJSON, err := incremental.StableJSON()
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	afterCache := session.PlanCacheStatus()
	statsFreshAfter := session.StatisticsSnapshot().IsFresh(session.CollectionVersion(), session.IndexSetVersion())
	statisticsInvalidated := !beforeStats.IsFresh(session.CollectionVersion(), session.IndexSetVersion())
	cacheRecompiled := false
	for _, record := range session.Trace() {
		if record.Event == "query_executed" {
			cacheRecompiled = record.CacheRecompiled
		}
	}
	stageTwo := stageReport{
		Stage:                 "2_incremental_update_query",
		Code:                  docquery.CodeOK,
		QueryID:               incremental.QueryID,
		CollectionVersion:     incremental.CollectionVersion,
		IndexSetVersion:       incremental.IndexSetVersion,
		PlanPath:              incremental.Plan.Path,
		IndexPath:             incremental.Plan.IndexPath,
		ScanDocuments:         incremental.Stats.ScanDocuments,
		MatchedDocuments:      incremental.Stats.MatchedDocuments,
		ReturnedDocuments:     incremental.Stats.ReturnedDocuments,
		ResultSummary:         incremental.Summary,
		PlanCacheStale:        boolPtr(staleAfterPatch.Stale),
		PlanCacheEntries:      intPtr(afterCache.EntryCount),
		CacheRecompiled:       boolPtr(cacheRecompiled),
		StatisticsFresh:       boolPtr(statsFreshAfter),
		StatisticsInvalidated: boolPtr(statisticsInvalidated),
		Result:                json.RawMessage(incrementalJSON),
	}
	if beforeCache.Stale || !staleAfterPatch.Stale || !statisticsInvalidated || !cacheRecompiled || !statsFreshAfter {
		return emitError(stdout, logger, docquery.AsBusinessError(docquery.CodeCache, fmt.Errorf("cache/statistics invalidation was not observed")), docquery.CodeCache, nil)
	}
	if code := writePretty(stdout, stageTwo); code != 0 {
		return code
	}

	if err := session.SaveSnapshot(resolve(*root, *snapshotPath)); err != nil {
		return emitError(stdout, logger, err, docquery.CodeReplay, nil)
	}
	if err := session.ReplaceCollection(initialCollection); err != nil {
		return emitError(stdout, logger, err, docquery.CodeReplay, nil)
	}
	restoreReport, err := session.RestoreSnapshot(resolve(*root, *snapshotPath))
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeReplay, nil)
	}
	replayed, err := session.Execute(query)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeReplay, nil)
	}
	summaryMatched := replayed.Summary == incremental.Summary
	stageThree := stageReport{
		Stage:               "3_snapshot_restore_replay",
		Code:                docquery.CodeOK,
		QueryID:             replayed.QueryID,
		CollectionVersion:   replayed.CollectionVersion,
		IndexSetVersion:     replayed.IndexSetVersion,
		PlanPath:            replayed.Plan.Path,
		IndexPath:           replayed.Plan.IndexPath,
		ScanDocuments:       replayed.Stats.ScanDocuments,
		MatchedDocuments:    replayed.Stats.MatchedDocuments,
		ReturnedDocuments:   replayed.Stats.ReturnedDocuments,
		ResultSummary:       replayed.Summary,
		Snapshot:            *snapshotPath,
		VersionMatched:      boolPtr(restoreReport.VersionMatched),
		StateReplaced:       boolPtr(restoreReport.StateReplaced),
		ReplacementMode:     restoreReport.ReplacementMode,
		ReplayedQueries:     intPtr(restoreReport.ReplayedQueries),
		SkippedStaleHistory: intPtr(restoreReport.SkippedStaleHistory),
		SummaryMatched:      boolPtr(summaryMatched),
	}
	if !summaryMatched {
		return emitError(stdout, logger, docquery.AsBusinessError(docquery.CodeReplay, fmt.Errorf("summary changed after restore")), docquery.CodeReplay, nil)
	}
	return writePretty(stdout, stageThree)
}

func runQuery(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("query", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	collectionPath := fs.String("collection", defaultCollection, "collection JSON file")
	queryPath := fs.String("query", defaultQuery, "query JSON file")
	indexSpec := fs.String("indexes", defaultIndexes, "comma-separated index paths")
	modeText := fs.String("mode", "auto", "auto, scan, or index")
	logPath := fs.String("log", "", "structured JSONL log file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger, closer, err := openLogger(*root, *logPath)
	if err != nil {
		fmt.Fprintf(stderr, "open log: %v\n", err)
		return 1
	}
	defer closer.Close()
	session, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	query, err := loadQuery(resolve(*root, *queryPath))
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeQuery, nil)
	}
	mode, err := docquery.ParsePathMode(*modeText)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeQuery, nil)
	}
	result, err := session.ExecuteMode(query, mode)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	body, err := result.StableJSON()
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, nil)
	}
	return writePretty(stdout, commandOutput{Status: "ok", Code: docquery.CodeOK, Result: json.RawMessage(body)})
}

func runValidate(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	collectionPath := fs.String("collection", defaultCollection, "collection JSON file")
	queryPath := fs.String("query", defaultQuery, "query JSON file")
	indexSpec := fs.String("indexes", defaultIndexes, "comma-separated index paths")
	logPath := fs.String("log", "artifacts/validate.log", "structured JSONL log file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger, closer, err := openLogger(*root, *logPath)
	if err != nil {
		fmt.Fprintf(stderr, "open log: %v\n", err)
		return 1
	}
	defer closer.Close()
	session, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	query, err := loadQuery(resolve(*root, *queryPath))
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeQuery, nil)
	}
	report, err := session.Validate(query)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodePlan, report)
	}
	return writePretty(stdout, commandOutput{Status: "ok", Code: docquery.CodeOK, Report: report})
}

func runSnapshot(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("snapshot", flag.ContinueOnError)
	fs.SetOutput(stderr)
	action := fs.String("action", "", "save or restore")
	root := fs.String("root", ".", "workspace root")
	collectionPath := fs.String("collection", defaultCollection, "collection JSON file")
	queryPath := fs.String("query", "", "optional query to execute before save or after restore")
	indexSpec := fs.String("indexes", defaultIndexes, "comma-separated index paths")
	name := fs.String("name", "artifacts/session.json", "snapshot file")
	logPath := fs.String("log", "", "structured JSONL log file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger, closer, err := openLogger(*root, *logPath)
	if err != nil {
		fmt.Fprintf(stderr, "open log: %v\n", err)
		return 1
	}
	defer closer.Close()
	session, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
	if err != nil {
		return emitError(stdout, logger, err, docquery.CodeDoc, nil)
	}
	var query *docquery.Query
	if strings.TrimSpace(*queryPath) != "" {
		query, err = loadQuery(resolve(*root, *queryPath))
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeQuery, nil)
		}
	}
	switch strings.ToLower(strings.TrimSpace(*action)) {
	case "save":
		if query != nil {
			if _, err := session.Execute(query); err != nil {
				return emitError(stdout, logger, err, docquery.CodePlan, nil)
			}
		}
		if err := session.SaveSnapshot(resolve(*root, *name)); err != nil {
			return emitError(stdout, logger, err, docquery.CodeReplay, nil)
		}
		return writePretty(stdout, commandOutput{Status: "ok", Code: docquery.CodeOK, Snapshot: *name})
	case "restore":
		report, err := session.RestoreSnapshot(resolve(*root, *name))
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeReplay, report)
		}
		output := commandOutput{Status: "ok", Code: docquery.CodeOK, Restore: report, Snapshot: *name}
		if query != nil {
			result, err := session.Execute(query)
			if err != nil {
				return emitError(stdout, logger, err, docquery.CodePlan, nil)
			}
			body, err := result.StableJSON()
			if err != nil {
				return emitError(stdout, logger, err, docquery.CodePlan, nil)
			}
			output.Result = json.RawMessage(body)
		}
		return writePretty(stdout, output)
	default:
		return emitError(stdout, logger, docquery.AsBusinessError(docquery.CodeReplay, fmt.Errorf("snapshot action must be save or restore")), docquery.CodeReplay, nil)
	}
}

func runInject(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("inject", flag.ContinueOnError)
	fs.SetOutput(stderr)
	kind := fs.String("kind", "", "doc-corruption, cache-stale, or replay-mismatch")
	root := fs.String("root", ".", "workspace root")
	collectionPath := fs.String("collection", defaultCollection, "collection JSON file")
	queryPath := fs.String("query", defaultQuery, "query JSON file")
	patchPath := fs.String("patch", defaultPatch, "incremental patch JSON file")
	indexSpec := fs.String("indexes", defaultIndexes, "comma-separated index paths")
	corruptPath := fs.String("corrupt-file", defaultCorrupt, "intentionally corrupt collection file")
	snapshotPath := fs.String("snapshot", defaultInjectSnap, "temporary replay snapshot file")
	logPath := fs.String("log", "artifacts/injection.log", "structured JSONL log file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger, closer, err := openLogger(*root, *logPath)
	if err != nil {
		fmt.Fprintf(stderr, "open log: %v\n", err)
		return 1
	}
	defer closer.Close()

	switch normalizeInjectKind(*kind) {
	case "doc-corruption":
		_, err := docquery.LoadCollection(resolve(*root, *corruptPath))
		report := injectionReport{Injection: "doc-corruption"}
		if err == nil {
			report.Code = docquery.CodeOK
			report.Message = "expected E_DOC but collection loaded successfully"
			return writeInjection(stdout, logger, report)
		}
		return finishInjection(stdout, logger, report, err, docquery.CodeDoc)
	case "cache-stale":
		session, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeDoc, nil)
		}
		query, err := loadQuery(resolve(*root, *queryPath))
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeQuery, nil)
		}
		if _, err := session.Execute(query); err != nil {
			return emitError(stdout, logger, err, docquery.CodePlan, nil)
		}
		if err := session.ApplyPatchFile(resolve(*root, *patchPath)); err != nil {
			return emitError(stdout, logger, err, docquery.CodeDoc, nil)
		}
		session.SetInjections(true, false)
		_, err = session.Execute(query)
		report := injectionReport{Injection: "cache-stale"}
		if err == nil {
			report.Code = docquery.CodeOK
			report.Message = "expected E_CACHE but query succeeded"
			return writeInjection(stdout, logger, report)
		}
		return finishInjection(stdout, logger, report, err, docquery.CodeCache)
	case "replay-mismatch":
		session, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeDoc, nil)
		}
		query, err := loadQuery(resolve(*root, *queryPath))
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeQuery, nil)
		}
		if _, err := session.Execute(query); err != nil {
			return emitError(stdout, logger, err, docquery.CodePlan, nil)
		}
		if err := session.SaveSnapshot(resolve(*root, *snapshotPath)); err != nil {
			return emitError(stdout, logger, err, docquery.CodeReplay, nil)
		}
		restoreSession, err := docquery.OpenSession(resolve(*root, *collectionPath), parseIndexes(*indexSpec), logger)
		if err != nil {
			return emitError(stdout, logger, err, docquery.CodeDoc, nil)
		}
		restoreSession.SetInjections(false, true)
		_, err = restoreSession.RestoreSnapshot(resolve(*root, *snapshotPath))
		report := injectionReport{Injection: "replay-mismatch", Snapshot: *snapshotPath}
		if err == nil {
			report.Code = docquery.CodeOK
			report.Message = "expected E_REPLAY but restore succeeded"
			return writeInjection(stdout, logger, report)
		}
		return finishInjection(stdout, logger, report, err, docquery.CodeReplay)
	default:
		return emitError(stdout, logger, docquery.AsBusinessError(docquery.CodeQuery, fmt.Errorf("unsupported injection kind %q", *kind)), docquery.CodeQuery, nil)
	}
}

func normalizeInjectKind(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "doc", "document", "doc-corruption", "document-corruption":
		return "doc-corruption"
	case "cache", "cache-stale", "plan-cache-stale":
		return "cache-stale"
	case "replay", "replay-mismatch", "result-replay-mismatch":
		return "replay-mismatch"
	default:
		return strings.ToLower(strings.TrimSpace(value))
	}
}

func finishInjection(stdout io.Writer, logger docquery.EventLogger, report injectionReport, err error, expected string) int {
	business := docquery.AsBusinessError(expected, err)
	if business.Code != expected {
		report.Code = business.Code
		report.Message = "unexpected business code: " + business.Message
		report.Error = business
		return writeInjection(stdout, logger, report)
	}
	report.Triggered = true
	report.Code = business.Code
	report.Message = business.Message
	report.Error = business
	fields := map[string]any{
		"injection": report.Injection,
		"message":   business.Message,
	}
	if business.DocIndex != nil {
		fields["doc_index"] = *business.DocIndex
	}
	if business.FieldPath != "" {
		fields["field_path"] = business.FieldPath
	}
	if business.QueryID != "" {
		fields["query_id"] = business.QueryID
	}
	if business.Expected != "" || business.Actual != "" {
		fields["expected"] = business.Expected
		fields["actual"] = business.Actual
	}
	logger.Log("injection_triggered", business.Code, fields)
	return writeInjection(stdout, logger, report)
}

func writeInjection(stdout io.Writer, logger docquery.EventLogger, report injectionReport) int {
	if code := writePretty(stdout, report); code != 0 {
		return code
	}
	if report.Triggered {
		return 0
	}
	return 1
}

func emitError(stdout io.Writer, logger docquery.EventLogger, err error, fallback string, extra any) int {
	business := docquery.AsBusinessError(fallback, err)
	fields := map[string]any{"message": business.Message}
	if business.QueryID != "" {
		fields["query_id"] = business.QueryID
	}
	if business.FirstDiffKey != "" {
		fields["first_diff_key"] = business.FirstDiffKey
	}
	if business.DocIndex != nil {
		fields["doc_index"] = *business.DocIndex
	}
	if business.FieldPath != "" {
		fields["field_path"] = business.FieldPath
	}
	logger.Log("command_failed", business.Code, fields)
	output := commandOutput{Status: "error", Code: business.Code, Error: business}
	switch value := extra.(type) {
	case *docquery.ValidationReport:
		output.Report = value
	case *docquery.RestoreReport:
		output.Restore = value
	}
	if code := writePretty(stdout, output); code != 0 {
		return code
	}
	return 1
}

func openLogger(root, path string) (docquery.EventLogger, io.Closer, error) {
	if strings.TrimSpace(path) == "" {
		return docquery.NewJSONLogger(io.Discard), nopCloser{}, nil
	}
	resolved := resolve(root, path)
	if directory := filepath.Dir(resolved); directory != "." && directory != "" {
		if err := os.MkdirAll(directory, 0o755); err != nil {
			return nil, nopCloser{}, err
		}
	}
	file, err := os.Create(resolved)
	if err != nil {
		return nil, nopCloser{}, err
	}
	return docquery.NewJSONLogger(file), file, nil
}

func resolve(root, path string) string {
	if path == "" || filepath.IsAbs(path) {
		return path
	}
	return filepath.Join(root, path)
}

func parseIndexes(value string) []string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	parts := strings.Split(value, ",")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			result = append(result, part)
		}
	}
	return result
}

func loadQuery(path string) (*docquery.Query, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, docquery.AsBusinessError(docquery.CodeQuery, fmt.Errorf("cannot read query %q: %w", path, err))
	}
	return docquery.ParseQuery(data)
}

func writePretty(w io.Writer, value any) int {
	body, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fmt.Fprintln(w, `{"status":"error","code":"E_QUERY","error":{"code":"E_QUERY","message":"cannot encode output"}}`)
		return 1
	}
	body = append(body, '\n')
	if _, err := w.Write(body); err != nil {
		return 1
	}
	return 0
}

func boolPtr(value bool) *bool { return &value }
func intPtr(value int) *int    { return &value }
