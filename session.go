package docquery

import (
	"encoding/json"
	"fmt"
	"strings"
)

type SessionOptions struct {
	IndexPaths           []string
	Logger               EventLogger
	InjectCacheStale     bool
	InjectReplayMismatch bool
}

type HistoryRecord struct {
	Sequence          int            `json:"sequence"`
	QueryID           string         `json:"query_id"`
	CanonicalQuery    string         `json:"canonical_query"`
	CollectionVersion string         `json:"collection_version"`
	IndexSetVersion   string         `json:"index_set_version"`
	Mode              string         `json:"mode"`
	Path              string         `json:"path"`
	PlanID            string         `json:"plan_id"`
	ResultSummary     string         `json:"result_summary"`
	Stats             ExecutionStats `json:"stats"`
}

type TraceRecord struct {
	Sequence                  int    `json:"sequence"`
	Event                     string `json:"event"`
	QueryID                   string `json:"query_id,omitempty"`
	Mode                      string `json:"mode,omitempty"`
	Path                      string `json:"path,omitempty"`
	IndexPath                 string `json:"index_path,omitempty"`
	PlanID                    string `json:"plan_id,omitempty"`
	CacheHit                  bool   `json:"cache_hit,omitempty"`
	CacheRecompiled           bool   `json:"cache_recompiled,omitempty"`
	CacheStaleDetected        bool   `json:"cache_stale_detected,omitempty"`
	CollectionVersion         string `json:"collection_version,omitempty"`
	IndexSetVersion           string `json:"index_set_version,omitempty"`
	PreviousCollectionVersion string `json:"previous_collection_version,omitempty"`
	PreviousIndexSetVersion   string `json:"previous_index_set_version,omitempty"`
	ResultSummary             string `json:"result_summary,omitempty"`
}

type QueryResultSummary struct {
	QueryID           string         `json:"query_id"`
	CollectionVersion string         `json:"collection_version"`
	IndexSetVersion   string         `json:"index_set_version"`
	Mode              string         `json:"mode"`
	Path              string         `json:"path"`
	PlanID            string         `json:"plan_id"`
	Summary           string         `json:"summary"`
	Stats             ExecutionStats `json:"stats"`
}

type StatisticsSnapshot struct {
	CollectionVersion string                    `json:"collection_version"`
	IndexSetVersion   string                    `json:"index_set_version"`
	Queries           map[string]ExecutionStats `json:"queries"`
	Indexes           []IndexStat               `json:"indexes"`
}

func (s StatisticsSnapshot) IsFresh(collectionVersion, indexVersion string) bool {
	return s.CollectionVersion == collectionVersion && s.IndexSetVersion == indexVersion
}

func (s StatisticsSnapshot) Clone() StatisticsSnapshot {
	out := StatisticsSnapshot{
		CollectionVersion: s.CollectionVersion,
		IndexSetVersion:   s.IndexSetVersion,
		Queries:           make(map[string]ExecutionStats, len(s.Queries)),
		Indexes:           append([]IndexStat(nil), s.Indexes...),
	}
	for key, stats := range s.Queries {
		out.Queries[key] = stats
	}
	return out
}

type Session struct {
	collection *Collection
	indexes    *IndexSet
	plans      *PlanCache
	logger     EventLogger

	history         []HistoryRecord
	trace           []TraceRecord
	resultSummaries map[string]QueryResultSummary
	statistics      StatisticsSnapshot

	injectCacheStale     bool
	injectReplayMismatch bool
}

func OpenSession(collectionPath string, indexPaths []string, logger EventLogger) (*Session, error) {
	collection, err := LoadCollection(collectionPath)
	if err != nil {
		return nil, err
	}
	return NewSession(collection, SessionOptions{IndexPaths: indexPaths, Logger: logger})
}

func NewSession(collection *Collection, options SessionOptions) (*Session, error) {
	if collection == nil {
		return nil, newBusinessError(CodeDoc, "collection must not be nil")
	}
	validated, err := NewCollection(collection.Name, collection.PrimaryKey, collection.Documents)
	if err != nil {
		return nil, err
	}
	indexes, err := NewIndexSet(validated, options.IndexPaths)
	if err != nil {
		return nil, err
	}
	logger := loggerOrNil(options.Logger)
	plans := NewPlanCache()
	plans.CollectionVersion = validated.Version
	plans.IndexSetVersion = indexes.Version
	session := &Session{
		collection:           validated,
		indexes:              indexes,
		plans:                plans,
		logger:               logger,
		history:              make([]HistoryRecord, 0),
		trace:                make([]TraceRecord, 0),
		resultSummaries:      make(map[string]QueryResultSummary),
		injectCacheStale:     options.InjectCacheStale,
		injectReplayMismatch: options.InjectReplayMismatch,
	}
	session.resetStatistics()
	logger.Log("collection_loaded", CodeOK, map[string]any{
		"collection": validated.Name,
		"version":    validated.Version,
		"documents":  len(validated.Documents),
	})
	logger.Log("indexes_built", CodeOK, map[string]any{
		"collection":        validated.Name,
		"index_set_version": indexes.Version,
		"specs":             indexes.Specs,
	})
	return session, nil
}

func (s *Session) CollectionVersion() string { return s.collection.Version }
func (s *Session) IndexSetVersion() string   { return s.indexes.Version }
func (s *Session) CollectionName() string    { return s.collection.Name }

func (s *Session) Collection() *Collection {
	return s.collection.Clone()
}

func (s *Session) SetInjections(cacheStale, replayMismatch bool) {
	s.injectCacheStale = cacheStale
	s.injectReplayMismatch = replayMismatch
}

func (s *Session) Execute(query *Query) (*Result, error) {
	return s.execute(query, PathAuto, true)
}

func (s *Session) ExecuteMode(query *Query, mode PathMode) (*Result, error) {
	return s.execute(query, mode, true)
}

func (s *Session) ExecuteJSON(data []byte) (*Result, error) {
	query, err := ParseQuery(data)
	if err != nil {
		return nil, err
	}
	return s.Execute(query)
}

// Plan returns the automatically selected plan, using and updating the cache.
// It does not create a query-history record.
func (s *Session) Plan(query *Query) (Plan, CacheInfo, error) {
	if err := s.validateQueryCollection(query); err != nil {
		return Plan{}, CacheInfo{}, err
	}
	return s.plans.Ensure(query.ID(), s.collection.Version, s.indexes.Version, s.injectCacheStale, func() (Plan, error) {
		return ChoosePlan(query, s.collection, s.indexes, PathAuto)
	})
}

func (s *Session) execute(query *Query, mode PathMode, record bool) (*Result, error) {
	if err := s.validateQueryCollection(query); err != nil {
		return nil, err
	}
	mode, err := ParsePathMode(string(mode))
	if err != nil {
		return nil, err
	}

	var plan Plan
	var cacheInfo CacheInfo
	if mode == PathAuto {
		plan, cacheInfo, err = s.plans.Ensure(query.ID(), s.collection.Version, s.indexes.Version, s.injectCacheStale, func() (Plan, error) {
			return ChoosePlan(query, s.collection, s.indexes, PathAuto)
		})
	} else {
		plan, err = ChoosePlan(query, s.collection, s.indexes, mode)
	}
	if err != nil {
		s.logError(err)
		return nil, err
	}
	if cacheInfo.StaleDetected && !s.injectCacheStale {
		s.logger.Log("plan_cache_replaced", CodeOK, map[string]any{
			"previous_collection_version": cacheInfo.PreviousCollectionVersion,
			"previous_index_set_version":  cacheInfo.PreviousIndexSetVersion,
			"collection_version":          s.collection.Version,
			"index_set_version":           s.indexes.Version,
			"query_id":                    query.ID(),
		})
	}

	result, err := executePlan(query, s.collection, s.indexes, plan)
	if err != nil {
		s.logError(err)
		return nil, err
	}
	if record {
		s.recordExecution(query, mode, plan, cacheInfo, result)
	}
	s.logger.Log("query_executed", CodeOK, map[string]any{
		"query_id":           result.QueryID,
		"plan_id":            plan.ID,
		"path":               plan.Path,
		"index_path":         plan.IndexPath,
		"scan_documents":     result.Stats.ScanDocuments,
		"matched_documents":  result.Stats.MatchedDocuments,
		"returned_documents": result.Stats.ReturnedDocuments,
		"collection_version": s.collection.Version,
		"index_set_version":  s.indexes.Version,
		"cache_hit":          cacheInfo.Hit,
		"cache_recompiled":   cacheInfo.Recompiled,
	})
	return result, nil
}

func (s *Session) validateQueryCollection(query *Query) error {
	if query == nil {
		return newBusinessError(CodeQuery, "query must not be nil")
	}
	if !strings.EqualFold(query.Collection, s.collection.Name) {
		return newBusinessError(CodeQuery, "query collection %q is not active (active: %q)", query.Collection, s.collection.Name)
	}
	return nil
}

func (s *Session) recordExecution(query *Query, mode PathMode, plan Plan, cacheInfo CacheInfo, result *Result) {
	sequence := len(s.history) + 1
	history := HistoryRecord{
		Sequence:          sequence,
		QueryID:           result.QueryID,
		CanonicalQuery:    query.CanonicalText(),
		CollectionVersion: s.collection.Version,
		IndexSetVersion:   s.indexes.Version,
		Mode:              string(mode),
		Path:              plan.Path,
		PlanID:            plan.ID,
		ResultSummary:     result.Summary,
		Stats:             result.Stats,
	}
	s.history = append(s.history, history)
	summary := QueryResultSummary{
		QueryID:           result.QueryID,
		CollectionVersion: s.collection.Version,
		IndexSetVersion:   s.indexes.Version,
		Mode:              string(mode),
		Path:              plan.Path,
		PlanID:            plan.ID,
		Summary:           result.Summary,
		Stats:             result.Stats,
	}
	s.resultSummaries[resultSummaryKey(summary.QueryID, summary.CollectionVersion, summary.IndexSetVersion, summary.Mode)] = summary

	trace := TraceRecord{
		Sequence:           len(s.trace) + 1,
		Event:              "query_executed",
		QueryID:            result.QueryID,
		Mode:               string(mode),
		Path:               plan.Path,
		IndexPath:          plan.IndexPath,
		PlanID:             plan.ID,
		CacheHit:           cacheInfo.Hit,
		CacheRecompiled:    cacheInfo.Recompiled,
		CacheStaleDetected: cacheInfo.StaleDetected,
		CollectionVersion:  s.collection.Version,
		IndexSetVersion:    s.indexes.Version,
		ResultSummary:      result.Summary,
	}
	s.trace = append(s.trace, trace)
	s.statistics.Queries[statisticsKey(result.QueryID, string(mode))] = result.Stats
}

func resultSummaryKey(queryID, collectionVersion, indexVersion, mode string) string {
	return collectionVersion + "\x00" + indexVersion + "\x00" + queryID + "\x00" + mode
}

func statisticsKey(queryID, mode string) string {
	return queryID + ":" + mode
}

func (s *Session) resetStatistics() {
	s.statistics = StatisticsSnapshot{
		CollectionVersion: s.collection.Version,
		IndexSetVersion:   s.indexes.Version,
		Queries:           make(map[string]ExecutionStats),
		Indexes:           s.indexes.Stats(),
	}
}

func (s *Session) StatisticsSnapshot() StatisticsSnapshot {
	return s.statistics.Clone()
}

func (s *Session) PlanCacheStatus() PlanCacheStatus {
	return s.plans.Status(s.collection.Version, s.indexes.Version)
}

func (s *Session) History() []HistoryRecord {
	return append([]HistoryRecord(nil), s.history...)
}

func (s *Session) Trace() []TraceRecord {
	return append([]TraceRecord(nil), s.trace...)
}

func (s *Session) ResultSummary(queryID, mode string) (QueryResultSummary, bool) {
	value, ok := s.resultSummaries[resultSummaryKey(queryID, s.collection.Version, s.indexes.Version, mode)]
	return value, ok
}

// ReplaceCollection swaps collection and indexes as one generation. Existing
// plan-cache entries remain visible as stale until the next lookup, which
// replaces the whole cache generation. Statistics are immediately invalidated.
func (s *Session) ReplaceCollection(collection *Collection) error {
	if collection == nil {
		return newBusinessError(CodeDoc, "collection must not be nil")
	}
	validated, err := NewCollection(collection.Name, collection.PrimaryKey, collection.Documents)
	if err != nil {
		return err
	}
	indexes, err := NewIndexSet(validated, s.indexes.SpecsCopy())
	if err != nil {
		return err
	}
	previousCollectionVersion := s.collection.Version
	previousIndexSetVersion := s.indexes.Version
	s.collection = validated
	s.indexes = indexes
	s.resetStatistics()
	s.trace = append(s.trace, TraceRecord{
		Sequence:                  len(s.trace) + 1,
		Event:                     "collection_replaced",
		CollectionVersion:         validated.Version,
		IndexSetVersion:           indexes.Version,
		PreviousCollectionVersion: previousCollectionVersion,
		PreviousIndexSetVersion:   previousIndexSetVersion,
	})
	s.logger.Log("collection_replaced", CodeOK, map[string]any{
		"previous_collection_version": previousCollectionVersion,
		"previous_index_set_version":  previousIndexSetVersion,
		"collection_version":          validated.Version,
		"index_set_version":           indexes.Version,
		"plan_cache_stale":            s.plans.IsStale(validated.Version, indexes.Version),
		"statistics_invalidated":      true,
	})
	return nil
}

func (s *Session) ApplyPatchFile(path string) error {
	updated, err := s.collection.ApplyPatchFile(path)
	if err != nil {
		return err
	}
	return s.ReplaceCollection(updated)
}

// Validate executes the same query through forced scan and forced index paths
// and compares every document byte-for-byte after deterministic sorting.
func (s *Session) Validate(query *Query) (*ValidationReport, error) {
	if err := s.validateQueryCollection(query); err != nil {
		return nil, err
	}
	scanPlan, err := ChoosePlan(query, s.collection, s.indexes, PathScan)
	if err != nil {
		return nil, err
	}
	scanResult, err := executePlan(query, s.collection, s.indexes, scanPlan)
	if err != nil {
		return nil, err
	}
	report := &ValidationReport{
		QueryID:       query.ID(),
		Equivalent:    true,
		IndexSetEmpty: s.indexes.Empty(),
		ScanStats:     scanResult.Stats,
		ScanPlanID:    scanPlan.ID,
	}
	if s.indexes.Empty() {
		report.Degraded = true
		report.DegradedReason = "index_set_empty"
		s.logger.Log("validation_completed", CodeOK, map[string]any{
			"query_id": query.ID(),
			"degraded": true,
			"reason":   report.DegradedReason,
		})
		return report, nil
	}
	if _, ok := s.indexes.EstimateCandidates(query.Filter); !ok {
		report.Degraded = true
		report.DegradedReason = "no_candidate_index"
		s.logger.Log("validation_completed", CodeOK, map[string]any{
			"query_id": query.ID(),
			"degraded": true,
			"reason":   report.DegradedReason,
		})
		return report, nil
	}
	indexPlan, err := ChoosePlan(query, s.collection, s.indexes, PathIndex)
	if err != nil {
		return nil, err
	}
	indexResult, err := executePlan(query, s.collection, s.indexes, indexPlan)
	if err != nil {
		return nil, err
	}
	report.IndexStats = indexResult.Stats
	report.IndexPlanID = indexPlan.ID

	firstDiffKey := firstDocumentDifference(scanResult.Documents, indexResult.Documents, s.collection.PrimaryKey)
	if firstDiffKey != "" || len(scanResult.Documents) != len(indexResult.Documents) {
		report.Equivalent = false
		e := newBusinessError(CodePlan, "scan and index paths produced different results")
		e.QueryID = query.ID()
		e.FirstDiffKey = firstDiffKey
		s.logger.Log("validation_failed", CodePlan, map[string]any{
			"query_id":       query.ID(),
			"first_diff_key": firstDiffKey,
		})
		return report, e
	}
	report.ComparedDocuments = len(scanResult.Documents)
	s.logger.Log("validation_completed", CodeOK, map[string]any{
		"query_id":           query.ID(),
		"equivalent":         true,
		"compared_documents": report.ComparedDocuments,
		"degraded":           false,
	})
	return report, nil
}

func firstDocumentDifference(scanDocs, indexDocs []Document, primaryKey string) string {
	limit := len(scanDocs)
	if len(indexDocs) < limit {
		limit = len(indexDocs)
	}
	for i := 0; i < limit; i++ {
		scanBytes := documentCanonicalBytes(scanDocs[i])
		indexBytes := documentCanonicalBytes(indexDocs[i])
		if string(scanBytes) != string(indexBytes) {
			return primaryKeyText(scanDocs[i], primaryKey)
		}
	}
	if len(scanDocs) > limit {
		return primaryKeyText(scanDocs[limit], primaryKey)
	}
	if len(indexDocs) > limit {
		return primaryKeyText(indexDocs[limit], primaryKey)
	}
	return ""
}

func primaryKeyText(doc Document, primaryKey string) string {
	value, ok := GetField(doc, primaryKey)
	if !ok {
		return "<missing:" + primaryKey + ">"
	}
	switch typed := value.(type) {
	case string:
		return typed
	case json.Number:
		return typed.String()
	}
	body, _ := json.Marshal(value)
	return string(body)
}

func (s *Session) logError(err error) {
	business := AsBusinessError(CodePlan, err)
	fields := map[string]any{"message": business.Message}
	if business.QueryID != "" {
		fields["query_id"] = business.QueryID
	}
	if business.Expected != "" || business.Actual != "" {
		fields["expected"] = business.Expected
		fields["actual"] = business.Actual
	}
	fields["collection_version"] = s.collection.Version
	fields["index_set_version"] = s.indexes.Version
	s.logger.Log("error", business.Code, fields)
}

func (s *Session) String() string {
	return fmt.Sprintf("Session{%s collection=%s index=%s}", s.collection.Name, s.collection.Version, s.indexes.Version)
}
