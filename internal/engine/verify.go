package engine

import (
	"fmt"
	"reflect"
	"sort"

	"configledger/internal/domain"
	"configledger/internal/spec"
)

func Verify(current, previous *domain.State) *domain.BusinessError {
	if current == nil {
		return domain.BizError(domain.CodeLedger, "snapshot", "active snapshot is missing")
	}
	if current.SchemaVersion != domain.SnapshotSchema {
		return ledgerError("snapshot_schema", "", fmt.Sprintf("unsupported snapshot schema %q", current.SchemaVersion))
	}
	if current.SessionID == "" {
		return ledgerError("session_id", "", "snapshot session id is empty")
	}
	if current.ManifestHash != spec.ManifestHash(&current.Manifest) {
		return ledgerError("manifest_hash", "", "stored manifest hash does not match manifest content")
	}
	graph, berr := spec.BuildGraph(&current.Manifest)
	if berr != nil {
		return ledgerError("graph", "", berr.Reason)
	}
	if current.GraphVersion != graph.Version {
		return ledgerError("graph_version", "", fmt.Sprintf("stored graph version %s, rebuilt %s", current.GraphVersion, graph.Version))
	}

	expectedVersions, expectedDiff := expectedVersionAndDiff(previous, &current.Manifest)
	if !reflect.DeepEqual(current.Ledger.Diff, expectedDiff) {
		item := firstSliceDifference(current.Ledger.Diff.Added, expectedDiff.Added)
		if item == "" {
			item = firstSliceDifference(current.Ledger.Diff.Modified, expectedDiff.Modified)
		}
		if item == "" {
			item = firstSliceDifference(current.Ledger.Diff.Removed, expectedDiff.Removed)
		}
		if item == "" {
			item = firstSliceDifference(current.Ledger.Diff.Unchanged, expectedDiff.Unchanged)
		}
		if item == "" {
			item = "diff"
		}
		return ledgerError("version_diff", item, "ledger version diff does not match recomputed manifest difference")
	}

	products := productMapFromProducts(current.Products)
	if len(products) != len(current.Manifest.Entries) {
		item := firstMissingEntry(&current.Manifest, products)
		return ledgerError("product_set", item, "product set does not contain exactly the current manifest entries")
	}
	for _, entry := range current.Manifest.Entries {
		product, exists := products[entry.Key]
		if !exists {
			return ledgerErrorNode("product_missing", entry.Key, firstNode(entry.Targets), "current entry has no product")
		}
		if product.DefinitionHash != spec.DefinitionHash(entry) {
			return ledgerErrorNode("definition_hash", entry.Key, firstNode(entry.Targets), "product definition hash does not match manifest entry")
		}
		expectedVersion := expectedVersions[entry.Key]
		if product.ProductVersion != expectedVersion {
			return ledgerErrorNode("product_version", entry.Key, firstNode(entry.Targets), fmt.Sprintf("stored product version %d, expected %d", product.ProductVersion, expectedVersion))
		}
	}

	cache := cacheMap(current)
	invalidatedChecks := invalidationForVerification(current, previous, products, cacheMap(previous))
	invalidated := invalidatedChecks
	if !equalStrings(current.Ledger.Invalidated, invalidated) {
		item := firstSliceDifference(current.Ledger.Invalidated, invalidated)
		return ledgerErrorNode("invalidated_set", item, nodeOfEntry(&current.Manifest, item), "ledger invalidated set does not match dependency propagation")
	}
	expectedAffected := append([]string(nil), current.Ledger.Diff.Added...)
	expectedAffected = append(expectedAffected, current.Ledger.Diff.Modified...)
	expectedAffected = append(expectedAffected, invalidated...)
	expectedAffected = dedupStrings(expectedAffected)
	if !equalStrings(current.Ledger.Affected, expectedAffected) {
		item := firstSliceDifference(current.Ledger.Affected, expectedAffected)
		return ledgerErrorNode("affected_set", item, nodeOfEntry(&current.Manifest, item), "ledger affected set is not the union of direct changes and invalidated entries")
	}

	decisionReused := make([]string, 0)
	decisionRecompiled := make([]string, 0)
	cachedDecisionKeys := make([]string, 0)
	for _, decision := range current.Ledger.Decisions {
		if _, exists := products[decision.Key]; !exists {
			if decision.Action != "drop_cache" {
				return ledgerErrorNode("decision_entry", decision.Key, decision.Node, "decision references an entry absent from the current manifest")
			}
			continue
		}
		switch decision.Action {
		case "reuse":
			decisionReused = append(decisionReused, decision.Key)
		case "recompile":
			decisionRecompiled = append(decisionRecompiled, decision.Key)
		case "drop_cache":
		default:
			return ledgerErrorNode("decision_action", decision.Key, nodeOfEntry(&current.Manifest, decision.Key), "unknown decision action "+decision.Action)
		}
		if decision.CacheAction == "kept" || decision.CacheAction == "cached" {
			cachedDecisionKeys = append(cachedDecisionKeys, decision.Key)
		}
	}
	sort.Strings(decisionReused)
	sort.Strings(decisionRecompiled)
	sort.Strings(cachedDecisionKeys)
	if !equalStrings(current.Ledger.Reused, decisionReused) {
		item := firstSliceDifference(current.Ledger.Reused, decisionReused)
		return ledgerErrorNode("reused_set", item, nodeOfEntry(&current.Manifest, item), "ledger reused set does not match reuse decisions")
	}
	if !equalStrings(current.Ledger.Recompiled, decisionRecompiled) {
		item := firstSliceDifference(current.Ledger.Recompiled, decisionRecompiled)
		return ledgerErrorNode("recompiled_set", item, nodeOfEntry(&current.Manifest, item), "ledger recompiled set does not match recompile decisions")
	}
	cacheKeys := make([]string, 0, len(cache))
	for key := range cache {
		cacheKeys = append(cacheKeys, key)
	}
	sort.Strings(cacheKeys)
	if !equalStrings(cachedDecisionKeys, cacheKeys) {
		item := firstSliceDifference(cachedDecisionKeys, cacheKeys)
		return ledgerErrorNode("cache_decisions", item, nodeOfEntry(&current.Manifest, item), "cache contents do not match cache decisions")
	}
	for _, key := range cacheKeys {
		item := cache[key]
		product, exists := products[key]
		if !exists {
			return ledgerErrorNode("cache_orphan", key, "", "cache contains an entry that is absent from the current manifest")
		}
		if item.ProductVersion != product.ProductVersion || item.ArtifactHash != product.ArtifactHash || item.Bytes != product.StorageBytes {
			return ledgerErrorNode("cache_artifact", key, firstNode(product.Targets), "cache metadata does not match the current product")
		}
	}
	var cacheBytes int64
	for _, item := range current.Cache {
		next, ok := safeAdd(cacheBytes, item.Bytes)
		if !ok {
			return ledgerErrorNode("cache_bytes", item.Key, "", "cache byte total overflow")
		}
		cacheBytes = next
	}
	if cacheBytes != current.Ledger.CacheBytes {
		item := firstCacheDifference(current.Cache, current.Ledger.CacheBytes, cacheBytes)
		return ledgerErrorNode("cache_bytes", item, "", fmt.Sprintf("ledger cache bytes %d, summed cache bytes %d", current.Ledger.CacheBytes, cacheBytes))
	}
	if cacheBytes > current.Ledger.Capacity {
		return ledgerError("capacity", "cache", fmt.Sprintf("cache bytes %d exceed capacity %d", cacheBytes, current.Ledger.Capacity))
	}

	expectedDelivery := buildNodeDelivery(&current.Manifest, products)
	if !reflect.DeepEqual(current.Ledger.Delivery, expectedDelivery) {
		item, node := firstDeliveryDifference(current.Ledger.Delivery, expectedDelivery)
		return ledgerErrorNode("node_delivery", item, node, "delivered product set does not match the expected node configuration combination")
	}

	planKeys := make([]string, 0, len(current.Ledger.Plan))
	for index, planItem := range current.Ledger.Plan {
		planKeys = append(planKeys, planItem.Key)
		product, exists := products[planItem.Key]
		if !exists {
			return ledgerErrorNode("plan_orphan", planItem.Key, "", "plan references an entry absent from the current manifest")
		}
		if planItem.ProductVersion != product.ProductVersion || planItem.ArtifactHash != product.ArtifactHash || planItem.DeliveryEntries != product.DeliveryEntries || planItem.StorageBytes != product.StorageBytes {
			return ledgerErrorNode("plan_item", planItem.Key, firstNode(product.Targets), "plan item does not match the current product")
		}
		if planItem.Compiled {
			if planItem.CompileWork != product.CompileWork {
				return ledgerErrorNode("plan_compile_work", planItem.Key, firstNode(product.Targets), "compiled plan item has incorrect work units")
			}
		} else if planItem.CompileWork != 0 {
			return ledgerErrorNode("plan_compile_work", planItem.Key, firstNode(product.Targets), "reused plan item must not carry compile work")
		}
		if index > 0 && current.Ledger.Plan[index-1].Key >= planItem.Key {
			return ledgerErrorNode("plan_order", planItem.Key, firstNode(product.Targets), "plan is not sorted by entry key")
		}
	}
	expectedPlanKeys := spec.EntryIDs(&current.Manifest)
	if !equalStrings(planKeys, expectedPlanKeys) {
		item := firstSliceDifference(planKeys, expectedPlanKeys)
		return ledgerErrorNode("plan_set", item, nodeOfEntry(&current.Manifest, item), "plan does not contain exactly the current entries")
	}

	expectedCompiledKeys := make([]string, 0)
	var expectedDeliveryTotal int64
	var expectedCompileTotal int64
	for _, planItem := range current.Ledger.Plan {
		var ok bool
		expectedDeliveryTotal, ok = safeAdd(expectedDeliveryTotal, planItem.DeliveryEntries)
		if !ok {
			return ledgerError("delivery_total", planItem.Key, "delivery total overflow")
		}
		if planItem.Compiled {
			expectedCompiledKeys = append(expectedCompiledKeys, planItem.Key)
			expectedCompileTotal, ok = safeAdd(expectedCompileTotal, planItem.CompileWork)
			if !ok {
				return ledgerError("compile_total", planItem.Key, "compile total overflow")
			}
		}
	}
	sort.Strings(expectedCompiledKeys)
	if !equalStrings(current.Ledger.Recompiled, expectedCompiledKeys) {
		item := firstSliceDifference(current.Ledger.Recompiled, expectedCompiledKeys)
		return ledgerErrorNode("compiled_plan", item, nodeOfEntry(&current.Manifest, item), "recompiled set does not match compiled plan items")
	}
	if current.Ledger.TotalDeliveryEntries != expectedDeliveryTotal {
		return ledgerError("delivery_total", "ledger", fmt.Sprintf("ledger delivery total %d, recomputed %d", current.Ledger.TotalDeliveryEntries, expectedDeliveryTotal))
	}
	if current.Ledger.TotalCompileWork != expectedCompileTotal {
		return ledgerError("compile_total", "ledger", fmt.Sprintf("ledger compile total %d, recomputed %d", current.Ledger.TotalCompileWork, expectedCompileTotal))
	}
	storageCost, ok := safeMul(current.Ledger.CacheBytes, current.Ledger.StorageUnitPrice)
	if !ok || current.Ledger.StorageCost != storageCost {
		return ledgerError("storage_cost", "ledger", fmt.Sprintf("ledger storage cost %d, recomputed cache_bytes*storage_price=%d", current.Ledger.StorageCost, storageCost))
	}
	compileCost, ok := safeMul(current.Ledger.TotalCompileWork, current.Ledger.CompileUnitPrice)
	if !ok || current.Ledger.CompileCost != compileCost {
		return ledgerError("compile_cost", "ledger", fmt.Sprintf("ledger compile cost %d, recomputed compile_work*compile_price=%d", current.Ledger.CompileCost, compileCost))
	}
	totalCost, ok := safeAdd(storageCost, compileCost)
	if !ok || current.Ledger.TotalCost != totalCost {
		return ledgerError("total_cost", "ledger", fmt.Sprintf("ledger total cost %d, recomputed %d", current.Ledger.TotalCost, totalCost))
	}
	if current.Ledger.InterfaceVersion != domain.InterfaceVersion || current.Ledger.EstimatorVersion != domain.EstimatorVersion || current.Ledger.CostSource == "" {
		return ledgerError("cost_source", "ledger", "ledger interface version, estimator version, or cost source is missing")
	}
	return nil
}

func Replay(saved, previous *domain.State) *domain.BusinessError {
	if saved == nil {
		return domain.BizError(domain.CodeReplay, "snapshot", "snapshot payload is missing")
	}
	if saved.SchemaVersion != domain.SnapshotSchema {
		return replayError("snapshot_schema", "", fmt.Sprintf("unsupported snapshot schema %q", saved.SchemaVersion))
	}
	manifest := saved.Manifest
	graph, berr := spec.BuildGraph(&manifest)
	if berr != nil {
		return replayError("graph_rebuild", "", berr.Reason)
	}
	options := Options{
		SessionID:        saved.SessionID,
		Capacity:         saved.Ledger.Capacity,
		StorageUnitPrice: saved.Ledger.StorageUnitPrice,
		CompileUnitPrice: saved.Ledger.CompileUnitPrice,
	}
	expected, berr := Apply(previous, &manifest, graph, options)
	if berr != nil {
		return replayError("apply", berr.Item, berr.Reason)
	}
	if saved.ManifestHash != expected.ManifestHash {
		return replayError("manifest_hash", "", fmt.Sprintf("saved=%s recomputed=%s", saved.ManifestHash, expected.ManifestHash))
	}
	if saved.GraphVersion != expected.GraphVersion {
		return replayError("graph_version", "", fmt.Sprintf("saved=%s recomputed=%s", saved.GraphVersion, expected.GraphVersion))
	}
	if !reflect.DeepEqual(saved.Ledger.Diff, expected.Ledger.Diff) {
		item := firstSliceDifference(saved.Ledger.Diff.Added, expected.Ledger.Diff.Added)
		if item == "" {
			item = firstSliceDifference(saved.Ledger.Diff.Modified, expected.Ledger.Diff.Modified)
		}
		if item == "" {
			item = firstSliceDifference(saved.Ledger.Diff.Removed, expected.Ledger.Diff.Removed)
		}
		if item == "" {
			item = firstSliceDifference(saved.Ledger.Diff.Unchanged, expected.Ledger.Diff.Unchanged)
		}
		if item == "" {
			item = "diff"
		}
		return replayError("version_diff", item, "saved version diff cannot be reproduced")
	}
	if !equalStrings(saved.Ledger.Invalidated, expected.Ledger.Invalidated) {
		item := firstSliceDifference(saved.Ledger.Invalidated, expected.Ledger.Invalidated)
		return replayError("invalidated_set", item, "saved invalidation set cannot be reproduced")
	}
	if !equalStrings(saved.Ledger.Affected, expected.Ledger.Affected) {
		item := firstSliceDifference(saved.Ledger.Affected, expected.Ledger.Affected)
		return replayError("affected_set", item, "saved affected set cannot be reproduced")
	}
	if !equalStrings(saved.Ledger.Reused, expected.Ledger.Reused) {
		item := firstSliceDifference(saved.Ledger.Reused, expected.Ledger.Reused)
		return replayError("reused_set", item, "saved reusable cache set cannot be reproduced")
	}
	if !equalStrings(saved.Ledger.Recompiled, expected.Ledger.Recompiled) {
		item := firstSliceDifference(saved.Ledger.Recompiled, expected.Ledger.Recompiled)
		return replayError("recompiled_set", item, "saved recompilation set cannot be reproduced")
	}
	if !equalStrings(saved.Ledger.Dropped, expected.Ledger.Dropped) {
		item := firstSliceDifference(saved.Ledger.Dropped, expected.Ledger.Dropped)
		return replayError("dropped_set", item, "saved dropped-cache set cannot be reproduced")
	}
	if !reflect.DeepEqual(saved.Cache, expected.Cache) {
		item := firstCacheItemDifference(saved.Cache, expected.Cache)
		return replayError("cache_state", item, "saved cache state cannot be reproduced")
	}
	if !reflect.DeepEqual(saved.Ledger.Decisions, expected.Ledger.Decisions) {
		item := firstDecisionDifference(saved.Ledger.Decisions, expected.Ledger.Decisions)
		return replayError("decisions", item, "saved cache/compile decisions cannot be reproduced")
	}
	if !reflect.DeepEqual(saved.Ledger.Plan, expected.Ledger.Plan) {
		item := firstPlanDifference(saved.Ledger.Plan, expected.Ledger.Plan)
		return replayError("plan", item, "saved distribution plan cannot be reproduced")
	}
	if !reflect.DeepEqual(saved.Ledger.Delivery, expected.Ledger.Delivery) {
		item, node := firstDeliveryDifference(saved.Ledger.Delivery, expected.Ledger.Delivery)
		return replayError("delivery", item, "saved node delivery cannot be reproduced for node "+node)
	}
	if !reflect.DeepEqual(saved.Products, expected.Products) {
		item := firstProductDifference(saved.Products, expected.Products)
		return replayError("product_state", item, "saved product version or artifact content cannot be reproduced")
	}
	if saved.Ledger.TotalDeliveryEntries != expected.Ledger.TotalDeliveryEntries ||
		saved.Ledger.TotalCompileWork != expected.Ledger.TotalCompileWork ||
		saved.Ledger.CacheBytes != expected.Ledger.CacheBytes ||
		saved.Ledger.StorageCost != expected.Ledger.StorageCost ||
		saved.Ledger.CompileCost != expected.Ledger.CompileCost ||
		saved.Ledger.TotalCost != expected.Ledger.TotalCost {
		return replayError("cost_ledger", "ledger", fmt.Sprintf("saved totals=(%d,%d,%d,%d,%d,%d) recomputed=(%d,%d,%d,%d,%d,%d)",
			saved.Ledger.TotalDeliveryEntries, saved.Ledger.TotalCompileWork, saved.Ledger.CacheBytes,
			saved.Ledger.StorageCost, saved.Ledger.CompileCost, saved.Ledger.TotalCost,
			expected.Ledger.TotalDeliveryEntries, expected.Ledger.TotalCompileWork, expected.Ledger.CacheBytes,
			expected.Ledger.StorageCost, expected.Ledger.CompileCost, expected.Ledger.TotalCost))
	}
	return nil
}

func BuildRestoreReport(current, target *domain.State) RestoreReport {
	report := RestoreReport{
		WholeReplaced:   true,
		AffectedEntries: []string{},
		Added:           []string{},
		Removed:         []string{},
		Modified:        []string{},
	}
	if target == nil {
		return report
	}
	report.ToVersion = target.ManifestVersion
	report.ToGraphVersion = target.GraphVersion
	if current == nil {
		report.Added = spec.EntryIDs(&target.Manifest)
		report.AffectedEntries = append([]string(nil), report.Added...)
		report.GraphChanged = true
		return report
	}
	report.FromVersion = current.ManifestVersion
	report.FromGraphVersion = current.GraphVersion
	report.GraphChanged = current.GraphVersion != target.GraphVersion
	currentEntries := entryMap(&current.Manifest)
	targetEntries := entryMap(&target.Manifest)
	currentProducts := productMapFromProducts(current.Products)
	targetProducts := productMapFromProducts(target.Products)
	affected := make(map[string]struct{})
	for key := range currentEntries {
		if _, exists := targetEntries[key]; !exists {
			report.Removed = append(report.Removed, key)
			affected[key] = struct{}{}
		}
	}
	for key, targetEntry := range targetEntries {
		currentEntry, exists := currentEntries[key]
		if !exists {
			report.Added = append(report.Added, key)
			affected[key] = struct{}{}
			continue
		}
		if spec.DefinitionHash(currentEntry) != spec.DefinitionHash(targetEntry) {
			report.Modified = append(report.Modified, key)
		}
		currentProduct, hadProduct := currentProducts[key]
		targetProduct := targetProducts[key]
		if !hadProduct || currentProduct.ProductVersion != targetProduct.ProductVersion || currentProduct.ArtifactHash != targetProduct.ArtifactHash || spec.DefinitionHash(currentEntry) != spec.DefinitionHash(targetEntry) {
			affected[key] = struct{}{}
		}
	}
	if report.GraphChanged {
		for key := range targetEntries {
			affected[key] = struct{}{}
		}
	}
	for key := range affected {
		report.AffectedEntries = append(report.AffectedEntries, key)
	}
	sort.Strings(report.Added)
	sort.Strings(report.Removed)
	sort.Strings(report.Modified)
	sort.Strings(report.AffectedEntries)
	if report.FromVersion == report.ToVersion && report.FromGraphVersion == report.ToGraphVersion && current.ManifestHash == target.ManifestHash &&
		reflect.DeepEqual(current.Cache, target.Cache) && reflect.DeepEqual(current.Products, target.Products) && reflect.DeepEqual(current.Ledger, target.Ledger) {
		report.WholeReplaced = false
	}
	return report
}

func ledgerError(category, item, reason string) *domain.BusinessError {
	if item == "" {
		item = category
	}
	return domain.BizError(domain.CodeLedger, item, category+": "+reason)
}

func ledgerErrorNode(category, item, node, reason string) *domain.BusinessError {
	if item == "" {
		item = category
	}
	return domain.BizErrorNode(domain.CodeLedger, item, node, category+": "+reason)
}

func replayError(category, item, reason string) *domain.BusinessError {
	if item == "" {
		item = category
	}
	return domain.BizError(domain.CodeReplay, item, category+": "+reason)
}

func expectedVersionAndDiff(previous *domain.State, manifest *domain.Manifest) (map[string]int64, domain.VersionDiff) {
	prevEntries := entryMap(nil)
	prevProducts := map[string]domain.Product{}
	if previous != nil {
		prevEntries = entryMap(&previous.Manifest)
		prevProducts = productMapFromProducts(previous.Products)
	}
	diff, versions := buildDiffAndVersions(prevEntries, entryMap(manifest), prevProducts)
	diff.From = ""
	diff.To = manifest.ManifestVersion
	if previous != nil {
		diff.From = previous.ManifestVersion
	}
	return versions, diff
}

func invalidationForVerification(current, previous *domain.State, products map[string]domain.Product, cache map[string]domain.CacheItem) []string {
	seeds := invalidationSeeds(previous, &current.Manifest, &current.Graph, current.Ledger.Diff)
	for key, item := range cache {
		product, exists := products[key]
		if exists && (item.ProductVersion != product.ProductVersion || item.ArtifactHash != product.ArtifactHash) {
			seeds = append(seeds, key)
		}
	}
	_, invalidated := deriveInvalidationWithSeeds(previous, &current.Manifest, &current.Graph, current.Ledger.Diff, seeds)
	return invalidated
}

func firstSliceDifference(actual, expected []string) string {
	max := len(actual)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(actual) {
			return expected[index]
		}
		if index >= len(expected) {
			return actual[index]
		}
		if actual[index] != expected[index] {
			return actual[index]
		}
	}
	return ""
}

func firstMissingEntry(manifest *domain.Manifest, products map[string]domain.Product) string {
	for _, entry := range manifest.Entries {
		if _, exists := products[entry.Key]; !exists {
			return entry.Key
		}
	}
	entries := entryMap(manifest)
	orphans := make([]string, 0)
	for key := range products {
		if _, exists := entries[key]; !exists {
			orphans = append(orphans, key)
		}
	}
	sort.Strings(orphans)
	if len(orphans) > 0 {
		return orphans[0]
	}
	return ""
}

func nodeOfEntry(manifest *domain.Manifest, key string) string {
	if entry, exists := spec.FindEntry(manifest, key); exists {
		return firstNode(entry.Targets)
	}
	return ""
}

func firstCacheDifference(cache []domain.CacheItem, actual, expected int64) string {
	for _, item := range cache {
		return item.Key
	}
	return ""
}

func firstCacheItemDifference(saved, expected []domain.CacheItem) string {
	max := len(saved)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(saved) {
			return expected[index].Key
		}
		if index >= len(expected) {
			return saved[index].Key
		}
		if !reflect.DeepEqual(saved[index], expected[index]) {
			return saved[index].Key
		}
	}
	return ""
}

func firstDecisionDifference(saved, expected []domain.Decision) string {
	max := len(saved)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(saved) {
			return expected[index].Key
		}
		if index >= len(expected) {
			return saved[index].Key
		}
		if !reflect.DeepEqual(saved[index], expected[index]) {
			return saved[index].Key
		}
	}
	return ""
}

func firstPlanDifference(saved, expected []domain.PlanItem) string {
	max := len(saved)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(saved) {
			return expected[index].Key
		}
		if index >= len(expected) {
			return saved[index].Key
		}
		if !reflect.DeepEqual(saved[index], expected[index]) {
			return saved[index].Key
		}
	}
	return ""
}

func firstProductDifference(saved, expected []domain.Product) string {
	max := len(saved)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(saved) {
			return expected[index].Key
		}
		if index >= len(expected) {
			return saved[index].Key
		}
		if !reflect.DeepEqual(saved[index], expected[index]) {
			return saved[index].Key
		}
	}
	return ""
}

func firstDeliveryDifference(saved, expected []domain.NodeDelivery) (string, string) {
	max := len(saved)
	if len(expected) > max {
		max = len(expected)
	}
	for index := 0; index < max; index++ {
		if index >= len(saved) {
			return "", expected[index].Node
		}
		if index >= len(expected) {
			return "", saved[index].Node
		}
		if !reflect.DeepEqual(saved[index], expected[index]) {
			node := saved[index].Node
			if node == "" {
				node = expected[index].Node
			}
			maxItems := len(saved[index].Items)
			if len(expected[index].Items) > maxItems {
				maxItems = len(expected[index].Items)
			}
			for itemIndex := 0; itemIndex < maxItems; itemIndex++ {
				if itemIndex >= len(saved[index].Items) {
					return expected[index].Items[itemIndex].Key, node
				}
				if itemIndex >= len(expected[index].Items) {
					return saved[index].Items[itemIndex].Key, node
				}
				if saved[index].Items[itemIndex].Key != expected[index].Items[itemIndex].Key {
					return saved[index].Items[itemIndex].Key, node
				}
			}
			return "", node
		}
	}
	return "", ""
}
