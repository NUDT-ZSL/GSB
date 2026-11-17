package main

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"configledger/internal/applog"
	"configledger/internal/domain"
	"configledger/internal/engine"
	"configledger/internal/spec"
	"configledger/internal/store"
)

const defaultCapacity = int64(8192)

type optionsFile struct {
	Session          *string `json:"session"`
	Capacity         *int64  `json:"capacity"`
	StorageUnitPrice *int64  `json:"storage_unit_price"`
	CompileUnitPrice *int64  `json:"compile_unit_price"`
}

type costReport struct {
	DeliveryEntries  int64  `json:"delivery_entries"`
	CompileWork      int64  `json:"compile_work"`
	CacheBytes       int64  `json:"cache_bytes"`
	StorageUnitPrice int64  `json:"storage_unit_price"`
	CompileUnitPrice int64  `json:"compile_unit_price"`
	StorageCost      int64  `json:"storage_cost"`
	CompileCost      int64  `json:"compile_cost"`
	TotalCost        int64  `json:"total_cost"`
	CostSource       string `json:"cost_source"`
	InterfaceVersion string `json:"interface_version"`
	EstimatorVersion string `json:"estimator_version"`
}

type applyReport struct {
	OK                   bool                  `json:"ok"`
	Command              string                `json:"command"`
	SessionID            string                `json:"session_id"`
	ManifestVersion      string                `json:"manifest_version"`
	GraphVersion         string                `json:"graph_version"`
	Snapshot             store.SnapshotMeta    `json:"snapshot"`
	VersionDiff          domain.VersionDiff    `json:"version_diff"`
	Invalidated          []string              `json:"invalidated"`
	Affected             []string              `json:"affected"`
	Reused               []string              `json:"reused"`
	Recompiled           []string              `json:"recompiled"`
	Dropped              []string              `json:"dropped"`
	Decisions            []domain.Decision     `json:"decisions"`
	Plan                 []domain.PlanItem     `json:"plan"`
	Delivery             []domain.NodeDelivery `json:"delivery"`
	Cost                 costReport            `json:"cost"`
	ProductVersions      map[string]int64      `json:"product_versions"`
	LedgerCheck          string                `json:"ledger_check"`
	ReplayCheck          string                `json:"replay_check"`
	RecoveryInstructions string                `json:"recovery"`
}

type verifyReport struct {
	OK              bool     `json:"ok"`
	Command         string   `json:"command"`
	SessionID       string   `json:"session_id"`
	ManifestVersion string   `json:"manifest_version"`
	GraphVersion    string   `json:"graph_version"`
	SnapshotID      int64    `json:"snapshot_id"`
	Result          string   `json:"result"`
	Checked         []string `json:"checked"`
}

type replayReport struct {
	OK               bool                `json:"ok"`
	Command          string              `json:"command"`
	SessionID        string              `json:"session_id"`
	ManifestVersion  string              `json:"manifest_version"`
	Snapshot         store.SnapshotMeta  `json:"snapshot"`
	PreviousSnapshot *store.SnapshotMeta `json:"previous_snapshot,omitempty"`
	Result           string              `json:"result"`
	Compared         []string            `json:"compared"`
}

type restoreReport struct {
	OK          bool                 `json:"ok"`
	Command     string               `json:"command"`
	SessionID   string               `json:"session_id"`
	Replacement engine.RestoreReport `json:"replacement"`
	RestoredTo  store.SnapshotMeta   `json:"restored_to"`
	ReplayCheck string               `json:"replay_check"`
}

type versionsReport struct {
	OK       bool                 `json:"ok"`
	Command  string               `json:"command"`
	Session  string               `json:"session_id"`
	Versions []store.SnapshotMeta `json:"versions"`
}

type errorResponse struct {
	OK    bool                  `json:"ok"`
	Error *domain.BusinessError `json:"error"`
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		writeError(stderr, domain.BizError(domain.CodeSpec, "command", "usage: ledger <apply|verify|replay|restore|versions> [flags]"))
		return 2
	}
	switch args[0] {
	case "apply":
		return runApply(args[1:], stdout, stderr)
	case "verify":
		return runVerify(args[1:], stdout, stderr)
	case "replay":
		return runReplay(args[1:], stdout, stderr)
	case "restore":
		return runRestore(args[1:], stdout, stderr)
	case "versions":
		return runVersions(args[1:], stdout, stderr)
	case "help", "-h", "--help":
		fmt.Fprintln(stdout, "ledger commands: apply, verify, replay, restore, versions")
		return 0
	default:
		writeError(stderr, domain.BizError(domain.CodeSpec, "command", "unknown command "+args[0]))
		return 2
	}
}

func runApply(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("apply", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	specPath := fs.String("spec", "", "path to manifest JSON")
	dbPath := fs.String("db", "data/ledger.db", "SQLite database path")
	session := fs.String("session", "demo", "session id")
	capacity := fs.Int64("capacity", defaultCapacity, "cache capacity in integer bytes")
	storagePrice := fs.Int64("storage-unit-price", 2, "integer storage unit price")
	compilePrice := fs.Int64("compile-unit-price", 5, "integer compile unit price")
	optionsPath := fs.String("options", "", "optional JSON file with session/capacity/prices")
	inject := fs.String("inject", "", "fault injection: dependency-cycle, corrupt-spec, or zero-capacity")
	logPath := fs.String("log", "", "structured JSONL log path")
	if berr := parseFlags(fs, args); berr != nil {
		writeError(stderr, berr)
		return 2
	}
	if *specPath == "" {
		writeError(stderr, domain.BizError(domain.CodeSpec, "spec", "--spec is required"))
		return 2
	}
	visited := visitedFlags(fs)
	if *optionsPath != "" {
		options, err := readOptions(*optionsPath)
		if err != nil {
			writeError(stderr, domain.BizError(domain.CodeSpec, *optionsPath, err.Error()))
			return 1
		}
		if options.Session != nil && !visited["session"] {
			*session = *options.Session
		}
		if options.Capacity != nil && !visited["capacity"] {
			*capacity = *options.Capacity
		}
		if options.StorageUnitPrice != nil && !visited["storage-unit-price"] {
			*storagePrice = *options.StorageUnitPrice
		}
		if options.CompileUnitPrice != nil && !visited["compile-unit-price"] {
			*compilePrice = *options.CompileUnitPrice
		}
	}
	if *logPath == "" {
		*logPath = *dbPath + ".log.jsonl"
	}
	logger, err := applog.New(*logPath)
	if err != nil {
		writeError(stderr, domain.BizError(domain.CodeStore, "log", err.Error()))
		return 1
	}
	defer logger.Close()

	injection, berr := normalizeInjection(*inject)
	if berr != nil {
		return fail(logger, stderr, "apply", berr)
	}
	if injection != "" {
		logger.Event("info", "fault.injected", map[string]any{"command": "apply", "injection": injection, "spec": *specPath})
	}
	if injection == "zero-capacity" {
		*capacity = 0
	}
	manifest, graph, berr := spec.LoadFile(*specPath, spec.LoadOptions{
		InjectDependencyCycle: injection == "dependency-cycle",
		InjectCorruptSpec:     injection == "corrupt-spec",
		AllowDanglingRefs:     true,
	})
	if berr != nil {
		return fail(logger, stderr, "apply", berr)
	}
	database, err := store.Open(*dbPath)
	if err != nil {
		return fail(logger, stderr, "apply", domain.BizError(domain.CodeStore, "database", err.Error()))
	}
	defer database.Close()
	previous, _, err := database.LoadActive(*session)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return fail(logger, stderr, "apply", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	if errors.Is(err, sql.ErrNoRows) {
		previous = nil
	}
	options := engine.Options{SessionID: *session, Capacity: *capacity, StorageUnitPrice: *storagePrice, CompileUnitPrice: *compilePrice}
	state, berr := engine.Apply(previous, manifest, graph, options)
	if berr != nil {
		return fail(logger, stderr, "apply", berr)
	}
	if berr := engine.Verify(state, previous); berr != nil {
		return fail(logger, stderr, "apply", berr)
	}
	if berr := engine.Replay(state, previous); berr != nil {
		return fail(logger, stderr, "apply", berr)
	}
	meta, err := database.Save(state)
	if err != nil {
		return fail(logger, stderr, "apply", domain.BizError(domain.CodeStore, "database", err.Error()))
	}
	versions := make(map[string]int64, len(state.Products))
	for _, product := range state.Products {
		versions[product.Key] = product.ProductVersion
	}
	report := applyReport{
		OK:                   true,
		Command:              "apply",
		SessionID:            state.SessionID,
		ManifestVersion:      state.ManifestVersion,
		GraphVersion:         state.GraphVersion,
		Snapshot:             meta,
		VersionDiff:          state.Ledger.Diff,
		Invalidated:          state.Ledger.Invalidated,
		Affected:             state.Ledger.Affected,
		Reused:               state.Ledger.Reused,
		Recompiled:           state.Ledger.Recompiled,
		Dropped:              state.Ledger.Dropped,
		Decisions:            state.Ledger.Decisions,
		Plan:                 state.Ledger.Plan,
		Delivery:             state.Ledger.Delivery,
		Cost:                 makeCostReport(state.Ledger),
		ProductVersions:      versions,
		LedgerCheck:          "passed",
		ReplayCheck:          "passed",
		RecoveryInstructions: "reopen the same SQLite file and run restore/replay for this session",
	}
	logger.Event("info", "apply.completed", map[string]any{
		"session":          state.SessionID,
		"manifest_version": state.ManifestVersion,
		"graph_version":    state.GraphVersion,
		"snapshot_id":      meta.ID,
		"sequence":         meta.Sequence,
		"invalidated":      state.Ledger.Invalidated,
		"total_cost":       state.Ledger.TotalCost,
	})
	writeJSON(stdout, report)
	return 0
}

func runVerify(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dbPath := fs.String("db", "data/ledger.db", "SQLite database path")
	session := fs.String("session", "demo", "session id")
	logPath := fs.String("log", "", "structured JSONL log path")
	if berr := parseFlags(fs, args); berr != nil {
		writeError(stderr, berr)
		return 2
	}
	if *logPath == "" {
		*logPath = *dbPath + ".log.jsonl"
	}
	logger, err := applog.New(*logPath)
	if err != nil {
		writeError(stderr, domain.BizError(domain.CodeStore, "log", err.Error()))
		return 1
	}
	defer logger.Close()
	database, state, meta, berr := openActive(*dbPath, *session)
	if berr != nil {
		return fail(logger, stderr, "verify", berr)
	}
	defer database.Close()
	previous, _, err := database.Previous(meta)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return fail(logger, stderr, "verify", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	if errors.Is(err, sql.ErrNoRows) {
		previous = nil
	}
	if berr := engine.Verify(state, previous); berr != nil {
		return fail(logger, stderr, "verify", berr)
	}
	report := verifyReport{
		OK:              true,
		Command:         "verify",
		SessionID:       state.SessionID,
		ManifestVersion: state.ManifestVersion,
		GraphVersion:    state.GraphVersion,
		SnapshotID:      meta.ID,
		Result:          "consistent",
		Checked:         []string{"product_versions", "invalidated_set", "reused_set", "node_delivery", "plan", "cache_bytes", "ledger_costs"},
	}
	logger.Event("info", "verify.completed", map[string]any{"session": state.SessionID, "manifest_version": state.ManifestVersion, "snapshot_id": meta.ID})
	writeJSON(stdout, report)
	return 0
}

func runReplay(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dbPath := fs.String("db", "data/ledger.db", "SQLite database path")
	session := fs.String("session", "demo", "session id")
	logPath := fs.String("log", "", "structured JSONL log path")
	if berr := parseFlags(fs, args); berr != nil {
		writeError(stderr, berr)
		return 2
	}
	if *logPath == "" {
		*logPath = *dbPath + ".log.jsonl"
	}
	logger, err := applog.New(*logPath)
	if err != nil {
		writeError(stderr, domain.BizError(domain.CodeStore, "log", err.Error()))
		return 1
	}
	defer logger.Close()
	database, state, meta, berr := openActive(*dbPath, *session)
	if berr != nil {
		return fail(logger, stderr, "replay", berr)
	}
	defer database.Close()
	previous, previousMeta, err := database.Previous(meta)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return fail(logger, stderr, "replay", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	if errors.Is(err, sql.ErrNoRows) {
		previous = nil
	}
	if berr := engine.Replay(state, previous); berr != nil {
		return fail(logger, stderr, "replay", berr)
	}
	report := replayReport{
		OK:              true,
		Command:         "replay",
		SessionID:       state.SessionID,
		ManifestVersion: state.ManifestVersion,
		Snapshot:        meta,
		Result:          "reproducible",
		Compared:        []string{"version_diff", "invalidated_set", "affected_set", "cache_decisions", "distribution_plan", "product_versions", "ledger_costs"},
	}
	if previousMeta.ID != 0 {
		report.PreviousSnapshot = &previousMeta
	}
	logger.Event("info", "replay.completed", map[string]any{"session": state.SessionID, "manifest_version": state.ManifestVersion, "snapshot_id": meta.ID})
	writeJSON(stdout, report)
	return 0
}

func runRestore(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("restore", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dbPath := fs.String("db", "data/ledger.db", "SQLite database path")
	session := fs.String("session", "demo", "session id")
	version := fs.String("version", "", "manifest version to restore")
	logPath := fs.String("log", "", "structured JSONL log path")
	if berr := parseFlags(fs, args); berr != nil {
		writeError(stderr, berr)
		return 2
	}
	if *version == "" {
		writeError(stderr, domain.BizError(domain.CodeSpec, "version", "--version is required"))
		return 2
	}
	if *logPath == "" {
		*logPath = *dbPath + ".log.jsonl"
	}
	logger, err := applog.New(*logPath)
	if err != nil {
		writeError(stderr, domain.BizError(domain.CodeStore, "log", err.Error()))
		return 1
	}
	defer logger.Close()
	database, err := store.Open(*dbPath)
	if err != nil {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "database", err.Error()))
	}
	defer database.Close()
	target, targetMeta, err := database.LoadVersion(*session, *version)
	if errors.Is(err, sql.ErrNoRows) {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "snapshot", "manifest version not found: "+*version))
	}
	if err != nil {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	previous, _, err := database.Previous(targetMeta)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	if errors.Is(err, sql.ErrNoRows) {
		previous = nil
	}
	current, _, err := database.LoadActive(*session)
	if errors.Is(err, sql.ErrNoRows) {
		current = nil
	} else if err != nil {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	if berr := engine.Replay(target, previous); berr != nil {
		return fail(logger, stderr, "restore", berr)
	}
	if berr := engine.Verify(target, previous); berr != nil {
		return fail(logger, stderr, "restore", berr)
	}
	report := engine.BuildRestoreReport(current, target)
	if err := database.SetActive(targetMeta); err != nil {
		return fail(logger, stderr, "restore", domain.BizError(domain.CodeStore, "active_snapshot", err.Error()))
	}
	logger.Event("info", "restore.completed", map[string]any{
		"session":           *session,
		"from_version":      report.FromVersion,
		"to_version":        report.ToVersion,
		"whole_replaced":    report.WholeReplaced,
		"affected_entries":  report.AffectedEntries,
		"restored_snapshot": targetMeta.ID,
	})
	writeJSON(stdout, restoreReport{
		OK:          true,
		Command:     "restore",
		SessionID:   *session,
		Replacement: report,
		RestoredTo:  targetMeta,
		ReplayCheck: "passed",
	})
	return 0
}

func runVersions(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("versions", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dbPath := fs.String("db", "data/ledger.db", "SQLite database path")
	session := fs.String("session", "demo", "session id")
	logPath := fs.String("log", "", "structured JSONL log path")
	if berr := parseFlags(fs, args); berr != nil {
		writeError(stderr, berr)
		return 2
	}
	if *logPath == "" {
		*logPath = *dbPath + ".log.jsonl"
	}
	logger, err := applog.New(*logPath)
	if err != nil {
		writeError(stderr, domain.BizError(domain.CodeStore, "log", err.Error()))
		return 1
	}
	defer logger.Close()
	database, err := store.Open(*dbPath)
	if err != nil {
		return fail(logger, stderr, "versions", domain.BizError(domain.CodeStore, "database", err.Error()))
	}
	defer database.Close()
	versions, err := database.ListVersions(*session)
	if err != nil {
		return fail(logger, stderr, "versions", domain.BizError(domain.CodeStore, "snapshot", err.Error()))
	}
	writeJSON(stdout, versionsReport{OK: true, Command: "versions", Session: *session, Versions: versions})
	return 0
}

func openActive(dbPath, session string) (*store.Store, *domain.State, store.SnapshotMeta, *domain.BusinessError) {
	database, err := store.Open(dbPath)
	if err != nil {
		return nil, nil, store.SnapshotMeta{}, domain.BizError(domain.CodeStore, "database", err.Error())
	}
	state, meta, err := database.LoadActive(session)
	if errors.Is(err, sql.ErrNoRows) {
		_ = database.Close()
		return nil, nil, store.SnapshotMeta{}, domain.BizError(domain.CodeStore, "snapshot", "no active snapshot for session "+session)
	}
	if err != nil {
		_ = database.Close()
		return nil, nil, store.SnapshotMeta{}, domain.BizError(domain.CodeStore, "snapshot", err.Error())
	}
	return database, state, meta, nil
}

func makeCostReport(ledger domain.Ledger) costReport {
	return costReport{
		DeliveryEntries:  ledger.TotalDeliveryEntries,
		CompileWork:      ledger.TotalCompileWork,
		CacheBytes:       ledger.CacheBytes,
		StorageUnitPrice: ledger.StorageUnitPrice,
		CompileUnitPrice: ledger.CompileUnitPrice,
		StorageCost:      ledger.StorageCost,
		CompileCost:      ledger.CompileCost,
		TotalCost:        ledger.TotalCost,
		CostSource:       ledger.CostSource,
		InterfaceVersion: ledger.InterfaceVersion,
		EstimatorVersion: ledger.EstimatorVersion,
	}
}

func readOptions(path string) (optionsFile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return optionsFile{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var options optionsFile
	if err := decoder.Decode(&options); err != nil {
		return optionsFile{}, fmt.Errorf("invalid options JSON: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return optionsFile{}, errors.New("options file contains trailing JSON")
		}
		return optionsFile{}, fmt.Errorf("invalid trailing options JSON: %w", err)
	}
	return options, nil
}

func parseFlags(fs *flag.FlagSet, args []string) *domain.BusinessError {
	if err := fs.Parse(args); err != nil {
		return domain.BizError(domain.CodeSpec, "flags", err.Error())
	}
	if fs.NArg() != 0 {
		return domain.BizError(domain.CodeSpec, "flags", "unexpected positional arguments: "+fmt.Sprint(fs.Args()))
	}
	return nil
}

func visitedFlags(fs *flag.FlagSet) map[string]bool {
	result := make(map[string]bool)
	fs.Visit(func(f *flag.Flag) { result[f.Name] = true })
	return result
}

func normalizeInjection(value string) (string, *domain.BusinessError) {
	switch value {
	case "", "dependency-cycle", "corrupt-spec", "zero-capacity":
		return value, nil
	default:
		return "", domain.BizError(domain.CodeSpec, "inject", "unsupported injection "+value)
	}
}

func fail(logger *applog.Logger, stderr io.Writer, command string, berr *domain.BusinessError) int {
	if logger != nil {
		fields := map[string]any{"command": command, "code": berr.Code, "item": berr.Item, "reason": berr.Reason}
		if berr.Node != "" {
			fields["node"] = berr.Node
		}
		if len(berr.Refs) > 0 {
			fields["references"] = berr.Refs
		}
		logger.Event("error", "command.failed", fields)
	}
	writeError(stderr, berr)
	return 1
}

func writeError(writer io.Writer, berr *domain.BusinessError) {
	writeJSON(writer, errorResponse{OK: false, Error: berr})
}

func writeJSON(writer io.Writer, value any) {
	encoder := json.NewEncoder(writer)
	encoder.SetIndent("", "  ")
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
}
