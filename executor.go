package docquery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
)

// ExecutionStats contains only deterministic values. Cache hit state and trace
// sequence numbers are deliberately excluded so repeated executions remain
// byte-identical.
type ExecutionStats struct {
	ScanDocuments     int    `json:"scan_documents"`
	MatchedDocuments  int    `json:"matched_documents"`
	ReturnedDocuments int    `json:"returned_documents"`
	Path              string `json:"path"`
	IndexPath         string `json:"index_path,omitempty"`
	CollectionVersion string `json:"collection_version"`
	IndexSetVersion   string `json:"index_set_version"`
}

// Result is the stable query result envelope.
type Result struct {
	QueryID           string         `json:"query_id"`
	Collection        string         `json:"collection"`
	CollectionVersion string         `json:"collection_version"`
	IndexSetVersion   string         `json:"index_set_version"`
	Plan              Plan           `json:"plan"`
	Stats             ExecutionStats `json:"stats"`
	Documents         []Document     `json:"documents"`
	Summary           string         `json:"summary"`
}

func computeResultSummary(result Result) string {
	result.Summary = ""
	body, _ := json.Marshal(result)
	sum := sha256.Sum256(append([]byte("docquery.result.v1\x00"), body...))
	return hex.EncodeToString(sum[:])
}

// StableJSON returns indented JSON with a freshly verified summary. The output
// is byte-identical for identical inputs and execution options.
func (r *Result) StableJSON() ([]byte, error) {
	if r == nil {
		return []byte("null"), nil
	}
	copyResult := *r
	copyResult.Summary = computeResultSummary(copyResult)
	return json.MarshalIndent(copyResult, "", "  ")
}

func (r *Result) CompactJSON() ([]byte, error) {
	if r == nil {
		return []byte("null"), nil
	}
	copyResult := *r
	copyResult.Summary = computeResultSummary(copyResult)
	return json.Marshal(copyResult)
}

func executePlan(query *Query, collection *Collection, indexes *IndexSet, plan Plan) (*Result, error) {
	if query == nil || collection == nil {
		return nil, newBusinessError(CodePlan, "query and collection are required")
	}
	stats := ExecutionStats{
		Path:              plan.Path,
		IndexPath:         plan.IndexPath,
		CollectionVersion: collection.Version,
		IndexSetVersion:   indexesVersion(indexes),
	}

	candidateIDs := make([]int, 0, len(collection.Documents))
	switch plan.Path {
	case string(PathScan):
		for i := range collection.Documents {
			candidateIDs = append(candidateIDs, i)
		}
	case string(PathIndex):
		estimate, ok := indexes.EstimateCandidates(query.Filter)
		if !ok {
			return nil, newBusinessError(CodePlan, "selected index path has no candidate estimate")
		}
		candidateIDs = estimate.DocIDs
	default:
		return nil, newBusinessError(CodePlan, "unknown execution path %q", plan.Path)
	}

	matched := make([]Document, 0)
	for _, documentIndex := range candidateIDs {
		stats.ScanDocuments++
		doc := collection.Documents[documentIndex]
		if query.Filter != nil && !evaluateFilter(query.Filter, doc) {
			continue
		}
		stats.MatchedDocuments++
		matched = append(matched, doc)
	}

	sortDocuments(matched, query.Sort, collection.PrimaryKey)

	start := query.Offset
	if start > len(matched) {
		start = len(matched)
	}
	end := len(matched)
	if query.Limit != nil {
		limit := *query.Limit
		if limit < end-start {
			end = start + limit
		}
	}
	page := matched[start:end]
	documents := make([]Document, 0, len(page))
	for _, doc := range page {
		documents = append(documents, projectDocument(doc, query.Projection))
	}
	stats.ReturnedDocuments = len(documents)

	result := &Result{
		QueryID:           query.ID(),
		Collection:        collection.Name,
		CollectionVersion: collection.Version,
		IndexSetVersion:   indexesVersion(indexes),
		Plan:              plan,
		Stats:             stats,
		Documents:         documents,
	}
	result.Summary = computeResultSummary(*result)
	return result, nil
}

func evaluateFilter(filter *Filter, doc Document) bool {
	if filter == nil {
		return true
	}
	switch filter.Op {
	case "eq":
		value, ok := GetField(doc, filter.Path)
		if !ok {
			return false
		}
		left, leftOK := canonicalScalarKey(value)
		right, rightOK := canonicalScalarKey(filter.Value)
		return leftOK && rightOK && left == right
	case "in":
		value, ok := GetField(doc, filter.Path)
		if !ok {
			return false
		}
		key, scalar := canonicalScalarKey(value)
		if !scalar {
			return false
		}
		for _, candidate := range filter.Values {
			candidateKey, ok := canonicalScalarKey(candidate)
			if ok && candidateKey == key {
				return true
			}
		}
		return false
	case "range":
		value, ok := GetField(doc, filter.Path)
		return ok && rangeMatches(value, filter.Bounds)
	case "and":
		for i := range filter.Args {
			if !evaluateFilter(&filter.Args[i], doc) {
				return false
			}
		}
		return true
	case "or":
		for i := range filter.Args {
			if evaluateFilter(&filter.Args[i], doc) {
				return true
			}
		}
		return false
	default:
		return false
	}
}

func sortDocuments(documents []Document, keys []SortKey, primaryKey string) {
	sort.Slice(documents, func(i, j int) bool {
		for _, key := range keys {
			left, leftOK := GetField(documents[i], key.Path)
			right, rightOK := GetField(documents[j], key.Path)
			comparison := compareSortValues(left, leftOK, right, rightOK)
			if comparison == 0 {
				continue
			}
			if key.Direction == "desc" {
				comparison = -comparison
			}
			return comparison < 0
		}
		leftKey, leftOK := GetField(documents[i], primaryKey)
		rightKey, rightOK := GetField(documents[j], primaryKey)
		comparison := compareSortValues(leftKey, leftOK, rightKey, rightOK)
		if comparison != 0 {
			return comparison < 0
		}
		return strings.Compare(string(documentCanonicalBytes(documents[i])), string(documentCanonicalBytes(documents[j]))) < 0
	})
}

func compareSortValues(left any, leftOK bool, right any, rightOK bool) int {
	leftRank := sortValueRank(left, leftOK)
	rightRank := sortValueRank(right, rightOK)
	if leftRank != rightRank {
		if leftRank < rightRank {
			return -1
		}
		return 1
	}
	switch leftRank {
	case -1, 0:
		return 0
	case 1:
		if left.(bool) == right.(bool) {
			return 0
		}
		if !left.(bool) {
			return -1
		}
		return 1
	case 2:
		leftRat, leftNumeric := numberRatForAny(left)
		rightRat, rightNumeric := numberRatForAny(right)
		if leftNumeric && rightNumeric {
			return leftRat.Cmp(rightRat)
		}
	case 3:
		return strings.Compare(left.(string), right.(string))
	}
	leftText, _ := json.Marshal(left)
	rightText, _ := json.Marshal(right)
	return strings.Compare(string(leftText), string(rightText))
}

func sortValueRank(value any, present bool) int {
	if !present {
		return -1
	}
	if value == nil {
		return 0
	}
	switch value.(type) {
	case bool:
		return 1
	case json.Number, float64, float32, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		return 2
	case string:
		return 3
	case []any:
		return 4
	case map[string]any, Document:
		return 5
	default:
		return 6
	}
}

func projectDocument(doc Document, projection []string) Document {
	if len(projection) == 0 {
		return cloneDocument(doc)
	}
	result := make(Document)
	for _, path := range projection {
		value, ok := GetField(doc, path)
		if !ok {
			continue
		}
		setField(result, path, value)
	}
	return result
}

// ValidationReport describes scan/index consistency without exposing cache
// state as part of the result.
type ValidationReport struct {
	QueryID           string         `json:"query_id"`
	Equivalent        bool           `json:"equivalent"`
	Degraded          bool           `json:"degraded"`
	DegradedReason    string         `json:"degraded_reason,omitempty"`
	IndexSetEmpty     bool           `json:"index_set_empty"`
	ComparedDocuments int            `json:"compared_documents"`
	ScanStats         ExecutionStats `json:"scan_stats"`
	IndexStats        ExecutionStats `json:"index_stats"`
	ScanPlanID        string         `json:"scan_plan_id"`
	IndexPlanID       string         `json:"index_plan_id,omitempty"`
}
