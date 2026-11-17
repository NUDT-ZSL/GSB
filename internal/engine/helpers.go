package engine

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"math/big"
	"sort"

	"configledger/internal/domain"
)

func entryMap(manifest *domain.Manifest) map[string]domain.Entry {
	result := make(map[string]domain.Entry)
	if manifest == nil {
		return result
	}
	for _, entry := range manifest.Entries {
		result[entry.Key] = entry
	}
	return result
}

func productMap(state *domain.State) map[string]domain.Product {
	if state == nil {
		return make(map[string]domain.Product)
	}
	return productMapFromProducts(state.Products)
}

func productMapFromProducts(products []domain.Product) map[string]domain.Product {
	result := make(map[string]domain.Product, len(products))
	for _, product := range products {
		result[product.Key] = product
	}
	return result
}

func cacheMap(state *domain.State) map[string]domain.CacheItem {
	result := make(map[string]domain.CacheItem)
	if state == nil {
		return result
	}
	for _, item := range state.Cache {
		result[item.Key] = item
	}
	return result
}

func contains(values []string, wanted string) bool {
	index := sort.SearchStrings(values, wanted)
	return index < len(values) && values[index] == wanted
}

func sliceSet(values []string) map[string]struct{} {
	result := make(map[string]struct{}, len(values))
	for _, value := range values {
		result[value] = struct{}{}
	}
	return result
}

func dedupStrings(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func shortHash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:8])
}

func safeAdd(left, right int64) (int64, bool) {
	if right > 0 && left > math.MaxInt64-right {
		return 0, false
	}
	if right < 0 && left < math.MinInt64-right {
		return 0, false
	}
	return left + right, true
}

func safeMul(left, right int64) (int64, bool) {
	if left == 0 || right == 0 {
		return 0, true
	}
	if left < 0 || right < 0 {
		return 0, false
	}
	if left > math.MaxInt64/right {
		return 0, false
	}
	return left * right, true
}

func cValue(work, price int64) int64 {
	value, ok := safeMul(work, price)
	if !ok {
		return math.MaxInt64
	}
	return value
}

func cMul(left, right int64) int64 {
	value, ok := safeMul(left, right)
	if !ok {
		return math.MaxInt64
	}
	return value
}

func cacheWorth(value, overhead *big.Int, options Options) bool {
	if options.StorageUnitPrice == 0 {
		return true
	}
	if options.CompileUnitPrice == 0 {
		return false
	}
	return value.Cmp(overhead) >= 0
}

func compareDensity(left, right cacheCandidate) int {
	leftProduct := new(big.Int).Mul(left.Value, big.NewInt(maxInt64(right.Bytes, 1)))
	rightProduct := new(big.Int).Mul(right.Value, big.NewInt(maxInt64(left.Bytes, 1)))
	return leftProduct.Cmp(rightProduct)
}

func maxInt64(left, right int64) int64 {
	if left > right {
		return left
	}
	return right
}

func sumCacheBytes(cache []domain.CacheItem) int64 {
	var total int64
	for _, item := range cache {
		next, ok := safeAdd(total, item.Bytes)
		if !ok {
			return math.MaxInt64
		}
		total = next
	}
	return total
}

func firstNode(targets []string) string {
	if len(targets) == 0 {
		return ""
	}
	return targets[0]
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func nodeDeps(manifest domain.Manifest) map[string][]string {
	result := make(map[string][]string, len(manifest.Nodes))
	for _, node := range manifest.Nodes {
		deps := append([]string(nil), node.DependsOn...)
		sort.Strings(deps)
		result[node.ID] = deps
	}
	return result
}

func addGraphAdjacency(adjacency map[string][]string, graph domain.Graph) {
	for _, edge := range graph.Edges {
		adjacency[edge.From] = append(adjacency[edge.From], edge.To)
	}
}
