package docquery

import (
	"bytes"
	"testing"
)

func TestExecutionIsByteStableAndUsesDeterministicIndexPlan(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")

	first, err := session.Execute(query)
	if err != nil {
		t.Fatalf("first execute: %v", err)
	}
	second, err := session.Execute(query)
	if err != nil {
		t.Fatalf("second execute: %v", err)
	}
	firstJSON, err := first.StableJSON()
	if err != nil {
		t.Fatal(err)
	}
	secondJSON, err := second.StableJSON()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(firstJSON, secondJSON) {
		t.Fatalf("repeated execution is not byte-identical")
	}
	if first.Stats != second.Stats {
		t.Fatalf("repeated execution stats differ: %#v != %#v", first.Stats, second.Stats)
	}
	if first.Plan.Path != "index" || first.Plan.IndexPath != "category" {
		t.Fatalf("expected deterministic category index plan, got %#v", first.Plan)
	}
	if first.Plan.Cost.IndexCost != 2*first.Plan.Cost.IndexCandidates+1 {
		t.Fatalf("unexpected index cost formula: %#v", first.Plan.Cost)
	}
	if first.Plan.Cost.IndexCost >= first.Plan.Cost.ScanCost {
		t.Fatalf("expected index cost below scan cost: %#v", first.Plan.Cost)
	}
	if first.Stats.ScanDocuments != 5 || first.Stats.MatchedDocuments != 2 || first.Stats.ReturnedDocuments != 2 {
		t.Fatalf("unexpected stats: %#v", first.Stats)
	}
}

func TestScanAndIndexValidationIsEquivalent(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	report, err := session.Validate(query)
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if !report.Equivalent || report.Degraded || report.ComparedDocuments != 2 {
		t.Fatalf("unexpected report: %#v", report)
	}
	if report.IndexStats.Path != "index" || report.ScanStats.Path != "scan" {
		t.Fatalf("expected both execution paths: %#v", report)
	}
}

func TestORAndINQueryExecutesDeterministically(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/in-list.json")
	result, err := session.Execute(query)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if result.Plan.Path != "scan" {
		t.Fatalf("expected scan plan for OR across different index paths, got %#v", result.Plan)
	}
	if result.Stats.ScanDocuments != 12 || result.Stats.MatchedDocuments != 7 || result.Stats.ReturnedDocuments != 7 {
		t.Fatalf("unexpected OR/IN stats: %#v", result.Stats)
	}
	first, _ := GetField(result.Documents[0], "id")
	last, _ := GetField(result.Documents[len(result.Documents)-1], "id")
	if first != "p004" || last != "p012" {
		t.Fatalf("unexpected sorted ids: first=%v last=%v", first, last)
	}
}

func TestEmptyIndexSetValidationDegradesToScanOnly(t *testing.T) {
	session := newTestSession(t, nil)
	query := mustLoadQuery(t, "queries/hardware.json")
	report, err := session.Validate(query)
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if !report.Degraded || report.DegradedReason != "index_set_empty" || !report.IndexSetEmpty {
		t.Fatalf("unexpected degraded report: %#v", report)
	}
	if report.ScanStats.Path != "scan" || report.IndexPlanID != "" {
		t.Fatalf("expected scan-only validation: %#v", report)
	}
}

func TestVersionChangeInvalidatesStatsAndReplacesWholePlanCache(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	queryA := mustLoadQuery(t, "queries/hardware.json")
	queryB := mustLoadQuery(t, "queries/in-list.json")
	if _, err := session.Execute(queryA); err != nil {
		t.Fatal(err)
	}
	if _, err := session.Execute(queryB); err != nil {
		t.Fatal(err)
	}
	beforeStats := session.StatisticsSnapshot()
	if !beforeStats.IsFresh(session.CollectionVersion(), session.IndexSetVersion()) {
		t.Fatal("expected fresh statistics before update")
	}
	status := session.PlanCacheStatus()
	if status.EntryCount != 2 || status.Stale {
		t.Fatalf("unexpected initial cache status: %#v", status)
	}
	oldVersion := session.CollectionVersion()
	if err := session.ApplyPatchFile("samples/products-patch.json"); err != nil {
		t.Fatalf("ApplyPatchFile: %v", err)
	}
	if session.CollectionVersion() == oldVersion {
		t.Fatal("collection version did not change after patch")
	}
	if beforeStats.IsFresh(session.CollectionVersion(), session.IndexSetVersion()) {
		t.Fatal("old statistics snapshot was not invalidated")
	}
	status = session.PlanCacheStatus()
	if !status.Stale || status.EntryCount != 2 {
		t.Fatalf("expected stale cache retaining old entries until lookup: %#v", status)
	}

	result, err := session.Execute(queryA)
	if err != nil {
		t.Fatalf("execute after patch: %v", err)
	}
	status = session.PlanCacheStatus()
	if status.Stale || status.EntryCount != 1 || status.QueryIDs[0] != queryA.ID() {
		t.Fatalf("expected whole cache replacement with only query A: %#v", status)
	}
	if !session.StatisticsSnapshot().IsFresh(session.CollectionVersion(), session.IndexSetVersion()) {
		t.Fatal("new statistics snapshot is not fresh")
	}
	if result.Stats.MatchedDocuments != 3 {
		t.Fatalf("expected three matching documents after patch, got %#v", result.Stats)
	}
}

func TestCacheStaleInjectionReturnsECache(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	if _, err := session.Execute(query); err != nil {
		t.Fatal(err)
	}
	if err := session.ApplyPatchFile("samples/products-patch.json"); err != nil {
		t.Fatal(err)
	}
	session.SetInjections(true, false)
	_, err := session.Execute(query)
	business := requireBusinessCode(t, err, CodeCache)
	if business.Expected == "" || business.Actual == "" {
		t.Fatalf("cache error is missing generation details: %#v", business)
	}
}

func TestIndexPathMismatchReturnsEPlanWithFirstDifferentPrimaryKey(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	index := session.indexes.indexes["category"]
	key, _ := canonicalScalarKey("hardware")
	entry := index.entries[key]
	if entry == nil {
		t.Fatal("expected hardware index entry")
	}
	entry.docIDs = []int{0}

	report, err := session.Validate(query)
	business := requireBusinessCode(t, err, CodePlan)
	if business.FirstDiffKey != "p002" {
		t.Fatalf("expected first diff primary key p002, got %q", business.FirstDiffKey)
	}
	if report == nil || report.Equivalent {
		t.Fatalf("expected non-equivalent report: %#v", report)
	}
}
