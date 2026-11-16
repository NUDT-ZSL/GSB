package docquery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
)

type IndexStat struct {
	Path             string `json:"path"`
	IndexedDocuments int    `json:"indexed_documents"`
	DistinctValues   int    `json:"distinct_values"`
}

type indexEntry struct {
	key    string
	value  any
	docIDs []int
}

type collectionIndex struct {
	path    string
	entries map[string]*indexEntry
	ordered []*indexEntry
}

// IndexSet contains candidate indexes for field paths. Its version changes when
// the collection content or index specification changes.
type IndexSet struct {
	Version           string   `json:"version"`
	Specs             []string `json:"specs"`
	CollectionVersion string   `json:"collection_version"`
	indexes           map[string]*collectionIndex
}

// NewIndexSet builds deterministic scalar indexes for the requested paths.
func NewIndexSet(collection *Collection, paths []string) (*IndexSet, error) {
	if collection == nil {
		return nil, newBusinessError(CodeDoc, "collection must not be nil")
	}
	unique := make(map[string]struct{}, len(paths))
	specs := make([]string, 0, len(paths))
	for _, path := range paths {
		path = strings.TrimSpace(path)
		if path == "" {
			continue
		}
		if err := ValidateFieldPath(path); err != nil {
			return nil, AsBusinessError(CodeQuery, err)
		}
		if _, exists := unique[path]; exists {
			continue
		}
		unique[path] = struct{}{}
		specs = append(specs, path)
	}
	sort.Strings(specs)

	indexes := make(map[string]*collectionIndex, len(specs))
	for _, path := range specs {
		index := &collectionIndex{
			path:    path,
			entries: make(map[string]*indexEntry),
		}
		for documentIndex, doc := range collection.Documents {
			value, ok := GetField(doc, path)
			if !ok {
				continue
			}
			key, scalar := canonicalScalarKey(value)
			if !scalar {
				continue
			}
			entry := index.entries[key]
			if entry == nil {
				entry = &indexEntry{key: key, value: cloneJSONValue(value)}
				index.entries[key] = entry
			}
			entry.docIDs = append(entry.docIDs, documentIndex)
		}
		index.ordered = make([]*indexEntry, 0, len(index.entries))
		for _, entry := range index.entries {
			index.ordered = append(index.ordered, entry)
		}
		sort.Slice(index.ordered, func(i, j int) bool {
			return index.ordered[i].key < index.ordered[j].key
		})
		indexes[path] = index
	}

	set := &IndexSet{
		Specs:             specs,
		CollectionVersion: collection.Version,
		indexes:           indexes,
	}
	set.Version = computeIndexSetVersion(set)
	return set, nil
}

func computeIndexSetVersion(set *IndexSet) string {
	payload := struct {
		CollectionVersion string   `json:"collection_version"`
		Specs             []string `json:"specs"`
	}{
		CollectionVersion: set.CollectionVersion,
		Specs:             set.Specs,
	}
	body, _ := json.Marshal(payload)
	sum := sha256.Sum256(append([]byte("docquery.indexset.v1\x00"), body...))
	return hex.EncodeToString(sum[:])
}

func (s *IndexSet) Empty() bool {
	return s == nil || len(s.Specs) == 0
}

func (s *IndexSet) SpecsCopy() []string {
	if s == nil {
		return nil
	}
	return append([]string(nil), s.Specs...)
}

func (s *IndexSet) Stats() []IndexStat {
	if s == nil {
		return nil
	}
	stats := make([]IndexStat, 0, len(s.Specs))
	for _, path := range s.Specs {
		index := s.indexes[path]
		indexed := 0
		if index != nil {
			for _, entry := range index.ordered {
				indexed += len(entry.docIDs)
			}
		}
		stats = append(stats, IndexStat{
			Path:             path,
			IndexedDocuments: indexed,
			DistinctValues:   len(index.entries),
		})
	}
	return stats
}

// CandidateEstimate is an exact upper-bound candidate document set for a
// predicate using a single candidate index path.
type CandidateEstimate struct {
	Path   string
	DocIDs []int
}

// EstimateCandidates returns a safe candidate set that can be used to narrow a
// scan before the complete predicate is re-evaluated on every candidate.
func (s *IndexSet) EstimateCandidates(filter *Filter) (CandidateEstimate, bool) {
	if s == nil || filter == nil {
		return CandidateEstimate{}, false
	}
	switch filter.Op {
	case "eq":
		return s.estimateEq(filter)
	case "in":
		return s.estimateIn(filter)
	case "range":
		return s.estimateRange(filter)
	case "and":
		var best CandidateEstimate
		found := false
		for i := range filter.Args {
			estimate, ok := s.EstimateCandidates(&filter.Args[i])
			if !ok {
				continue
			}
			if !found || len(estimate.DocIDs) < len(best.DocIDs) ||
				(len(estimate.DocIDs) == len(best.DocIDs) && estimate.Path < best.Path) {
				best = estimate
				found = true
			}
		}
		return best, found
	case "or":
		var result CandidateEstimate
		for i := range filter.Args {
			estimate, ok := s.EstimateCandidates(&filter.Args[i])
			if !ok {
				return CandidateEstimate{}, false
			}
			if i == 0 {
				result = CandidateEstimate{Path: estimate.Path, DocIDs: append([]int(nil), estimate.DocIDs...)}
				continue
			}
			if estimate.Path != result.Path {
				return CandidateEstimate{}, false
			}
			result.DocIDs = unionIDs(result.DocIDs, estimate.DocIDs)
		}
		if result.Path == "" {
			return CandidateEstimate{}, false
		}
		return result, true
	default:
		return CandidateEstimate{}, false
	}
}

func (s *IndexSet) estimateEq(filter *Filter) (CandidateEstimate, bool) {
	index := s.indexes[filter.Path]
	if index == nil {
		return CandidateEstimate{}, false
	}
	key, ok := canonicalScalarKey(filter.Value)
	if !ok {
		return CandidateEstimate{}, false
	}
	if entry := index.entries[key]; entry != nil {
		return CandidateEstimate{Path: filter.Path, DocIDs: append([]int(nil), entry.docIDs...)}, true
	}
	return CandidateEstimate{Path: filter.Path, DocIDs: []int{}}, true
}

func (s *IndexSet) estimateIn(filter *Filter) (CandidateEstimate, bool) {
	index := s.indexes[filter.Path]
	if index == nil {
		return CandidateEstimate{}, false
	}
	var ids []int
	for _, value := range filter.Values {
		key, ok := canonicalScalarKey(value)
		if !ok {
			continue
		}
		if entry := index.entries[key]; entry != nil {
			ids = unionIDs(ids, entry.docIDs)
		}
	}
	return CandidateEstimate{Path: filter.Path, DocIDs: ids}, true
}

func (s *IndexSet) estimateRange(filter *Filter) (CandidateEstimate, bool) {
	index := s.indexes[filter.Path]
	if index == nil {
		return CandidateEstimate{}, false
	}
	var ids []int
	for _, entry := range index.ordered {
		if rangeMatches(entry.value, filter.Bounds) {
			ids = unionIDs(ids, entry.docIDs)
		}
	}
	return CandidateEstimate{Path: filter.Path, DocIDs: ids}, true
}

func rangeMatches(value any, bounds RangeBounds) bool {
	if bounds.GT != nil {
		comparison, ok := compareScalars(value, bounds.GT)
		if !ok || comparison <= 0 {
			return false
		}
	}
	if bounds.GTE != nil {
		comparison, ok := compareScalars(value, bounds.GTE)
		if !ok || comparison < 0 {
			return false
		}
	}
	if bounds.LT != nil {
		comparison, ok := compareScalars(value, bounds.LT)
		if !ok || comparison >= 0 {
			return false
		}
	}
	if bounds.LTE != nil {
		comparison, ok := compareScalars(value, bounds.LTE)
		if !ok || comparison > 0 {
			return false
		}
	}
	return true
}

func unionIDs(left, right []int) []int {
	if len(left) == 0 {
		return append([]int(nil), right...)
	}
	if len(right) == 0 {
		return append([]int(nil), left...)
	}
	seen := make(map[int]struct{}, len(left)+len(right))
	result := make([]int, 0, len(left)+len(right))
	for _, id := range left {
		if _, ok := seen[id]; !ok {
			seen[id] = struct{}{}
			result = append(result, id)
		}
	}
	for _, id := range right {
		if _, ok := seen[id]; !ok {
			seen[id] = struct{}{}
			result = append(result, id)
		}
	}
	sort.Ints(result)
	return result
}

func validateIndexSet(set *IndexSet, collection *Collection) error {
	if set == nil {
		return newBusinessError(CodeReplay, "index set is missing")
	}
	if set.CollectionVersion != collection.Version {
		return newBusinessError(CodeReplay, "index set collection version does not match snapshot collection")
	}
	expected := computeIndexSetVersion(set)
	if set.Version != expected {
		return newBusinessError(CodeReplay, "index set version checksum mismatch: expected %s, got %s", expected, set.Version)
	}
	for _, path := range set.Specs {
		if err := ValidateFieldPath(path); err != nil {
			return AsBusinessError(CodeReplay, err)
		}
	}
	return nil
}
