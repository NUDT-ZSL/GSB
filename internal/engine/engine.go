package engine

import (
	"encoding/json"
	"fmt"
	"math/big"
	"sort"

	"configledger/internal/domain"
	"configledger/internal/spec"
)

type Options struct {
	SessionID        string
	Capacity         int64
	StorageUnitPrice int64
	CompileUnitPrice int64
}

type RestoreReport struct {
	WholeReplaced    bool     `json:"whole_replaced"`
	FromVersion      string   `json:"from_version"`
	ToVersion        string   `json:"to_version"`
	FromGraphVersion string   `json:"from_graph_version"`
	ToGraphVersion   string   `json:"to_graph_version"`
	GraphChanged     bool     `json:"graph_changed"`
	AffectedEntries  []string `json:"affected_entries"`
	Added            []string `json:"added"`
	Removed          []string `json:"removed"`
	Modified         []string `json:"modified"`
}

func Apply(prev *domain.State, manifest *domain.Manifest, graph *domain.Graph, options Options) (*domain.State, *domain.BusinessError) {
	if manifest == nil {
		return nil, domain.BizError(domain.CodeSpec, "manifest", "manifest is required")
	}
	if graph == nil {
		var berr *domain.BusinessError
		graph, berr = spec.BuildGraph(manifest)
		if berr != nil {
			return nil, berr
		}
	}
	if options.SessionID == "" {
		return nil, domain.BizError(domain.CodeSpec, "session_id", "session id is required")
	}
	if options.Capacity <= 0 {
		return nil, domain.BizError(domain.CodeCapacity, "capacity", fmt.Sprintf("cache capacity must be greater than zero, got %d", options.Capacity))
	}
	if options.StorageUnitPrice < 0 {
		return nil, domain.BizError(domain.CodeSpec, "storage_unit_price", "storage unit price cannot be negative")
	}
	if options.CompileUnitPrice < 0 {
		return nil, domain.BizError(domain.CodeSpec, "compile_unit_price", "compile unit price cannot be negative")
	}
	if prev != nil && prev.SessionID != options.SessionID {
		return nil, domain.BizError(domain.CodeReplay, "session_id", fmt.Sprintf("previous snapshot belongs to session %q, not %q", prev.SessionID, options.SessionID))
	}

	manifestHash := spec.ManifestHash(manifest)
	if prev != nil && prev.ManifestVersion == manifest.ManifestVersion && prev.ManifestHash != manifestHash {
		return nil, domain.BizError(domain.CodeSpec, "manifest_version", fmt.Sprintf("manifest version %q was already persisted with different content", manifest.ManifestVersion))
	}
	if berr := validateRemovedReferences(prev, manifest); berr != nil {
		return nil, berr
	}
	if berr := validateDependencyReferences(manifest); berr != nil {
		return nil, berr
	}

	prevEntries := entryMap(nil)
	prevProducts := productMap(nil)
	prevCache := cacheMap(nil)
	if prev != nil {
		prevEntries = entryMap(&prev.Manifest)
		prevProducts = productMap(prev)
		prevCache = cacheMap(prev)
	}
	currentEntries := entryMap(manifest)

	diff, currentVersions := buildDiffAndVersions(prevEntries, currentEntries, prevProducts)
	if prev != nil {
		diff.From = prev.ManifestVersion
	}
	diff.To = manifest.ManifestVersion
	products, berr := buildProducts(manifest, graph, currentVersions, prevProducts)
	if berr != nil {
		return nil, berr
	}
	productByKey := productMapFromProducts(products)

	affected, invalidated := deriveInvalidation(prev, manifest, graph, diff, productByKey, prevCache)
	cacheSeeds := make([]string, 0)
	for key, cacheItem := range prevCache {
		product, exists := productByKey[key]
		if !exists {
			continue
		}
		if cacheItem.ProductVersion != product.ProductVersion || cacheItem.ArtifactHash != product.ArtifactHash {
			cacheSeeds = append(cacheSeeds, key)
		}
	}
	if len(cacheSeeds) > 0 {
		seeds := invalidationSeeds(prev, manifest, graph, diff)
		seeds = append(seeds, cacheSeeds...)
		affected, invalidated = deriveInvalidationWithSeeds(prev, manifest, graph, diff, seeds)
	}

	directCurrent := make(map[string]struct{})
	for _, key := range diff.Added {
		directCurrent[key] = struct{}{}
	}
	for _, key := range diff.Modified {
		directCurrent[key] = struct{}{}
	}

	decisions, cache, reused, recompiled, dropped := planDecisions(prev, manifest, graph, products, productByKey, prevCache, affected, directCurrent, diff, options)
	cacheBytes := sumCacheBytes(cache)
	totalDelivery, totalCompile, berr := planTotals(products, reused)
	if berr != nil {
		return nil, berr
	}
	storageCost, ok := safeMul(cacheBytes, options.StorageUnitPrice)
	if !ok {
		return nil, domain.BizError(domain.CodeCapacity, "storage_cost", "integer storage cost overflow")
	}
	compileCost, ok := safeMul(totalCompile, options.CompileUnitPrice)
	if !ok {
		return nil, domain.BizError(domain.CodeCapacity, "compile_cost", "integer compile cost overflow")
	}
	totalCost, ok := safeAdd(storageCost, compileCost)
	if !ok {
		return nil, domain.BizError(domain.CodeCapacity, "total_cost", "integer total cost overflow")
	}

	plan := make([]domain.PlanItem, 0, len(products))
	for _, product := range products {
		wasReused := contains(reused, product.Key)
		item := domain.PlanItem{
			Key:             product.Key,
			ProductVersion:  product.ProductVersion,
			Targets:         append([]string(nil), product.Targets...),
			DeliveryEntries: product.DeliveryEntries,
			Compiled:        !wasReused,
			CompileWork:     0,
			StorageBytes:    product.StorageBytes,
			ArtifactHash:    product.ArtifactHash,
		}
		if !wasReused {
			item.CompileWork = product.CompileWork
		}
		plan = append(plan, item)
	}
	sort.Slice(plan, func(i, j int) bool { return plan[i].Key < plan[j].Key })

	previousVersion := ""
	previousGraphVersion := ""
	previousCacheBytes := int64(0)
	if prev != nil {
		previousVersion = prev.ManifestVersion
		previousGraphVersion = prev.GraphVersion
		previousCacheBytes = sumCacheBytes(prev.Cache)
	}
	costSource := fmt.Sprintf("%s;%s;capacity=%d;storage_price=%d;compile_price=%d;formula=delivery_targets+compile_work+cache_bytes", domain.EstimatorVersion, domain.InterfaceVersion, options.Capacity, options.StorageUnitPrice, options.CompileUnitPrice)
	ledger := domain.Ledger{
		InterfaceVersion:        domain.InterfaceVersion,
		EstimatorVersion:        domain.EstimatorVersion,
		CostSource:              costSource,
		ManifestVersion:         manifest.ManifestVersion,
		PreviousManifestVersion: previousVersion,
		ManifestHash:            manifestHash,
		GraphVersion:            graph.Version,
		PreviousGraphVersion:    previousGraphVersion,
		GraphChanged:            prev != nil && prev.GraphVersion != graph.Version,
		Capacity:                options.Capacity,
		StorageUnitPrice:        options.StorageUnitPrice,
		CompileUnitPrice:        options.CompileUnitPrice,
		Diff:                    diff,
		Invalidated:             invalidated,
		Affected:                affected,
		Reused:                  reused,
		Recompiled:              recompiled,
		Dropped:                 dropped,
		Decisions:               decisions,
		Plan:                    plan,
		Delivery:                buildNodeDelivery(manifest, productByKey),
		TotalDeliveryEntries:    totalDelivery,
		TotalCompileWork:        totalCompile,
		PreviousCacheBytes:      previousCacheBytes,
		CacheBytes:              cacheBytes,
		StorageCost:             storageCost,
		CompileCost:             compileCost,
		TotalCost:               totalCost,
	}
	return &domain.State{
		SchemaVersion:   domain.SnapshotSchema,
		SessionID:       options.SessionID,
		ManifestVersion: manifest.ManifestVersion,
		ManifestHash:    manifestHash,
		GraphVersion:    graph.Version,
		Graph:           *graph,
		Manifest:        *manifest,
		Products:        products,
		Cache:           cache,
		Ledger:          ledger,
	}, nil
}

func validateRemovedReferences(prev *domain.State, manifest *domain.Manifest) *domain.BusinessError {
	if prev == nil {
		return nil
	}
	current := entryMap(manifest)
	for _, oldEntry := range prev.Manifest.Entries {
		if _, exists := current[oldEntry.Key]; exists {
			continue
		}
		refs := make([]string, 0)
		for _, entry := range manifest.Entries {
			if contains(entry.DependsOn, oldEntry.Key) {
				refs = append(refs, entry.Key)
			}
		}
		if len(refs) > 0 {
			sort.Strings(refs)
			return &domain.BusinessError{
				Code:   domain.CodeRef,
				Item:   "entry:" + oldEntry.Key,
				Reason: "cannot delete an entry that is still referenced",
				Refs:   refs,
			}
		}
	}
	return nil
}

func validateDependencyReferences(manifest *domain.Manifest) *domain.BusinessError {
	keys := entryMap(manifest)
	for _, entry := range manifest.Entries {
		for _, dep := range entry.DependsOn {
			if _, exists := keys[dep]; !exists {
				return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "dependency reference does not exist: "+dep)
			}
		}
	}
	return nil
}

func buildDiffAndVersions(prevEntries map[string]domain.Entry, currentEntries map[string]domain.Entry, prevProducts map[string]domain.Product) (domain.VersionDiff, map[string]int64) {
	diff := domain.VersionDiff{
		Added:     []string{},
		Modified:  []string{},
		Removed:   []string{},
		Unchanged: []string{},
	}
	versions := make(map[string]int64, len(currentEntries))
	for key := range currentEntries {
		currentHash := spec.DefinitionHash(currentEntries[key])
		previous, existed := prevEntries[key]
		product, productExists := prevProducts[key]
		switch {
		case !existed:
			diff.Added = append(diff.Added, key)
			versions[key] = 1
		case currentHash != spec.DefinitionHash(previous) || !productExists:
			diff.Modified = append(diff.Modified, key)
			versions[key] = product.ProductVersion + 1
			if versions[key] < 1 {
				versions[key] = 1
			}
		default:
			diff.Unchanged = append(diff.Unchanged, key)
			versions[key] = product.ProductVersion
		}
	}
	for key := range prevEntries {
		if _, exists := currentEntries[key]; !exists {
			diff.Removed = append(diff.Removed, key)
		}
	}
	sort.Strings(diff.Added)
	sort.Strings(diff.Modified)
	sort.Strings(diff.Removed)
	sort.Strings(diff.Unchanged)
	return diff, versions
}

func buildProducts(manifest *domain.Manifest, graph *domain.Graph, versions map[string]int64, prevProducts map[string]domain.Product) ([]domain.Product, *domain.BusinessError) {
	entryByKey := entryMap(manifest)
	upstream := make(map[string][]string)
	for _, edge := range graph.Edges {
		upstream[edge.To] = append(upstream[edge.To], edge.From)
	}
	for key := range upstream {
		sort.Strings(upstream[key])
	}
	products := make([]domain.Product, 0, len(manifest.Entries))
	productByKey := make(map[string]domain.Product, len(manifest.Entries))
	seen := make(map[string]bool, len(manifest.Entries))
	for _, key := range graph.TopologicalOrder {
		entry, exists := entryByKey[key]
		if !exists {
			return nil, domain.BizError(domain.CodeLedger, "entry:"+key, "topological graph references an unknown entry")
		}
		if seen[key] {
			return nil, domain.BizError(domain.CodeLedger, "entry:"+key, "duplicate entry in topological order")
		}
		seen[key] = true
		type upstreamArtifact struct {
			Key            string `json:"key"`
			ProductVersion int64  `json:"product_version"`
			ArtifactHash   string `json:"artifact_hash"`
		}
		upstreamItems := make([]upstreamArtifact, 0)
		seenUpstream := make(map[string]struct{})
		for _, upstreamKey := range upstream[key] {
			if _, exists := seenUpstream[upstreamKey]; exists {
				continue
			}
			seenUpstream[upstreamKey] = struct{}{}
			product, exists := productByKey[upstreamKey]
			if !exists {
				return nil, domain.BizError(domain.CodeLedger, "entry:"+key, "upstream artifact is not compiled: "+upstreamKey)
			}
			upstreamItems = append(upstreamItems, upstreamArtifact{Key: upstreamKey, ProductVersion: product.ProductVersion, ArtifactHash: product.ArtifactHash})
		}
		version := versions[key]
		if version < 1 {
			version = 1
		}
		doc := struct {
			ArtifactSchema string             `json:"artifact_schema"`
			Key            string             `json:"key"`
			ProductVersion int64              `json:"product_version"`
			Value          any                `json:"value"`
			Targets        []string           `json:"targets"`
			DependsOn      []string           `json:"depends_on"`
			Upstream       []upstreamArtifact `json:"upstream"`
		}{
			ArtifactSchema: domain.ArtifactSchema,
			Key:            entry.Key,
			ProductVersion: version,
			Value:          entry.Value,
			Targets:        append([]string(nil), entry.Targets...),
			DependsOn:      append([]string(nil), entry.DependsOn...),
			Upstream:       upstreamItems,
		}
		payload, err := json.Marshal(doc)
		if err != nil {
			return nil, domain.BizError(domain.CodeLedger, "entry:"+key, "cannot encode artifact: "+err.Error())
		}
		storageBytes := int64(len(payload))
		compileBase := int64(1 + len(entry.DependsOn) + len(entry.Targets) + len(upstreamItems))
		compileWork, ok := safeAdd(compileBase, storageBytes)
		if !ok {
			return nil, domain.BizError(domain.CodeCapacity, "entry:"+key, "compile work overflow")
		}
		product := domain.Product{
			Key:             entry.Key,
			ProductVersion:  version,
			DefinitionHash:  spec.DefinitionHash(entry),
			ArtifactHash:    shortHash(payload),
			ArtifactPayload: json.RawMessage(payload),
			Targets:         append([]string(nil), entry.Targets...),
			DependsOn:       append([]string(nil), entry.DependsOn...),
			StorageBytes:    storageBytes,
			CompileWork:     compileWork,
			DeliveryEntries: int64(len(entry.Targets)),
		}
		products = append(products, product)
		productByKey[key] = product
	}
	if len(products) != len(manifest.Entries) {
		for _, entry := range manifest.Entries {
			if _, exists := productByKey[entry.Key]; !exists {
				return nil, domain.BizError(domain.CodeLedger, "entry:"+entry.Key, "entry is missing from topological order")
			}
		}
	}
	sort.Slice(products, func(i, j int) bool { return products[i].Key < products[j].Key })
	return products, nil
}

func invalidationSeeds(prev *domain.State, manifest *domain.Manifest, graph *domain.Graph, diff domain.VersionDiff) []string {
	seeds := make([]string, 0)
	seeds = append(seeds, diff.Added...)
	seeds = append(seeds, diff.Modified...)
	seeds = append(seeds, diff.Removed...)
	if prev != nil && prev.GraphVersion != graph.Version {
		seeds = append(seeds, topologySeeds(prev.Manifest, *manifest)...)
	}
	return dedupStrings(seeds)
}

func deriveInvalidation(prev *domain.State, manifest *domain.Manifest, graph *domain.Graph, diff domain.VersionDiff, products map[string]domain.Product, prevCache map[string]domain.CacheItem) ([]string, []string) {
	return deriveInvalidationWithSeeds(prev, manifest, graph, diff, invalidationSeeds(prev, manifest, graph, diff))
}

func deriveInvalidationWithSeeds(prev *domain.State, manifest *domain.Manifest, graph *domain.Graph, diff domain.VersionDiff, seeds []string) ([]string, []string) {
	adjacency := make(map[string][]string)
	if prev != nil {
		addGraphAdjacency(adjacency, prev.Graph)
	}
	addGraphAdjacency(adjacency, *graph)
	for key := range adjacency {
		sort.Strings(adjacency[key])
	}
	seen := make(map[string]struct{})
	queue := make([]string, 0)
	for _, seed := range seeds {
		if seed == "" {
			continue
		}
		if _, exists := seen[seed]; !exists {
			seen[seed] = struct{}{}
			queue = append(queue, seed)
		}
	}
	sort.Strings(queue)
	for index := 0; index < len(queue); index++ {
		current := queue[index]
		for _, next := range adjacency[current] {
			if _, exists := seen[next]; exists {
				continue
			}
			seen[next] = struct{}{}
			queue = append(queue, next)
			sort.Strings(queue[index+1:])
		}
	}
	current := entryMap(manifest)
	affected := make([]string, 0)
	invalidated := make([]string, 0)
	direct := make(map[string]struct{})
	for _, key := range diff.Added {
		direct[key] = struct{}{}
	}
	for _, key := range diff.Modified {
		direct[key] = struct{}{}
	}
	for _, key := range queue {
		if _, exists := current[key]; !exists {
			continue
		}
		affected = append(affected, key)
		if _, isDirect := direct[key]; !isDirect {
			invalidated = append(invalidated, key)
		}
	}
	sort.Strings(affected)
	sort.Strings(invalidated)
	return affected, invalidated
}

func topologySeeds(prevManifest, currentManifest domain.Manifest) []string {
	previousNodes := nodeDeps(prevManifest)
	currentNodes := nodeDeps(currentManifest)
	changedNodes := make(map[string]struct{})
	for node := range previousNodes {
		if !equalStrings(previousNodes[node], currentNodes[node]) {
			changedNodes[node] = struct{}{}
		}
	}
	for node := range currentNodes {
		if !equalStrings(previousNodes[node], currentNodes[node]) {
			changedNodes[node] = struct{}{}
		}
	}
	if len(changedNodes) == 0 {
		return nil
	}
	affectedNodes := make(map[string]struct{}, len(changedNodes))
	queue := make([]string, 0, len(changedNodes))
	for node := range changedNodes {
		affectedNodes[node] = struct{}{}
		queue = append(queue, node)
	}
	sort.Strings(queue)
	allNodes := make(map[string]struct{})
	for node := range previousNodes {
		allNodes[node] = struct{}{}
	}
	for node := range currentNodes {
		allNodes[node] = struct{}{}
	}
	for index := 0; index < len(queue); index++ {
		current := queue[index]
		downstream := make([]string, 0)
		for node := range allNodes {
			if contains(previousNodes[node], current) || contains(currentNodes[node], current) {
				downstream = append(downstream, node)
			}
		}
		sort.Strings(downstream)
		for _, node := range downstream {
			if _, exists := affectedNodes[node]; exists {
				continue
			}
			affectedNodes[node] = struct{}{}
			queue = append(queue, node)
		}
	}
	seeds := make([]string, 0)
	for _, entry := range currentManifest.Entries {
		for _, target := range entry.Targets {
			if _, exists := affectedNodes[target]; exists {
				seeds = append(seeds, entry.Key)
				break
			}
		}
	}
	sort.Strings(seeds)
	return seeds
}

type cacheCandidate struct {
	Key            string
	ProductVersion int64
	ArtifactHash   string
	Bytes          int64
	CompileWork    int64
	Affected       bool
	HadCache       bool
	CacheMatches   bool
	Value          *big.Int
	Overhead       *big.Int
	Worth          bool
}

func planDecisions(prev *domain.State, manifest *domain.Manifest, graph *domain.Graph, products []domain.Product, productByKey map[string]domain.Product, prevCache map[string]domain.CacheItem, affected []string, direct map[string]struct{}, diff domain.VersionDiff, options Options) ([]domain.Decision, []domain.CacheItem, []string, []string, []string) {
	affectedSet := sliceSet(affected)
	reasons := invalidationReasons(diff, affectedSet, direct)
	candidates := make([]cacheCandidate, 0, len(products))
	for _, product := range products {
		_, isAffected := affectedSet[product.Key]
		oldCache, hadCache := prevCache[product.Key]
		value, _ := safeMul(product.CompileWork, options.CompileUnitPrice)
		overhead, _ := safeMul(product.StorageBytes, options.StorageUnitPrice)
		candidate := cacheCandidate{
			Key:            product.Key,
			ProductVersion: product.ProductVersion,
			ArtifactHash:   product.ArtifactHash,
			Bytes:          product.StorageBytes,
			CompileWork:    product.CompileWork,
			Affected:       isAffected,
			HadCache:       hadCache,
			CacheMatches:   hadCache && oldCache.ProductVersion == product.ProductVersion && oldCache.ArtifactHash == product.ArtifactHash,
			Value:          big.NewInt(value),
			Overhead:       big.NewInt(overhead),
		}
		candidate.Worth = cacheWorth(candidate.Value, candidate.Overhead, options)
		if hadCache && !candidate.Worth {
			_ = oldCache
		}
		candidates = append(candidates, candidate)
	}
	stableCandidates := make([]cacheCandidate, 0, len(candidates))
	freshCandidates := make([]cacheCandidate, 0, len(candidates))
	for _, candidate := range candidates {
		if !candidate.Affected && candidate.CacheMatches {
			stableCandidates = append(stableCandidates, candidate)
			continue
		}
		freshCandidates = append(freshCandidates, candidate)
	}
	sortCacheCandidates(stableCandidates)
	sortCacheCandidates(freshCandidates)
	selected := make(map[string]cacheCandidate)
	usedBytes := int64(0)
	admit := func(candidate cacheCandidate) {
		if !candidate.Worth {
			return
		}
		next, ok := safeAdd(usedBytes, candidate.Bytes)
		if !ok || next > options.Capacity {
			return
		}
		selected[candidate.Key] = candidate
		usedBytes = next
	}
	for _, candidate := range stableCandidates {
		admit(candidate)
	}
	for _, candidate := range freshCandidates {
		admit(candidate)
	}
	cache := make([]domain.CacheItem, 0, len(selected))
	for _, candidate := range selected {
		cache = append(cache, domain.CacheItem{
			Key:            candidate.Key,
			ProductVersion: candidate.ProductVersion,
			ArtifactHash:   candidate.ArtifactHash,
			Bytes:          candidate.Bytes,
		})
	}
	sort.Slice(cache, func(i, j int) bool { return cache[i].Key < cache[j].Key })

	decisions := make([]domain.Decision, 0, len(products)+len(diff.Removed))
	reused := make([]string, 0)
	recompiled := make([]string, 0)
	dropped := make([]string, 0)
	selectedByKey := make(map[string]cacheCandidate, len(selected))
	for key, candidate := range selected {
		selectedByKey[key] = candidate
	}
	for _, product := range products {
		_, isAffected := affectedSet[product.Key]
		_, retained := selectedByKey[product.Key]
		oldCache, hadCache := prevCache[product.Key]
		decision := domain.Decision{Key: product.Key, Node: firstNode(product.Targets)}
		switch {
		case retained && !isAffected && hadCache && oldCache.ProductVersion == product.ProductVersion && oldCache.ArtifactHash == product.ArtifactHash:
			decision.Action = "reuse"
			decision.CacheAction = "kept"
			decision.Reason = "definition and dependency artifacts match the retained cache"
			decision.CacheReason = fmt.Sprintf("avoided_compile_value=%d >= storage_cost=%d", cValue(product.CompileWork, options.CompileUnitPrice), cMul(product.StorageBytes, options.StorageUnitPrice))
			reused = append(reused, product.Key)
		case retained:
			decision.Action = "recompile"
			decision.CacheAction = "cached"
			decision.Reason = reasons[product.Key]
			if decision.Reason == "" {
				decision.Reason = "affected by dependency invalidation"
			}
			decision.CacheReason = fmt.Sprintf("avoided_compile_value=%d >= storage_cost=%d", cValue(product.CompileWork, options.CompileUnitPrice), cMul(product.StorageBytes, options.StorageUnitPrice))
			recompiled = append(recompiled, product.Key)
		default:
			decision.Action = "recompile"
			decision.CacheAction = "none"
			decision.Reason = reasons[product.Key]
			if decision.Reason == "" {
				decision.Reason = "no reusable cache was selected for this artifact"
			}
			if hadCache {
				decision.CacheAction = "dropped"
				decision.AbandonedReuseReason = cacheDropReason(product, oldCache, options)
				dropped = append(dropped, product.Key)
			}
			decision.CacheReason = cacheDropReason(product, oldCache, options)
			recompiled = append(recompiled, product.Key)
		}
		if !isAffected && decision.Action == "recompile" && !hadCache {
			decision.Reason = "cache was unavailable; artifact was recompiled for delivery"
		}
		decisions = append(decisions, decision)
	}
	for _, key := range diff.Removed {
		decision := domain.Decision{
			Key:         key,
			Action:      "drop_cache",
			CacheAction: "dropped",
			Reason:      "entry was removed from the current manifest",
			CacheReason: "removed entries cannot remain in the cache",
		}
		if _, hadCache := prevCache[key]; hadCache {
			decision.AbandonedReuseReason = "entry removed"
			dropped = append(dropped, key)
		}
		decisions = append(decisions, decision)
	}
	sort.Slice(decisions, func(i, j int) bool { return decisions[i].Key < decisions[j].Key })
	sort.Strings(reused)
	sort.Strings(recompiled)
	dropped = dedupStrings(dropped)
	sort.Strings(dropped)
	return decisions, cache, reused, recompiled, dropped
}

func sortCacheCandidates(candidates []cacheCandidate) {
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Worth != candidates[j].Worth {
			return candidates[i].Worth
		}
		cmp := compareDensity(candidates[i], candidates[j])
		if cmp != 0 {
			return cmp > 0
		}
		if candidates[i].Bytes != candidates[j].Bytes {
			return candidates[i].Bytes < candidates[j].Bytes
		}
		return candidates[i].Key < candidates[j].Key
	})
}

func invalidationReasons(diff domain.VersionDiff, affected map[string]struct{}, direct map[string]struct{}) map[string]string {
	result := make(map[string]string)
	for _, key := range diff.Added {
		result[key] = "entry was added; a version 1 artifact was compiled"
	}
	for _, key := range diff.Modified {
		result[key] = "definition changed; product version was incremented"
	}
	for key := range affected {
		if _, exists := direct[key]; exists {
			continue
		}
		result[key] = "dependency invalidation: an upstream entry or node fragment changed"
	}
	return result
}

func cacheDropReason(product domain.Product, oldCache domain.CacheItem, options Options) string {
	value := cValue(product.CompileWork, options.CompileUnitPrice)
	overhead := cMul(product.StorageBytes, options.StorageUnitPrice)
	if options.CompileUnitPrice == 0 && options.StorageUnitPrice > 0 {
		return "compile price is zero; caching provides no cost benefit"
	}
	if options.StorageUnitPrice == 0 {
		return "cache was not selected under the deterministic capacity order"
	}
	if value < overhead {
		return fmt.Sprintf("storage cost %d exceeds avoided compile value %d", overhead, value)
	}
	return fmt.Sprintf("capacity could not fit this artifact (bytes=%d, avoided_compile_value=%d, storage_cost=%d)", product.StorageBytes, value, overhead)
}

func planTotals(products []domain.Product, reused []string) (int64, int64, *domain.BusinessError) {
	reusedSet := make(map[string]struct{}, len(reused))
	for _, key := range reused {
		reusedSet[key] = struct{}{}
	}
	var delivery int64
	var compile int64
	for _, product := range products {
		var ok bool
		delivery, ok = safeAdd(delivery, product.DeliveryEntries)
		if !ok {
			return 0, 0, domain.BizError(domain.CodeCapacity, "total_delivery_entries", "integer delivery total overflow")
		}
		if _, exists := reusedSet[product.Key]; exists {
			continue
		}
		compile, ok = safeAdd(compile, product.CompileWork)
		if !ok {
			return 0, 0, domain.BizError(domain.CodeCapacity, "total_compile_work", "integer compile total overflow")
		}
	}
	return delivery, compile, nil
}

func buildNodeDelivery(manifest *domain.Manifest, products map[string]domain.Product) []domain.NodeDelivery {
	byNode := make(map[string][]domain.DeliveredItem)
	for _, entry := range manifest.Entries {
		product := products[entry.Key]
		for _, target := range entry.Targets {
			byNode[target] = append(byNode[target], domain.DeliveredItem{Key: product.Key, ProductVersion: product.ProductVersion, ArtifactHash: product.ArtifactHash})
		}
	}
	nodes := make([]string, 0, len(byNode))
	for node := range byNode {
		nodes = append(nodes, node)
	}
	sort.Strings(nodes)
	result := make([]domain.NodeDelivery, 0, len(nodes))
	for _, node := range nodes {
		items := byNode[node]
		sort.Slice(items, func(i, j int) bool { return items[i].Key < items[j].Key })
		result = append(result, domain.NodeDelivery{Node: node, Items: items})
	}
	return result
}

func computeInvalidatedForVerify(current, prev *domain.State) []string {
	if current == nil {
		return nil
	}
	diff := current.Ledger.Diff
	if prev == nil {
		diff = domain.VersionDiff{Added: spec.EntryIDs(&current.Manifest)}
	}
	products := productMapFromProducts(current.Products)
	cache := cacheMap(nil)
	if prev != nil {
		cache = cacheMap(prev)
	}
	_, invalidated := deriveInvalidation(prev, &current.Manifest, &current.Graph, diff, products, cache)
	return invalidated
}
