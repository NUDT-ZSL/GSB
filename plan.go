package docquery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
)

type PathMode string

const (
	PathAuto  PathMode = "auto"
	PathScan  PathMode = "scan"
	PathIndex PathMode = "index"
)

func ParsePathMode(value string) (PathMode, error) {
	switch PathMode(strings.ToLower(strings.TrimSpace(value))) {
	case PathAuto, "":
		return PathAuto, nil
	case PathScan:
		return PathScan, nil
	case PathIndex:
		return PathIndex, nil
	default:
		return "", newBusinessError(CodeQuery, "unsupported path mode %q", value)
	}
}

type CostDetail struct {
	Documents       int    `json:"documents"`
	ScanCost        int    `json:"scan_cost"`
	IndexPath       string `json:"index_path,omitempty"`
	IndexCandidates int    `json:"index_candidates"`
	IndexCost       int    `json:"index_cost"`
	ChosenCost      int    `json:"chosen_cost"`
	Rule            string `json:"rule"`
}

// Plan records the selected physical path and its deterministic cost detail.
type Plan struct {
	ID                string     `json:"id"`
	QueryID           string     `json:"query_id"`
	CollectionVersion string     `json:"collection_version"`
	IndexSetVersion   string     `json:"index_set_version"`
	Path              string     `json:"path"`
	IndexPath         string     `json:"index_path,omitempty"`
	Cost              CostDetail `json:"cost"`
}

// ChoosePlan implements the fixed cost rule: scan cost is the number of
// documents; candidate-index cost is 2*candidates+1. An index wins only when
// its cost is strictly smaller. Forced modes exist for consistency validation.
func ChoosePlan(query *Query, collection *Collection, indexes *IndexSet, mode PathMode) (Plan, error) {
	if query == nil || collection == nil {
		return Plan{}, newBusinessError(CodePlan, "query and collection are required for planning")
	}
	if !strings.EqualFold(query.Collection, collection.Name) {
		return Plan{}, newBusinessError(CodeQuery, "query collection %q does not match active collection %q", query.Collection, collection.Name)
	}
	if mode == "" {
		mode = PathAuto
	}
	documents := len(collection.Documents)
	cost := CostDetail{
		Documents: documents,
		ScanCost:  documents,
	}
	estimate, hasEstimate := CandidateEstimate{}, false
	if indexes != nil {
		estimate, hasEstimate = indexes.EstimateCandidates(query.Filter)
	}
	if hasEstimate {
		cost.IndexPath = estimate.Path
		cost.IndexCandidates = len(estimate.DocIDs)
		cost.IndexCost = 2*cost.IndexCandidates + 1
	}

	selected := PathScan
	switch mode {
	case PathScan:
		cost.Rule = "forced_scan"
	case PathIndex:
		if !hasEstimate {
			return Plan{}, newBusinessError(CodePlan, "index path requested but no candidate index is available for this query")
		}
		selected = PathIndex
		cost.Rule = "forced_index"
	case PathAuto:
		if hasEstimate && cost.IndexCost < cost.ScanCost {
			selected = PathIndex
			cost.Rule = "index_cheaper: 2*candidates+1 < documents"
		} else if hasEstimate {
			cost.Rule = "scan_cheaper_or_equal: 2*candidates+1 >= documents"
		} else {
			cost.Rule = "scan_only: no usable candidate index"
		}
	default:
		return Plan{}, newBusinessError(CodeQuery, "unsupported path mode %q", mode)
	}

	if selected == PathIndex {
		cost.ChosenCost = cost.IndexCost
	} else {
		cost.ChosenCost = cost.ScanCost
	}
	plan := Plan{
		QueryID:           query.ID(),
		CollectionVersion: collection.Version,
		IndexSetVersion:   indexesVersion(indexes),
		Path:              string(selected),
		Cost:              cost,
	}
	if selected == PathIndex {
		plan.IndexPath = estimate.Path
	}
	plan.ID = computePlanID(plan)
	return plan, nil
}

func indexesVersion(indexes *IndexSet) string {
	if indexes == nil {
		return ""
	}
	return indexes.Version
}

func computePlanID(plan Plan) string {
	plan.ID = ""
	body, _ := json.Marshal(plan)
	sum := sha256.Sum256(append([]byte("docquery.plan.v1\x00"), body...))
	return hex.EncodeToString(sum[:])
}

// CacheInfo describes a plan lookup without affecting stable query statistics.
type CacheInfo struct {
	Hit                       bool   `json:"hit"`
	Recompiled                bool   `json:"recompiled"`
	StaleDetected             bool   `json:"stale_detected"`
	PreviousCollectionVersion string `json:"previous_collection_version,omitempty"`
	PreviousIndexSetVersion   string `json:"previous_index_set_version,omitempty"`
}

// PlanCache stores only the active collection/index generation. On any version
// change, the whole entry map is replaced, never incrementally merged.
type PlanCache struct {
	CollectionVersion string          `json:"collection_version"`
	IndexSetVersion   string          `json:"index_set_version"`
	Entries           map[string]Plan `json:"entries"`
}

func NewPlanCache() *PlanCache {
	return &PlanCache{Entries: make(map[string]Plan)}
}

func (c *PlanCache) GenerationKey() string {
	if c == nil {
		return ""
	}
	return c.CollectionVersion + "@" + c.IndexSetVersion
}

func (c *PlanCache) IsStale(collectionVersion, indexVersion string) bool {
	if c == nil {
		return true
	}
	if c.CollectionVersion == "" && c.IndexSetVersion == "" && len(c.Entries) == 0 {
		return false
	}
	return c.CollectionVersion != collectionVersion || c.IndexSetVersion != indexVersion
}

func (c *PlanCache) activateGeneration(collectionVersion, indexVersion string) {
	c.CollectionVersion = collectionVersion
	c.IndexSetVersion = indexVersion
	c.Entries = make(map[string]Plan)
}

func (c *PlanCache) Clone() *PlanCache {
	if c == nil {
		return NewPlanCache()
	}
	out := &PlanCache{
		CollectionVersion: c.CollectionVersion,
		IndexSetVersion:   c.IndexSetVersion,
		Entries:           make(map[string]Plan, len(c.Entries)),
	}
	for key, plan := range c.Entries {
		out.Entries[key] = plan
	}
	return out
}

// Ensure returns a cached plan or compiles a replacement. If injectStale is
// true, a detected generation mismatch is reported as E_CACHE instead of being
// transparently recompiled.
func (c *PlanCache) Ensure(queryID, collectionVersion, indexVersion string, injectStale bool, compile func() (Plan, error)) (Plan, CacheInfo, error) {
	if c == nil {
		return Plan{}, CacheInfo{}, newBusinessError(CodePlan, "plan cache is nil")
	}
	if c.Entries == nil {
		c.Entries = make(map[string]Plan)
	}
	info := CacheInfo{}
	if c.CollectionVersion == "" && c.IndexSetVersion == "" && len(c.Entries) == 0 {
		c.CollectionVersion = collectionVersion
		c.IndexSetVersion = indexVersion
	}
	stale := c.IsStale(collectionVersion, indexVersion)
	if stale {
		info.StaleDetected = true
		info.PreviousCollectionVersion = c.CollectionVersion
		info.PreviousIndexSetVersion = c.IndexSetVersion
		if injectStale {
			e := newBusinessError(CodeCache, "plan cache generation is stale")
			e.QueryID = queryID
			e.Expected = collectionVersion + "@" + indexVersion
			e.Actual = c.GenerationKey()
			return Plan{}, info, e
		}
		c.activateGeneration(collectionVersion, indexVersion)
		info.Recompiled = true
	}
	if cached, ok := c.Entries[queryID]; ok && planVersionMatches(cached, collectionVersion, indexVersion) {
		info.Hit = true
		info.Recompiled = false
		return cached, info, nil
	}
	compiled, err := compile()
	if err != nil {
		return Plan{}, info, err
	}
	if compiled.QueryID != queryID {
		return Plan{}, info, newBusinessError(CodePlan, "compiled plan query id does not match cache key")
	}
	c.Entries[queryID] = compiled
	info.Recompiled = true
	return compiled, info, nil
}

func planVersionMatches(plan Plan, collectionVersion, indexVersion string) bool {
	return plan.CollectionVersion == collectionVersion && plan.IndexSetVersion == indexVersion
}

// PlanCacheStatus is safe to expose for diagnostics and tests.
type PlanCacheStatus struct {
	CollectionVersion string   `json:"collection_version"`
	IndexSetVersion   string   `json:"index_set_version"`
	EntryCount        int      `json:"entry_count"`
	QueryIDs          []string `json:"query_ids"`
	Stale             bool     `json:"stale"`
}

func (c *PlanCache) Status(collectionVersion, indexVersion string) PlanCacheStatus {
	status := PlanCacheStatus{
		CollectionVersion: c.CollectionVersion,
		IndexSetVersion:   c.IndexSetVersion,
		EntryCount:        len(c.Entries),
		Stale:             c.IsStale(collectionVersion, indexVersion),
	}
	status.QueryIDs = make([]string, 0, len(c.Entries))
	for queryID := range c.Entries {
		status.QueryIDs = append(status.QueryIDs, queryID)
	}
	sort.Strings(status.QueryIDs)
	return status
}
