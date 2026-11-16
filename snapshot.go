package docquery

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const snapshotFormat = "docquery.session.v1"

type SnapshotCollection struct {
	Name       string            `json:"name"`
	PrimaryKey string            `json:"primary_key"`
	Documents  []json.RawMessage `json:"documents"`
	Version    string            `json:"version"`
}

// SessionSnapshot is self-contained: it includes collection content and index
// specifications so restore never depends on the current on-disk collection.
type SessionSnapshot struct {
	Format            string               `json:"format"`
	CollectionVersion string               `json:"collection_version"`
	IndexSetVersion   string               `json:"index_set_version"`
	Collection        SnapshotCollection   `json:"collection"`
	IndexSpecs        []string             `json:"index_specs"`
	PlanCache         *PlanCache           `json:"plan_cache"`
	QueryHistory      []HistoryRecord      `json:"query_history"`
	ExecutionTrace    []TraceRecord        `json:"execution_trace"`
	ResultSummaries   []QueryResultSummary `json:"result_summaries"`
	Statistics        StatisticsSnapshot   `json:"statistics"`
}

type RestoreReport struct {
	Snapshot            string `json:"snapshot"`
	VersionMatched      bool   `json:"version_matched"`
	ReplacementMode     string `json:"replacement_mode"`
	StateReplaced       bool   `json:"state_replaced"`
	CollectionVersion   string `json:"collection_version"`
	IndexSetVersion     string `json:"index_set_version"`
	ReplayedQueries     int    `json:"replayed_queries"`
	SkippedStaleHistory int    `json:"skipped_stale_history"`
}

func (s *Session) SaveSnapshot(path string) error {
	if strings.TrimSpace(path) == "" {
		return newBusinessError(CodeReplay, "snapshot path must not be empty")
	}
	collection := SnapshotCollection{
		Name:       s.collection.Name,
		PrimaryKey: s.collection.PrimaryKey,
		Version:    s.collection.Version,
		Documents:  make([]json.RawMessage, 0, len(s.collection.Documents)),
	}
	for _, doc := range s.collection.Documents {
		body, err := json.Marshal(doc)
		if err != nil {
			return newBusinessError(CodeReplay, "cannot serialize snapshot document: %v", err)
		}
		collection.Documents = append(collection.Documents, body)
	}
	summaries := s.resultSummaryList()
	snapshot := SessionSnapshot{
		Format:            snapshotFormat,
		CollectionVersion: s.collection.Version,
		IndexSetVersion:   s.indexes.Version,
		Collection:        collection,
		IndexSpecs:        s.indexes.SpecsCopy(),
		PlanCache:         s.plans.Clone(),
		QueryHistory:      append([]HistoryRecord(nil), s.history...),
		ExecutionTrace:    append([]TraceRecord(nil), s.trace...),
		ResultSummaries:   summaries,
		Statistics:        s.statistics.Clone(),
	}
	body, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return newBusinessError(CodeReplay, "cannot serialize snapshot: %v", err)
	}
	if directory := filepath.Dir(path); directory != "." && directory != "" {
		if err := os.MkdirAll(directory, 0o755); err != nil {
			return newBusinessError(CodeReplay, "cannot create snapshot directory: %v", err)
		}
	}
	if err := os.WriteFile(path, body, 0o644); err != nil {
		return newBusinessError(CodeReplay, "cannot write snapshot %q: %v", path, err)
	}
	s.logger.Log("snapshot_saved", CodeOK, map[string]any{
		"snapshot":           path,
		"collection_version": s.collection.Version,
		"index_set_version":  s.indexes.Version,
		"history_records":    len(s.history),
	})
	return nil
}

func (s *Session) resultSummaryList() []QueryResultSummary {
	values := make([]QueryResultSummary, 0, len(s.resultSummaries))
	for _, summary := range s.resultSummaries {
		values = append(values, summary)
	}
	sort.Slice(values, func(i, j int) bool {
		if values[i].CollectionVersion != values[j].CollectionVersion {
			return values[i].CollectionVersion < values[j].CollectionVersion
		}
		if values[i].IndexSetVersion != values[j].IndexSetVersion {
			return values[i].IndexSetVersion < values[j].IndexSetVersion
		}
		if values[i].QueryID != values[j].QueryID {
			return values[i].QueryID < values[j].QueryID
		}
		return values[i].Mode < values[j].Mode
	})
	return values
}

// RestoreSnapshot compares snapshot versions with the current session, replaces
// the entire collection/index/cache state, then replays every history record
// belonging to the snapshot generation and verifies its stored summary.
func (s *Session) RestoreSnapshot(path string) (*RestoreReport, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, newBusinessError(CodeReplay, "cannot read snapshot %q: %v", path, err)
	}
	raw, err := decodeRawObject(data)
	if err != nil {
		return nil, replayError("$", "invalid snapshot JSON: %v", err)
	}
	normalized, err := normalizeRawKeys(raw)
	if err != nil {
		return nil, replayError("$", "invalid snapshot JSON: %v", err)
	}
	required := []string{"format", "collection_version", "index_set_version", "collection", "index_specs", "plan_cache", "query_history", "execution_trace", "result_summaries", "statistics"}
	for _, key := range required {
		if _, ok := normalized[key]; !ok {
			return nil, replayError(key, "snapshot is missing required field %q", key)
		}
	}
	var snapshot SessionSnapshot
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return nil, replayError("$", "invalid snapshot: %v", err)
	}
	if snapshot.Format != snapshotFormat {
		return nil, replayError("format", "unsupported snapshot format %q", snapshot.Format)
	}
	if strings.TrimSpace(snapshot.CollectionVersion) == "" {
		return nil, replayError("collection_version", "snapshot collection version is missing")
	}
	if strings.TrimSpace(snapshot.IndexSetVersion) == "" {
		return nil, replayError("index_set_version", "snapshot index set version is missing")
	}
	if snapshot.PlanCache == nil {
		return nil, replayError("plan_cache", "snapshot plan cache is missing")
	}
	if strings.TrimSpace(snapshot.PlanCache.CollectionVersion) == "" || strings.TrimSpace(snapshot.PlanCache.IndexSetVersion) == "" {
		return nil, replayError("plan_cache", "snapshot plan cache versions are missing")
	}

	restoredCollection, err := collectionFromSnapshot(snapshot.Collection)
	if err != nil {
		return nil, err
	}
	if restoredCollection.Version != snapshot.Collection.Version {
		return nil, replayError("collection.version", "snapshot collection version checksum does not match content")
	}
	if restoredCollection.Version != snapshot.CollectionVersion {
		return nil, replayError("collection_version", "top-level collection version does not match collection content")
	}
	restoredIndexes, err := NewIndexSet(restoredCollection, snapshot.IndexSpecs)
	if err != nil {
		return nil, replayFromError(err)
	}
	if restoredIndexes.Version != snapshot.IndexSetVersion {
		return nil, replayError("index_set_version", "snapshot index set version does not match rebuilt indexes")
	}
	if snapshot.PlanCache.CollectionVersion != snapshot.CollectionVersion || snapshot.PlanCache.IndexSetVersion != snapshot.IndexSetVersion {
		return nil, replayError("plan_cache", "snapshot plan cache generation does not match snapshot versions")
	}
	if snapshot.PlanCache.Entries == nil {
		snapshot.PlanCache.Entries = make(map[string]Plan)
	}
	for queryID, plan := range snapshot.PlanCache.Entries {
		if plan.QueryID != queryID {
			return nil, replayError("plan_cache.entries", "plan cache key does not match plan query id")
		}
		if plan.CollectionVersion != snapshot.CollectionVersion || plan.IndexSetVersion != snapshot.IndexSetVersion {
			return nil, replayError("plan_cache.entries", "cached plan versions do not match snapshot versions")
		}
		if computePlanID(plan) != plan.ID {
			return nil, replayError("plan_cache.entries", "cached plan id checksum mismatch")
		}
	}
	if !snapshot.Statistics.IsFresh(snapshot.CollectionVersion, snapshot.IndexSetVersion) {
		return nil, replayError("statistics", "snapshot statistics versions are stale or missing")
	}
	expectedIndexStats, _ := json.Marshal(restoredIndexes.Stats())
	actualIndexStats, _ := json.Marshal(snapshot.Statistics.Indexes)
	if string(expectedIndexStats) != string(actualIndexStats) {
		return nil, replayError("statistics.indexes", "snapshot index statistics do not match rebuilt indexes")
	}
	summaries, err := validateSnapshotSummaries(snapshot.ResultSummaries)
	if err != nil {
		return nil, err
	}
	if err := validateSnapshotHistory(snapshot.QueryHistory); err != nil {
		return nil, err
	}
	if err := validateHistorySummaryLinks(snapshot.QueryHistory, summaries); err != nil {
		return nil, err
	}

	previousCollectionVersion := s.collection.Version
	previousIndexSetVersion := s.indexes.Version
	versionMatched := previousCollectionVersion == snapshot.CollectionVersion && previousIndexSetVersion == snapshot.IndexSetVersion
	previousCollection := s.collection
	previousIndexes := s.indexes
	previousPlans := s.plans
	previousHistory := s.history
	previousTrace := s.trace
	previousSummaries := s.resultSummaries
	previousStatistics := s.statistics

	s.collection = restoredCollection
	s.indexes = restoredIndexes
	s.plans = snapshot.PlanCache.Clone()
	s.history = append([]HistoryRecord(nil), snapshot.QueryHistory...)
	s.trace = append([]TraceRecord(nil), snapshot.ExecutionTrace...)
	s.resultSummaries = summaries
	s.statistics = snapshot.Statistics.Clone()

	replayed, skipped, replayErr := s.replayCurrentHistory(summaries)
	report := &RestoreReport{
		Snapshot:            path,
		VersionMatched:      versionMatched,
		StateReplaced:       true,
		CollectionVersion:   restoredCollection.Version,
		IndexSetVersion:     restoredIndexes.Version,
		ReplayedQueries:     replayed,
		SkippedStaleHistory: skipped,
	}
	if versionMatched {
		report.ReplacementMode = "overall_replace_same_version"
	} else {
		report.ReplacementMode = "overall_replace"
	}
	if replayErr != nil {
		s.collection = previousCollection
		s.indexes = previousIndexes
		s.plans = previousPlans
		s.history = previousHistory
		s.trace = previousTrace
		s.resultSummaries = previousSummaries
		s.statistics = previousStatistics
		report.StateReplaced = false
		report.ReplacementMode = "rolled_back"
		s.logError(replayErr)
		return report, replayErr
	}
	s.logger.Log("snapshot_restored", CodeOK, map[string]any{
		"snapshot":                    path,
		"version_matched":             versionMatched,
		"replacement_mode":            report.ReplacementMode,
		"previous_collection_version": previousCollectionVersion,
		"previous_index_set_version":  previousIndexSetVersion,
		"collection_version":          restoredCollection.Version,
		"index_set_version":           restoredIndexes.Version,
		"replayed_queries":            replayed,
		"skipped_stale_history":       skipped,
	})
	return report, nil
}

func replayError(path, format string, args ...any) *BusinessError {
	e := newBusinessError(CodeReplay, format, args...)
	e.FieldPath = path
	return e
}

func collectionFromSnapshot(snapshot SnapshotCollection) (*Collection, error) {
	if strings.TrimSpace(snapshot.Name) == "" {
		return nil, replayError("collection.name", "snapshot collection name is missing")
	}
	if strings.TrimSpace(snapshot.PrimaryKey) == "" {
		return nil, replayError("collection.primary_key", "snapshot primary key is missing")
	}
	documents := make([]Document, 0, len(snapshot.Documents))
	for index, raw := range snapshot.Documents {
		value, err := decodeJSON(raw)
		if err != nil {
			return nil, replayError("collection.documents", "invalid snapshot document at index %d: %v", index, err)
		}
		object, ok := value.(map[string]any)
		if !ok {
			return nil, replayError("collection.documents", "snapshot document at index %d is not an object", index)
		}
		if shapeErr := validateDocumentShape(object, index, ""); shapeErr != nil {
			converted := *shapeErr
			converted.Code = CodeReplay
			return nil, &converted
		}
		documents = append(documents, Document(object))
	}
	collection, err := NewCollection(snapshot.Name, snapshot.PrimaryKey, documents)
	if err != nil {
		business := AsBusinessError(CodeReplay, err)
		business.Code = CodeReplay
		return nil, business
	}
	if snapshot.Version != "" && snapshot.Version != collection.Version {
		return nil, replayError("collection.version", "snapshot collection version checksum does not match content")
	}
	return collection, nil
}

func validateSnapshotSummaries(values []QueryResultSummary) (map[string]QueryResultSummary, error) {
	result := make(map[string]QueryResultSummary, len(values))
	for _, summary := range values {
		if summary.QueryID == "" || summary.CollectionVersion == "" || summary.IndexSetVersion == "" || summary.Mode == "" || summary.Summary == "" {
			return nil, replayError("result_summaries", "result summary is missing required version or digest fields")
		}
		key := resultSummaryKey(summary.QueryID, summary.CollectionVersion, summary.IndexSetVersion, summary.Mode)
		if previous, exists := result[key]; exists && previous.Summary != summary.Summary {
			return nil, replayError("result_summaries", "conflicting duplicate result summaries for query %s", summary.QueryID)
		}
		result[key] = summary
	}
	return result, nil
}

func validateSnapshotHistory(history []HistoryRecord) error {
	seenSequence := make(map[int]struct{}, len(history))
	for _, record := range history {
		if record.Sequence <= 0 {
			return replayError("query_history", "history record sequence must be positive")
		}
		if _, exists := seenSequence[record.Sequence]; exists {
			return replayError("query_history", "duplicate history sequence %d", record.Sequence)
		}
		seenSequence[record.Sequence] = struct{}{}
		if record.QueryID == "" || record.CanonicalQuery == "" || record.CollectionVersion == "" || record.IndexSetVersion == "" || record.Mode == "" || record.ResultSummary == "" {
			return replayError("query_history", "history record is missing query, version, mode, or summary fields")
		}
		query, err := ParseQuery([]byte(record.CanonicalQuery))
		if err != nil {
			return replayError("query_history", "history contains an invalid canonical query: %v", err)
		}
		if query.ID() != record.QueryID {
			return replayError("query_history", "history query id does not match canonical query text")
		}
	}
	return nil
}

func validateHistorySummaryLinks(history []HistoryRecord, summaries map[string]QueryResultSummary) error {
	for _, record := range history {
		key := resultSummaryKey(record.QueryID, record.CollectionVersion, record.IndexSetVersion, record.Mode)
		summary, ok := summaries[key]
		if !ok {
			return replayError("result_summaries", "missing result summary for history query %s", record.QueryID)
		}
		if summary.Summary != record.ResultSummary {
			return replayError("result_summaries", "history summary does not match result summary for query %s", record.QueryID)
		}
	}
	return nil
}

func (s *Session) replayCurrentHistory(summaries map[string]QueryResultSummary) (int, int, error) {
	replayCache := s.plans.Clone()
	replayed := 0
	skipped := 0
	for _, record := range s.history {
		if record.CollectionVersion != s.collection.Version || record.IndexSetVersion != s.indexes.Version {
			skipped++
			continue
		}
		summary, ok := summaries[resultSummaryKey(record.QueryID, record.CollectionVersion, record.IndexSetVersion, record.Mode)]
		if !ok {
			return replayed, skipped, replayError("result_summaries", "missing result summary for query %s", record.QueryID)
		}
		if summary.Summary != record.ResultSummary {
			return replayed, skipped, replayError("result_summaries", "history summary does not match result summary for query %s", record.QueryID)
		}
		query, err := ParseQuery([]byte(record.CanonicalQuery))
		if err != nil {
			return replayed, skipped, replayFromError(err)
		}
		if query.ID() != record.QueryID {
			return replayed, skipped, replayError("query_history", "history query id mismatch during replay")
		}
		mode, err := ParsePathMode(record.Mode)
		if err != nil {
			return replayed, skipped, replayFromError(err)
		}
		var plan Plan
		if mode == PathAuto {
			plan, _, err = replayCache.Ensure(query.ID(), s.collection.Version, s.indexes.Version, false, func() (Plan, error) {
				return ChoosePlan(query, s.collection, s.indexes, PathAuto)
			})
		} else {
			plan, err = ChoosePlan(query, s.collection, s.indexes, mode)
		}
		if err != nil {
			return replayed, skipped, replayFromError(err)
		}
		result, err := executePlan(query, s.collection, s.indexes, plan)
		if err != nil {
			return replayed, skipped, replayFromError(err)
		}
		expected := summary.Summary
		if s.injectReplayMismatch && replayed == 0 {
			expected = "injected:replay-mismatch"
		}
		if result.Summary != expected {
			e := newBusinessError(CodeReplay, "replayed result summary does not match snapshot")
			e.QueryID = record.QueryID
			e.Expected = expected
			e.Actual = result.Summary
			s.logger.Log("replay_mismatch", CodeReplay, map[string]any{
				"query_id": record.QueryID,
				"expected": expected,
				"actual":   result.Summary,
			})
			return replayed, skipped, e
		}
		replayed++
	}
	return replayed, skipped, nil
}

func replayFromError(err error) *BusinessError {
	business := AsBusinessError(CodeReplay, err)
	copyBusiness := *business
	copyBusiness.Code = CodeReplay
	return &copyBusiness
}
